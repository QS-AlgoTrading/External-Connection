"""FastAPI REST service for external-strategy paper trading.

Multi-tenant: every endpoint is rooted at `/strategies/{strategy_id}`.
Each `strategy_id` has its own `PaperSession` + state file + audit log,
and shares nothing with other strategies.

Endpoints (matching Hermes's order spec):

    POST   /strategies/{id}                          create / get session
    POST   /strategies/{id}/orders/market            open market position
    GET    /strategies/{id}/positions                list open
    GET    /strategies/{id}/positions/{symbol}       one open by symbol
    POST   /strategies/{id}/positions/{symbol}/close full / partial close
    PATCH  /strategies/{id}/positions/{symbol}/sl    modify SL
    GET    /strategies/{id}/balance                  realised + unrealised
    GET    /strategies/{id}/positions/closed         closed history

The mark price is refreshed on every state-affecting endpoint before
the action runs. SL/TP auto-triggers fire when the refreshed mark
crosses the level. The same `update_mark` runs before GET /balance
and GET /positions too — so any auto-close that happened between
calls shows up immediately.

State persistence: every state-changing operation flushes the session
to disk before returning. Restart-safe — on startup the service
loads every existing session from `data_root` into memory.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, status
from pydantic import BaseModel, Field

from engine_external.application.paper_session import (
    ExternalSessionState,
    OperationResult,
    PaperSession,
)
from engine_external.domain.external_risk_rules import ExternalRiskLimits
from engine_external.domain.order_models import (
    ExternalClosedPosition,
    ExternalOrderRequest,
    ExternalPosition,
    OrderError,
)
from engine_external.infrastructure.audit_log import (
    audit_log_path,
    log_auto_close,
    log_close,
    log_modify_sl,
    log_order,
)
from engine_external.infrastructure.price_source import (
    KuCoinTickerSource,
    PriceSource,
)
from engine_external.infrastructure.state_persistence import (
    load_state,
    save_state,
    session_state_path,
)
from engine_futures.domain.strategy.strategy_models import Direction

logger = logging.getLogger(__name__)


# ─── Defaults ───────────────────────────────────────────────────────────────


DEFAULT_DATA_ROOT = Path("data/external")
DEFAULT_STARTING_EQUITY = 10_000.0
DEFAULT_SLIPPAGE_BPS = 10.0
DEFAULT_SUPPORTED_SYMBOLS = ("BTCUSDT",)
DEFAULT_KILL_SWITCH_PATH = Path("data/kill_switch.txt")


# ─── Pydantic request / response models ─────────────────────────────────────


class CreateSessionRequest(BaseModel):
    starting_equity: float = Field(DEFAULT_STARTING_EQUITY, gt=0)
    slippage_bps: float = Field(DEFAULT_SLIPPAGE_BPS, ge=0)


class MarketOrderRequest(BaseModel):
    symbol: str
    side: str  # "LONG" or "SHORT"
    risk_pct: float = Field(..., gt=0, le=100)
    sl_price: float = Field(..., gt=0)
    tp_price: Optional[float] = Field(None, gt=0)
    leverage: Optional[int] = None  # reserved for v2, ignored in v1
    client_order_id: Optional[str] = None


class CloseRequest(BaseModel):
    percentage: float = Field(100.0, gt=0, le=100)


class ModifySLRequest(BaseModel):
    sl_price: float = Field(..., gt=0)


class PositionResponse(BaseModel):
    symbol: str
    side: str
    entry_price: float
    sl_price: float
    tp_price: Optional[float]
    volume: float
    initial_volume: float
    pnl_usdt: float
    current_price_rr: float
    open_amount_percentage: float
    closed_amount_percentage: float
    entry_time: str


class ClosedPositionResponse(BaseModel):
    symbol: str
    side: str
    entry_price: float
    exit_price: float
    pnl_usdt: float
    pnl_rr: float
    entry_time: str
    exit_time: str


class BalanceResponse(BaseModel):
    strategy_id: str
    starting_equity: float
    realised_equity: float
    unrealised_pnl: float
    total_equity: float
    peak_equity: float
    open_positions_count: int
    closed_positions_count: int


class OrderResponse(BaseModel):
    error: str
    reason: str
    position: Optional[PositionResponse] = None
    closed_position: Optional[ClosedPositionResponse] = None


class SessionInfoResponse(BaseModel):
    strategy_id: str
    starting_equity: float
    slippage_bps: float
    created_at: str
    last_updated: str


# ─── Conversions ────────────────────────────────────────────────────────────


def _position_to_pydantic(pos: ExternalPosition) -> PositionResponse:
    return PositionResponse(
        symbol=pos.symbol,
        side=pos.side.value,
        entry_price=pos.entry_price,
        sl_price=pos.sl_price,
        tp_price=pos.tp_price,
        volume=pos.volume,
        initial_volume=pos.initial_volume,
        pnl_usdt=pos.pnl_usdt,
        current_price_rr=pos.current_price_rr,
        open_amount_percentage=pos.open_amount_percentage,
        closed_amount_percentage=pos.closed_amount_percentage,
        entry_time=pos.entry_time.isoformat(),
    )


def _closed_to_pydantic(c: ExternalClosedPosition) -> ClosedPositionResponse:
    return ClosedPositionResponse(
        symbol=c.symbol,
        side=c.side.value,
        entry_price=c.entry_price,
        exit_price=c.exit_price,
        pnl_usdt=c.pnl_usdt,
        pnl_rr=c.pnl_rr,
        entry_time=c.entry_time.isoformat(),
        exit_time=c.exit_time.isoformat(),
    )


def _result_to_response(r: OperationResult) -> OrderResponse:
    return OrderResponse(
        error=r.error.value,
        reason=r.reason,
        position=_position_to_pydantic(r.position) if r.position else None,
        closed_position=_closed_to_pydantic(r.closed_position) if r.closed_position else None,
    )


def _parse_side(s: str) -> Direction:
    if s.upper() == "LONG":
        return Direction.LONG
    if s.upper() == "SHORT":
        return Direction.SHORT
    raise HTTPException(
        status_code=400,
        detail={"error": OrderError.INVALID_SIDE.value, "reason": f"side must be LONG or SHORT, got {s}"},
    )


# ─── App state container ────────────────────────────────────────────────────


class AppState:
    """In-process store of sessions, shared across requests via app.state."""

    def __init__(
        self,
        *,
        data_root: Path,
        price_source: PriceSource,
        supported_symbols: tuple[str, ...],
        kill_switch_path: Path,
        limits: ExternalRiskLimits,
    ):
        self.data_root = data_root
        self.price_source = price_source
        self.supported_symbols = supported_symbols
        self.kill_switch_path = kill_switch_path
        self.limits = limits
        self.sessions: dict[str, PaperSession] = {}

    def kill_switch_active(self) -> bool:
        return self.kill_switch_path.exists()

    def load_all_existing(self) -> None:
        """On startup: load every session from data_root/<strategy_id>/state.json."""
        if not self.data_root.exists():
            return
        for strategy_dir in self.data_root.iterdir():
            if not strategy_dir.is_dir():
                continue
            state_path = strategy_dir / "state.json"
            state = load_state(state_path)
            if state is None:
                continue
            self.sessions[state.strategy_id] = PaperSession(
                state, self.supported_symbols, self.limits
            )
            logger.info("Loaded session %s from %s", state.strategy_id, state_path)


def get_app_state(request: Request) -> AppState:
    return request.app.state.app_state


# ─── App factory ────────────────────────────────────────────────────────────


def create_app(
    *,
    data_root: Path = DEFAULT_DATA_ROOT,
    price_source: Optional[PriceSource] = None,
    supported_symbols: tuple[str, ...] = DEFAULT_SUPPORTED_SYMBOLS,
    kill_switch_path: Path = DEFAULT_KILL_SWITCH_PATH,
    limits: Optional[ExternalRiskLimits] = None,
) -> FastAPI:
    """Build the FastAPI app. Inject price_source for tests."""

    ps = price_source if price_source is not None else KuCoinTickerSource()
    risk_limits = limits if limits is not None else ExternalRiskLimits()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.app_state = AppState(
            data_root=data_root,
            price_source=ps,
            supported_symbols=supported_symbols,
            kill_switch_path=kill_switch_path,
            limits=risk_limits,
        )
        app.state.app_state.load_all_existing()
        logger.info(
            "engine_external REST service starting: data_root=%s, supported=%s, "
            "kill_switch=%s, %d existing sessions",
            data_root,
            supported_symbols,
            kill_switch_path,
            len(app.state.app_state.sessions),
        )
        yield
        # No shutdown work — sessions are persisted on every mutation.

    app = FastAPI(
        title="QuantStand External Paper Trader",
        description="Per-strategy paper trading for external signal sources (Hermes, etc.)",
        version="0.1.0",
        lifespan=lifespan,
    )

    # ─── Health ─────────────────────────────────────────────────────────

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # ─── Session management ─────────────────────────────────────────────

    @app.post("/strategies/{strategy_id}", response_model=SessionInfoResponse)
    def create_or_get_session(
        strategy_id: str,
        req: CreateSessionRequest,
        request: Request,
    ) -> SessionInfoResponse:
        st = get_app_state(request)
        if strategy_id not in st.sessions:
            now = datetime.now(tz=timezone.utc)
            state = ExternalSessionState.initial(
                strategy_id=strategy_id,
                starting_equity=req.starting_equity,
                slippage_bps=req.slippage_bps,
                now=now,
            )
            st.sessions[strategy_id] = PaperSession(state, st.supported_symbols, st.limits)
            save_state(state, session_state_path(st.data_root, strategy_id))
            logger.info("Created session %s with $%.2f starting equity", strategy_id, req.starting_equity)

        sess = st.sessions[strategy_id]
        return SessionInfoResponse(
            strategy_id=sess.state.strategy_id,
            starting_equity=sess.state.starting_equity,
            slippage_bps=sess.state.slippage_bps,
            created_at=sess.state.created_at.isoformat(),
            last_updated=sess.state.last_updated.isoformat(),
        )

    def _require_session(strategy_id: str, st: AppState) -> PaperSession:
        sess = st.sessions.get(strategy_id)
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail={"error": "session_not_found", "reason": f"unknown strategy_id: {strategy_id}"},
            )
        return sess

    def _refresh_mark_and_persist(
        st: AppState, sess: PaperSession, symbol: str, now: datetime
    ) -> Optional[float]:
        """Update the session's mark for `symbol`; persist any auto-closes."""
        price = st.price_source.get_mark(symbol)
        if price is None:
            return None
        closed = sess.update_mark(symbol, price, now)
        if closed:
            audit_path = audit_log_path(st.data_root, sess.state.strategy_id)
            for c in closed:
                log_auto_close(
                    audit_path,
                    strategy_id=sess.state.strategy_id,
                    closed=c,
                    trigger="sl_or_tp",
                )
            save_state(sess.state, session_state_path(st.data_root, sess.state.strategy_id))
        return price

    # ─── Orders ─────────────────────────────────────────────────────────

    @app.post(
        "/strategies/{strategy_id}/orders/market",
        response_model=OrderResponse,
    )
    def open_market_order(
        strategy_id: str,
        body: MarketOrderRequest,
        request: Request,
    ) -> OrderResponse:
        st = get_app_state(request)
        sess = _require_session(strategy_id, st)
        side = _parse_side(body.side)
        now = datetime.now(tz=timezone.utc)

        req = ExternalOrderRequest(
            symbol=body.symbol,
            side=side,
            risk_pct=body.risk_pct,
            sl_price=body.sl_price,
            tp_price=body.tp_price,
            leverage=body.leverage,
            client_order_id=body.client_order_id,
        )

        # Refresh mark before deciding. SL/TP auto-close may fire here too.
        # Skip the mark fetch for symbols we don't support — `open_market`'s
        # internal validation will catch the symbol issue and return
        # SYMBOL_NOT_SUPPORTED rather than NO_MARK_PRICE.
        if body.symbol not in st.supported_symbols:
            mark = None
        else:
            mark = _refresh_mark_and_persist(st, sess, body.symbol, now)
            if mark is None:
                r = OperationResult(
                    error=OrderError.NO_MARK_PRICE, reason="price_source_returned_none"
                )
                log_order(
                    audit_log_path(st.data_root, strategy_id),
                    strategy_id=strategy_id,
                    req=req,
                    mark_price=None,
                    result=r,
                )
                return _result_to_response(r)

        r = sess.open_market(
            req=req,
            mark_price=mark if mark is not None else 0.0,
            kill_switch_active=st.kill_switch_active(),
            now=now,
        )
        # Persist + audit regardless of success
        save_state(sess.state, session_state_path(st.data_root, strategy_id))
        log_order(
            audit_log_path(st.data_root, strategy_id),
            strategy_id=strategy_id,
            req=req,
            mark_price=mark,
            result=r,
        )
        return _result_to_response(r)

    # ─── Read open + closed positions ───────────────────────────────────
    # NOTE on route order: more specific paths (`/positions/closed`) MUST
    # be registered before path-parameter routes (`/positions/{symbol}`),
    # otherwise FastAPI matches `symbol=closed` and the closed-history
    # endpoint becomes unreachable.

    @app.get(
        "/strategies/{strategy_id}/positions/closed",
        response_model=list[ClosedPositionResponse],
    )
    def list_closed(
        strategy_id: str, request: Request, symbol: Optional[str] = None
    ) -> list[ClosedPositionResponse]:
        st = get_app_state(request)
        sess = _require_session(strategy_id, st)
        return [_closed_to_pydantic(c) for c in sess.list_closed(symbol)]

    @app.get(
        "/strategies/{strategy_id}/positions",
        response_model=list[PositionResponse],
    )
    def list_open_positions(strategy_id: str, request: Request) -> list[PositionResponse]:
        st = get_app_state(request)
        sess = _require_session(strategy_id, st)
        now = datetime.now(tz=timezone.utc)
        # Refresh marks for every open symbol — fires any pending SL/TP.
        for sym in list(sess.state.open_positions.keys()):
            _refresh_mark_and_persist(st, sess, sym, now)
        return [_position_to_pydantic(p) for p in sess.list_open()]

    @app.get(
        "/strategies/{strategy_id}/positions/{symbol}",
        response_model=PositionResponse,
    )
    def get_open_position(strategy_id: str, symbol: str, request: Request) -> PositionResponse:
        st = get_app_state(request)
        sess = _require_session(strategy_id, st)
        now = datetime.now(tz=timezone.utc)
        _refresh_mark_and_persist(st, sess, symbol, now)
        pos = sess.get_open(symbol)
        if pos is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": OrderError.POSITION_NOT_FOUND.value,
                    "reason": f"no open position for {symbol}",
                },
            )
        return _position_to_pydantic(pos)

    # ─── Close (full / partial) ─────────────────────────────────────────

    @app.post(
        "/strategies/{strategy_id}/positions/{symbol}/close",
        response_model=OrderResponse,
    )
    def close_position(
        strategy_id: str,
        symbol: str,
        body: CloseRequest,
        request: Request,
    ) -> OrderResponse:
        st = get_app_state(request)
        sess = _require_session(strategy_id, st)
        now = datetime.now(tz=timezone.utc)
        _refresh_mark_and_persist(st, sess, symbol, now)
        if body.percentage >= 100.0:
            r = sess.close_position(symbol, now)
        else:
            r = sess.partial_close(symbol, body.percentage, now)
        save_state(sess.state, session_state_path(st.data_root, strategy_id))
        log_close(
            audit_log_path(st.data_root, strategy_id),
            strategy_id=strategy_id,
            symbol=symbol,
            percentage=body.percentage,
            result=r,
        )
        return _result_to_response(r)

    # ─── Modify SL ──────────────────────────────────────────────────────

    @app.patch(
        "/strategies/{strategy_id}/positions/{symbol}/sl",
        response_model=OrderResponse,
    )
    def modify_sl(
        strategy_id: str,
        symbol: str,
        body: ModifySLRequest,
        request: Request,
    ) -> OrderResponse:
        st = get_app_state(request)
        sess = _require_session(strategy_id, st)
        now = datetime.now(tz=timezone.utc)
        r = sess.modify_sl(symbol, body.sl_price, now)
        save_state(sess.state, session_state_path(st.data_root, strategy_id))
        log_modify_sl(
            audit_log_path(st.data_root, strategy_id),
            strategy_id=strategy_id,
            symbol=symbol,
            old_sl=r.old_sl if r.old_sl is not None else body.sl_price,
            new_sl=body.sl_price,
            result=r,
        )
        return _result_to_response(r)

    # ─── Balance ────────────────────────────────────────────────────────

    @app.get("/strategies/{strategy_id}/balance", response_model=BalanceResponse)
    def get_balance(strategy_id: str, request: Request) -> BalanceResponse:
        st = get_app_state(request)
        sess = _require_session(strategy_id, st)
        now = datetime.now(tz=timezone.utc)
        for sym in list(sess.state.open_positions.keys()):
            _refresh_mark_and_persist(st, sess, sym, now)
        return BalanceResponse(
            strategy_id=sess.state.strategy_id,
            starting_equity=sess.state.starting_equity,
            realised_equity=sess.realised_equity(),
            unrealised_pnl=sess.unrealised_pnl(),
            total_equity=sess.total_equity(),
            peak_equity=sess.state.peak_equity,
            open_positions_count=len(sess.state.open_positions),
            closed_positions_count=len(sess.state.closed_positions),
        )

    return app
