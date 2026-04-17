"""Human Arbiter View -- investor-facing dashboard showing AI systems thinking in real time.

Core Truth #4: "If it cannot be seen, it does not count as built."

A stranger opens a browser and SEES:
  - Fleet nodes coordinating (heartbeats, messages flowing)
  - Surgeon cross-examination happening live (each surgeon's position)
  - Evidence chain building (claims -> quarantine -> promoted -> wisdom)
  - Disagreements surfaced (red highlights, preserved dissent)
  - Corrigibility gauge (how often surgeons changed decisions)

Single HTML page, no dependencies, auto-refreshes every 5s via JS fetch.

Usage:
    GET /arbiter on daemon (:8855)
    GET /arbiter on web server (:8856)
"""

import html as html_mod
import json
import logging
import os
import time
import urllib.request
from typing import Optional

logger = logging.getLogger("fleet-nerve.human-arbiter")


def _fetch(path: str, port: int = 8855, timeout: float = 2.0) -> Optional[dict]:
    """Fetch JSON from daemon."""
    try:
        url = f"http://127.0.0.1:{port}{path}"
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _esc(s: str) -> str:
    return html_mod.escape(str(s)) if s else ""


def build_human_arbiter_html(daemon_port: int = 8855) -> str:
    """Build the self-contained Human Arbiter View HTML."""
    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Human Arbiter View -- AI Systems Thinking in Real Time</title>
<style>
:root {{
  --bg: #0a0e1a;
  --bg-card: #111827;
  --bg-card-header: #0d1424;
  --border: #1e293b;
  --border-accent: #334155;
  --text: #e2e8f0;
  --text-dim: #94a3b8;
  --text-muted: #64748b;
  --blue: #3b82f6;
  --green: #22c55e;
  --yellow: #eab308;
  --red: #ef4444;
  --purple: #a855f7;
  --cyan: #06b6d4;
  --orange: #f97316;
  --pink: #ec4899;
}}

* {{ margin: 0; padding: 0; box-sizing: border-box; }}

body {{
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", system-ui, sans-serif;
  background: var(--bg);
  color: var(--text);
  min-height: 100vh;
}}

/* Header */
.header {{
  display: flex; align-items: center; justify-content: space-between;
  padding: 14px 24px;
  background: linear-gradient(135deg, #111827 0%, #0f172a 100%);
  border-bottom: 1px solid var(--border);
}}
.header-left {{ display: flex; align-items: baseline; gap: 12px; }}
.header-title {{ font-size: 20px; font-weight: 800; letter-spacing: -0.5px; }}
.header-title span {{ color: var(--cyan); }}
.header-sub {{ font-size: 13px; color: var(--text-muted); font-style: italic; }}
.header-right {{ display: flex; align-items: center; gap: 16px; font-size: 13px; color: var(--text-dim); }}
.live-dot {{
  width: 10px; height: 10px; border-radius: 50%; display: inline-block;
  animation: pulse 2s ease-in-out infinite;
}}
@keyframes pulse {{ 0%,100% {{ opacity: 1; }} 50% {{ opacity: 0.3; }} }}
.status-chip {{
  padding: 3px 10px; border-radius: 12px; font-size: 11px;
  font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px;
}}

/* Grid */
.grid {{
  display: grid;
  grid-template-columns: 1fr 1fr;
  grid-template-rows: auto auto;
  gap: 16px;
  padding: 16px 24px;
  max-width: 1600px;
  margin: 0 auto;
}}

/* Cards */
.card {{
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 10px;
  overflow: hidden;
  transition: border-color 0.2s;
}}
.card:hover {{ border-color: var(--border-accent); }}
.card-head {{
  display: flex; align-items: center; justify-content: space-between;
  padding: 12px 16px;
  background: var(--bg-card-header);
  border-bottom: 1px solid var(--border);
}}
.card-head h2 {{
  font-size: 13px; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.8px; color: var(--text-dim);
  display: flex; align-items: center; gap: 8px;
}}
.card-head .count {{
  font-size: 11px; font-weight: 400; color: var(--text-muted);
  text-transform: none; letter-spacing: 0;
}}
.card-body {{ padding: 16px; }}
.empty {{ color: var(--text-muted); font-size: 13px; text-align: center; padding: 24px 0; }}

/* Fleet Constellation */
.node-grid {{ display: flex; flex-wrap: wrap; gap: 12px; justify-content: center; }}
.node {{
  width: 110px; padding: 12px 8px; border-radius: 8px; text-align: center;
  border: 1px solid var(--border); transition: all 0.2s;
  position: relative;
}}
.node.online {{ border-color: var(--green); background: rgba(34,197,94,0.06); }}
.node.stale {{ border-color: var(--yellow); background: rgba(234,179,8,0.06); }}
.node.offline {{ border-color: var(--red); background: rgba(239,68,68,0.06); opacity: 0.6; }}
.node.self {{ box-shadow: 0 0 12px rgba(34,197,94,0.2); }}
.node-name {{ font-size: 14px; font-weight: 700; margin-bottom: 4px; }}
.node-status {{ font-size: 11px; text-transform: uppercase; font-weight: 600; letter-spacing: 0.5px; }}
.node-meta {{ font-size: 10px; color: var(--text-muted); margin-top: 4px; }}
.node-pulse {{
  position: absolute; top: 8px; right: 8px; width: 8px; height: 8px;
  border-radius: 50%;
}}
.msg-flow {{ margin-top: 12px; padding-top: 12px; border-top: 1px solid var(--border); }}
.msg-item {{
  display: flex; align-items: center; gap: 8px; padding: 4px 0;
  font-size: 12px; color: var(--text-dim);
}}
.msg-arrow {{ color: var(--cyan); font-weight: 700; }}

/* Surgeon Feed */
.surgeon-row {{ display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; }}
.surgeon-badge {{
  display: flex; align-items: center; gap: 6px;
  padding: 6px 12px; border-radius: 8px; font-size: 13px;
  border: 1px solid var(--border); background: rgba(255,255,255,0.02);
}}
.surgeon-badge .dot {{ width: 8px; height: 8px; border-radius: 50%; }}
.exam-feed {{ max-height: 300px; overflow-y: auto; }}
.exam-entry {{
  padding: 10px 12px; margin-bottom: 8px; border-radius: 6px;
  background: rgba(255,255,255,0.02); border-left: 3px solid var(--border);
}}
.exam-entry.dissent {{ border-left-color: var(--red); background: rgba(239,68,68,0.04); }}
.exam-entry.consensus {{ border-left-color: var(--green); }}
.exam-topic {{ font-size: 13px; font-weight: 600; margin-bottom: 4px; }}
.exam-meta {{ font-size: 11px; color: var(--text-muted); display: flex; gap: 12px; flex-wrap: wrap; }}
.position {{
  margin-top: 6px; padding: 6px 10px; border-radius: 4px;
  font-size: 12px; border-left: 2px solid var(--border);
  background: rgba(255,255,255,0.01);
}}
.position.changed {{ border-left-color: var(--yellow); background: rgba(234,179,8,0.05); }}

/* Evidence Timeline */
.timeline {{ max-height: 350px; overflow-y: auto; }}
.tl-entry {{
  display: flex; gap: 12px; padding: 8px 0;
  border-bottom: 1px solid rgba(30,41,59,0.5);
}}
.tl-dot {{
  width: 10px; height: 10px; border-radius: 50%; margin-top: 4px; flex-shrink: 0;
}}
.tl-content {{ flex: 1; min-width: 0; }}
.tl-type {{ font-size: 12px; font-weight: 700; }}
.tl-subject {{ font-size: 12px; color: var(--text-dim); margin-top: 2px; }}
.tl-hash {{ font-family: monospace; font-size: 10px; color: var(--text-muted); }}
.chain-badge {{
  padding: 4px 12px; border-radius: 12px; font-size: 11px;
  font-weight: 700; display: inline-block; margin-bottom: 10px;
}}
.chain-valid {{ background: rgba(34,197,94,0.12); color: var(--green); }}
.chain-broken {{ background: rgba(239,68,68,0.12); color: var(--red); }}

/* Corrigibility Gauge */
.gauge-center {{ display: flex; flex-direction: column; align-items: center; padding: 8px 0; }}
.gauge-score {{ font-size: 48px; font-weight: 800; letter-spacing: -2px; }}
.gauge-label {{ font-size: 13px; color: var(--text-dim); margin-top: -4px; }}
.factor-list {{ margin-top: 12px; }}
.factor {{
  display: flex; align-items: center; justify-content: space-between;
  padding: 6px 0; font-size: 13px;
}}
.factor-name {{ color: var(--text-dim); }}
.factor-bar {{
  width: 80px; height: 6px; background: var(--border); border-radius: 3px;
  overflow: hidden; margin: 0 10px;
}}
.factor-fill {{ height: 100%; border-radius: 3px; transition: width 0.5s; }}
.factor-pct {{ font-weight: 600; min-width: 40px; text-align: right; }}
.disagree-section {{ margin-top: 16px; padding-top: 12px; border-top: 1px solid var(--border); }}
.disagree-item {{
  padding: 8px 10px; margin-bottom: 6px; border-radius: 6px;
  border: 1px solid rgba(239,68,68,0.3); background: rgba(239,68,68,0.04);
  font-size: 12px;
}}

/* Footer */
.footer {{
  padding: 12px 24px; text-align: center; font-size: 11px;
  color: var(--text-muted); border-top: 1px solid var(--border);
}}

/* Scrollbar */
::-webkit-scrollbar {{ width: 5px; }}
::-webkit-scrollbar-track {{ background: transparent; }}
::-webkit-scrollbar-thumb {{ background: var(--border-accent); border-radius: 3px; }}

/* Responsive */
@media (max-width: 900px) {{ .grid {{ grid-template-columns: 1fr; }} }}
</style>
</head>
<body>

<div class="header">
  <div class="header-left">
    <div class="header-title">Human <span>Arbiter</span> View</div>
    <div class="header-sub">AI systems thinking in real time</div>
  </div>
  <div class="header-right">
    <span class="live-dot" id="conn-dot" style="background:var(--yellow)"></span>
    <span id="status-text">Connecting...</span>
    <span id="clock" style="font-family:monospace;color:var(--cyan)">--:--:--</span>
  </div>
</div>

<div class="grid">
  <div class="card" id="fleet-card">
    <div class="card-head">
      <h2>Fleet Constellation <span class="count" id="fleet-count">-- nodes</span></h2>
    </div>
    <div class="card-body" id="fleet-body">
      <div class="empty">Waiting for fleet data...</div>
    </div>
  </div>

  <div class="card" id="surgeon-card">
    <div class="card-head">
      <h2>Surgeon Cross-Examination <span class="count" id="surgeon-count">-- exams</span></h2>
    </div>
    <div class="card-body" id="surgeon-body">
      <div class="empty">Waiting for surgeon activity...</div>
    </div>
  </div>

  <div class="card" id="evidence-card">
    <div class="card-head">
      <h2>Evidence Chain <span class="count" id="evidence-count">-- entries</span></h2>
    </div>
    <div class="card-body" id="evidence-body">
      <div class="empty">Waiting for evidence data...</div>
    </div>
  </div>

  <div class="card" id="corr-card">
    <div class="card-head">
      <h2>Corrigibility Gauge <span class="count" id="corr-count">--</span></h2>
    </div>
    <div class="card-body" id="corr-body">
      <div class="empty">Waiting for corrigibility data...</div>
    </div>
  </div>
</div>

<div class="footer">
  Context DNA -- Human Arbiter View -- Auto-refreshes every 5s -- If it cannot be seen, it does not count as built
</div>

<script>
const DAEMON = 'http://127.0.0.1:{daemon_port}';
let lastData = null;

async function fetchJSON(url) {{
  try {{
    const r = await fetch(url, {{ signal: AbortSignal.timeout(4000) }});
    return await r.json();
  }} catch(e) {{ return null; }}
}}

function esc(s) {{ const d=document.createElement('div'); d.textContent=s||''; return d.innerHTML; }}

function timeAgo(ts) {{
  if (!ts) return '--';
  const s = Math.floor(Date.now()/1000 - ts);
  if (s < 0) return 'just now';
  if (s < 60) return s + 's ago';
  if (s < 3600) return Math.floor(s/60) + 'm ago';
  return Math.floor(s/3600) + 'h ago';
}}

function colorForScore(v) {{
  if (v >= 0.8) return 'var(--green)';
  if (v >= 0.6) return 'var(--yellow)';
  if (v >= 0.4) return 'var(--orange)';
  return 'var(--red)';
}}

// -- Fleet Constellation --
function renderFleet(health) {{
  if (!health) return;
  const nodes = health.nodes || [];
  const nodeId = health.node_id || health.nodeId || '?';
  const el = document.getElementById('fleet-body');
  document.getElementById('fleet-count').textContent = nodes.length + ' nodes';

  if (nodes.length === 0) {{
    // Build from peers if no nodes array
    const peers = health.peers || {{}};
    const peerList = Object.keys(peers);
    if (peerList.length === 0 && !health.node_id) {{
      el.innerHTML = '<div class="empty">No fleet nodes detected</div>';
      return;
    }}
    // Self + peers
    let html = '<div class="node-grid">';
    html += `<div class="node online self">
      <div class="node-pulse" style="background:var(--green)"></div>
      <div class="node-name">${{esc(nodeId)}}</div>
      <div class="node-status" style="color:var(--green)">SELF</div>
      <div class="node-meta">uptime: ${{health.uptime_s ? Math.floor(health.uptime_s) + 's' : '?'}}</div>
    </div>`;
    for (const p of peerList) {{
      const st = peers[p]?.status || 'unknown';
      const cls = st === 'online' ? 'online' : st === 'stale' ? 'stale' : 'offline';
      const color = cls === 'online' ? 'var(--green)' : cls === 'stale' ? 'var(--yellow)' : 'var(--red)';
      html += `<div class="node ${{cls}}">
        <div class="node-pulse" style="background:${{color}}"></div>
        <div class="node-name">${{esc(p)}}</div>
        <div class="node-status" style="color:${{color}}">${{esc(st.toUpperCase())}}</div>
      </div>`;
    }}
    html += '</div>';
    document.getElementById('fleet-count').textContent = (1 + peerList.length) + ' nodes';
    el.innerHTML = html;
    return;
  }}

  let html = '<div class="node-grid">';
  for (const n of nodes) {{
    const st = n.status || 'offline';
    const cls = (st === 'online' ? 'online' : st === 'stale' ? 'stale' : 'offline')
      + (n.is_self ? ' self' : '');
    const color = st === 'online' ? 'var(--green)' : st === 'stale' ? 'var(--yellow)' : 'var(--red)';
    html += `<div class="node ${{cls}}">
      <div class="node-pulse" style="background:${{color}}"></div>
      <div class="node-name">${{esc(n.id || n.node_id || '?')}}</div>
      <div class="node-status" style="color:${{color}}">${{esc(st.toUpperCase())}}</div>
      <div class="node-meta">${{n.sessions || 0}} sessions</div>
    </div>`;
  }}
  html += '</div>';

  // Recent messages
  const msgs = health.recent_messages || [];
  if (msgs.length > 0) {{
    html += '<div class="msg-flow">';
    for (const m of msgs.slice(0, 6)) {{
      html += `<div class="msg-item">
        <span>${{esc(m.from || '?')}}</span>
        <span class="msg-arrow">-></span>
        <span>${{esc(m.to || '?')}}</span>
        <span style="color:var(--text-muted);margin-left:auto">${{esc(m.type || '?')}} ${{timeAgo(m.timestamp)}}</span>
      </div>`;
    }}
    html += '</div>';
  }}

  el.innerHTML = html;
}}

// -- Surgeon Feed --
function renderSurgeons(data) {{
  const el = document.getElementById('surgeon-body');
  if (!data) {{
    el.innerHTML = '<div class="empty">Surgeon adapter offline</div>';
    return;
  }}

  let html = '<div class="surgeon-row">';
  const surgeons = {{ atlas: 'var(--blue)', cardiologist: 'var(--red)', neurologist: 'var(--purple)' }};
  const state = data.state || {{}};
  const counters = data.counters || {{}};

  for (const [name, color] of Object.entries(surgeons)) {{
    const ok = data.available;
    const dotColor = ok ? 'var(--green)' : 'var(--red)';
    html += `<div class="surgeon-badge">
      <span class="dot" style="background:${{dotColor}}"></span>
      <span style="color:${{color}};font-weight:600">${{name.charAt(0).toUpperCase() + name.slice(1)}}</span>
    </div>`;
  }}
  html += '</div>';

  // Show counters as exam feed
  const total = counters.cross_exams || counters.total_exams || 0;
  document.getElementById('surgeon-count').textContent = total + ' exams';

  if (total > 0 || Object.keys(counters).length > 0) {{
    html += '<div class="exam-feed">';
    for (const [k, v] of Object.entries(counters)) {{
      if (typeof v !== 'number') continue;
      const isDisagree = k.includes('disagree') || k.includes('dissent');
      html += `<div class="exam-entry ${{isDisagree ? 'dissent' : ''}}">
        <div class="exam-topic">${{esc(k.replace(/_/g, ' '))}}</div>
        <div class="exam-meta"><span style="font-weight:700">${{v}}</span></div>
      </div>`;
    }}
    html += '</div>';
  }} else {{
    html += '<div class="empty">No cross-examinations yet. Surgeons await tasks.</div>';
  }}

  el.innerHTML = html;
}}

// -- Evidence Timeline --
function renderEvidence(data) {{
  const el = document.getElementById('evidence-body');
  if (!data || !data.entries) {{
    el.innerHTML = '<div class="empty">Evidence ledger not available</div>';
    return;
  }}

  const entries = data.entries || [];
  const total = data.total || entries.length;
  document.getElementById('evidence-count').textContent = total + ' entries';

  const chainValid = data.chain_valid;
  let html = '';
  if (chainValid === true) {{
    html += `<span class="chain-badge chain-valid">Chain Intact -- ${{data.chain_checked || 0}} verified</span>`;
  }} else if (chainValid === false) {{
    html += '<span class="chain-badge chain-broken">Chain Broken</span>';
  }} else {{
    html += '<span class="chain-badge" style="background:rgba(100,116,139,0.12);color:var(--text-muted)">Unverified</span>';
  }}

  const typeColors = {{
    merge_decision: 'var(--blue)',
    lease_granted: 'var(--purple)',
    rebuttal_resolved: 'var(--cyan)',
    gains_gate_result: 'var(--green)',
    channel_repair: 'var(--orange)',
    consensus_reached: 'var(--pink)',
    human_decision: 'var(--yellow)',
  }};

  html += '<div class="timeline">';
  for (const e of entries.slice(0, 15)) {{
    const color = typeColors[e.event_type] || 'var(--text-muted)';
    const hash = (e.entry_hash || '').substring(0, 12);
    html += `<div class="tl-entry">
      <div class="tl-dot" style="background:${{color}}"></div>
      <div class="tl-content">
        <div class="tl-type" style="color:${{color}}">${{esc(e.event_type || '?')}}</div>
        <div class="tl-subject">${{esc(e.subject || '')}} <span style="color:var(--text-muted)">-- ${{esc(e.node_id || '?')}} -- ${{timeAgo(e.timestamp)}}</span></div>
        ${{hash ? `<div class="tl-hash">${{esc(hash)}}</div>` : ''}}
      </div>
    </div>`;
  }}
  if (entries.length === 0) {{
    html += '<div class="empty">No evidence entries yet</div>';
  }}
  html += '</div>';

  el.innerHTML = html;
}}

// -- Corrigibility Gauge --
function renderCorrigibility(data) {{
  const el = document.getElementById('corr-body');
  if (!data || data.score === undefined) {{
    el.innerHTML = '<div class="empty">Corrigibility data not available</div>';
    return;
  }}

  const score = data.score || 0;
  const color = colorForScore(score);
  const label = score >= 0.8 ? 'High' : score >= 0.6 ? 'Moderate' : score >= 0.4 ? 'Low' : 'Critical';
  document.getElementById('corr-count').textContent = label;

  let html = `<div class="gauge-center">
    <div class="gauge-score" style="color:${{color}}">${{Math.round(score * 100)}}%</div>
    <div class="gauge-label">${{label}} Corrigibility</div>
  </div>`;

  // Factors
  const factors = data.factors || {{}};
  if (Object.keys(factors).length > 0) {{
    html += '<div class="factor-list">';
    for (const [name, val] of Object.entries(factors)) {{
      const v = typeof val === 'number' ? val : 0;
      const fColor = colorForScore(v);
      html += `<div class="factor">
        <span class="factor-name">${{esc(name.replace(/_/g, ' '))}}</span>
        <div class="factor-bar"><div class="factor-fill" style="width:${{Math.round(v*100)}}%;background:${{fColor}}"></div></div>
        <span class="factor-pct" style="color:${{fColor}}">${{Math.round(v*100)}}%</span>
      </div>`;
    }}
    html += '</div>';
  }}

  // Stats
  const stats = data.stats || {{}};
  if (stats.total_decisions > 0) {{
    html += `<div style="margin-top:12px;padding-top:10px;border-top:1px solid var(--border);font-size:12px;color:var(--text-dim);display:flex;gap:16px">
      <span>${{stats.total_decisions}} decisions</span>
      <span style="color:var(--green)">${{stats.changed_by_surgeon || 0}} changed by surgeon</span>
      <span>${{stats.total_disagreements || 0}} disagreements</span>
    </div>`;
  }}

  // Disagreements
  const disagreements = data.recent_disagreements || [];
  if (disagreements.length > 0) {{
    html += '<div class="disagree-section">';
    html += '<div style="font-size:11px;color:var(--text-muted);text-transform:uppercase;margin-bottom:8px;letter-spacing:0.5px">Active Disagreements</div>';
    for (const d of disagreements.slice(0, 5)) {{
      html += `<div class="disagree-item">
        <div style="font-weight:600;color:var(--orange);margin-bottom:4px">${{esc(d.topic || '?')}}</div>
        <div style="color:var(--text-dim)">A: ${{esc((d.surgeon_a_position || '').substring(0, 100))}}</div>
        <div style="color:var(--text-dim)">B: ${{esc((d.surgeon_b_position || '').substring(0, 100))}}</div>
        <div style="color:var(--green);margin-top:4px">Resolution: ${{esc(d.resolution || 'pending')}}</div>
      </div>`;
    }}
    html += '</div>';
  }}

  el.innerHTML = html;
}}

// -- Main Poll --
async function refresh() {{
  const clock = document.getElementById('clock');
  clock.textContent = new Date().toLocaleTimeString();

  // Fetch all data sources in parallel
  const [health, theatrical, surgeon, evidence] = await Promise.all([
    fetchJSON(DAEMON + '/health'),
    fetchJSON(DAEMON + '/theatrical/json'),
    fetchJSON(DAEMON + '/api/surgeon-status').catch(() => null),
    fetchJSON(DAEMON + '/evidence/latest').catch(() => null),
  ]);

  // Connection status
  const dot = document.getElementById('conn-dot');
  const st = document.getElementById('status-text');
  if (health) {{
    dot.style.background = 'var(--green)';
    st.textContent = 'Connected';
  }} else {{
    dot.style.background = 'var(--red)';
    st.textContent = 'Disconnected';
  }}

  // Fleet
  renderFleet(health);

  // Surgeons -- try web server API first, fall back to theatrical data
  if (surgeon) {{
    renderSurgeons(surgeon);
  }} else if (theatrical && theatrical.surgeon) {{
    renderSurgeons({{ available: true, state: {{}}, counters: theatrical.surgeon }});
  }} else {{
    renderSurgeons(null);
  }}

  // Evidence -- try direct, fall back to theatrical
  if (evidence && evidence.entries) {{
    renderEvidence(evidence);
  }} else if (theatrical && theatrical.evidence) {{
    renderEvidence(theatrical.evidence);
  }} else {{
    renderEvidence(null);
  }}

  // Corrigibility -- from theatrical data
  if (theatrical && theatrical.corrigibility) {{
    renderCorrigibility(theatrical.corrigibility);
  }} else {{
    renderCorrigibility(null);
  }}
}}

// Start
refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>'''
