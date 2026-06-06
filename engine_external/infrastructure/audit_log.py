"""Append-only JSONL audit log for external-strategy paper sessions.

One file per strategy at `data/external/<strategy_id>/audit.jsonl`.
Every order submission, every fill, every gate decision, every
mark-price-induced SL/TP trigger writes a line. Read with `jq` or
the same Python one-liners we use for the multi-coin runner's log.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from engine_external.domain.order_models import (
    ExternalClosedPosition,
    ExternalOrderRequest,
    OrderError,
)
from engine_external.application.paper_session import OperationResult


def audit_log_path(root: Path, strategy_id: str) -> Path:
    return root / strategy_id / "audit.jsonl"


def _ts() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _append(path: Path, entry: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")


# ─── Entry-builders for each event type ─────────────────────────────────────


def log_order(
    path: Path,
    *,
    strategy_id: str,
    req: ExternalOrderRequest,
    mark_price: float | None,
    result: OperationResult,
) -> None:
    """Audit a single market-order submission and its outcome."""
    _append(
        path,
        {
            "event": "order_market",
            "ts": _ts(),
            "strategy_id": strategy_id,
            "request": {
                "symbol": req.symbol,
                "side": req.side.value,
                "risk_pct": req.risk_pct,
                "sl_price": req.sl_price,
                "tp_price": req.tp_price,
                "client_order_id": req.client_order_id,
            },
            "mark_price": mark_price,
            "result": {
                "error": result.error.value,
                "reason": result.reason,
                "position": _serialise_position(result.position),
            },
        },
    )


def log_close(
    path: Path,
    *,
    strategy_id: str,
    symbol: str,
    percentage: float,
    result: OperationResult,
) -> None:
    _append(
        path,
        {
            "event": "close",
            "ts": _ts(),
            "strategy_id": strategy_id,
            "symbol": symbol,
            "percentage": percentage,
            "result": {
                "error": result.error.value,
                "reason": result.reason,
                "closed_position": _serialise_closed(result.closed_position),
                "position_after": _serialise_position(result.position),
            },
        },
    )


def log_modify_sl(
    path: Path,
    *,
    strategy_id: str,
    symbol: str,
    old_sl: float,
    new_sl: float,
    result: OperationResult,
) -> None:
    _append(
        path,
        {
            "event": "modify_sl",
            "ts": _ts(),
            "strategy_id": strategy_id,
            "symbol": symbol,
            "old_sl": old_sl,
            "new_sl": new_sl,
            "result": {
                "error": result.error.value,
                "reason": result.reason,
                "position": _serialise_position(result.position),
            },
        },
    )


def log_auto_close(
    path: Path,
    *,
    strategy_id: str,
    closed: ExternalClosedPosition,
    trigger: str,
) -> None:
    """SL/TP auto-trigger — fires when update_mark() crosses the level."""
    _append(
        path,
        {
            "event": "auto_close",
            "ts": _ts(),
            "strategy_id": strategy_id,
            "trigger": trigger,
            "closed_position": _serialise_closed(closed),
        },
    )


# ─── Serialisation helpers (mirror state_persistence.py shapes) ─────────────


def _serialise_position(pos) -> dict | None:
    if pos is None:
        return None
    return {
        "symbol": pos.symbol,
        "side": pos.side.value,
        "entry_price": pos.entry_price,
        "sl_price": pos.sl_price,
        "tp_price": pos.tp_price,
        "volume": pos.volume,
        "initial_volume": pos.initial_volume,
        "pnl_usdt": pos.pnl_usdt,
        "current_price_rr": pos.current_price_rr,
        "open_amount_percentage": pos.open_amount_percentage,
        "closed_amount_percentage": pos.closed_amount_percentage,
        "entry_time": pos.entry_time.isoformat(),
    }


def _serialise_closed(closed) -> dict | None:
    if closed is None:
        return None
    return {
        "symbol": closed.symbol,
        "side": closed.side.value,
        "entry_price": closed.entry_price,
        "exit_price": closed.exit_price,
        "pnl_usdt": closed.pnl_usdt,
        "pnl_rr": closed.pnl_rr,
        "entry_time": closed.entry_time.isoformat(),
        "exit_time": closed.exit_time.isoformat(),
    }
