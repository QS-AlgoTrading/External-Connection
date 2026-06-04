"""JSON state persistence for external-strategy paper sessions.

Atomic write via tempfile + rename. Schema-versioned so future
migrations don't silently corrupt old state files.

Path layout:
    data/external/<strategy_id>/state.json
    data/external/<strategy_id>/audit.jsonl   (see audit_log.py)

The state file is the truth at process restart. Reloading reconstructs
an `ExternalSessionState` byte-identical to what was last saved.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from engine_external.application.paper_session import (
    ExternalSessionState,
    _OpenPositionRecord,
)
from engine_external.domain.order_models import ExternalClosedPosition
from engine_futures.domain.strategy.strategy_models import Direction


STATE_SCHEMA_VERSION = 1


def session_state_path(root: Path, strategy_id: str) -> Path:
    return root / strategy_id / "state.json"


# ─── Serialise ──────────────────────────────────────────────────────────────


def _serialise_position(pos: _OpenPositionRecord) -> dict:
    return {
        "symbol": pos.symbol,
        "side": pos.side.value,
        "entry_price": pos.entry_price,
        "entry_time": pos.entry_time.isoformat(),
        "initial_volume": pos.initial_volume,
        "current_volume": pos.current_volume,
        "sl_price": pos.sl_price,
        "tp_price": pos.tp_price,
    }


def _serialise_closed(c: ExternalClosedPosition) -> dict:
    return {
        "symbol": c.symbol,
        "side": c.side.value,
        "entry_price": c.entry_price,
        "exit_price": c.exit_price,
        "pnl_usdt": c.pnl_usdt,
        "pnl_rr": c.pnl_rr,
        "entry_time": c.entry_time.isoformat(),
        "exit_time": c.exit_time.isoformat(),
    }


def serialise_state(state: ExternalSessionState) -> dict:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "strategy_id": state.strategy_id,
        "starting_equity": state.starting_equity,
        "slippage_bps": state.slippage_bps,
        "created_at": state.created_at.isoformat(),
        "last_updated": state.last_updated.isoformat(),
        "equity_base": state.equity_base,
        "last_prices": dict(state.last_prices),
        "open_positions": {
            sym: _serialise_position(p) for sym, p in state.open_positions.items()
        },
        "closed_positions": [_serialise_closed(c) for c in state.closed_positions],
        "fills_count": state.fills_count,
        "orders_seen": dict(state.orders_seen),
        "peak_equity": state.peak_equity,
        "day_start_equity": state.day_start_equity,
        "day_start_ts": (
            state.day_start_ts.isoformat() if state.day_start_ts else None
        ),
    }


# ─── Deserialise ────────────────────────────────────────────────────────────


def _parse_ts(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def _parse_position(d: dict) -> _OpenPositionRecord:
    return _OpenPositionRecord(
        symbol=d["symbol"],
        side=Direction(d["side"]),
        entry_price=float(d["entry_price"]),
        entry_time=datetime.fromisoformat(d["entry_time"]),
        initial_volume=float(d["initial_volume"]),
        current_volume=float(d["current_volume"]),
        sl_price=float(d["sl_price"]),
        tp_price=float(d["tp_price"]) if d["tp_price"] is not None else None,
    )


def _parse_closed(d: dict) -> ExternalClosedPosition:
    return ExternalClosedPosition(
        symbol=d["symbol"],
        side=Direction(d["side"]),
        entry_price=float(d["entry_price"]),
        exit_price=float(d["exit_price"]),
        pnl_usdt=float(d["pnl_usdt"]),
        pnl_rr=float(d["pnl_rr"]),
        entry_time=datetime.fromisoformat(d["entry_time"]),
        exit_time=datetime.fromisoformat(d["exit_time"]),
    )


def deserialise_state(data: dict) -> ExternalSessionState:
    version = data.get("schema_version", 1)
    if version != STATE_SCHEMA_VERSION:
        raise ValueError(
            f"State schema version mismatch: file is v{version}, "
            f"code expects v{STATE_SCHEMA_VERSION}"
        )
    return ExternalSessionState(
        strategy_id=data["strategy_id"],
        starting_equity=float(data["starting_equity"]),
        slippage_bps=float(data["slippage_bps"]),
        created_at=datetime.fromisoformat(data["created_at"]),
        last_updated=datetime.fromisoformat(data["last_updated"]),
        equity_base=float(data["equity_base"]),
        last_prices={k: float(v) for k, v in data["last_prices"].items()},
        open_positions={
            sym: _parse_position(p) for sym, p in data["open_positions"].items()
        },
        closed_positions=[_parse_closed(c) for c in data["closed_positions"]],
        fills_count=int(data["fills_count"]),
        orders_seen=dict(data.get("orders_seen", {})),
        peak_equity=float(data["peak_equity"]),
        day_start_equity=(
            float(data["day_start_equity"])
            if data["day_start_equity"] is not None
            else None
        ),
        day_start_ts=_parse_ts(data["day_start_ts"]),
    )


# ─── Atomic file IO ─────────────────────────────────────────────────────────


def save_state(state: ExternalSessionState, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(serialise_state(state), f, indent=2)
    tmp.replace(path)  # atomic on POSIX


def load_state(path: Path) -> ExternalSessionState | None:
    """Return the persisted state, or None if no file exists yet."""
    if not path.exists():
        return None
    with open(path) as f:
        return deserialise_state(json.load(f))
