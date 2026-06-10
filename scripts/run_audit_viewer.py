#!/usr/bin/env python3
"""Standalone web viewer for engine_external audit logs.

Zero-coupling read-only dashboard. Reads data/external/<id>/audit.jsonl
directly from disk — no dependency on the REST service.

Usage:
    python scripts/run_audit_viewer.py                          # bind 0.0.0.0:8766
    python scripts/run_audit_viewer.py --port 9001 --data-root ./data/external
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

DEFAULT_DATA_ROOT = _PROJECT_ROOT / "data" / "external"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8766

logger = logging.getLogger(__name__)


def _load_audit_entries(
    data_root: Path,
    strategy_id: str,
    *,
    limit: int = 100,
    event_filter: Optional[str] = None,
) -> list[dict]:
    path = data_root / strategy_id / "audit.jsonl"
    if not path.exists():
        return []
    entries: list[dict] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event_filter and entry.get("event") != event_filter:
                continue
            entries.append(entry)
    entries.reverse()
    return entries[:limit]


def _list_strategies(data_root: Path) -> list[str]:
    if not data_root.exists():
        return []
    return sorted(
        d.name
        for d in data_root.iterdir()
        if d.is_dir() and (d / "audit.jsonl").exists()
    )


# ── HTML template ───────────────────────────────────────────────────────

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>QuantStand Audit Log</title>
<style>
:root {
  --bg: #0d1117;
  --surface: #161b22;
  --border: #30363d;
  --text: #c9d1d9;
  --muted: #8b949e;
  --green: #3fb950;
  --red: #f85149;
  --blue: #58a6ff;
  --yellow: #d2991d;
  --purple: #bc8cff;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: var(--bg); color: var(--text); font: 13px/1.5 'SF Mono', 'Fira Code', monospace; padding: 24px; }
h1 { font-size: 18px; margin-bottom: 16px; color: var(--blue); }
.controls { display: flex; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; align-items: center; }
select, button { background: var(--surface); color: var(--text); border: 1px solid var(--border); padding: 6px 12px; border-radius: 6px; font: inherit; cursor: pointer; }
select:hover, button:hover { border-color: var(--blue); }
button.active { background: var(--blue); color: #000; border-color: var(--blue); }
.badge { display: inline-block; padding: 1px 7px; border-radius: 10px; font-size: 11px; font-weight: 600; }
.badge-order { background: #1a3a5c; color: var(--blue); }
.badge-close { background: #1a3a1a; color: var(--green); }
.badge-modify { background: #3a2a0a; color: var(--yellow); }
.badge-auto { background: #2a1a3a; color: var(--purple); }
.badge-ok { background: #1a3a1a; color: var(--green); }
.badge-err { background: #3a1a1a; color: var(--red); }
table { width: 100%; border-collapse: collapse; }
th, td { padding: 6px 10px; text-align: left; border-bottom: 1px solid var(--border); white-space: nowrap; }
th { color: var(--muted); font-weight: 600; font-size: 11px; text-transform: uppercase; position: sticky; top: 0; background: var(--bg); z-index: 1; }
tr:hover { background: var(--surface); }
tr.detail { display: none; background: var(--surface); }
tr.detail.open { display: table-row; }
tr.detail td { white-space: pre-wrap; font-size: 11px; color: var(--muted); padding: 12px 10px; max-width: 0; }
.clickable { cursor: pointer; }
.mono { font-variant-numeric: tabular-nums; }
.pnl-pos { color: var(--green); }
.pnl-neg { color: var(--red); }
.info { color: var(--muted); font-size: 12px; margin-top: 8px; }
.empty { color: var(--muted); text-align: center; padding: 40px; }
#counter { color: var(--muted); font-size: 11px; margin-left: auto; }
@media (max-width: 800px) {
  .hide-sm { display: none; }
}
</style>
</head>
<body>
<h1>QuantStand Audit Log</h1>
<div class="controls">
  <select id="strategy" onchange="load()"></select>
  <select id="eventFilter" onchange="load()">
    <option value="">All events</option>
    <option value="order_market">Order</option>
    <option value="close">Close</option>
    <option value="modify_sl">Modify SL</option>
    <option value="auto_close">Auto close</option>
  </select>
  <button id="liveBtn" onclick="toggleLive()" class="active">LIVE</button>
  <span id="counter"></span>
</div>
<table>
  <thead>
    <tr>
      <th>Time</th>
      <th>Event</th>
      <th>Symbol</th>
      <th class="hide-sm">Side</th>
      <th>Status</th>
      <th>P&L / Detail</th>
    </tr>
  </thead>
  <tbody id="rows"></tbody>
</table>
<p class="info">Click any row to expand JSON detail. Auto-refresh every 5s when LIVE is active.</p>
<script>
let live = true, strategy = '', timer = 0;
async function load() {
  strategy = document.getElementById('strategy').value;
  const ef = document.getElementById('eventFilter').value;
  const url = strategy
    ? '/api/' + strategy + '/audit?limit=200' + (ef ? '&event=' + encodeURIComponent(ef) : '')
    : '/api/strategies';
  try {
    const r = await fetch(url);
    const data = await r.json();
    if (!strategy && Array.isArray(data)) {
      const sel = document.getElementById('strategy');
      const cur = sel.value;
      sel.innerHTML = '<option value="">-- select strategy --</option>';
      data.forEach(s => { const o = document.createElement('option'); o.value = s; o.textContent = s; sel.appendChild(o); });
      if (cur && data.includes(cur)) sel.value = cur;
      else if (data.length) { sel.value = data[0]; load(); }
      return;
    }
    render(data || []);
  } catch(e) { document.getElementById('rows').innerHTML = '<tr><td colspan="6" class="empty">Failed to load</td></tr>'; }
}
function render(entries) {
  const tbody = document.getElementById('rows');
  document.getElementById('counter').textContent = entries.length + ' entries';
  if (!entries.length) { tbody.innerHTML = '<tr><td colspan="6" class="empty">No entries</td></tr>'; return; }
  tbody.innerHTML = '';
  entries.forEach((e, i) => {
    const badge = badgeFor(e.event);
    const sym = e.symbol || (e.request && e.request.symbol) || '—';
    const side = (e.closed_position && e.closed_position.side) || (e.request && e.request.side) || '—';
    const err = e.result ? e.result.error : 'ok';
    const statusBadge = err === 'ok'
      ? '<span class="badge badge-ok">OK</span>'
      : '<span class="badge badge-err">' + err + '</span>';
    const pnl = pnlSummary(e);
    const req = requestSummary(e);
    const ts = e.ts ? e.ts.replace('T', ' ').substring(0, 19) + 'Z' : '—';

    const row = document.createElement('tr');
    row.className = 'clickable';
    row.onclick = () => {
      const det = document.getElementById('detail-' + i);
      det.classList.toggle('open');
    };
    row.innerHTML =
      '<td class="mono">' + ts + '</td>' +
      '<td>' + badge + '</td>' +
      '<td>' + sym + '</td>' +
      '<td class="hide-sm">' + side + '</td>' +
      '<td>' + statusBadge + '</td>' +
      '<td class="mono" style="white-space:normal;max-width:300px">' + (req ? req + '<br>' : '') + pnl + '</td>';
    tbody.appendChild(row);

    const det = document.createElement('tr');
    det.className = 'detail';
    det.id = 'detail-' + i;
    det.innerHTML = '<td colspan="6">' + JSON.stringify(e, null, 2).replace(/</g, '&lt;') + '</td>';
    tbody.appendChild(det);
  });
}
function badgeFor(ev) {
  const map = {
    order_market: '<span class="badge badge-order">ORDER</span>',
    close: '<span class="badge badge-close">CLOSE</span>',
    modify_sl: '<span class="badge badge-modify">MOD SL</span>',
    auto_close: '<span class="badge badge-auto">AUTO</span>'
  };
  return map[ev] || ev;
}
function pnlSummary(e) {
  let pnl = null, rr = null;
  const cp = e.closed_position || (e.result && e.result.closed_position);
  if (cp && cp.pnl_usdt != null) { pnl = cp.pnl_usdt; rr = cp.pnl_rr; }
  else if (e.result && e.result.position && e.result.position.pnl_usdt != null) { pnl = e.result.position.pnl_usdt; rr = e.result.position.current_price_rr; }
  if (pnl == null) return '—';
  const cls = pnl >= 0 ? 'pnl-pos' : 'pnl-neg';
  let s = '<span class="' + cls + '">' + (pnl >= 0 ? '+' : '') + pnl.toFixed(2) + '</span>';
  if (rr != null && Number.isFinite(rr)) s += ' <span class="' + cls + '">(' + (rr >= 0 ? '+' : '') + rr.toFixed(2) + 'R)</span>';
  return s;
}
function requestSummary(e) {
  var p = [];
  if (e.request) {
    if (e.request.risk_pct != null) p.push('risk=' + e.request.risk_pct + '%');
    if (e.request.sl_price != null) p.push('SL=' + e.request.sl_price);
    if (e.request.tp_price != null) p.push('TP=' + e.request.tp_price);
    if (e.request.client_order_id) p.push('#=' + e.request.client_order_id.substring(0, 8));
  }
  if (e.new_sl != null) {
    var parts = [];
    if (e.old_sl != null) parts.push('' + e.old_sl + '→');
    parts.push('' + e.new_sl);
    p.push('SL=' + parts.join(''));
  }
  if (e.percentage != null) p.push('pct=' + e.percentage + '%');
  if (e.trigger != null) p.push('trigger=' + e.trigger);
  if (e.mark_price != null) p.push('mark=' + e.mark_price);
  return p.length ? '<span style="color:var(--muted);font-size:11px">' + p.join(' ') + '</span>' : '';
}
function toggleLive() {
  live = !live;
  const btn = document.getElementById('liveBtn');
  btn.textContent = live ? 'LIVE' : 'PAUSED';
  btn.className = live ? 'active' : '';
  if (live) poll();
}
function poll() {
  if (!live) return;
  load();
  timer = setTimeout(poll, 5000);
}
load();
poll();
</script>
</body>
</html>"""


# ── FastAPI app ─────────────────────────────────────────────────────────

import uvicorn  # noqa: E402
from fastapi import FastAPI, Query  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402

app = FastAPI(title="QuantStand Audit Log Viewer", version="0.1.0")


@app.get("/", response_class=HTMLResponse)
def index():
    return _HTML


@app.get("/api/strategies")
def api_strategies(data_root: str = Query(str(DEFAULT_DATA_ROOT))):
    return _list_strategies(Path(data_root))


@app.get("/api/{strategy_id}/audit")
def api_audit(
    strategy_id: str,
    data_root: str = Query(str(DEFAULT_DATA_ROOT)),
    limit: int = Query(200, ge=1, le=2000),
    event: Optional[str] = Query(None),
):
    return _load_audit_entries(Path(data_root), strategy_id, limit=limit, event_filter=event)


def main() -> int:
    parser = argparse.ArgumentParser(description="Standalone audit log viewer.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT, help="Directory containing per-strategy audit.jsonl files.")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    print(f"Audit log viewer on http://{args.host}:{args.port}")
    print(f"  data root: {args.data_root}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info" if args.verbose else "warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
