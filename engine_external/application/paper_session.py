"""External paper-trading session — per-strategy state + operations.

Application layer. No I/O — caller provides mark prices, persists
state. The session class is mutable for ergonomics (REST handler
holds one session per strategy and updates it in place). The
underlying state dataclasses follow the rest of the project's
mutable-only-when-deliberate pattern.

Why not reuse `PaperBroker`?
    PaperBroker carries slippage, commission, fills log, attached
    SL/TP, multi-instrument state — all designed for the multi-coin
    backtest harness. The external paper session has narrower
    requirements:
        - one strategy, isolated equity pool
        - SL/TP auto-trigger checked on every mark-price update
        - explicit partial-close semantics (Hermes's "50% of remaining
          margin" rule)
        - persistable state with a simple JSON schema
    Building this directly is ~200 lines and avoids serialising
    PaperBroker's private fields.

Pricing model (v1):
    Mark price is refreshed by the caller (REST infrastructure)
    before every state-querying operation. SL/TP auto-trigger fires
    when `update_mark()` is called with a price that has crossed the
    trigger level. The fill price is the SL/TP level (not the
    triggering mark), modelling a stop-market fill.
    Slippage is applied via `slippage_bps` (a single absolute value
    per session, mirroring AroonRsi's 10 bps assumption).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

from engine_external.domain.external_risk_rules import (
    ExternalRiskLimits,
    evaluate_external_gate,
)
from engine_external.domain.order_models import (
    ExternalClosedPosition,
    ExternalOrderRequest,
    ExternalPosition,
    OrderError,
    validate_order,
    validate_sl_vs_entry,
    validate_trailing_sl,
)
from engine_external.domain.risk_sizing import direction_sign, size_from_risk
from engine_external.domain.rr_metrics import (
    current_price_rr,
    realised_pnl_usdt,
    realised_rr,
    unrealised_pnl_usdt,
)
from engine_futures.domain.strategy.strategy_models import Direction


# ─── Persistent records (subset of broker state we serialise) ────────────────


@dataclass
class _OpenPositionRecord:
    """One open position. Mutable — partial closes shrink current_volume.

    `initial_sl_price` is frozen at entry time and never modified — all
    R-multiple calculations use this value so that trail updates don't
    alter the risk baseline. `sl_price` is mutable (trailed by the
    agent) and drives SL/TP auto-triggers.
    """

    symbol: str
    side: Direction
    entry_price: float
    entry_time: datetime
    initial_volume: float
    current_volume: float
    sl_price: float
    initial_sl_price: float
    tp_price: float | None


@dataclass
class ExternalSessionState:
    """All persisted state for one external strategy's paper session.

    `equity_base` = starting equity + realised P&L from closed and
    partially-closed positions − commissions. The session's current
    total equity = `equity_base` + unrealised P&L over open positions.

    `peak_equity` is the high-water mark used by the drawdown gate.
    Updated whenever total equity exceeds the previous peak.
    """

    strategy_id: str
    starting_equity: float
    slippage_bps: float
    created_at: datetime
    last_updated: datetime

    # Broker-equivalent state
    equity_base: float
    last_prices: dict[str, float] = field(default_factory=dict)
    open_positions: dict[str, _OpenPositionRecord] = field(default_factory=dict)

    # History
    closed_positions: list[ExternalClosedPosition] = field(default_factory=list)
    fills_count: int = 0
    orders_seen: dict[str, str] = field(default_factory=dict)
    """Mapping client_order_id -> result hash (for idempotent retries)."""

    # Risk-gate ledger
    peak_equity: float = 0.0  # initialised to starting_equity at construction
    day_start_equity: float | None = None
    day_start_ts: datetime | None = None

    @classmethod
    def initial(
        cls,
        strategy_id: str,
        starting_equity: float,
        slippage_bps: float,
        now: datetime,
    ) -> "ExternalSessionState":
        return cls(
            strategy_id=strategy_id,
            starting_equity=starting_equity,
            slippage_bps=slippage_bps,
            created_at=now,
            last_updated=now,
            equity_base=starting_equity,
            peak_equity=starting_equity,
        )


# ─── Operation results ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class OperationResult:
    """Outcome of a session operation.

    `error` is `OrderError.OK` when the operation succeeded. `position`
    is populated on a successful open or post-modify; `closed_position`
    on a successful close. Callers should match on `error` first.
    """

    error: OrderError
    reason: str = ""
    position: ExternalPosition | None = None
    closed_position: ExternalClosedPosition | None = None
    old_sl: float | None = None


# ─── Session ────────────────────────────────────────────────────────────────


class PaperSession:
    """One external strategy's paper-trading session.

    Construct from an `ExternalSessionState`. All operations mutate
    `state` in place; the caller is responsible for persisting after
    each operation.

    Operations:
        update_mark        — set the current mark price for a symbol;
                             fires SL/TP auto-close if crossed.
        open_market        — handle an `ExternalOrderRequest`.
        close_position     — full close.
        partial_close      — close a percentage of REMAINING volume
                             (Hermes's spec — "50% of remaining margin").
        modify_sl          — update the SL price for an open position.
        list_open          — return Hermes-shaped open-position views.
        get_open           — one open position by symbol, or None.
        list_closed        — closed positions, optionally filtered by
                             symbol.
        balance            — realised + unrealised, total equity.
    """

    def __init__(
        self,
        state: ExternalSessionState,
        supported_symbols: tuple[str, ...],
        limits: ExternalRiskLimits,
    ):
        self.state = state
        self.supported_symbols = supported_symbols
        self.limits = limits

    # ── Equity helpers ──────────────────────────────────────────────────

    def realised_equity(self) -> float:
        return self.state.equity_base

    def unrealised_pnl(self) -> float:
        total = 0.0
        for pos in self.state.open_positions.values():
            mark = self.state.last_prices.get(pos.symbol)
            if mark is None:
                continue
            total += unrealised_pnl_usdt(
                side=pos.side,
                entry_price=pos.entry_price,
                current_price=mark,
                volume=pos.current_volume,
            )
        return total

    def total_equity(self) -> float:
        return self.realised_equity() + self.unrealised_pnl()

    # ── Mark price + SL/TP triggers ─────────────────────────────────────

    def update_mark(self, symbol: str, price: float, now: datetime) -> list[ExternalClosedPosition]:
        """Set the mark price and fire SL/TP auto-closes.

        Returns the list of positions auto-closed by this update (empty
        list if none). The caller can audit these as broker-side fills.
        """
        self.state.last_prices[symbol] = price
        closed: list[ExternalClosedPosition] = []
        pos = self.state.open_positions.get(symbol)
        if pos is None:
            self._touch(now)
            return closed

        # SL/TP triggers — fill at the trigger level, not the mark.
        if _sl_triggered(pos, price):
            closed.append(self._close_internal(pos, pos.sl_price, now, reason="sl_hit"))
        elif pos.tp_price is not None and _tp_triggered(pos, price):
            closed.append(self._close_internal(pos, pos.tp_price, now, reason="tp_hit"))

        self._update_peak()
        self._touch(now)
        return closed

    # ── Open ────────────────────────────────────────────────────────────

    def open_market(
        self,
        req: ExternalOrderRequest,
        mark_price: float,
        kill_switch_active: bool,
        now: datetime,
    ) -> OperationResult:
        """Handle a market-order request.

        Pipeline: validate request -> validate SL vs mark -> risk gate
        -> sizing -> open. Idempotent on `client_order_id` — repeated
        submission of the same id returns the original outcome.
        """
        # 1. Idempotency check
        if req.client_order_id is not None and req.client_order_id in self.state.orders_seen:
            existing = self.state.open_positions.get(req.symbol)
            if existing is not None:
                return OperationResult(
                    error=OrderError.OK,
                    reason="idempotent_retry",
                    position=self._position_view(existing, mark_price),
                )
            # Logged as seen but no open position — treat as no-op success.
            return OperationResult(error=OrderError.OK, reason="idempotent_retry")

        # 2. Structural validation
        err = validate_order(req, self.supported_symbols)
        if err is not OrderError.OK:
            return OperationResult(error=err, reason=err.value)

        # 3. SL vs entry sanity (using mark as entry)
        err = validate_sl_vs_entry(req.side, mark_price, req.sl_price)
        if err is not OrderError.OK:
            return OperationResult(error=err, reason="sl_on_wrong_side_of_entry")

        # 4. Apply slippage to entry (adverse: LONG pays more, SHORT receives less)
        slippage = mark_price * self.state.slippage_bps / 10_000.0
        if req.side is Direction.LONG:
            entry_price = mark_price + slippage
        else:
            entry_price = mark_price - slippage

        # 5. Risk gate
        open_count = 1 if req.symbol in self.state.open_positions else 0
        gate = evaluate_external_gate(
            instrument=req.symbol,
            open_positions_for_symbol=open_count,
            peak_equity=self.state.peak_equity,
            current_equity=self.total_equity(),
            limits=self.limits,
            kill_switch_active=kill_switch_active,
        )
        if not gate.allowed:
            return OperationResult(error=gate.error, reason=gate.reason)

        # 6. Sizing
        try:
            volume = size_from_risk(
                risk_pct=req.risk_pct,
                equity=self.total_equity(),
                entry_price=entry_price,
                sl_price=req.sl_price,
            )
        except ValueError as e:
            return OperationResult(error=OrderError.INVALID_RISK, reason=str(e))

        # 7. Record + commit
        record = _OpenPositionRecord(
            symbol=req.symbol,
            side=req.side,
            entry_price=entry_price,
            entry_time=now,
            initial_volume=volume,
            current_volume=volume,
            sl_price=req.sl_price,
            initial_sl_price=req.sl_price,
            tp_price=req.tp_price,
        )
        self.state.open_positions[req.symbol] = record
        self.state.fills_count += 1
        if req.client_order_id is not None:
            self.state.orders_seen[req.client_order_id] = str(uuid.uuid4())
        self._update_peak()
        self._touch(now)

        return OperationResult(
            error=OrderError.OK,
            reason="opened",
            position=self._position_view(record, mark_price),
        )

    # ── Close (full / partial) ──────────────────────────────────────────

    def close_position(self, symbol: str, now: datetime) -> OperationResult:
        pos = self.state.open_positions.get(symbol)
        if pos is None:
            return OperationResult(
                error=OrderError.POSITION_NOT_FOUND, reason="not_found"
            )
        mark = self.state.last_prices.get(symbol)
        if mark is None:
            return OperationResult(
                error=OrderError.NO_MARK_PRICE, reason="no_mark_price"
            )
        # Apply close-side slippage adverse to direction
        slippage = mark * self.state.slippage_bps / 10_000.0
        exit_price = mark - slippage if pos.side is Direction.LONG else mark + slippage
        closed = self._close_internal(pos, exit_price, now, reason="manual_close")
        return OperationResult(
            error=OrderError.OK, reason="closed", closed_position=closed
        )

    def partial_close(
        self, symbol: str, percentage: float, now: datetime
    ) -> OperationResult:
        """Close `percentage`% of REMAINING volume (Hermes's spec).

        A 50% close on a position already half-closed leaves 25% of the
        initial volume open. The closed slice's P&L is added to
        `equity_base`; the original record's `current_volume` shrinks
        but `initial_volume` stays — this is what drives the
        open/closed amount percentages reported to Hermes.
        """
        if percentage <= 0 or percentage > 100:
            return OperationResult(
                error=OrderError.INVALID_CLOSE_PERCENTAGE,
                reason=f"percentage must be in (0, 100], got {percentage}",
            )
        pos = self.state.open_positions.get(symbol)
        if pos is None:
            return OperationResult(
                error=OrderError.POSITION_NOT_FOUND, reason="not_found"
            )
        mark = self.state.last_prices.get(symbol)
        if mark is None:
            return OperationResult(
                error=OrderError.NO_MARK_PRICE, reason="no_mark_price"
            )
        slippage = mark * self.state.slippage_bps / 10_000.0
        exit_price = mark - slippage if pos.side is Direction.LONG else mark + slippage

        close_volume = pos.current_volume * (percentage / 100.0)
        # Realised P&L on the closed slice
        pnl = realised_pnl_usdt(
            side=pos.side,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            volume=close_volume,
        )
        self.state.equity_base += pnl

        if percentage >= 100.0:
            # Full close — same as close_position
            return self._finalise_close(pos, exit_price, now, reason="partial_full")

        # Shrink the open record
        pos.current_volume -= close_volume
        # Append a closed-slice entry so history reflects the partial
        self.state.closed_positions.append(
            ExternalClosedPosition(
                symbol=symbol,
                side=pos.side,
                entry_price=pos.entry_price,
                exit_price=exit_price,
                pnl_usdt=pnl,
                pnl_rr=realised_rr(
                    side=pos.side,
                    entry_price=pos.entry_price,
                    sl_price=pos.initial_sl_price,
                    exit_price=exit_price,
                ),
                entry_time=pos.entry_time,
                exit_time=now,
            )
        )
        self.state.fills_count += 1
        self._update_peak()
        self._touch(now)
        return OperationResult(
            error=OrderError.OK,
            reason="partial_closed",
            position=self._position_view(pos, mark),
        )

    # ── Modify SL ───────────────────────────────────────────────────────

    def modify_sl(self, symbol: str, new_sl: float, now: datetime) -> OperationResult:
        pos = self.state.open_positions.get(symbol)
        if pos is None:
            return OperationResult(
                error=OrderError.POSITION_NOT_FOUND, reason="not_found"
            )
        if new_sl <= 0:
            return OperationResult(
                error=OrderError.INVALID_SL, reason="non_positive_sl"
            )
        old_sl = pos.sl_price
        err = validate_trailing_sl(pos.side, old_sl, new_sl)
        if err is not OrderError.OK:
            return OperationResult(
                error=err, reason=err.value, old_sl=old_sl
            )
        pos.sl_price = new_sl
        self._touch(now)
        mark = self.state.last_prices.get(symbol)
        return OperationResult(
            error=OrderError.OK,
            reason="sl_modified",
            position=self._position_view(pos, mark if mark is not None else pos.entry_price),
            old_sl=old_sl,
        )

    # ── Reads ───────────────────────────────────────────────────────────

    def list_open(self) -> list[ExternalPosition]:
        out = []
        for pos in self.state.open_positions.values():
            mark = self.state.last_prices.get(pos.symbol, pos.entry_price)
            out.append(self._position_view(pos, mark))
        return out

    def get_open(self, symbol: str) -> ExternalPosition | None:
        pos = self.state.open_positions.get(symbol)
        if pos is None:
            return None
        mark = self.state.last_prices.get(symbol, pos.entry_price)
        return self._position_view(pos, mark)

    def list_closed(self, symbol: str | None = None) -> list[ExternalClosedPosition]:
        if symbol is None:
            return list(self.state.closed_positions)
        return [c for c in self.state.closed_positions if c.symbol == symbol]

    # ── Internals ───────────────────────────────────────────────────────

    def _close_internal(
        self,
        pos: _OpenPositionRecord,
        exit_price: float,
        now: datetime,
        reason: str,
    ) -> ExternalClosedPosition:
        pnl = realised_pnl_usdt(
            side=pos.side,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            volume=pos.current_volume,
        )
        self.state.equity_base += pnl
        return self._finalise_close(pos, exit_price, now, reason=reason).closed_position  # type: ignore[return-value]

    def _finalise_close(
        self,
        pos: _OpenPositionRecord,
        exit_price: float,
        now: datetime,
        reason: str,
    ) -> OperationResult:
        closed = ExternalClosedPosition(
            symbol=pos.symbol,
            side=pos.side,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            pnl_usdt=realised_pnl_usdt(
                side=pos.side,
                entry_price=pos.entry_price,
                exit_price=exit_price,
                volume=pos.current_volume,
            ),
            pnl_rr=realised_rr(
                side=pos.side,
                entry_price=pos.entry_price,
                sl_price=pos.initial_sl_price,
                exit_price=exit_price,
            ),
            entry_time=pos.entry_time,
            exit_time=now,
        )
        self.state.closed_positions.append(closed)
        self.state.open_positions.pop(pos.symbol, None)
        self.state.fills_count += 1
        self._update_peak()
        self._touch(now)
        return OperationResult(
            error=OrderError.OK, reason=reason, closed_position=closed
        )

    def _position_view(
        self, pos: _OpenPositionRecord, mark_price: float
    ) -> ExternalPosition:
        open_pct = (
            100.0 * pos.current_volume / pos.initial_volume
            if pos.initial_volume > 0
            else 0.0
        )
        return ExternalPosition(
            symbol=pos.symbol,
            side=pos.side,
            entry_price=pos.entry_price,
            sl_price=pos.sl_price,
            tp_price=pos.tp_price,
            volume=pos.current_volume,
            initial_volume=pos.initial_volume,
            pnl_usdt=unrealised_pnl_usdt(
                side=pos.side,
                entry_price=pos.entry_price,
                current_price=mark_price,
                volume=pos.current_volume,
            ),
            current_price_rr=current_price_rr(
                side=pos.side,
                entry_price=pos.entry_price,
                sl_price=pos.initial_sl_price,
                current_price=mark_price,
            ),
            open_amount_percentage=open_pct,
            closed_amount_percentage=100.0 - open_pct,
            entry_time=pos.entry_time,
        )

    def _update_peak(self) -> None:
        eq = self.total_equity()
        if eq > self.state.peak_equity:
            self.state.peak_equity = eq

    def _touch(self, now: datetime) -> None:
        self.state.last_updated = now


# ─── Trigger helpers ────────────────────────────────────────────────────────


def _sl_triggered(pos: _OpenPositionRecord, mark: float) -> bool:
    """LONG SL fires when mark <= sl; SHORT SL fires when mark >= sl."""
    if pos.side is Direction.LONG:
        return mark <= pos.sl_price
    return mark >= pos.sl_price


def _tp_triggered(pos: _OpenPositionRecord, mark: float) -> bool:
    """LONG TP fires when mark >= tp; SHORT TP fires when mark <= tp."""
    if pos.tp_price is None:
        return False
    if pos.side is Direction.LONG:
        return mark >= pos.tp_price
    return mark <= pos.tp_price
