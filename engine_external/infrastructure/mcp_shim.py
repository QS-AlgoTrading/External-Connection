"""MCP shim wrapping the engine_external REST service.

Thin client + FastMCP server. The shim runs as a separate process
(typically over stdio, launched by the agent) and forwards every tool
call to the REST service running on `base_url`.

Layered as:

    HermesPaperClient       low-level HTTP client (one method per
                            REST endpoint). Pure transport — takes
                            an `httpx.Client` so tests can swap in a
                            FastAPI `TestClient`.
    register_tools          binds each client method as an MCP tool
                            on a FastMCP server. Sync tools — FastMCP
                            wraps them for the async server runtime.
    main                    construct + run over stdio.

The MCP server is single-tenant: launched with one `strategy_id`,
every tool acts on that strategy. Multi-tenant agents that need to
operate several strategies should launch one MCP shim per strategy.

This matches the Hermes-spec tools 1:1:

    open_market_order            -> POST /orders/market
    get_open_positions           -> GET /positions
    get_open_position            -> GET /positions/{symbol}
    close_position               -> POST /positions/{symbol}/close
    modify_sl                    -> PATCH /positions/{symbol}/sl
    get_balance                  -> GET /balance
    get_closed_positions         -> GET /positions/closed
    create_session               -> POST /strategies/{id}

Trigger orders and leverage management are deferred to v2 (see
TASK_LIST.md Phase 4) and intentionally NOT exposed here — Hermes
v1 is market-orders-only.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

import httpx
from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)


DEFAULT_BASE_URL = "http://127.0.0.1:8765"
DEFAULT_STRATEGY_ID = "hermes"
DEFAULT_TIMEOUT_SEC = 10.0


# ─── Low-level HTTP client ──────────────────────────────────────────────────


@dataclass
class HermesPaperClient:
    """One method per REST endpoint. Pure transport, no MCP awareness."""

    http: httpx.Client
    strategy_id: str

    def _strategy_path(self, suffix: str = "") -> str:
        return f"/strategies/{self.strategy_id}{suffix}"

    # Session ----------------------------------------------------------------

    def create_session(self, starting_equity: float, slippage_bps: float) -> dict[str, Any]:
        r = self.http.post(
            self._strategy_path(),
            json={"starting_equity": starting_equity, "slippage_bps": slippage_bps},
        )
        return _unwrap(r)

    # Orders -----------------------------------------------------------------

    def open_market_order(
        self,
        symbol: str,
        side: str,
        risk_pct: float,
        sl_price: float,
        tp_price: Optional[float] = None,
        client_order_id: Optional[str] = None,
    ) -> dict[str, Any]:
        payload = {
            "symbol": symbol,
            "side": side,
            "risk_pct": risk_pct,
            "sl_price": sl_price,
        }
        if tp_price is not None:
            payload["tp_price"] = tp_price
        if client_order_id is not None:
            payload["client_order_id"] = client_order_id
        r = self.http.post(self._strategy_path("/orders/market"), json=payload)
        return _unwrap(r)

    # Positions -------------------------------------------------------------

    def get_open_positions(self) -> list[dict[str, Any]]:
        r = self.http.get(self._strategy_path("/positions"))
        return _unwrap(r)

    def get_open_position(self, symbol: str) -> Optional[dict[str, Any]]:
        r = self.http.get(self._strategy_path(f"/positions/{symbol}"))
        if r.status_code == 404:
            return None
        return _unwrap(r)

    def close_position(self, symbol: str, percentage: float = 100.0) -> dict[str, Any]:
        r = self.http.post(
            self._strategy_path(f"/positions/{symbol}/close"),
            json={"percentage": percentage},
        )
        return _unwrap(r)

    def modify_sl(self, symbol: str, sl_price: float) -> dict[str, Any]:
        r = self.http.patch(
            self._strategy_path(f"/positions/{symbol}/sl"),
            json={"sl_price": sl_price},
        )
        return _unwrap(r)

    # Reads ------------------------------------------------------------------

    def get_balance(self) -> dict[str, Any]:
        r = self.http.get(self._strategy_path("/balance"))
        return _unwrap(r)

    def get_closed_positions(self, symbol: Optional[str] = None) -> list[dict[str, Any]]:
        params = {"symbol": symbol} if symbol else None
        r = self.http.get(self._strategy_path("/positions/closed"), params=params)
        return _unwrap(r)

    def health(self) -> dict[str, Any]:
        r = self.http.get("/health")
        return _unwrap(r)


def _unwrap(r: httpx.Response) -> Any:
    """Raise for non-2xx, otherwise return JSON body."""
    if r.status_code >= 400:
        raise RuntimeError(f"REST {r.request.method} {r.request.url} -> {r.status_code}: {r.text}")
    return r.json()


# ─── MCP tool registration ──────────────────────────────────────────────────


def register_tools(mcp: FastMCP, client: HermesPaperClient) -> None:
    """Bind each client method as an MCP tool.

    Tool names + docstrings are what the agent (Hermes) sees. Keep
    docstrings actionable — they're the agent's only documentation.
    """

    @mcp.tool(name="open_market_order")
    def open_market_order(
        symbol: str,
        side: str,
        risk_pct: float,
        sl_price: float,
        tp_price: Optional[float] = None,
        client_order_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Open a market position on the paper trader.

        Args:
            symbol: Trading symbol (e.g., "BTCUSDT"). For v1 only
                BTCUSDT is supported; other symbols return
                error="symbol_not_supported".
            side: "LONG" or "SHORT". Anything else is rejected.
            risk_pct: Percent of strategy equity to risk if SL hits
                (1.0 = 1%). Must be in (0, 100].
            sl_price: Stop-loss price. Must be on the loss side of the
                current mark: LONG -> below mark; SHORT -> above mark.
            tp_price: Optional take-profit price.
            client_order_id: Optional idempotency key. If you resubmit
                the same id, the original outcome is returned without
                opening a second position.

        Returns:
            A dict with `error` ("ok" on success, otherwise a typed
            error code), `reason` (human-readable), and `position`
            (the resulting open position summary, or null if the order
            was rejected). On `error="ok"`, `position` carries
            `entry_price`, `volume`, `sl_price`, `tp_price`,
            `current_price_rr`, and the open/closed amount percentages.
        """
        return client.open_market_order(
            symbol=symbol,
            side=side,
            risk_pct=risk_pct,
            sl_price=sl_price,
            tp_price=tp_price,
            client_order_id=client_order_id,
        )

    @mcp.tool(name="get_open_positions")
    def get_open_positions() -> list[dict[str, Any]]:
        """Return the list of currently-open positions for this strategy.

        Each entry carries `symbol`, `side`, `entry_price`, `sl_price`,
        `tp_price`, `volume`, `initial_volume`, `pnl_usdt` (unrealised),
        `current_price_rr` (signed R-multiple at current mark),
        and `open_amount_percentage` / `closed_amount_percentage`.

        Calling this endpoint refreshes the mark price for every open
        symbol — any pending SL/TP triggers fire immediately and the
        affected positions move into closed history.
        """
        return client.get_open_positions()

    @mcp.tool(name="get_open_position")
    def get_open_position(symbol: str) -> Optional[dict[str, Any]]:
        """Return one open position by symbol, or null if none.

        Same fields as `get_open_positions`. The mark price is
        refreshed before reading — SL/TP triggers fire if crossed.
        """
        return client.get_open_position(symbol)

    @mcp.tool(name="close_position")
    def close_position(symbol: str, percentage: float = 100.0) -> dict[str, Any]:
        """Close a position fully or partially.

        Args:
            symbol: The open position's symbol.
            percentage: Fraction of the REMAINING volume to close, in
                (0, 100]. Hermes semantics: "50% twice" leaves 25% open
                — the percentage applies to whatever is currently open,
                not the original at-entry volume.

        Returns:
            `error` ("ok" on success), `reason`, `closed_position` (with
            the realised P&L + R-multiple), and on partial closes
            `position` carrying the still-open remainder.
        """
        return client.close_position(symbol, percentage)

    @mcp.tool(name="modify_sl")
    def modify_sl(symbol: str, sl_price: float) -> dict[str, Any]:
        """Move the stop-loss price for an open position.

        The new SL must remain on the loss side of the entry price
        (LONG: below entry; SHORT: above entry). Wrong-side requests
        are rejected with error="invalid_sl".
        """
        return client.modify_sl(symbol, sl_price)

    @mcp.tool(name="get_balance")
    def get_balance() -> dict[str, Any]:
        """Return the strategy's balance and equity stats.

        Fields: `starting_equity`, `realised_equity` (starting + closed
        P&L), `unrealised_pnl` (sum of open positions' floating P&L at
        current marks), `total_equity` (realised + unrealised),
        `peak_equity` (high-water mark for the DD halt),
        `open_positions_count`, `closed_positions_count`.

        Mark prices are refreshed before computing — SL/TP triggers
        fire if crossed.
        """
        return client.get_balance()

    @mcp.tool(name="get_closed_positions")
    def get_closed_positions(symbol: Optional[str] = None) -> list[dict[str, Any]]:
        """Return closed positions for this strategy.

        Args:
            symbol: Optional — when set, only closed positions on this
                symbol are returned.

        Each entry has `entry_price`, `exit_price`, `pnl_usdt`,
        `pnl_rr` (realised R-multiple), `entry_time`, `exit_time`.
        Partial closes show up here too — every closed slice is one
        entry.
        """
        return client.get_closed_positions(symbol)

    @mcp.tool(name="create_session")
    def create_session(
        starting_equity: float = 10_000.0, slippage_bps: float = 10.0
    ) -> dict[str, Any]:
        """Create the paper-trading session for this strategy (idempotent).

        If a session for this strategy_id already exists, the existing
        config is returned and the arguments here are IGNORED — equity
        is not reset, slippage is not changed. Call this once at the
        beginning of a deployment to seed the session.
        """
        return client.create_session(starting_equity, slippage_bps)


# ─── Entry point ────────────────────────────────────────────────────────────


def build_server(
    base_url: str = DEFAULT_BASE_URL,
    strategy_id: str = DEFAULT_STRATEGY_ID,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
) -> FastMCP:
    """Construct an MCP server pointed at a REST base URL.

    The http client is owned by the server; cleanup happens when the
    server's process exits.
    """
    http = httpx.Client(base_url=base_url, timeout=timeout_sec)
    client = HermesPaperClient(http=http, strategy_id=strategy_id)

    mcp = FastMCP(
        name=f"quantstand-external-{strategy_id}",
        instructions=(
            f"Paper-trading bridge for strategy '{strategy_id}'. All tools "
            f"act on this strategy's isolated equity pool. Use "
            f"open_market_order to enter positions; close_position to exit; "
            f"modify_sl to adjust stops; get_balance / get_open_positions / "
            f"get_closed_positions to inspect state. SL/TP auto-trigger when "
            f"the mark price crosses the level — the server checks on every "
            f"read or mutation."
        ),
    )
    register_tools(mcp, client)
    return mcp


def main() -> int:
    """Run the MCP server over stdio. Configured via env vars."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    base_url = os.environ.get("QUANTSTAND_EXTERNAL_BASE_URL", DEFAULT_BASE_URL)
    strategy_id = os.environ.get("QUANTSTAND_EXTERNAL_STRATEGY_ID", DEFAULT_STRATEGY_ID)
    timeout = float(os.environ.get("QUANTSTAND_EXTERNAL_TIMEOUT_SEC", DEFAULT_TIMEOUT_SEC))

    mcp = build_server(base_url=base_url, strategy_id=strategy_id, timeout_sec=timeout)
    logger.info(
        "Starting MCP shim: strategy_id=%s, REST base_url=%s, timeout=%.1fs",
        strategy_id,
        base_url,
        timeout,
    )
    mcp.run()  # blocks on stdio
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
