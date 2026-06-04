#!/usr/bin/env python3
"""Run the engine_external REST paper-trading service.

Local dev: simple uvicorn run. Production: see Phase 4 systemd/launchd
service definitions (deferred).

Usage:
    python scripts/run_external_paper_service.py             # bind 127.0.0.1:8765
    python scripts/run_external_paper_service.py --port 9000
    python scripts/run_external_paper_service.py --host 0.0.0.0 --port 8765

Default endpoints:
    POST   http://127.0.0.1:8765/strategies/hermes
    POST   http://127.0.0.1:8765/strategies/hermes/orders/market
    GET    http://127.0.0.1:8765/strategies/hermes/positions
    GET    http://127.0.0.1:8765/strategies/hermes/balance
    ...
    GET    http://127.0.0.1:8765/health
    GET    http://127.0.0.1:8765/docs    (Swagger UI)

Smoke test (after starting the service):
    curl -X POST http://127.0.0.1:8765/strategies/hermes \\
        -H 'content-type: application/json' \\
        -d '{"starting_equity": 10000, "slippage_bps": 10}'

    curl http://127.0.0.1:8765/strategies/hermes/balance
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import uvicorn  # noqa: E402

from engine_external.infrastructure.rest_service import (  # noqa: E402
    DEFAULT_DATA_ROOT,
    DEFAULT_KILL_SWITCH_PATH,
    DEFAULT_SUPPORTED_SYMBOLS,
    create_app,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the engine_external REST paper-trading service."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--data-root",
        type=Path,
        default=_PROJECT_ROOT / DEFAULT_DATA_ROOT,
        help="Directory where per-strategy state files live.",
    )
    parser.add_argument(
        "--kill-switch-path",
        type=Path,
        default=_PROJECT_ROOT / DEFAULT_KILL_SWITCH_PATH,
        help="File whose existence halts all new opens engine-wide.",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    app = create_app(
        data_root=args.data_root,
        supported_symbols=DEFAULT_SUPPORTED_SYMBOLS,
        kill_switch_path=args.kill_switch_path,
    )

    print(f"Starting engine_external REST service on http://{args.host}:{args.port}")
    print(f"  data root:   {args.data_root}")
    print(f"  kill switch: {args.kill_switch_path}"
          f"  ({'ARMED' if args.kill_switch_path.exists() else 'idle'})")
    print(f"  symbols:     {DEFAULT_SUPPORTED_SYMBOLS}")
    print(f"  docs:        http://{args.host}:{args.port}/docs")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info" if args.verbose else "warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
