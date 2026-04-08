"""Fleet Web Dashboard -- real-time browser UI for multi-fleet monitoring.

Superset-inspired dark theme. Self-contained HTML (no external deps).
Connects to daemon :8855 (HTTP polling) and EventBus :8031 (WebSocket).

Usage:
    python3 -m multifleet.web_dashboard          # Serve on :8080
    python3 -m multifleet.web_dashboard --port 9090

Or via CLI:
    fleet dashboard --web
    fleet dashboard --web --port 9090

Or embedded in daemon:
    GET /dashboard  -> serves the HTML page
"""

import argparse
import json
import logging
import os
import sys
import time
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional

logger = logging.getLogger("fleet-nerve.web-dashboard")

DAEMON_PORT = int(os.environ.get("FLEET_DAEMON_PORT", "8855"))
EVENTBUS_PORT = int(os.environ.get("FLEET_EVENTBUS_PORT", "8031"))
DEFAULT_WEB_PORT = 8080


def _fetch_daemon(path: str, port: int = DAEMON_PORT, timeout: float = 3.0) -> Optional[dict]:
    """Fetch JSON from daemon. Returns None on failure."""
    try:
        url = f"http://127.0.0.1:{port}{path}"
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def build_dashboard_html(daemon_port: int = DAEMON_PORT, eventbus_port: int = EVENTBUS_PORT) -> str:
    """Build the complete self-contained HTML dashboard.

    Single HTML string with inline CSS + JS. No external dependencies.
    Connects to daemon via fetch() and EventBus via WebSocket.
    """
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fleet Dashboard</title>
<style>
  :root {{
    --bg-primary: #1a1a2e;
    --bg-secondary: #16213e;
    --bg-card: #0f3460;
    --bg-card-hover: #1a4a7a;
    --bg-surface: #1e2a4a;
    --border: #2a3a5a;
    --text-primary: #e2e8f0;
    --text-secondary: #94a3b8;
    --text-muted: #64748b;
    --accent: #4fc3f7;
    --accent-dim: #2a7ab0;
    --green: #22c55e;
    --green-dim: #166534;
    --yellow: #eab308;
    --yellow-dim: #854d0e;
    --red: #ef4444;
    --red-dim: #991b1b;
    --purple: #a78bfa;
    --cyan: #22d3ee;
    --orange: #fb923c;
    --radius: 8px;
    --shadow: 0 2px 8px rgba(0,0,0,0.3);
  }}

  * {{ margin: 0; padding: 0; box-sizing: border-box; }}

  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Segoe UI', system-ui, sans-serif;
    background: var(--bg-primary);
    color: var(--text-primary);
    min-height: 100vh;
    overflow-x: hidden;
  }}

  /* ── Top Bar ── */
  .topbar {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 12px 24px;
    background: var(--bg-secondary);
    border-bottom: 1px solid var(--border);
    position: sticky;
    top: 0;
    z-index: 100;
  }}
  .topbar-left {{
    display: flex;
    align-items: center;
    gap: 12px;
  }}
  .topbar-title {{
    font-size: 18px;
    font-weight: 700;
    letter-spacing: -0.3px;
  }}
  .topbar-node {{
    font-size: 13px;
    color: var(--text-secondary);
    background: var(--bg-surface);
    padding: 3px 10px;
    border-radius: 12px;
  }}
  .topbar-right {{
    display: flex;
    align-items: center;
    gap: 16px;
    font-size: 13px;
    color: var(--text-secondary);
  }}
  .topbar-right .live-dot {{
    width: 8px;
    height: 8px;
    border-radius: 50%;
    display: inline-block;
    animation: pulse 2s infinite;
  }}
  @keyframes pulse {{
    0%, 100% {{ opacity: 1; }}
    50% {{ opacity: 0.4; }}
  }}

  /* ── Grid Layout ── */
  .grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(340px, 1fr));
    gap: 16px;
    padding: 20px 24px;
    max-width: 1600px;
    margin: 0 auto;
  }}
  .grid-wide {{
    grid-column: 1 / -1;
  }}

  /* ── Cards ── */
  .card {{
    background: var(--bg-secondary);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    overflow: hidden;
    transition: border-color 0.2s;
  }}
  .card:hover {{
    border-color: var(--accent-dim);
  }}
  .card-header {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 12px 16px;
    border-bottom: 1px solid var(--border);
    font-size: 13px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: var(--text-secondary);
  }}
  .card-header .badge {{
    font-size: 11px;
    padding: 2px 8px;
    border-radius: 10px;
    font-weight: 500;
    text-transform: none;
  }}
  .card-body {{
    padding: 16px;
  }}

  /* ── Status Dots ── */
  .dot {{
    width: 10px;
    height: 10px;
    border-radius: 50%;
    display: inline-block;
    flex-shrink: 0;
  }}
  .dot-green {{ background: var(--green); box-shadow: 0 0 6px var(--green); }}
  .dot-yellow {{ background: var(--yellow); box-shadow: 0 0 6px var(--yellow); }}
  .dot-red {{ background: var(--red); box-shadow: 0 0 6px var(--red); }}
  .dot-gray {{ background: var(--text-muted); }}

  /* ── Node Grid ── */
  .node-row {{
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 10px 0;
    border-bottom: 1px solid rgba(42, 58, 90, 0.5);
  }}
  .node-row:last-child {{ border-bottom: none; }}
  .node-name {{
    font-weight: 600;
    min-width: 60px;
  }}
  .node-name.self {{ color: var(--accent); }}
  .node-meta {{
    display: flex;
    gap: 16px;
    font-size: 12px;
    color: var(--text-secondary);
    flex-wrap: wrap;
  }}
  .node-meta span {{
    display: flex;
    align-items: center;
    gap: 4px;
  }}

  /* ── Stats Grid ── */
  .stats-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(100px, 1fr));
    gap: 12px;
  }}
  .stat-item {{
    text-align: center;
    padding: 12px 8px;
    background: var(--bg-surface);
    border-radius: 6px;
  }}
  .stat-value {{
    font-size: 24px;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
  }}
  .stat-label {{
    font-size: 11px;
    color: var(--text-muted);
    margin-top: 2px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }}

  /* ── Evidence Stream ── */
  .evidence-item {{
    display: flex;
    align-items: flex-start;
    gap: 10px;
    padding: 8px 0;
    border-bottom: 1px solid rgba(42, 58, 90, 0.3);
    font-size: 13px;
  }}
  .evidence-item:last-child {{ border-bottom: none; }}
  .evidence-ts {{
    font-family: 'SF Mono', 'Fira Code', monospace;
    font-size: 11px;
    color: var(--text-muted);
    white-space: nowrap;
    min-width: 70px;
  }}
  .evidence-type {{
    font-weight: 600;
    color: var(--accent);
    min-width: 80px;
  }}
  .evidence-detail {{
    color: var(--text-secondary);
    word-break: break-word;
  }}

  /* ── EventBus Stream ── */
  .event-stream {{
    max-height: 280px;
    overflow-y: auto;
    scrollbar-width: thin;
    scrollbar-color: var(--border) transparent;
  }}
  .event-item {{
    display: flex;
    gap: 8px;
    padding: 6px 0;
    border-bottom: 1px solid rgba(42, 58, 90, 0.2);
    font-size: 12px;
    font-family: 'SF Mono', 'Fira Code', monospace;
    animation: fadeIn 0.3s ease;
  }}
  @keyframes fadeIn {{
    from {{ opacity: 0; transform: translateY(-4px); }}
    to {{ opacity: 1; transform: translateY(0); }}
  }}
  .event-item .ev-ts {{ color: var(--text-muted); min-width: 65px; }}
  .event-item .ev-type {{ color: var(--purple); font-weight: 500; }}
  .event-item .ev-data {{ color: var(--text-secondary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}

  /* ── Truth Ladder ── */
  .truth-bar {{
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 8px;
  }}
  .truth-label {{
    font-size: 12px;
    min-width: 80px;
    color: var(--text-secondary);
  }}
  .truth-track {{
    flex: 1;
    height: 6px;
    background: var(--bg-surface);
    border-radius: 3px;
    overflow: hidden;
  }}
  .truth-fill {{
    height: 100%;
    border-radius: 3px;
    transition: width 0.5s ease;
  }}
  .truth-count {{
    font-size: 12px;
    min-width: 30px;
    text-align: right;
    font-variant-numeric: tabular-nums;
  }}

  /* ── Gains Gate ── */
  .gate-check {{
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 6px 0;
    font-size: 13px;
  }}
  .gate-icon {{ font-size: 14px; }}
  .gate-name {{ flex: 1; }}
  .gate-msg {{ color: var(--text-muted); font-size: 12px; }}

  /* ── Race Card ── */
  .race-item {{
    padding: 10px 0;
    border-bottom: 1px solid rgba(42, 58, 90, 0.3);
  }}
  .race-item:last-child {{ border-bottom: none; }}
  .race-topic {{
    font-weight: 600;
    margin-bottom: 4px;
  }}
  .race-meta {{
    font-size: 12px;
    color: var(--text-secondary);
    display: flex;
    gap: 12px;
  }}

  /* ── Probe Findings ── */
  .probe-item {{
    padding: 8px 0;
    border-bottom: 1px solid rgba(42, 58, 90, 0.3);
  }}
  .probe-item:last-child {{ border-bottom: none; }}
  .probe-severity {{
    display: inline-block;
    font-size: 11px;
    font-weight: 600;
    padding: 1px 6px;
    border-radius: 4px;
    text-transform: uppercase;
    margin-right: 6px;
  }}
  .probe-severity.critical {{ background: var(--red-dim); color: var(--red); }}
  .probe-severity.warn {{ background: var(--yellow-dim); color: var(--yellow); }}
  .probe-severity.info {{ background: rgba(79, 195, 247, 0.15); color: var(--accent); }}

  /* ── Overlays ── */
  .overlay-item {{
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 6px 0;
    font-size: 13px;
  }}
  .overlay-node {{ font-weight: 600; min-width: 50px; color: var(--cyan); }}
  .overlay-state {{ color: var(--text-secondary); }}

  /* ── Empty State ── */
  .empty {{
    color: var(--text-muted);
    font-size: 13px;
    padding: 12px 0;
    text-align: center;
    font-style: italic;
  }}

  /* ── Connection Status Bar ── */
  .conn-bar {{
    display: flex;
    gap: 16px;
    align-items: center;
  }}
  .conn-item {{
    display: flex;
    align-items: center;
    gap: 4px;
    font-size: 12px;
  }}
  .conn-dot {{
    width: 6px;
    height: 6px;
    border-radius: 50%;
  }}

  /* ── Scrollbar ── */
  ::-webkit-scrollbar {{ width: 6px; }}
  ::-webkit-scrollbar-track {{ background: transparent; }}
  ::-webkit-scrollbar-thumb {{ background: var(--border); border-radius: 3px; }}
  ::-webkit-scrollbar-thumb:hover {{ background: var(--text-muted); }}
</style>
</head>
<body>

<!-- ═══ Top Bar ═══ -->
<div class="topbar">
  <div class="topbar-left">
    <div class="topbar-title">Fleet Dashboard</div>
    <div class="topbar-node" id="node-id">--</div>
  </div>
  <div class="topbar-right">
    <div class="conn-bar">
      <div class="conn-item">
        <span class="conn-dot dot-gray" id="conn-daemon"></span>
        <span>Daemon</span>
      </div>
      <div class="conn-item">
        <span class="conn-dot dot-gray" id="conn-eventbus"></span>
        <span>EventBus</span>
      </div>
    </div>
    <span class="live-dot dot-green" id="heartbeat"></span>
    <span id="last-update">--</span>
  </div>
</div>

<!-- ═══ Dashboard Grid ═══ -->
<div class="grid">

  <!-- Nodes Card -->
  <div class="card">
    <div class="card-header">
      Nodes
      <span class="badge" id="nodes-count" style="background:var(--bg-surface);color:var(--accent)">0</span>
    </div>
    <div class="card-body" id="nodes-body">
      <div class="empty">Connecting to daemon...</div>
    </div>
  </div>

  <!-- Message Stats Card -->
  <div class="card">
    <div class="card-header">Messages</div>
    <div class="card-body">
      <div class="stats-grid" id="stats-grid">
        <div class="stat-item"><div class="stat-value" id="stat-sent">-</div><div class="stat-label">Sent</div></div>
        <div class="stat-item"><div class="stat-value" id="stat-recv">-</div><div class="stat-label">Received</div></div>
        <div class="stat-item"><div class="stat-value" id="stat-bcast">-</div><div class="stat-label">Broadcast</div></div>
        <div class="stat-item"><div class="stat-value" id="stat-errors" style="color:var(--text-primary)">-</div><div class="stat-label">Errors</div></div>
      </div>
    </div>
  </div>

  <!-- Gains Gate Card -->
  <div class="card">
    <div class="card-header">
      Gains Gate
      <span class="badge" id="gate-badge" style="background:var(--bg-surface);color:var(--text-muted)">--</span>
    </div>
    <div class="card-body" id="gate-body">
      <div class="empty">Loading...</div>
    </div>
  </div>

  <!-- Active Races Card -->
  <div class="card">
    <div class="card-header">
      Races
      <span class="badge" id="races-count" style="background:var(--bg-surface);color:var(--orange)">0</span>
    </div>
    <div class="card-body" id="races-body">
      <div class="empty">No active races</div>
    </div>
  </div>

  <!-- Evidence Stream Card -->
  <div class="card">
    <div class="card-header">
      Evidence Ledger
      <span class="badge" id="evidence-count" style="background:var(--bg-surface);color:var(--green)">0</span>
    </div>
    <div class="card-body" id="evidence-body">
      <div class="empty">No evidence entries</div>
    </div>
  </div>

  <!-- Truth Ladder Card -->
  <div class="card">
    <div class="card-header">Truth Ladder</div>
    <div class="card-body" id="truth-body">
      <div class="empty">Loading...</div>
    </div>
  </div>

  <!-- Probe Findings Card -->
  <div class="card">
    <div class="card-header">
      Probe Findings
      <span class="badge" id="probe-count" style="background:var(--bg-surface);color:var(--yellow)">0</span>
    </div>
    <div class="card-body" id="probe-body">
      <div class="empty">No findings</div>
    </div>
  </div>

  <!-- Node Overlays Card -->
  <div class="card">
    <div class="card-header">Node Overlays</div>
    <div class="card-body" id="overlays-body">
      <div class="empty">No overlays</div>
    </div>
  </div>

  <!-- EventBus Live Stream Card (full width) -->
  <div class="card grid-wide">
    <div class="card-header">
      EventBus Live
      <span class="badge" id="events-count" style="background:var(--bg-surface);color:var(--purple)">0</span>
    </div>
    <div class="card-body">
      <div class="event-stream" id="events-stream">
        <div class="empty" id="events-empty">Waiting for WebSocket connection...</div>
      </div>
    </div>
  </div>

</div>

<script>
(function() {{
  'use strict';

  const DAEMON = 'http://127.0.0.1:{daemon_port}';
  const EVENTBUS_WS = 'ws://127.0.0.1:{eventbus_port}';
  const POLL_INTERVAL = 5000;
  const MAX_EVENTS = 80;

  let ws = null;
  let wsConnected = false;
  let daemonConnected = false;
  let eventCount = 0;
  let liveEvents = [];

  // ── Helpers ──

  function esc(s) {{ return s ? String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;') : ''; }}

  function ago(s) {{
    s = Math.floor(s);
    if (s < 0) return '?';
    if (s < 60) return s + 's';
    if (s < 3600) return Math.floor(s/60) + 'm ' + (s%60) + 's';
    return Math.floor(s/3600) + 'h ' + Math.floor((s%3600)/60) + 'm';
  }}

  function dotClass(lastSeen) {{
    if (lastSeen < 30) return 'dot-green';
    if (lastSeen < 120) return 'dot-yellow';
    return 'dot-red';
  }}

  function statusDotClass(status) {{
    if (['online','ok','active','self'].includes(status)) return 'dot-green';
    if (['stale','degraded','warning'].includes(status)) return 'dot-yellow';
    return 'dot-red';
  }}

  function ts() {{ return new Date().toLocaleTimeString('en-US', {{hour12:false}}); }}

  function setConn(id, ok) {{
    const el = document.getElementById(id);
    el.className = 'conn-dot ' + (ok ? 'dot-green' : 'dot-red');
  }}

  // ── Fetch from daemon ──

  async function fetchJSON(path) {{
    try {{
      const r = await fetch(DAEMON + path, {{signal: AbortSignal.timeout(4000)}});
      if (!r.ok) return null;
      return await r.json();
    }} catch(e) {{
      return null;
    }}
  }}

  // ── Render: Nodes ──

  function renderNodes(nodes) {{
    const body = document.getElementById('nodes-body');
    const count = document.getElementById('nodes-count');
    if (!nodes || !nodes.length) {{
      body.innerHTML = '<div class="empty">No nodes discovered</div>';
      count.textContent = '0';
      return;
    }}
    count.textContent = nodes.length;
    body.innerHTML = nodes.map(n => {{
      const dc = statusDotClass(n.status);
      const isSelf = n.is_self;
      const nameClass = isSelf ? 'node-name self' : 'node-name';
      const seenText = isSelf ? 'self' : ago(n.last_seen_s || 0);
      const sessions = n.sessions || 0;
      const transport = n.transport || '';
      const chScore = n.channel_score || '';
      const broken = (n.broken_channels || []).join(', ');
      return `<div class="node-row">
        <span class="dot ${{dc}}"></span>
        <span class="${{nameClass}}">${{esc(n.id)}}</span>
        <div class="node-meta">
          <span>seen: ${{seenText}}</span>
          <span>sessions: ${{sessions}}</span>
          ${{transport ? `<span>via: ${{esc(transport)}}</span>` : ''}}
          ${{chScore ? `<span>ch: ${{esc(String(chScore))}}</span>` : ''}}
          ${{broken ? `<span style="color:var(--yellow)">broken: ${{esc(broken)}}</span>` : ''}}
        </div>
      </div>`;
    }}).join('');
  }}

  // ── Render: Messages ──

  function renderMessages(msgs) {{
    if (!msgs) return;
    const el = (id, v) => {{ document.getElementById(id).textContent = v; }};
    el('stat-sent', msgs.sent || 0);
    el('stat-recv', msgs.received || 0);
    el('stat-bcast', msgs.broadcasts || 0);
    const errEl = document.getElementById('stat-errors');
    const errors = msgs.errors || 0;
    errEl.textContent = errors;
    errEl.style.color = errors > 0 ? 'var(--red)' : 'var(--text-primary)';
  }}

  // ── Render: Gains Gate ──

  function renderGainsGate(data) {{
    const body = document.getElementById('gate-body');
    const badge = document.getElementById('gate-badge');
    if (!data || data.error) {{
      body.innerHTML = '<div class="empty">Unavailable</div>';
      badge.textContent = '?';
      badge.style.color = 'var(--text-muted)';
      return;
    }}
    const passed = data.passed;
    badge.textContent = passed ? 'PASS' : 'FAIL';
    badge.style.background = passed ? 'var(--green-dim)' : 'var(--red-dim)';
    badge.style.color = passed ? 'var(--green)' : 'var(--red)';

    const checks = data.checks || [];
    if (!checks.length) {{
      body.innerHTML = `<div class="empty">${{passed ? 'All clear' : 'No details'}}</div>`;
      return;
    }}
    body.innerHTML = checks.map(c => {{
      const ok = c.passed;
      const icon = ok ? '<span style="color:var(--green)">&#10003;</span>' : '<span style="color:var(--red)">&#10007;</span>';
      return `<div class="gate-check">
        <span class="gate-icon">${{icon}}</span>
        <span class="gate-name">${{esc(c.name || '?')}}</span>
        <span class="gate-msg">${{esc(c.message || '')}}</span>
      </div>`;
    }}).join('');
  }}

  // ── Render: Races ──

  function renderRaces(races) {{
    const body = document.getElementById('races-body');
    const count = document.getElementById('races-count');
    if (!races || !races.length) {{
      body.innerHTML = '<div class="empty">No active races</div>';
      count.textContent = '0';
      return;
    }}
    count.textContent = races.length;
    body.innerHTML = races.map(r => {{
      const participants = (r.participants || []).join(', ') || '?';
      const status = r.status || '?';
      return `<div class="race-item">
        <div class="race-topic">${{esc(r.topic || r.task || '?')}}</div>
        <div class="race-meta">
          <span>status: ${{esc(status)}}</span>
          <span>nodes: ${{esc(participants)}}</span>
          ${{r.race_id ? `<span style="color:var(--text-muted)">${{esc(r.race_id.slice(0,8))}}</span>` : ''}}
        </div>
      </div>`;
    }}).join('');
  }}

  // ── Render: Evidence ──

  function renderEvidence(entries) {{
    const body = document.getElementById('evidence-body');
    const count = document.getElementById('evidence-count');
    if (!entries || !entries.length) {{
      body.innerHTML = '<div class="empty">No evidence entries</div>';
      count.textContent = '0';
      return;
    }}
    count.textContent = entries.length;
    body.innerHTML = entries.slice(0, 12).map(e => {{
      const t = e.timestamp || e.ts || '';
      let tsStr = '';
      if (t) {{
        try {{ tsStr = new Date(typeof t === 'number' ? t * 1000 : t).toLocaleTimeString('en-US', {{hour12:false}}); }}
        catch(ex) {{ tsStr = String(t).slice(0,8); }}
      }}
      return `<div class="evidence-item">
        <span class="evidence-ts">${{esc(tsStr)}}</span>
        <span class="evidence-type">${{esc(e.type || e.event_type || '?')}}</span>
        <span class="evidence-detail">${{esc(e.detail || e.description || e.hash || '')}}</span>
      </div>`;
    }}).join('');
  }}

  // ── Render: Truth Ladder ──

  function renderTruthLadder(data) {{
    const body = document.getElementById('truth-body');
    if (!data || (!data.levels && !data.rungs && !data.tiers)) {{
      body.innerHTML = '<div class="empty">No truth data</div>';
      return;
    }}
    const levels = data.levels || data.rungs || data.tiers || {{}};
    const colors = ['var(--red)', 'var(--orange)', 'var(--yellow)', 'var(--green)', 'var(--cyan)', 'var(--purple)'];
    const names = Object.keys(levels);
    const maxVal = Math.max(1, ...Object.values(levels).map(v => typeof v === 'number' ? v : (v.count || 0)));

    body.innerHTML = names.map((name, i) => {{
      const val = typeof levels[name] === 'number' ? levels[name] : (levels[name].count || 0);
      const pct = Math.round((val / maxVal) * 100);
      const color = colors[i % colors.length];
      return `<div class="truth-bar">
        <span class="truth-label">${{esc(name)}}</span>
        <div class="truth-track">
          <div class="truth-fill" style="width:${{pct}}%;background:${{color}}"></div>
        </div>
        <span class="truth-count">${{val}}</span>
      </div>`;
    }}).join('');
  }}

  // ── Render: Probe Findings ──

  function renderProbes(results) {{
    const body = document.getElementById('probe-body');
    const count = document.getElementById('probe-count');
    if (!results || !results.length) {{
      body.innerHTML = '<div class="empty">No findings</div>';
      count.textContent = '0';
      return;
    }}
    count.textContent = results.length;
    body.innerHTML = results.slice(0, 8).map(p => {{
      const sev = (p.severity || p.level || 'info').toLowerCase();
      const sevClass = sev === 'critical' ? 'critical' : (sev === 'warn' || sev === 'warning' ? 'warn' : 'info');
      return `<div class="probe-item">
        <span class="probe-severity ${{sevClass}}">${{esc(sev)}}</span>
        <span>${{esc(p.finding || p.message || p.description || '?')}}</span>
      </div>`;
    }}).join('');
  }}

  // ── Render: Overlays ──

  function renderOverlays(overlays) {{
    const body = document.getElementById('overlays-body');
    if (!overlays || !overlays.length) {{
      body.innerHTML = '<div class="empty">No overlays</div>';
      return;
    }}
    body.innerHTML = overlays.map(o => {{
      return `<div class="overlay-item">
        <span class="overlay-node">${{esc(o.node_id || o.node || '?')}}</span>
        <span class="overlay-state">${{esc(o.state || o.status || '?')}}</span>
      </div>`;
    }}).join('');
  }}

  // ── Render: Live Events ──

  function appendEvent(ev) {{
    const container = document.getElementById('events-stream');
    const emptyEl = document.getElementById('events-empty');
    if (emptyEl) emptyEl.remove();

    eventCount++;
    document.getElementById('events-count').textContent = eventCount;

    const evType = ev.type || '?';
    const evTs = ev.timestamp ? new Date(typeof ev.timestamp === 'number' && ev.timestamp < 1e12 ? ev.timestamp * 1000 : ev.timestamp).toLocaleTimeString('en-US', {{hour12:false}}) : ts();
    const payload = ev.payload || {{}};
    const summary = Object.keys(payload).slice(0,3).map(k => k + '=' + JSON.stringify(payload[k]).slice(0,30)).join(' ');

    const div = document.createElement('div');
    div.className = 'event-item';
    div.innerHTML = `<span class="ev-ts">${{esc(evTs)}}</span><span class="ev-type">${{esc(evType)}}</span><span class="ev-data">${{esc(summary)}}</span>`;
    container.insertBefore(div, container.firstChild);

    // Trim old events
    while (container.children.length > MAX_EVENTS) {{
      container.removeChild(container.lastChild);
    }}
  }}

  // ── WebSocket: EventBus ──

  function connectWS() {{
    if (ws && (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN)) return;

    try {{
      ws = new WebSocket(EVENTBUS_WS);
    }} catch(e) {{
      wsConnected = false;
      setConn('conn-eventbus', false);
      return;
    }}

    ws.onopen = function() {{
      wsConnected = true;
      setConn('conn-eventbus', true);
      // Subscribe to all events
      ws.send(JSON.stringify({{ type: 'subscribe', patterns: ['*'] }}));
    }};

    ws.onmessage = function(msg) {{
      try {{
        const data = JSON.parse(msg.data);
        if (data.type === 'event' && data.event) {{
          appendEvent(data.event);
        }}
      }} catch(e) {{}}
    }};

    ws.onclose = function() {{
      wsConnected = false;
      setConn('conn-eventbus', false);
      // Reconnect after 3s
      setTimeout(connectWS, 3000);
    }};

    ws.onerror = function() {{
      wsConnected = false;
      setConn('conn-eventbus', false);
    }};
  }}

  // ── Poll: Daemon data ──

  async function poll() {{
    // Use FleetDashboardData-style snapshot via multiple endpoints
    const [health, races, evidence, gainsGate, probes, truth, overlays] = await Promise.all([
      fetchJSON('/health'),
      fetchJSON('/races'),
      fetchJSON('/evidence/latest'),
      fetchJSON('/gains-gate?quick=1'),
      fetchJSON('/scan/results'),
      fetchJSON('/truth/stats'),
      fetchJSON('/overlays'),
    ]);

    daemonConnected = !!health;
    setConn('conn-daemon', daemonConnected);

    if (health) {{
      // Update node ID
      document.getElementById('node-id').textContent = health.nodeId || '?';

      // Build node list (same logic as FleetDashboardData._get_nodes)
      const nodes = [];
      const ch = health.channel_health || {{}};
      nodes.push({{
        id: health.nodeId || '?',
        status: 'online',
        last_seen_s: 0,
        sessions: health.activeSessions || 0,
        channel_score: 'self',
        broken_channels: [],
        is_self: true,
      }});
      const peers = health.peers || {{}};
      for (const [name, peer] of Object.entries(peers)) {{
        const ls = peer.lastSeen || 9999;
        const peerCh = ch[name] || {{}};
        nodes.push({{
          id: name,
          status: ls < 30 ? 'online' : (ls < 120 ? 'stale' : 'offline'),
          last_seen_s: ls,
          sessions: peer.sessions || 0,
          transport: peer.transport || '?',
          vscode: peer.vscode || '?',
          channel_score: peerCh.score || '?',
          broken_channels: peerCh.broken || [],
          is_self: false,
        }});
      }}
      renderNodes(nodes);

      // Message stats — prefer fleet-wide aggregation, fall back to local
      const fleetMetrics = await fetchJSON('/fleet-metrics');
      if (fleetMetrics && !fleetMetrics.error) {{
        renderMessages({{
          sent: fleetMetrics.total_messages_sent || 0,
          received: fleetMetrics.total_messages_received || 0,
          broadcasts: 0,
          errors: fleetMetrics.total_errors || 0,
        }});
      }} else {{
        const stats = health.stats || {{}};
        renderMessages({{
          sent: stats.sent || 0,
          received: stats.received || 0,
          broadcasts: stats.broadcasts || 0,
          errors: stats.errors || 0,
        }});
      }}
    }}

    renderRaces(races ? (races.races || []) : []);
    renderEvidence(evidence ? (evidence.entries || []) : []);
    renderGainsGate(gainsGate);
    renderProbes(probes ? (probes.results || []) : []);
    renderTruthLadder(truth);
    renderOverlays(overlays ? (overlays.overlays || []) : []);

    // Update timestamp
    document.getElementById('last-update').textContent = ts();
  }}

  // ── Init ──

  connectWS();
  poll();
  setInterval(poll, POLL_INTERVAL);

}})();
</script>
</body>
</html>"""


class DashboardHandler(BaseHTTPRequestHandler):
    """HTTP handler that serves the dashboard HTML and proxies API requests."""

    # Class-level config (set before serving)
    daemon_port = DAEMON_PORT
    eventbus_port = EVENTBUS_PORT

    def do_GET(self):
        if self.path == "/" or self.path == "/dashboard":
            html = build_dashboard_html(self.daemon_port, self.eventbus_port)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))
        elif self.path == "/health":
            body = json.dumps({"status": "ok", "type": "web-dashboard"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/fleet-metrics":
            # Fleet-wide aggregated metrics for the Messages panel
            try:
                from multifleet.fleet_metrics import FleetMetricsAggregator
                agg = FleetMetricsAggregator(daemon_port=self.daemon_port)
                result = agg.aggregate()
                body = json.dumps(result).encode()
            except Exception as e:
                body = json.dumps({"error": str(e)}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "max-age=5")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        """Suppress default request logging — too noisy for a dashboard."""
        pass


def serve(port: int = DEFAULT_WEB_PORT, daemon_port: int = DAEMON_PORT,
          eventbus_port: int = EVENTBUS_PORT, open_browser: bool = True) -> None:
    """Start the web dashboard server.

    Args:
        port: Port to serve on (default 8080)
        daemon_port: Fleet daemon port (default 8855)
        eventbus_port: EventBus WebSocket port (default 8031)
        open_browser: Auto-open browser on start
    """
    DashboardHandler.daemon_port = daemon_port
    DashboardHandler.eventbus_port = eventbus_port

    server = HTTPServer(("127.0.0.1", port), DashboardHandler)
    url = f"http://127.0.0.1:{port}"

    print(f"Fleet Web Dashboard running at {url}")
    print(f"  Daemon: http://127.0.0.1:{daemon_port}")
    print(f"  EventBus WS: ws://127.0.0.1:{eventbus_port}")
    print(f"  Press Ctrl+C to stop")

    if open_browser:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description="Fleet Web Dashboard")
    parser.add_argument("--port", type=int, default=DEFAULT_WEB_PORT, help="Web server port")
    parser.add_argument("--daemon-port", type=int, default=DAEMON_PORT, help="Fleet daemon port")
    parser.add_argument("--eventbus-port", type=int, default=EVENTBUS_PORT, help="EventBus WS port")
    parser.add_argument("--no-browser", action="store_true", help="Don't auto-open browser")
    args = parser.parse_args()
    serve(args.port, args.daemon_port, args.eventbus_port, not args.no_browser)


if __name__ == "__main__":
    main()
