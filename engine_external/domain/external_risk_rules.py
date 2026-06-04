"""Stripped-down risk rules for external-strategy execution.

Pure-logic layer. Zero I/O. Stdlib only.

External strategies (Hermes, Apollo, future signal sources) compute
their own position sizing via `risk_pct` — they do NOT go through
the AroonRsi allocation framework (bucket caps, per-coin shares).
The QuantStand platform still enforces three universal safety nets:

    1. Kill switch       — operator override file
    2. Drawdown halt     — per-strategy peak equity, default 30%
    3. Position limit    — 1 open position per symbol per strategy

Notably absent vs the AroonRsi risk gate:
    - No bucket / per-coin allocation cap (Hermes sizes itself).
    - No cooldown (external strategies may want quick re-entry).
    - No daily-loss cap by default (operator can opt in if desired).

The gate is intentionally minimal: the goal is to prevent runaway
losses from a misbehaving signal source, not to enforce a portfolio
construction view. The bucket caps remain available to strategies
whose authors derive them via research; Hermes is not yet at that
stage.
"""

from __future__ import annotations

from dataclasses import dataclass

from engine_external.domain.order_models import OrderError


@dataclass(frozen=True)
class ExternalRiskLimits:
    """Per-strategy risk gate parameters for external strategies.

    Defaults match the Hermes v1 integration:
        max_drawdown_pct             = 30.0
        max_positions_per_instrument = 1
        max_daily_loss_pct           = None  (disabled)
    """

    max_drawdown_pct: float = 30.0
    max_positions_per_instrument: int = 1
    max_daily_loss_pct: float | None = None


@dataclass(frozen=True)
class ExternalGateDecision:
    """Outcome of a single external-order risk gate evaluation."""

    allowed: bool
    error: OrderError
    reason: str = ""


def check_drawdown(
    peak_equity: float,
    current_equity: float,
    limits: ExternalRiskLimits,
) -> bool:
    """True if equity has fallen ≥ max_drawdown_pct from peak."""
    if peak_equity <= 0:
        return False
    drop_pct = (peak_equity - current_equity) / peak_equity * 100.0
    return drop_pct >= limits.max_drawdown_pct


def check_position_limit(
    open_count: int,
    limits: ExternalRiskLimits,
) -> bool:
    """True if at or over the per-symbol position limit."""
    return open_count >= limits.max_positions_per_instrument


def evaluate_external_gate(
    *,
    instrument: str,
    open_positions_for_symbol: int,
    peak_equity: float,
    current_equity: float,
    limits: ExternalRiskLimits,
    kill_switch_active: bool,
) -> ExternalGateDecision:
    """Run the three universal safety nets in priority order.

    First failure wins (kill switch > DD halt > position limit).
    Returns an `OrderError.OK` decision when all pass.
    """
    if kill_switch_active:
        return ExternalGateDecision(
            allowed=False,
            error=OrderError.KILL_SWITCH_ACTIVE,
            reason="kill_switch_active",
        )
    if check_drawdown(peak_equity, current_equity, limits):
        return ExternalGateDecision(
            allowed=False,
            error=OrderError.DRAWDOWN_HALT,
            reason=(
                f"drawdown_halt: peak={peak_equity:.2f} "
                f"current={current_equity:.2f} "
                f"limit={limits.max_drawdown_pct:.1f}%"
            ),
        )
    if check_position_limit(open_positions_for_symbol, limits):
        return ExternalGateDecision(
            allowed=False,
            error=OrderError.POSITION_LIMIT,
            reason=(
                f"position_limit: {instrument} has {open_positions_for_symbol} open "
                f"(limit={limits.max_positions_per_instrument})"
            ),
        )
    return ExternalGateDecision(allowed=True, error=OrderError.OK, reason="ok")
