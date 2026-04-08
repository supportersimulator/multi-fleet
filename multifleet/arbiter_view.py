"""Human Arbiter View — decision interface when machines disagree.

When a fleet_race completes with low confidence (<0.8) or a rebuttal
is unresolved, the Human Arbiter View presents Aaron with:
  - Winner recommendation + reasoning
  - Alternative approaches as cards
  - Unresolved dissent from 3-surgeon cross-exam
  - Full evidence chain with hash verification
  - One-click action buttons (accept, override, request more info)

Superset dark theme (#1a1a2e). Self-contained HTML (no external deps).

Usage:
    python3 -m multifleet.arbiter_view           # Serve on :8857
    python3 -m multifleet.arbiter_view --port 9091

Or via CLI:
    fleet arbiter

Or embedded in daemon:
    GET /arbiter       -> serves the HTML page
    GET /arbiter/pending -> list pending decisions
    POST /arbiter/decide -> record human decision
"""

import argparse
import json
import logging
import os
import time
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional

logger = logging.getLogger("fleet-nerve.arbiter-view")

DAEMON_PORT = int(os.environ.get("FLEET_DAEMON_PORT", "8855"))
EVENTBUS_PORT = int(os.environ.get("FLEET_EVENTBUS_PORT", "8031"))
DEFAULT_ARBITER_PORT = 8857

# ── In-memory arbiter state ──────────────────────────────────────────

_pending_decisions: dict[str, dict] = {}  # decision_id -> decision state
_decided: dict[str, dict] = {}  # decision_id -> decided state


def _fetch_daemon(path: str, port: int = DAEMON_PORT, timeout: float = 3.0) -> Optional[dict]:
    """Fetch JSON from daemon. Returns None on failure."""
    try:
        url = f"http://127.0.0.1:{port}{path}"
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def register_pending_decision(decision: dict) -> str:
    """Register a new decision awaiting human input.

    Args:
        decision: Dict with keys:
            - decision_id: unique id
            - type: "race" or "rebuttal"
            - race_id / rebuttal_id: source identifier
            - task: task description
            - recommendation: {branch, node, confidence, reasoning, evidence}
            - alternatives: [{branch, node, summary, strengths, weaknesses, probe_health}]
            - dissent: [{surgeon, concern, severity, evidence_refs}]
            - synthesis: full synthesis dict
            - created_at: timestamp

    Returns:
        The decision_id.
    """
    did = decision.get("decision_id", f"arb-{int(time.time())}")
    decision["decision_id"] = did
    decision.setdefault("created_at", time.time())
    decision.setdefault("status", "awaiting_human")
    _pending_decisions[did] = decision
    logger.info("Arbiter: registered pending decision %s (type=%s)",
                did, decision.get("type", "?"))
    return did


def get_pending_decisions() -> list[dict]:
    """Return all pending decisions."""
    return list(_pending_decisions.values())


def get_decision(decision_id: str) -> Optional[dict]:
    """Get a specific pending or decided decision."""
    return _pending_decisions.get(decision_id) or _decided.get(decision_id)


def record_decision(decision_id: str, action: str, details: dict = None) -> dict:
    """Record a human decision.

    Args:
        decision_id: The pending decision to resolve.
        action: "accept_recommendation", "accept_alternative", "request_info", "override"
        details: Additional details (alternative_index, custom_text, etc.)

    Returns:
        The resolved decision dict.
    """
    pending = _pending_decisions.get(decision_id)
    if not pending:
        return {"error": f"no pending decision: {decision_id}"}

    resolved = {
        **pending,
        "status": "decided",
        "action": action,
        "action_details": details or {},
        "decided_at": time.time(),
        "decision_latency_s": round(time.time() - pending.get("created_at", time.time()), 1),
    }

    # Record in evidence ledger
    _record_evidence("human_decision", {
        "decision_id": decision_id,
        "type": pending.get("type", "unknown"),
        "action": action,
        "race_id": pending.get("race_id"),
        "rebuttal_id": pending.get("rebuttal_id"),
        "confidence": pending.get("recommendation", {}).get("confidence", 0),
        "latency_s": resolved["decision_latency_s"],
        "details": details or {},
    })

    _decided[decision_id] = resolved
    del _pending_decisions[decision_id]
    logger.info("Arbiter: decision %s resolved with action=%s (%.1fs)",
                decision_id, action, resolved["decision_latency_s"])
    return resolved


def _record_evidence(event_type: str, payload: dict):
    """Record evidence via daemon."""
    try:
        data = json.dumps({
            "event_type": event_type,
            "node_id": os.environ.get("MULTIFLEET_NODE_ID", "unknown"),
            "subject": f"arbiter:{payload.get('decision_id', '?')}",
            "payload": payload,
        }).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{DAEMON_PORT}/evidence",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=3)
    except Exception as e:
        logger.debug("Evidence record failed: %s", e)


def create_pending_from_race(race: dict) -> Optional[str]:
    """Create a pending arbiter decision from a completed race.

    Called by fleet_race._run_synthesis when confidence < 0.8.
    Returns decision_id or None if race not eligible.
    """
    synthesis = race.get("synthesis", {})
    confidence = synthesis.get("confidence", 1.0)
    if confidence >= 0.8 and synthesis.get("decision") != "needs_rebuttal":
        return None

    # Build recommendation
    recommendation = {
        "branch": synthesis.get("winner_branch", ""),
        "node": race.get("winner", ""),
        "confidence": confidence,
        "reasoning": synthesis.get("reasoning", ""),
        "evidence": synthesis.get("chief_decision_packet", {}),
    }

    # Build alternatives from non-winner participants
    alternatives = []
    winner = race.get("winner", "")
    for node, p in race.get("participants", {}).items():
        if node == winner or p.get("status") != "completed":
            continue
        result = p.get("result", {}) or {}
        alternatives.append({
            "branch": p.get("branch", ""),
            "node": node,
            "summary": result.get("summary", "No summary"),
            "strengths": result.get("strengths", []),
            "weaknesses": result.get("risks", []),
            "probe_health": result.get("confidence", 0.5),
            "why_not_recommended": "",  # filled by synthesis dissent
        })

    # Build dissent
    dissent = []
    for d in synthesis.get("dissent_preserved", []):
        dissent.append({
            "surgeon": d.get("surgeon", d.get("machine", "unknown")),
            "concern": d.get("claim", d.get("concern", "")),
            "severity": d.get("severity", "medium"),
            "evidence_refs": d.get("evidence_refs", []),
        })

    decision = {
        "type": "race",
        "race_id": race.get("race_id", ""),
        "task": race.get("task", ""),
        "recommendation": recommendation,
        "alternatives": alternatives,
        "dissent": dissent,
        "synthesis": synthesis,
        "duration_s": round(time.time() - race.get("started_at", time.time()), 1),
    }

    return register_pending_decision(decision)


# ── HTML Builder ──────────────────────────────────────────────────────

def build_arbiter_html(daemon_port: int = DAEMON_PORT) -> str:
    """Build the self-contained Arbiter View HTML page."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fleet Arbiter -- Human Decision Interface</title>
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
  .topbar-badge {{
    font-size: 12px;
    background: var(--orange);
    color: #000;
    padding: 2px 8px;
    border-radius: 10px;
    font-weight: 600;
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

  /* ── Container ── */
  .container {{
    max-width: 1200px;
    margin: 0 auto;
    padding: 20px 24px;
  }}

  /* ── Decision Header ── */
  .decision-header {{
    background: var(--bg-secondary);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 24px;
    margin-bottom: 20px;
  }}
  .decision-header .status {{
    font-size: 14px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 1px;
    margin-bottom: 8px;
  }}
  .decision-header .status.awaiting {{ color: var(--orange); }}
  .decision-header .status.decided {{ color: var(--green); }}
  .decision-header .task {{ font-size: 22px; font-weight: 700; margin-bottom: 8px; }}
  .decision-header .meta {{
    display: flex;
    gap: 20px;
    font-size: 13px;
    color: var(--text-secondary);
  }}
  .decision-header .meta span {{ display: flex; align-items: center; gap: 4px; }}

  /* ── No Decisions ── */
  .no-decisions {{
    text-align: center;
    padding: 80px 24px;
    color: var(--text-secondary);
  }}
  .no-decisions h2 {{
    font-size: 24px;
    color: var(--green);
    margin-bottom: 12px;
  }}
  .no-decisions p {{ font-size: 15px; }}

  /* ── Panels ── */
  .panels {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 16px;
    margin-bottom: 20px;
  }}
  .panel-wide {{
    grid-column: 1 / -1;
  }}

  .panel {{
    background: var(--bg-secondary);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    overflow: hidden;
  }}
  .panel-header {{
    padding: 14px 18px;
    font-size: 14px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    gap: 8px;
  }}
  .panel-body {{
    padding: 16px 18px;
  }}

  /* ── Recommendation ── */
  .recommendation {{
    border-left: 3px solid var(--green);
  }}
  .rec-confidence {{
    display: inline-block;
    padding: 4px 12px;
    border-radius: 12px;
    font-size: 14px;
    font-weight: 700;
    margin-bottom: 12px;
  }}
  .conf-high {{ background: var(--green-dim); color: var(--green); }}
  .conf-mid {{ background: var(--yellow-dim); color: var(--yellow); }}
  .conf-low {{ background: var(--red-dim); color: var(--red); }}

  .rec-reasoning {{
    font-size: 14px;
    line-height: 1.6;
    color: var(--text-primary);
    margin-bottom: 12px;
  }}
  .rec-meta {{
    font-size: 13px;
    color: var(--text-secondary);
  }}
  .rec-meta span {{ margin-right: 16px; }}

  /* ── Alternative Cards ── */
  .alt-card {{
    background: var(--bg-surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 14px 16px;
    margin-bottom: 10px;
    transition: border-color 0.15s;
  }}
  .alt-card:hover {{
    border-color: var(--accent-dim);
  }}
  .alt-card .alt-header {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 8px;
  }}
  .alt-card .alt-node {{
    font-size: 13px;
    font-weight: 600;
    color: var(--accent);
  }}
  .alt-card .alt-health {{
    font-size: 12px;
    padding: 2px 8px;
    border-radius: 10px;
  }}
  .alt-card .alt-summary {{
    font-size: 13px;
    color: var(--text-secondary);
    margin-bottom: 8px;
    line-height: 1.5;
  }}
  .alt-card .alt-tags {{
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }}
  .tag {{
    font-size: 11px;
    padding: 2px 8px;
    border-radius: 4px;
    background: var(--bg-card);
    color: var(--text-secondary);
  }}
  .tag.strength {{ background: var(--green-dim); color: var(--green); }}
  .tag.weakness {{ background: var(--red-dim); color: var(--red); }}

  /* ── Dissent ── */
  .dissent-item {{
    background: var(--bg-surface);
    border-left: 3px solid var(--yellow);
    border-radius: 0 var(--radius) var(--radius) 0;
    padding: 12px 14px;
    margin-bottom: 10px;
  }}
  .dissent-item.critical {{ border-left-color: var(--red); }}
  .dissent-item .surgeon {{
    font-size: 12px;
    font-weight: 600;
    color: var(--purple);
    margin-bottom: 4px;
  }}
  .dissent-item .concern {{
    font-size: 13px;
    line-height: 1.5;
    color: var(--text-primary);
  }}
  .dissent-item .severity-tag {{
    font-size: 11px;
    padding: 1px 6px;
    border-radius: 4px;
    margin-left: 8px;
  }}
  .sev-critical {{ background: var(--red-dim); color: var(--red); }}
  .sev-high {{ background: var(--orange); color: #000; }}
  .sev-medium {{ background: var(--yellow-dim); color: var(--yellow); }}
  .sev-low {{ background: var(--bg-card); color: var(--text-secondary); }}

  /* ── Evidence Chain ── */
  .evidence-list {{
    max-height: 300px;
    overflow-y: auto;
  }}
  .evidence-entry {{
    display: grid;
    grid-template-columns: auto 1fr auto;
    gap: 12px;
    align-items: center;
    padding: 8px 0;
    border-bottom: 1px solid rgba(42,58,90,0.5);
    font-size: 13px;
  }}
  .evidence-entry:last-child {{ border-bottom: none; }}
  .ev-type {{
    font-size: 11px;
    padding: 2px 6px;
    border-radius: 4px;
    background: var(--bg-card);
    color: var(--accent);
    font-weight: 600;
    white-space: nowrap;
  }}
  .ev-info {{ color: var(--text-secondary); }}
  .ev-hash {{
    font-family: 'SF Mono', 'Fira Code', monospace;
    font-size: 11px;
    color: var(--text-muted);
  }}
  .chain-status {{
    padding: 8px 12px;
    border-radius: var(--radius);
    font-size: 13px;
    font-weight: 600;
    margin-bottom: 12px;
  }}
  .chain-valid {{ background: var(--green-dim); color: var(--green); }}
  .chain-broken {{ background: var(--red-dim); color: var(--red); }}

  /* ── Action Buttons ── */
  .actions {{
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
    padding: 24px;
    background: var(--bg-secondary);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    margin-bottom: 20px;
  }}
  .action-btn {{
    padding: 10px 20px;
    border: none;
    border-radius: var(--radius);
    font-size: 14px;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.15s;
    font-family: inherit;
  }}
  .action-btn:hover {{ transform: translateY(-1px); box-shadow: 0 4px 12px rgba(0,0,0,0.3); }}
  .action-btn:active {{ transform: translateY(0); }}
  .btn-accept {{ background: var(--green); color: #000; }}
  .btn-alt {{ background: var(--accent); color: #000; }}
  .btn-info {{ background: var(--yellow); color: #000; }}
  .btn-override {{ background: var(--orange); color: #000; }}
  .btn-disabled {{ background: var(--bg-surface); color: var(--text-muted); cursor: not-allowed; }}
  .btn-disabled:hover {{ transform: none; box-shadow: none; }}

  /* ── Override Modal ── */
  .modal-overlay {{
    display: none;
    position: fixed;
    top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(0,0,0,0.6);
    z-index: 200;
    align-items: center;
    justify-content: center;
  }}
  .modal-overlay.visible {{ display: flex; }}
  .modal {{
    background: var(--bg-secondary);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 24px;
    width: 90%;
    max-width: 500px;
  }}
  .modal h3 {{ margin-bottom: 12px; }}
  .modal textarea {{
    width: 100%;
    height: 100px;
    background: var(--bg-surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    color: var(--text-primary);
    padding: 10px;
    font-family: inherit;
    font-size: 14px;
    resize: vertical;
    margin-bottom: 12px;
  }}
  .modal textarea:focus {{ outline: none; border-color: var(--accent); }}
  .modal-actions {{ display: flex; gap: 10px; justify-content: flex-end; }}

  /* ── Decision List ── */
  .decision-list {{
    margin-bottom: 20px;
  }}
  .decision-list-item {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 12px 16px;
    background: var(--bg-secondary);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    margin-bottom: 8px;
    cursor: pointer;
    transition: border-color 0.15s;
  }}
  .decision-list-item:hover {{ border-color: var(--accent-dim); }}
  .decision-list-item.active {{ border-color: var(--accent); }}
  .dli-info {{ display: flex; flex-direction: column; gap: 2px; }}
  .dli-task {{ font-size: 14px; font-weight: 600; }}
  .dli-meta {{ font-size: 12px; color: var(--text-secondary); }}
  .dli-status {{
    font-size: 12px;
    padding: 3px 10px;
    border-radius: 10px;
    font-weight: 600;
  }}

  /* ── Toast ── */
  .toast {{
    position: fixed;
    bottom: 24px;
    right: 24px;
    padding: 12px 20px;
    border-radius: var(--radius);
    font-size: 14px;
    font-weight: 600;
    z-index: 300;
    transition: opacity 0.3s;
    opacity: 0;
    pointer-events: none;
  }}
  .toast.visible {{ opacity: 1; pointer-events: auto; }}
  .toast.success {{ background: var(--green); color: #000; }}
  .toast.error {{ background: var(--red); color: #fff; }}

  /* ── Responsive ── */
  @media (max-width: 768px) {{
    .panels {{ grid-template-columns: 1fr; }}
    .actions {{ flex-direction: column; }}
    .decision-header .meta {{ flex-wrap: wrap; }}
  }}
</style>
</head>
<body>

<!-- Top Bar -->
<div class="topbar">
  <div class="topbar-left">
    <span class="topbar-title">Fleet Arbiter</span>
    <span class="topbar-badge" id="pending-count">0 PENDING</span>
  </div>
  <div class="topbar-right">
    <span><span class="live-dot" id="conn-dot" style="background:var(--yellow)"></span> Polling</span>
    <span id="last-update">--</span>
  </div>
</div>

<div class="container">

  <!-- Decision List (when multiple pending) -->
  <div id="decision-list" class="decision-list" style="display:none"></div>

  <!-- No Decisions State -->
  <div id="no-decisions" class="no-decisions">
    <h2>All Clear</h2>
    <p>No decisions awaiting human input. The fleet is in consensus.</p>
    <p style="margin-top:12px;font-size:13px;color:var(--text-muted)">
      This page auto-refreshes every 3 seconds. Decisions appear when a race
      completes with confidence &lt;0.8 or a rebuttal remains unresolved.
    </p>
  </div>

  <!-- Active Decision Detail -->
  <div id="decision-detail" style="display:none">

    <!-- Decision Header -->
    <div class="decision-header" id="dec-header">
      <div class="status awaiting" id="dec-status">AWAITING HUMAN DECISION</div>
      <div class="task" id="dec-task">--</div>
      <div class="meta">
        <span id="dec-id">--</span>
        <span id="dec-type">--</span>
        <span id="dec-time">--</span>
        <span id="dec-duration">--</span>
      </div>
    </div>

    <div class="panels">

      <!-- Recommendation Panel -->
      <div class="panel recommendation">
        <div class="panel-header">Recommendation</div>
        <div class="panel-body">
          <div class="rec-confidence" id="rec-conf">--</div>
          <div class="rec-reasoning" id="rec-reasoning">--</div>
          <div class="rec-meta">
            <span id="rec-branch">Branch: --</span>
            <span id="rec-node">Node: --</span>
          </div>
        </div>
      </div>

      <!-- Dissent Panel -->
      <div class="panel">
        <div class="panel-header">Dissent &amp; Objections</div>
        <div class="panel-body" id="dissent-body">
          <p style="color:var(--text-muted);font-size:13px">No dissent recorded.</p>
        </div>
      </div>

      <!-- Alternatives Panel -->
      <div class="panel panel-wide">
        <div class="panel-header">Alternative Approaches</div>
        <div class="panel-body" id="alternatives-body">
          <p style="color:var(--text-muted);font-size:13px">No alternatives available.</p>
        </div>
      </div>

      <!-- Evidence Chain -->
      <div class="panel panel-wide">
        <div class="panel-header">Evidence Chain</div>
        <div class="panel-body">
          <div class="chain-status chain-valid" id="chain-status">CHAIN: LOADING...</div>
          <div class="evidence-list" id="evidence-list">
            <p style="color:var(--text-muted);font-size:13px">Loading evidence...</p>
          </div>
        </div>
      </div>
    </div>

    <!-- Action Buttons -->
    <div class="actions" id="action-bar">
      <button class="action-btn btn-accept" onclick="doAction('accept_recommendation')">Accept Recommendation</button>
      <div id="alt-buttons"></div>
      <button class="action-btn btn-info" onclick="doAction('request_info')">Request More Info</button>
      <button class="action-btn btn-override" onclick="showOverrideModal()">Override: Custom</button>
    </div>

  </div>
</div>

<!-- Override Modal -->
<div class="modal-overlay" id="override-modal">
  <div class="modal">
    <h3>Custom Override</h3>
    <p style="font-size:13px;color:var(--text-secondary);margin-bottom:12px">
      Provide your decision. This will be recorded in the evidence ledger.
    </p>
    <textarea id="override-text" placeholder="Describe your decision..."></textarea>
    <div class="modal-actions">
      <button class="action-btn" style="background:var(--bg-surface);color:var(--text-primary)" onclick="hideOverrideModal()">Cancel</button>
      <button class="action-btn btn-override" onclick="submitOverride()">Submit Override</button>
    </div>
  </div>
</div>

<!-- Toast -->
<div class="toast" id="toast"></div>

<script>
const DAEMON = 'http://127.0.0.1:{daemon_port}';
let currentDecision = null;
let pendingDecisions = [];
let pollInterval = null;

// ── Polling ──

async function fetchJSON(url) {{
  try {{
    const r = await fetch(url, {{signal: AbortSignal.timeout(5000)}});
    return await r.json();
  }} catch(e) {{
    return null;
  }}
}}

async function postJSON(url, body) {{
  try {{
    const r = await fetch(url, {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(10000),
    }});
    return await r.json();
  }} catch(e) {{
    return {{error: e.message}};
  }}
}}

async function poll() {{
  // Fetch pending decisions
  const pending = await fetchJSON(DAEMON + '/arbiter/pending');
  if (pending && pending.decisions) {{
    pendingDecisions = pending.decisions;
  }}

  // Also check races for any awaiting_arbiter
  const races = await fetchJSON(DAEMON + '/races');
  if (races && races.races) {{
    for (const r of races.races) {{
      if (r.status === 'awaiting_arbiter' && !pendingDecisions.find(d => d.race_id === r.race_id)) {{
        // Race needs arbiter but not yet registered -- fetch full details
        const full = await fetchJSON(DAEMON + '/race/' + r.race_id);
        if (full && !full.error) {{
          pendingDecisions.push(buildDecisionFromRace(full));
        }}
      }}
    }}
  }}

  updateUI();
  document.getElementById('last-update').textContent = new Date().toLocaleTimeString();
  document.getElementById('conn-dot').style.background =
    (pending || races) ? 'var(--green)' : 'var(--red)';
}}

function buildDecisionFromRace(race) {{
  const syn = race.synthesis || {{}};
  return {{
    decision_id: 'race-' + race.race_id,
    type: 'race',
    race_id: race.race_id,
    task: race.task || '--',
    status: 'awaiting_human',
    created_at: race.started_at,
    duration_s: syn.elapsed_s || 0,
    recommendation: {{
      branch: syn.winner_branch || '',
      node: race.winner || '',
      confidence: syn.confidence || 0,
      reasoning: syn.reasoning || 'No reasoning available.',
    }},
    alternatives: Object.entries(race.participants || {{}}).filter(
      ([n, p]) => n !== race.winner && p.status === 'completed'
    ).map(([n, p]) => ({{
      branch: p.branch || '',
      node: n,
      summary: (p.result || {{}}).summary || 'No summary',
      strengths: (p.result || {{}}).strengths || [],
      weaknesses: (p.result || {{}}).risks || [],
      probe_health: (p.result || {{}}).confidence || 0.5,
    }})),
    dissent: (syn.dissent_preserved || []).map(d => ({{
      surgeon: d.surgeon || d.machine || 'unknown',
      concern: d.claim || d.concern || '',
      severity: d.severity || 'medium',
    }})),
    synthesis: syn,
  }};
}}

// ── UI Rendering ──

function updateUI() {{
  const badge = document.getElementById('pending-count');
  badge.textContent = pendingDecisions.length + ' PENDING';

  if (pendingDecisions.length === 0) {{
    document.getElementById('no-decisions').style.display = 'block';
    document.getElementById('decision-detail').style.display = 'none';
    document.getElementById('decision-list').style.display = 'none';
    return;
  }}

  document.getElementById('no-decisions').style.display = 'none';

  // Show list if multiple
  if (pendingDecisions.length > 1) {{
    document.getElementById('decision-list').style.display = 'block';
    renderDecisionList();
  }} else {{
    document.getElementById('decision-list').style.display = 'none';
  }}

  // Auto-select first if none selected
  if (!currentDecision || !pendingDecisions.find(d => d.decision_id === currentDecision.decision_id)) {{
    currentDecision = pendingDecisions[0];
  }}

  renderDecisionDetail(currentDecision);
  document.getElementById('decision-detail').style.display = 'block';
}}

function renderDecisionList() {{
  const el = document.getElementById('decision-list');
  el.innerHTML = pendingDecisions.map(d => `
    <div class="decision-list-item ${{currentDecision && currentDecision.decision_id === d.decision_id ? 'active' : ''}}"
         onclick="selectDecision('${{d.decision_id}}')">
      <div class="dli-info">
        <div class="dli-task">${{d.task || '--'}}</div>
        <div class="dli-meta">${{d.type}} | ${{d.decision_id}} | ${{timeAgo(d.created_at)}}</div>
      </div>
      <span class="dli-status" style="background:var(--orange);color:#000">${{d.status}}</span>
    </div>
  `).join('');
}}

function renderDecisionDetail(dec) {{
  if (!dec) return;

  // Header
  document.getElementById('dec-status').textContent =
    dec.status === 'awaiting_human' ? 'AWAITING HUMAN DECISION' : dec.status.toUpperCase();
  document.getElementById('dec-status').className =
    'status ' + (dec.status === 'awaiting_human' ? 'awaiting' : 'decided');
  document.getElementById('dec-task').textContent = dec.task || '--';
  document.getElementById('dec-id').textContent = dec.decision_id || '--';
  document.getElementById('dec-type').textContent = (dec.type || '--').toUpperCase();
  document.getElementById('dec-time').textContent = dec.created_at ? new Date(dec.created_at * 1000).toLocaleString() : '--';
  document.getElementById('dec-duration').textContent = dec.duration_s ? dec.duration_s + 's' : '--';

  // Recommendation
  const rec = dec.recommendation || {{}};
  const conf = rec.confidence || 0;
  const confEl = document.getElementById('rec-conf');
  confEl.textContent = (conf * 100).toFixed(0) + '% confidence';
  confEl.className = 'rec-confidence ' +
    (conf >= 0.8 ? 'conf-high' : conf >= 0.6 ? 'conf-mid' : 'conf-low');
  document.getElementById('rec-reasoning').textContent = rec.reasoning || 'No reasoning provided.';
  document.getElementById('rec-branch').textContent = 'Branch: ' + (rec.branch || '--');
  document.getElementById('rec-node').textContent = 'Node: ' + (rec.node || '--');

  // Dissent
  const dissentBody = document.getElementById('dissent-body');
  const dissents = dec.dissent || [];
  if (dissents.length === 0) {{
    dissentBody.innerHTML = '<p style="color:var(--text-muted);font-size:13px">No dissent recorded. Full consensus.</p>';
  }} else {{
    dissentBody.innerHTML = dissents.map(d => `
      <div class="dissent-item ${{d.severity === 'critical' ? 'critical' : ''}}">
        <div class="surgeon">${{d.surgeon}}
          <span class="severity-tag sev-${{d.severity || 'medium'}}">${{d.severity || 'medium'}}</span>
        </div>
        <div class="concern">${{d.concern}}</div>
      </div>
    `).join('');
  }}

  // Alternatives
  const altBody = document.getElementById('alternatives-body');
  const alts = dec.alternatives || [];
  const altBtns = document.getElementById('alt-buttons');
  if (alts.length === 0) {{
    altBody.innerHTML = '<p style="color:var(--text-muted);font-size:13px">No alternatives -- single completion.</p>';
    altBtns.innerHTML = '';
  }} else {{
    altBody.innerHTML = alts.map((a, i) => `
      <div class="alt-card">
        <div class="alt-header">
          <span class="alt-node">${{a.node}} -- ${{a.branch || 'unknown branch'}}</span>
          <span class="alt-health" style="background:${{a.probe_health >= 0.7 ? 'var(--green-dim)' : 'var(--yellow-dim)'}};
            color:${{a.probe_health >= 0.7 ? 'var(--green)' : 'var(--yellow)'}}">${{(a.probe_health * 100).toFixed(0)}}% health</span>
        </div>
        <div class="alt-summary">${{a.summary}}</div>
        <div class="alt-tags">
          ${{(a.strengths || []).map(s => '<span class="tag strength">+ ' + s + '</span>').join('')}}
          ${{(a.weaknesses || []).map(w => '<span class="tag weakness">- ' + w + '</span>').join('')}}
        </div>
      </div>
    `).join('');
    altBtns.innerHTML = alts.map((a, i) =>
      `<button class="action-btn btn-alt" onclick="doAction('accept_alternative', {{alternative_index: ${{i}}, node: '${{a.node}}'}})">Accept ${{a.node}}</button>`
    ).join('');
  }}

  // Evidence
  loadEvidence(dec);

  // Disable actions if already decided
  const actionBar = document.getElementById('action-bar');
  if (dec.status === 'decided') {{
    actionBar.querySelectorAll('.action-btn').forEach(b => {{
      b.classList.add('btn-disabled');
      b.disabled = true;
    }});
  }}
}}

async function loadEvidence(dec) {{
  const chainEl = document.getElementById('chain-status');
  const listEl = document.getElementById('evidence-list');

  // Fetch evidence chain
  const evidence = await fetchJSON(DAEMON + '/evidence/latest');
  const verify = await fetchJSON(DAEMON + '/evidence/verify');

  if (verify) {{
    const valid = verify.valid !== false;
    chainEl.textContent = valid ? 'CHAIN: VALID (' + (verify.total || 0) + ' entries)' : 'CHAIN: BROKEN at entry ' + (verify.broken_at || '?');
    chainEl.className = 'chain-status ' + (valid ? 'chain-valid' : 'chain-broken');
  }} else {{
    chainEl.textContent = 'CHAIN: UNAVAILABLE';
    chainEl.className = 'chain-status chain-broken';
  }}

  if (evidence && evidence.entries) {{
    listEl.innerHTML = evidence.entries.map(e => `
      <div class="evidence-entry">
        <span class="ev-type">${{e.event_type}}</span>
        <span class="ev-info">${{e.node_id}} -- ${{e.subject || '--'}} -- ${{timeAgo(e.timestamp)}}</span>
        <span class="ev-hash">${{(e.entry_hash || '').substring(0, 12)}}...</span>
      </div>
    `).join('');
  }} else {{
    listEl.innerHTML = '<p style="color:var(--text-muted);font-size:13px">No evidence entries found.</p>';
  }}
}}

// ── Actions ──

async function doAction(action, details) {{
  if (!currentDecision) return;
  const result = await postJSON(DAEMON + '/arbiter/decide', {{
    decision_id: currentDecision.decision_id,
    action: action,
    details: details || {{}},
  }});

  if (result && !result.error) {{
    showToast('Decision recorded: ' + action, 'success');
    // Remove from pending
    pendingDecisions = pendingDecisions.filter(d => d.decision_id !== currentDecision.decision_id);
    currentDecision = pendingDecisions[0] || null;
    updateUI();
  }} else {{
    showToast('Error: ' + (result ? result.error : 'unknown'), 'error');
  }}
}}

function selectDecision(id) {{
  currentDecision = pendingDecisions.find(d => d.decision_id === id) || currentDecision;
  updateUI();
}}

function showOverrideModal() {{
  document.getElementById('override-modal').classList.add('visible');
  document.getElementById('override-text').focus();
}}

function hideOverrideModal() {{
  document.getElementById('override-modal').classList.remove('visible');
}}

async function submitOverride() {{
  const text = document.getElementById('override-text').value.trim();
  if (!text) return;
  hideOverrideModal();
  await doAction('override', {{custom_text: text}});
}}

// ── Helpers ──

function timeAgo(ts) {{
  if (!ts) return '--';
  const s = Math.floor(Date.now() / 1000 - ts);
  if (s < 60) return s + 's ago';
  if (s < 3600) return Math.floor(s/60) + 'm ago';
  if (s < 86400) return Math.floor(s/3600) + 'h ago';
  return Math.floor(s/86400) + 'd ago';
}}

function showToast(msg, type) {{
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast visible ' + type;
  setTimeout(() => {{ t.className = 'toast'; }}, 3000);
}}

// ── Init ──

poll();
pollInterval = setInterval(poll, 3000);
</script>
</body>
</html>"""


# ── HTTP Server (standalone mode) ─────────────────────────────────

class ArbiterHandler(BaseHTTPRequestHandler):
    """HTTP handler for standalone arbiter server."""

    daemon_port = DAEMON_PORT

    def log_message(self, *args):
        pass  # suppress access logs

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/arbiter"):
            html = build_arbiter_html(self.daemon_port)
            data = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif path == "/arbiter/pending":
            self._json_response(200, {
                "decisions": get_pending_decisions(),
                "count": len(_pending_decisions),
            })
        elif path == "/health":
            self._json_response(200, {
                "status": "ok",
                "type": "fleet-arbiter",
                "pending": len(_pending_decisions),
                "decided": len(_decided),
            })
        else:
            self.send_error(404, f"Not found: {path}")

    def do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}

        if path == "/arbiter/decide":
            decision_id = body.get("decision_id", "")
            action = body.get("action", "")
            details = body.get("details", {})
            if not decision_id or not action:
                self._json_response(400, {"error": "missing decision_id or action"})
                return
            result = record_decision(decision_id, action, details)
            code = 200 if "error" not in result else 404
            self._json_response(code, result)
        else:
            self.send_error(404, f"Not found: {path}")

    def _json_response(self, code, obj):
        data = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)


def serve(port: int = DEFAULT_ARBITER_PORT, daemon_port: int = DAEMON_PORT):
    """Start standalone arbiter web server."""
    ArbiterHandler.daemon_port = daemon_port
    server = HTTPServer(("127.0.0.1", port), ArbiterHandler)
    logger.info("Arbiter View serving on http://127.0.0.1:%d", port)
    print(f"Fleet Arbiter View: http://127.0.0.1:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    parser = argparse.ArgumentParser(description="Fleet Arbiter View")
    parser.add_argument("--port", type=int, default=DEFAULT_ARBITER_PORT)
    parser.add_argument("--daemon-port", type=int, default=DAEMON_PORT)
    args = parser.parse_args()
    serve(args.port, args.daemon_port)
