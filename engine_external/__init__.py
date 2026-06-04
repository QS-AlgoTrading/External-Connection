"""External signal-source engine — paper / live execution for external strategies.

Hosts strategies whose signals come from outside the QuantStand codebase:
chart-image AI agents (Hermes), human traders via UI, third-party signal
providers, etc. Each external strategy gets its own equity pool, audit
log, and risk gate — see `docs/research/` if/when those are documented.

Layering follows the project-wide rule:
    domain/        pure logic, zero I/O
    application/   orchestration, no exchange APIs
    infrastructure/ all I/O lives here (REST service, persistence, MCP shim)

Per-strategy partitioning: every public entry point takes a `strategy_id`
(typically a slug like "hermes" or "apollo"). State lives under
`data/external/<strategy_id>/`. Adding a new external strategy is a
config change, not a code change.
"""
