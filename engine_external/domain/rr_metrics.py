"""Risk-reward (R-multiple) metrics for external positions.

Pure-logic layer. Zero I/O. Stdlib only.

R-multiple semantics (Hermes spec field `current_price_RR` on open
positions and `p&l_RR` on closed):

    R = (current_price - entry_price) / sl_distance  [LONG]
    R = (entry_price - current_price) / sl_distance  [SHORT]

    SL distance is `|entry - sl|`.

So R = +1.0 means current price has moved exactly 1 SL-distance in
the profitable direction (i.e., we're up by the risk amount). R = -1.0
means we're at the SL.

This module just exposes the formula; whatever calls it provides the
prices it has.
"""

from __future__ import annotations

from engine_futures.domain.strategy.strategy_models import Direction
from engine_external.domain.risk_sizing import direction_sign, sl_distance


def current_price_rr(
    *,
    side: Direction,
    entry_price: float,
    sl_price: float,
    current_price: float,
) -> float:
    """Signed R-multiple of the current price.

    Returns 0.0 when sl_distance is zero (degenerate input — the
    caller is responsible for not constructing such positions).

    Positive return = profit; negative = drawdown vs SL. At
    `current_price == sl_price` the result is exactly -1.0; at one
    SL-distance past entry in the profitable direction, +1.0.
    """
    dist = sl_distance(entry_price, sl_price)
    if dist == 0:
        return 0.0
    sign = direction_sign(side)
    return sign * (current_price - entry_price) / dist


def realised_rr(
    *,
    side: Direction,
    entry_price: float,
    sl_price: float,
    exit_price: float,
) -> float:
    """Realised R-multiple at the close price — alias of `current_price_rr`."""
    return current_price_rr(
        side=side,
        entry_price=entry_price,
        sl_price=sl_price,
        current_price=exit_price,
    )


def unrealised_pnl_usdt(
    *,
    side: Direction,
    entry_price: float,
    current_price: float,
    volume: float,
) -> float:
    """Open-position unrealised P&L in quote currency (USDT).

    LONG:  pnl = (current - entry) * volume
    SHORT: pnl = (entry - current) * volume
    """
    sign = direction_sign(side)
    return sign * (current_price - entry_price) * volume


def realised_pnl_usdt(
    *,
    side: Direction,
    entry_price: float,
    exit_price: float,
    volume: float,
) -> float:
    """Realised P&L at a close — alias of `unrealised_pnl_usdt` at the close price."""
    return unrealised_pnl_usdt(
        side=side,
        entry_price=entry_price,
        current_price=exit_price,
        volume=volume,
    )
