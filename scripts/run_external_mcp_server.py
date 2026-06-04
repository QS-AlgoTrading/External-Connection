#!/usr/bin/env python3
"""Run the engine_external MCP shim over stdio.

Launched by the agent (Hermes or another MCP client) — typically as
a subprocess via its MCP server configuration. The REST service must
be running independently (see scripts/run_external_paper_service.py).

Configuration via environment variables:

    QUANTSTAND_EXTERNAL_BASE_URL     default http://127.0.0.1:8765
    QUANTSTAND_EXTERNAL_STRATEGY_ID  default "hermes"
    QUANTSTAND_EXTERNAL_TIMEOUT_SEC  default 10.0

Example agent-side configuration (Claude Desktop style):

    {
      "mcpServers": {
        "quantstand-hermes": {
          "command": "/path/to/python",
          "args": [
            "/path/to/QuantStand-Ant/scripts/run_external_mcp_server.py"
          ],
          "env": {
            "QUANTSTAND_EXTERNAL_STRATEGY_ID": "hermes",
            "QUANTSTAND_EXTERNAL_BASE_URL": "http://127.0.0.1:8765"
          }
        }
      }
    }

For local manual testing of the shim (without a full agent), see
tests/integration/test_external_mcp_shim.py — it constructs the shim
and drives the underlying client directly without stdio.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from engine_external.infrastructure.mcp_shim import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
