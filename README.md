# QuantStand External Connection

Integration layer for external signal-source agents (Hermes, Apollo, …)
to connect to QuantStand's paper-trading platform.

Each external strategy gets its own isolated equity pool, its own audit
log, and a stripped-down risk gate (kill switch + drawdown halt +
position limit per symbol). Paper fills are simulated against live
KuCoin mark prices. No real money is at risk in v1.

## Quickstart

```bash
# 1. Install dependencies (Python 3.11+)
pip install -r requirements.txt

# 2. Run the REST paper-trading service
python scripts/run_external_paper_service.py --verbose
# Listens on http://127.0.0.1:8765
# Swagger UI at http://127.0.0.1:8765/docs
```

For MCP-capable agents (e.g. Claude-based), launch the shim instead of
the service — see the integration guide below.

```bash
# 3. (Optional) Run the MCP shim — typically launched by your agent
#    automatically via its mcpServers config; this command is for
#    manual testing only.
python scripts/run_external_mcp_server.py
```

## Documentation

- **`docs/integrations/hermes_integration.md`** — full step-by-step
  integration guide. Covers MCP and REST paths, all 8 endpoints with
  request/response shapes, error codes, env-var configuration, and a
  typical agent session walkthrough.

The Swagger UI at `http://127.0.0.1:8765/docs` is the live source of
truth for the API schema once the service is running.

## Layout

```
External-Connection/
├── engine_external/         # paper-trader application + REST + MCP shim
│   ├── domain/              # pure-logic models (orders, sizing, RR, risk)
│   ├── application/         # per-strategy session state machine
│   └── infrastructure/      # FastAPI service, MCP shim, persistence, KuCoin ticker
├── engine_futures/          # stub — only `Direction` enum, for shared import path
├── scripts/                 # entry points
│   ├── run_external_paper_service.py
│   └── run_external_mcp_server.py
└── docs/integrations/       # integration guide
```

## What's NOT in this repo

This is the **integration layer only**. The full QuantStand platform
(internal strategies like AroonRsi, multi-coin runner, allocation
framework, research data) is a private repo. External agents do not
need access to any of that to integrate.

## Status

- v1 — paper trading only, BTC-only universe
- Deferred to v2: trigger orders, leverage management, live mode swap
  to KuCoin executor

## License

Internal — distribution by request only.
