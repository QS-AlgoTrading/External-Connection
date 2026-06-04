"""Risk-percentage to position-size translation (Hermes-spec sizing).

Pure-logic layer. Zero I/O. Stdlib only.

Hermes specifies positions by *risk percentage*: "if SL hits, lose
risk_pct% of equity." This module computes the corresponding position
size in base units:

    risk_quote   = (risk_pct / 100) * equity
    sl_distance  = |entry - sl|
    volume_base  = risk_quote / sl_distance

The same formula is what AroonRsi's ATR sizing reduces to once
sl_distance is given; we just expose it directly here.

Per the Hermes integration spec, leverage management is deferred to
v2 — v1 sizes in base units with no leverage multiplier.
"""

from __future__ import annotations

from engine_futures.domain.strategy.strategy_models import Direction


def size_from_risk(
    *,
    risk_pct: float,
    equity: float,
    entry_price: float,
    sl_price: float,
) -> float:
    """Compute position size in base units from risk percentage and SL.

    Args:
        risk_pct:     percent of equity to lose if SL fires (1.0 = 1%).
        equity:       current strategy equity in quote (USDT).
        entry_price:  expected fill price (the market mark when the
                      order is submitted).
        sl_price:     absolute stop-loss price.

    Returns:
        Position size in base units (e.g., BTC for BTCUSDT).

    Raises:
        ValueError on degenerate inputs (zero SL distance, non-positive
        equity, etc.). The application layer should catch these and
        return `OrderError.INVALID_SL` / `INVALID_RISK` upstream.
    """
    if equity <= 0:
        raise ValueError(f"equity must be positive (got {equity})")
    if risk_pct <= 0:
        raise ValueError(f"risk_pct must be positive (got {risk_pct})")
    if entry_price <= 0:
        raise ValueError(f"entry_price must be positive (got {entry_price})")
    sl_distance = abs(entry_price - sl_price)
    if sl_distance <= 0:
        raise ValueError(
            f"sl_price must differ from entry_price "
            f"(entry={entry_price}, sl={sl_price})"
        )
    risk_quote = (risk_pct / 100.0) * equity
    return risk_quote / sl_distance


def sl_distance(entry_price: float, sl_price: float) -> float:
    """Return the absolute SL distance — utility used by RR metrics."""
    return abs(entry_price - sl_price)


def direction_sign(side: Direction) -> int:
    """Return +1 for LONG, -1 for SHORT. FLAT raises."""
    if side is Direction.LONG:
        return 1
    if side is Direction.SHORT:
        return -1
    raise ValueError(f"FLAT has no directional sign (got {side})")
