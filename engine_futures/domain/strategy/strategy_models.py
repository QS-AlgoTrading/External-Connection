"""Minimal stub — `Direction` enum only.

This file is a trimmed-down copy of QuantStand's internal
`engine_futures/domain/strategy/strategy_models.py`. The full version
also defines `StrategyConfig` and other strategy-domain types not
needed by the external paper trader.

`engine_external/` consumes only `Direction`; keeping the import path
identical to the main repo means the same code runs in both places
with zero changes.
"""

from __future__ import annotations

from enum import Enum


class Direction(Enum):
    """Trade direction for a single instrument at a single point in time.

    FLAT means "no exposure" — either the signal is off or the regime
    gate rejected it. Downstream layers treat FLAT as "close any open
    position; do not open a new one". The external paper trader
    rejects FLAT as a side on new orders.
    """

    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"
