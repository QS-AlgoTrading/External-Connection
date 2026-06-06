"""External-strategy order + position models (Hermes-shaped).

Pure-logic layer. Zero I/O. Stdlib only.

Maps Hermes's spec (see integration thread) onto frozen dataclasses
the REST + MCP layers consume:

    ExternalOrderRequest    — incoming "open me a market position"
    ExternalPosition        — open-position summary (Hermes's
                              "Open position info structure")
    ExternalClosedPosition  — closed-position summary (Hermes's
                              "Closed position info structure")
    OrderError              — typed rejection reasons

Reuses `Direction` from `engine_futures.domain.strategy.strategy_models`
to keep direction semantics consistent across the platform. FLAT is
explicitly invalid as a side on a new order — close requests are a
different endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from engine_futures.domain.strategy.strategy_models import Direction


# ─── Error taxonomy ─────────────────────────────────────────────────────────


class OrderError(Enum):
    """Typed rejection reasons returned by the REST layer.

    Hermes's spec says order endpoints return `error_code`. We return
    a member of this enum (serialised to its string value over the wire)
    so the agent can pattern-match cleanly.
    """

    OK = "ok"
    SYMBOL_NOT_SUPPORTED = "symbol_not_supported"
    INVALID_SIDE = "invalid_side"
    INVALID_SL = "invalid_sl"
    INVALID_RISK = "invalid_risk"
    POSITION_LIMIT = "position_limit"
    KILL_SWITCH_ACTIVE = "kill_switch_active"
    DRAWDOWN_HALT = "drawdown_halt"
    NO_MARK_PRICE = "no_mark_price"
    POSITION_NOT_FOUND = "position_not_found"
    INVALID_CLOSE_PERCENTAGE = "invalid_close_percentage"
    BROKER_REJECTED = "broker_rejected"


# ─── Inputs ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ExternalOrderRequest:
    """A market-order intent from an external strategy.

    Fields match Hermes's order spec one-for-one:
        side:      Direction.LONG or Direction.SHORT (FLAT is rejected).
        risk_pct:  percentage of strategy equity to risk if SL hits.
                   E.g., 1.0 means 1% of equity = max loss if SL fires.
        sl_price:  absolute stop-loss price. Must be on the loss side
                   of entry: LONG → sl < entry; SHORT → sl > entry.
        tp_price:  optional take-profit price.

    `leverage` is reserved for v2 (futures-aware liquidation-price
    management). Pass None in v1 — it's accepted but ignored.

    `client_order_id` lets the caller idempotently retry a request: if
    the server has already seen this id for this strategy, it returns
    the original outcome rather than opening a duplicate position.
    """

    symbol: str
    side: Direction
    risk_pct: float
    sl_price: float
    tp_price: float | None = None
    leverage: int | None = None
    client_order_id: str | None = None


# ─── Position views ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ExternalPosition:
    """Open-position summary as Hermes will read it.

    Field names match Hermes's "Open position info structure". The two
    percentages describe how much of the at-open volume is still live
    vs already closed:

        open_amount_percentage   = volume / initial_volume * 100
        closed_amount_percentage = 100 - open_amount_percentage

    After a 50% partial close: open=50, closed=50. After full close
    the position transitions to `ExternalClosedPosition` and is no
    longer reported here.
    """

    symbol: str
    side: Direction
    entry_price: float
    sl_price: float
    tp_price: float | None
    volume: float                          # current open volume
    initial_volume: float                  # at-open volume (frozen)
    pnl_usdt: float                        # realised + unrealised
    current_price_rr: float                # signed R-multiple at current mark
    open_amount_percentage: float
    closed_amount_percentage: float
    entry_time: datetime


@dataclass(frozen=True)
class ExternalClosedPosition:
    """Closed-position summary as Hermes will read it.

    Field names match Hermes's "Closed position info structure". `pnl_rr`
    is the realised R-multiple at the close price (signed for profit).
    """

    symbol: str
    side: Direction
    entry_price: float
    exit_price: float
    pnl_usdt: float
    pnl_rr: float
    entry_time: datetime
    exit_time: datetime


# ─── Validation helpers ─────────────────────────────────────────────────────


def validate_order(
    req: ExternalOrderRequest,
    supported_symbols: tuple[str, ...],
) -> OrderError:
    """Return OrderError.OK if the request is structurally valid, else a typed error.

    Checks (in order):
        1. side is not FLAT
        2. symbol is in the supported universe
        3. risk_pct is positive
        4. sl_price is on the loss side of entry — but since we don't
           have entry yet, we only check sl_price > 0 here. The
           entry-vs-SL sanity check happens at the application layer
           when the mark price is known.
    """
    if req.side is Direction.FLAT:
        return OrderError.INVALID_SIDE
    if req.symbol not in supported_symbols:
        return OrderError.SYMBOL_NOT_SUPPORTED
    if req.risk_pct <= 0 or req.risk_pct > 100.0:
        return OrderError.INVALID_RISK
    if req.sl_price <= 0:
        return OrderError.INVALID_SL
    return OrderError.OK


def validate_sl_vs_entry(
    side: Direction,
    entry_price: float,
    sl_price: float,
) -> OrderError:
    """Check the SL is on the loss side of entry.

    LONG: sl < entry. SHORT: sl > entry. Anything else is rejected.
    """
    if side is Direction.LONG and sl_price >= entry_price:
        return OrderError.INVALID_SL
    if side is Direction.SHORT and sl_price <= entry_price:
        return OrderError.INVALID_SL
    return OrderError.OK


def validate_trailing_sl(
    side: Direction,
    old_sl: float,
    new_sl: float,
) -> OrderError:
    """Check a trailing-stop modification does not increase risk.

    Only allows moving the SL in the risk-reducing direction:
        LONG:  new_sl >= old_sl  (move SL up, never down)
        SHORT: new_sl <= old_sl  (move SL down, never up)

    This is deliberately more permissive than `validate_sl_vs_entry`
    (which requires the SL stay on the loss side of entry) — trailing
    stops are expected to cross the entry price for breakeven trades.
    """
    if side is Direction.LONG and new_sl < old_sl:
        return OrderError.INVALID_SL
    if side is Direction.SHORT and new_sl > old_sl:
        return OrderError.INVALID_SL
    return OrderError.OK
