# Hermes Integration Guide

**Status**: v1 — paper trading only, BTC-only universe
**Target**: Hermes (or any external signal-source agent) integrating with QuantStand's external paper trader
**Last updated**: 2026-06-03

---

## What this is

The QuantStand External Paper Trader is a per-strategy paper-trading service. Hermes gets its own isolated $10,000 equity pool, its own audit log, and the platform tracks every position and decision. No real money is at risk in v1 — every fill is simulated against KuCoin's live mark price.

Two integration paths, your choice:

- **MCP (recommended)** — your agent launches the MCP shim subprocess on demand; the shim handles all communication. Best for Claude / MCP-aware agents.
- **REST directly** — hit the HTTP endpoints with any client (curl, requests, fetch). Works regardless of agent framework.

Same API behind both paths. Pick MCP if your agent natively supports it; pick REST otherwise.

---

## Architecture

```
┌─────────────────────────────────────┐         ┌──────────────────────────────────┐
│  QUANTSTAND HOST                    │         │  YOUR MACHINE                    │
│                                     │         │                                  │
│  ┌───────────────────────────────┐  │  HTTP   │  ┌────────────────────────────┐  │
│  │  REST service                 │◀─┼─────────┼──│  Hermes (your agent)       │  │
│  │  run_external_paper_service.py│  │         │  │       │                    │  │
│  │  Listens on :8765             │  │         │  │       │ stdio (MCP)        │  │
│  │  Persistent state on disk     │  │         │  │       ▼                    │  │
│  │  Always running               │  │         │  │  MCP shim subprocess       │  │
│  └───────────────────────────────┘  │         │  │  run_external_mcp_server.py│  │
│                                     │         │  │  Launched by Hermes        │  │
│  Run by QuantStand operator        │         │  └────────────────────────────┘  │
└─────────────────────────────────────┘         └──────────────────────────────────┘
```

- The **REST service** is run by the QuantStand operator. It owns the state and the persistence.
- The **MCP shim** is run by your agent (subprocess, launched on demand). It's a stateless HTTP client.

---

## Setup — MCP path

### 1. Install Python dependencies (your machine)

The shim needs Python 3.11+ with two libraries:

```bash
pip install mcp==1.27.2 httpx==0.28.1
```

### 2. Get the shim code

Either:

- **Clone the repo**:
  ```bash
  git clone https://github.com/QS-AlgoTrading/QuantStand-Ant.git
  ```
- **Or** copy just these files into a directory of your choice:
  - `engine_external/__init__.py`
  - `engine_external/infrastructure/__init__.py`
  - `engine_external/infrastructure/mcp_shim.py`
  - `scripts/run_external_mcp_server.py`

### 3. Configure your agent

Sample Claude Desktop `mcpServers` block (other MCP-aware agents use similar config):

```json
{
  "mcpServers": {
    "quantstand-hermes": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": [
        "/absolute/path/to/External-Connection/scripts/run_external_mcp_server.py"
      ],
      "env": {
        "QUANTSTAND_EXTERNAL_BASE_URL": "http://<quantstand-host>:8765",
        "QUANTSTAND_EXTERNAL_STRATEGY_ID": "hermes",
        "QUANTSTAND_EXTERNAL_TIMEOUT_SEC": "10.0",
        "QUANTSTAND_EXTERNAL_API_KEY": "<API key the operator gives you>"
      }
    }
  }
}
```

Substitute:
- `<quantstand-host>` — the hostname/IP where the REST service runs (see "How to reach the service" below).
- `<API key the operator gives you>` — a shared secret the operator generates and sends you out-of-band (e.g., over a chat that isn't this repo). The same string also lives in `QUANTSTAND_EXTERNAL_API_KEY` on the server side. If the operator says auth is disabled (local dev only), omit this env var.

### 4. Verify

First call from your agent should be `create_session`. Returns the strategy info dict. After that you can call any of the other 7 tools.

---

## How to reach the service

The QuantStand operator runs the REST service on their machine. There are three ways to expose it to your agent depending on the network setup:

1. **Same machine** — both you and the operator running on the same box. `QUANTSTAND_EXTERNAL_BASE_URL=http://127.0.0.1:8765`.
2. **Same local network** — operator binds to `0.0.0.0` and shares a LAN IP. `QUANTSTAND_EXTERNAL_BASE_URL=http://192.168.x.x:8765` (or whatever the operator sends).
3. **Different networks (recommended for remote integrations)** — operator runs Tailscale. They send you an invite to their tailnet plus a stable hostname like `mac-mini.tail-XXXX.ts.net`. You install Tailscale on your side, accept the invite, then `QUANTSTAND_EXTERNAL_BASE_URL=http://mac-mini.tail-XXXX.ts.net:8765`.

Tailscale install:

```bash
# macOS
brew install --cask tailscale
# Linux
curl -fsSL https://tailscale.com/install.sh | sh
```

Then `sudo tailscale up` and accept the operator's invite link. The hostname they send you works from anywhere both machines are online.

---

## Setup — REST path (no MCP)

The REST service is plain HTTP. Any client works.

When the operator has API-key auth ON (the normal case for any non-localhost deployment), every request needs an `X-API-Key: <key>` header. `/health` and `/docs` are exempt so you can do liveness checks and browse Swagger without the key.

```bash
# Sanity — no key needed
curl http://<quantstand-host>:8765/health
# {"status":"ok"}

# Create a session — key required if auth is on
curl -X POST http://<quantstand-host>:8765/strategies/hermes \
  -H 'X-API-Key: <key>' \
  -H 'content-type: application/json' \
  -d '{"starting_equity": 10000, "slippage_bps": 10}'
```

You'll find an interactive Swagger UI with try-it-out forms at:

```
http://<quantstand-host>:8765/docs
```

Same schemas as the reference below.

---

## API reference

All endpoints below assume your strategy id is `hermes`. Substitute as needed.

### Session

#### `POST /strategies/{strategy_id}`

Create or get the strategy session. **Idempotent** — calling twice returns the existing session unchanged; the second call's `starting_equity` / `slippage_bps` are ignored. Call this once before any other endpoint.

**Body**:
| Field | Type | Default | Notes |
|---|---|---|---|
| `starting_equity` | float | 10000.0 | Initial equity in USDT |
| `slippage_bps` | float | 10.0 | Per-side basis points applied adversely on fills |

**Response 200**:
```json
{
  "strategy_id": "hermes",
  "starting_equity": 10000.0,
  "slippage_bps": 10.0,
  "created_at": "2026-06-03T...",
  "last_updated": "2026-06-03T..."
}
```

---

### Orders

#### `POST /strategies/{strategy_id}/orders/market`

Open a market position. Sized from `risk_pct` + the distance between current mark and `sl_price`.

**Body**:
| Field | Type | Required | Notes |
|---|---|---|---|
| `symbol` | string | yes | `"BTCUSDT"` only in v1 |
| `side` | string | yes | `"LONG"` or `"SHORT"` |
| `risk_pct` | float | yes | Percent of equity to risk if SL hits (e.g. `1.0` = 1%). Range (0, 100] |
| `sl_price` | float | yes | Absolute stop-loss price. LONG: must be below mark. SHORT: must be above. |
| `tp_price` | float | no | Optional take-profit price |
| `leverage` | int | no | Reserved for v2 — currently ignored |
| `client_order_id` | string | no | Idempotency key. Resubmitting the same id returns the original outcome. |

**Response 200** (success or rejection — check `error`):
```json
{
  "error": "ok",
  "reason": "opened",
  "position": {
    "symbol": "BTCUSDT",
    "side": "LONG",
    "entry_price": 60060.0,
    "sl_price": 59000.0,
    "tp_price": 62000.0,
    "volume": 0.0943,
    "initial_volume": 0.0943,
    "pnl_usdt": -5.66,
    "current_price_rr": -0.057,
    "open_amount_percentage": 100.0,
    "closed_amount_percentage": 0.0,
    "entry_time": "2026-06-03T..."
  },
  "closed_position": null
}
```

On rejection: `error` is one of the codes in the [Error reference](#error-reference) below, `position` is `null`.

---

### Read positions

#### `GET /strategies/{strategy_id}/positions`

List currently open positions. Refreshes mark prices first — any SL/TP that should have triggered fires here.

**Response 200**: JSON array of position objects (same shape as the `position` field above).

#### `GET /strategies/{strategy_id}/positions/{symbol}`

One open position by symbol. Same mark refresh / auto-trigger logic as the list endpoint.

**Response 200**: position object.
**Response 404**: no open position on this symbol.

#### `GET /strategies/{strategy_id}/positions/closed`

Closed-position history. Each partial close shows up as a separate entry.

**Query params**:
| Field | Type | Default | Notes |
|---|---|---|---|
| `symbol` | string | (all) | Filter to one symbol |

**Response 200**: JSON array of closed-position objects:
```json
[
  {
    "symbol": "BTCUSDT",
    "side": "LONG",
    "entry_price": 60060.0,
    "exit_price": 62000.0,
    "pnl_usdt": 183.02,
    "pnl_rr": 1.83,
    "entry_time": "2026-06-03T...",
    "exit_time": "2026-06-03T..."
  }
]
```

---

### Close / modify

#### `POST /strategies/{strategy_id}/positions/{symbol}/close`

Full or partial close. Refreshes mark, fills at `mark - adverse_slippage`, realised P&L credited to equity.

**Body**:
| Field | Type | Default | Notes |
|---|---|---|---|
| `percentage` | float | 100.0 | Percent of **remaining** volume to close. `50` twice leaves 25% open. |

**Response 200**:
```json
{
  "error": "ok",
  "reason": "closed",
  "position": null,                  // null on full close; populated remainder on partial
  "closed_position": { ... }         // the just-closed slice
}
```

#### `PATCH /strategies/{strategy_id}/positions/{symbol}/sl`

Move the stop-loss price. New SL must remain on the loss side of the entry (LONG: below entry; SHORT: above entry).

**Body**:
| Field | Type | Required | Notes |
|---|---|---|---|
| `sl_price` | float | yes | New SL price |

**Response 200**: same shape as orders endpoint; `position` carries the updated SL.

---

### Balance

#### `GET /strategies/{strategy_id}/balance`

Snapshot of the strategy's equity. Refreshes marks before computing — auto-triggers fire here too.

**Response 200**:
```json
{
  "strategy_id": "hermes",
  "starting_equity": 10000.0,
  "realised_equity": 10183.02,
  "unrealised_pnl": 0.0,
  "total_equity": 10183.02,
  "peak_equity": 10250.45,
  "open_positions_count": 0,
  "closed_positions_count": 1
}
```

`total_equity = realised_equity + unrealised_pnl`. `peak_equity` is the high-water mark used by the drawdown gate.

---

## MCP tool names (if you're using the shim)

The MCP shim exposes one tool per REST endpoint. The mapping is 1:1:

| MCP tool | REST endpoint |
|---|---|
| `create_session` | `POST /strategies/{id}` |
| `open_market_order` | `POST /strategies/{id}/orders/market` |
| `get_open_positions` | `GET /strategies/{id}/positions` |
| `get_open_position` | `GET /strategies/{id}/positions/{symbol}` |
| `close_position` | `POST /strategies/{id}/positions/{symbol}/close` |
| `modify_sl` | `PATCH /strategies/{id}/positions/{symbol}/sl` |
| `get_balance` | `GET /strategies/{id}/balance` |
| `get_closed_positions` | `GET /strategies/{id}/positions/closed` |

Tool argument names match the REST body field names exactly.

---

## Error reference

The `error` field on order-style responses is one of:

| Code | Meaning |
|---|---|
| `ok` | Success |
| `symbol_not_supported` | Symbol outside v1 universe (only BTCUSDT currently) |
| `invalid_side` | Side wasn't LONG or SHORT |
| `invalid_sl` | SL on wrong side of entry / non-positive / wrong side of new entry on modify |
| `invalid_risk` | risk_pct outside (0, 100] |
| `position_limit` | Already have an open position on this symbol (limit = 1) |
| `kill_switch_active` | Operator armed the kill switch — all new opens halted |
| `drawdown_halt` | Equity has dropped 30%+ from peak — all new opens halted until reset |
| `no_mark_price` | Couldn't fetch a mark price from KuCoin |
| `position_not_found` | Tried to close/modify a position that doesn't exist |
| `invalid_close_percentage` | Close percentage outside (0, 100] |
| `broker_rejected` | Internal broker rejected the order (rare) |

**HTTP status codes**:
- `200`: request reached the service. Check `error` field for business outcome.
- `400`: malformed request body (bad JSON, missing field).
- `401`: API-key auth failure. Body is `{"detail": {"error": "missing_api_key" | "invalid_api_key", "reason": "..."}}`.
- `404`: unknown strategy_id or symbol (for read endpoints).

---

## Configuration parameters (env vars for the shim)

| Variable | Default | Notes |
|---|---|---|
| `QUANTSTAND_EXTERNAL_BASE_URL` | `http://127.0.0.1:8765` | Where the REST service is reachable |
| `QUANTSTAND_EXTERNAL_STRATEGY_ID` | `hermes` | Which strategy this shim instance acts as |
| `QUANTSTAND_EXTERNAL_TIMEOUT_SEC` | `10.0` | HTTP timeout for each call |
| `QUANTSTAND_EXTERNAL_API_KEY` | _unset_ | Auth header value. Operator gives you the string out-of-band. Leave unset only if the operator says auth is off. |

---

## Typical Hermes session

1. Once at startup: `create_session(starting_equity=10000, slippage_bps=10)`. Idempotent — safe to call every restart.
2. Periodically (each hour / each chart-image evaluation):
   - `get_balance()` → know your current equity.
   - `get_open_positions()` → see what's still open. (Side effect: any SL/TP that crossed since the last call will have auto-closed.)
3. When a signal fires:
   - `open_market_order(symbol="BTCUSDT", side="LONG", risk_pct=1.0, sl_price=59000, tp_price=63000)`.
   - If `error != "ok"`, inspect `reason` and back off (don't retry on `position_limit` — you already have an open position).
4. If you want to update a stop:
   - `modify_sl(symbol="BTCUSDT", sl_price=60000)`. Wrong-side requests are rejected (LONG can't have SL above entry).
5. To exit manually:
   - `close_position(symbol="BTCUSDT", percentage=100)` for full close.
   - `close_position(symbol="BTCUSDT", percentage=50)` for partial. Hermes-spec semantics: 50% of REMAINING volume.
6. For post-hoc analysis:
   - `get_closed_positions()` → history of every closed slice, with realised P&L and R-multiple.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `ERR_CONNECTION_REFUSED` / 503 | REST service isn't running on the operator side. Ping them. |
| `401 missing_api_key` on every call | You don't have `QUANTSTAND_EXTERNAL_API_KEY` set on the shim's environment. |
| `401 invalid_api_key` on every call | Your key doesn't match what the server expects. Get the current key from the operator. |
| Every order returns `symbol_not_supported` | Wrong symbol format. Use `BTCUSDT`, not `BTC/USDT` or `BTC-USDT`. |
| Every order returns `kill_switch_active` | Operator armed the kill switch. Out of your hands. |
| Every order returns `drawdown_halt` | Strategy equity is 30%+ below peak. Out of your hands until operator resets. |
| `position_limit` on an open you expected to succeed | You already have an open position on that symbol — only one at a time per symbol per strategy. Close it first. |
| `no_mark_price` | Transient — KuCoin's ticker endpoint flaked. Retry. |
| Modify SL rejected as `invalid_sl` | New SL is on the wrong side of entry. LONG needs SL below entry; SHORT needs SL above entry. |

---

## What's NOT in v1

The Hermes order spec includes a few features deliberately deferred:

- **Trigger orders** (pending until trigger_price hit). v1 is market-only. Defer to v2.
- **Leverage management** (liq < SL constraint, multiples of 5, min margin). v1 sizes in base units with no leverage modelling. Defer to v2.
- **Open universe** beyond BTCUSDT. Adding more coins is a config change on our side; ping us when Hermes is ready to watch more.
- **Live mode** (real KuCoin orders instead of paper). After paper validates Hermes's signal quality.

---

## Contact / questions

Ping the QuantStand team. The REST service exposes auto-generated docs at `http://<host>:8765/docs` (Swagger UI) — useful for trying calls without writing code.
