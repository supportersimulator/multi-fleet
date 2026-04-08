"""Fleet Dashboard -- renders fleet status as self-contained HTML.

5-Zone Theatrical Layout (Superset-inspired dark theme):
  Zone 0: Vital signs bar (top — probe health indicators)
  Zone 1: Node rail (left sidebar — per-node status cards)
  Zone 2: Local theater (center — current machine activity)
  Zone 3: Chief synthesis pane (right — decisions + evidence)
  Zone 4: Packet ledger (bottom — message flow + branch convergence)

All HTML/CSS/JS is inline — no external dependencies.
Auto-refreshes every 10s via JS fetch (graceful) with meta fallback at 30s.
"""

import html
import json
import time
from datetime import datetime, timezone
from typing import Any


def _esc(text: str) -> str:
    """HTML-escape a string."""
    return html.escape(str(text)) if text else ""


def _health_color(dot: str) -> str:
    """Map health dot name to hex color."""
    return {"green": "#22c55e", "yellow": "#eab308", "red": "#ef4444"}.get(dot, "#6b7280")


def _health_glow(dot: str) -> str:
    """CSS box-shadow glow for health status."""
    c = _health_color(dot)
    return f"0 0 8px {c}40, 0 0 2px {c}80"


def _format_ts(ts: Any) -> str:
    """Format unix timestamp to human-readable."""
    if not ts:
        return "\u2014"
    try:
        dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        return dt.strftime("%H:%M:%S UTC")
    except (ValueError, TypeError, OSError):
        return str(ts)


def _format_ts_short(ts: Any) -> str:
    """Format timestamp as HH:MM:SS only."""
    if not ts:
        return "\u2014"
    try:
        dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        return dt.strftime("%H:%M:%S")
    except (ValueError, TypeError, OSError):
        return str(ts)


# ── Zone 0: Vital Signs Bar ──


def _render_vital_signs(snapshot: dict) -> str:
    """Top bar with probe health indicators."""
    health = snapshot.get("health_dot", "red")
    node_id = _esc(snapshot.get("node_id", "unknown"))
    ts = _format_ts(snapshot.get("timestamp", time.time()))
    peers = snapshot.get("peers", {})
    reachable = peers.get("reachable", 0)
    total = peers.get("total", 0)
    daemon = snapshot.get("daemon", {})
    nats = daemon.get("nats", {})
    nats_ok = nats.get("connected", False) if isinstance(nats, dict) else daemon.get("nats_connected", False)
    liaison = snapshot.get("liaison", {})
    rebuttals = snapshot.get("rebuttals", {})
    leases = snapshot.get("leases", {})

    # Probe vitals from snapshot (if available)
    probes = snapshot.get("probes", {})
    probe_items = []
    probe_names = ["mirror", "deps", "stale", "failures", "invariants", "docs"]
    for name in probe_names:
        p = probes.get(name, {})
        status = p.get("status", "gray")
        count = p.get("count", 0)
        color = _health_color(status) if status != "gray" else "#374151"
        label = name[:6].upper()
        badge = f'<span class="vital-badge" style="color:{color}">{count}</span>' if count else ""
        pulse_class = " vital-pulse" if status == "red" else ""
        probe_items.append(
            f'<div class="vital-item{pulse_class}" title="{_esc(name)}">'
            f'<span class="vital-dot" style="background:{color};box-shadow:0 0 6px {color}60"></span>'
            f'<span class="vital-label">{label}</span>{badge}</div>'
        )

    probes_html = "".join(probe_items) if probe_items else ""

    # Status indicators
    nats_color = "#22c55e" if nats_ok else "#ef4444"
    fleet_text = f"F:{reachable}/{total}"
    reviews = liaison.get("pending_reviews", 0)
    unread = liaison.get("unread", 0)
    active_rebuttals = rebuttals.get("active", 0)
    active_leases = leases.get("active", 0)

    status_items = [
        f'<span class="vital-stat"><span class="vital-dot" style="background:{nats_color};box-shadow:0 0 6px {nats_color}60"></span>NATS</span>',
        f'<span class="vital-stat">{_esc(fleet_text)}</span>',
    ]
    if reviews:
        status_items.append(f'<span class="vital-stat warn">{reviews} review{"s" if reviews != 1 else ""}</span>')
    if unread:
        status_items.append(f'<span class="vital-stat">{unread} unread</span>')
    if active_rebuttals:
        status_items.append(f'<span class="vital-stat warn">{active_rebuttals} rebuttal{"s" if active_rebuttals != 1 else ""}</span>')
    if active_leases:
        status_items.append(f'<span class="vital-stat">{active_leases} lease{"s" if active_leases != 1 else ""}</span>')

    h_color = _health_color(health)
    return f"""<div id="zone0" class="zone-vital-signs">
  <div class="vital-header">
    <span class="vital-dot-lg" style="background:{h_color};box-shadow:{_health_glow(health)}"></span>
    <span class="vital-title">FLEET DASHBOARD</span>
    <span class="vital-node">{node_id}</span>
    <span class="vital-ts" id="last-update">{ts}</span>
  </div>
  <div class="vital-probes">{probes_html}</div>
  <div class="vital-status">{"".join(status_items)}</div>
  <div class="vital-tabs">
    <button class="tab active" data-tab="theater">Theater</button>
    <button class="tab" data-tab="metrics">Metrics</button>
    <button class="tab" data-tab="branches">Branches</button>
  </div>
</div>"""


# ── Zone 1: Node Rail ──


def _render_node_card(name: str, health: str, status: str, branch: str,
                      sessions: Any, nats_ok: bool, role: str = "",
                      is_self: bool = False) -> str:
    """Render a single node card for the rail."""
    h_color = _health_color(health)
    icon = {"mac1": "\U0001f3db", "mac2": "\u26a1", "mac3": "\U0001f52c"}.get(name, "\U0001f4e6")
    self_marker = ' <span class="self-marker">YOU</span>' if is_self else ""
    role_text = f'<div class="node-role">{_esc(role)}</div>' if role else ""
    return f"""<div class="node-card" style="border-left:3px solid {h_color}" data-node="{_esc(name)}">
  <div class="node-header">
    <span class="node-icon">{icon}</span>
    <span class="node-name">{_esc(name)}{self_marker}</span>
    <span class="node-dot" style="background:{h_color};box-shadow:0 0 6px {h_color}80"></span>
  </div>
  {role_text}
  <div class="node-meta">
    <span title="Branch">\U0001f33f {_esc(str(branch))}</span>
    <span title="Sessions">\U0001f4cb {_esc(str(sessions))}</span>
  </div>
  <div class="node-meta">
    <span title="NATS">NATS: {"ON" if nats_ok else "OFF"}</span>
    <span title="Status">{_esc(status)}</span>
  </div>
</div>"""


def _render_node_rail(snapshot: dict) -> str:
    """Left sidebar with per-node status cards."""
    daemon = snapshot.get("daemon", {})
    peers_info = snapshot.get("peers", {})
    node_id = snapshot.get("node_id", "unknown")
    health = snapshot.get("health_dot", "red")

    nats = daemon.get("nats", {})
    nats_ok = nats.get("connected", False) if isinstance(nats, dict) else daemon.get("nats_connected", False)

    cards = [_render_node_card(
        name=node_id,
        health=health,
        status=daemon.get("status", "unknown"),
        branch=daemon.get("branch", "\u2014"),
        sessions=daemon.get("sessions", "\u2014"),
        nats_ok=nats_ok,
        role=daemon.get("role", ""),
        is_self=True,
    )]

    # Peer cards
    peer_names = peers_info.get("peers", [])
    for pname in peer_names:
        cards.append(_render_node_card(
            name=pname,
            health="green",
            status="peer",
            branch="\u2014",
            sessions="\u2014",
            nats_ok=False,
            role="",
            is_self=False,
        ))

    reachable = peers_info.get("reachable", 0)
    total = peers_info.get("total", 0)
    return f"""<div id="zone1" class="zone-node-rail">
  <div class="rail-header">
    <span class="rail-title">NODES</span>
    <span class="rail-count">{reachable}/{total}</span>
  </div>
  {"".join(cards)}
</div>"""


# ── Zone 2: Local Theater ──


def _render_local_theater(snapshot: dict) -> str:
    """Center pane showing current machine activity."""
    node_id = _esc(snapshot.get("node_id", "unknown"))
    events = snapshot.get("recent_events", [])
    daemon = snapshot.get("daemon", {})

    # Agent sessions
    agent_sessions = snapshot.get("agent_sessions", [])
    session_items = []
    for sess in agent_sessions[:5]:
        if isinstance(sess, dict):
            sid = _esc(sess.get("id", "?")[:8])
            state = _esc(sess.get("state", "unknown"))
            task = _esc(sess.get("task", "")[:60])
            state_color = {"running": "#22c55e", "idle": "#94a3b8", "blocked": "#eab308", "completed": "#6366f1"}.get(state, "#94a3b8")
            session_items.append(
                f'<div class="session-item">'
                f'<span class="session-dot" style="background:{state_color}"></span>'
                f'<code>{sid}</code> <span class="session-state">{state}</span>'
                f'<span class="session-task">{task}</span></div>'
            )

    sessions_html = "".join(session_items) if session_items else '<div class="empty-state">No active agent sessions</div>'

    # Activity feed (events styled as terminal output)
    feed_items = []
    for ev in events[:12]:
        if isinstance(ev, dict):
            ts = _format_ts_short(ev.get("timestamp", ev.get("ts", "")))
            etype = _esc(ev.get("type", ev.get("event", ev.get("description", "?"))))
            detail = _esc(ev.get("detail", ev.get("message", "")))
            # Color code by event type
            type_colors = {
                "surgeon": "#a78bfa", "probe": "#fbbf24", "fleet": "#60a5fa",
                "phase": "#34d399", "heartbeat": "#475569", "ghost": "#2dd4bf",
            }
            color = "#94a3b8"
            for prefix, c in type_colors.items():
                if prefix in etype.lower():
                    color = c
                    break
            feed_items.append(
                f'<div class="feed-entry">'
                f'<code class="feed-ts">{ts}</code>'
                f'<span class="feed-type" style="color:{color}">{etype}</span>'
                f'<span class="feed-detail">{detail}</span></div>'
            )
        else:
            feed_items.append(f'<div class="feed-entry"><span class="feed-detail">{_esc(str(ev))}</span></div>')

    feed_html = "".join(feed_items) if feed_items else '<div class="empty-state">No recent activity</div>'

    # Probe findings (if available)
    findings = snapshot.get("probe_findings", [])
    findings_items = []
    for f in findings[:6]:
        if isinstance(f, dict):
            sev = f.get("severity", "info")
            probe_id = _esc(f.get("probe_id", "unknown"))
            msg = _esc(f.get("message", f.get("finding", ""))[:80])
            sev_color = {"critical": "#ef4444", "high": "#f97316", "medium": "#eab308", "low": "#22c55e", "info": "#94a3b8"}.get(sev, "#94a3b8")
            findings_items.append(
                f'<div class="finding-item">'
                f'<span class="finding-sev" style="color:{sev_color}">{_esc(sev.upper())}</span>'
                f'<span class="finding-probe">{probe_id}</span>'
                f'<span class="finding-msg">{msg}</span></div>'
            )
    findings_html = "".join(findings_items) if findings_items else '<div class="empty-state">No probe findings</div>'

    return f"""<div id="zone2" class="zone-local-theater">
  <div class="theater-header">
    <span class="theater-title">LOCAL THEATER</span>
    <span class="theater-node">{node_id}</span>
  </div>
  <div class="theater-section">
    <div class="section-label">AGENT SESSIONS</div>
    {sessions_html}
  </div>
  <div class="theater-section">
    <div class="section-label">PROBE FINDINGS</div>
    {findings_html}
  </div>
  <div class="theater-section theater-feed">
    <div class="section-label">ACTIVITY FEED</div>
    {feed_html}
  </div>
</div>"""


# ── Zone 3: Chief Synthesis Pane ──


def _render_chief_synthesis(snapshot: dict) -> str:
    """Right pane with decisions, evidence, rebuttals."""
    rebuttals = snapshot.get("rebuttals", {})
    active_rb = rebuttals.get("active", 0)
    total_rb = rebuttals.get("total", 0)
    leases = snapshot.get("leases", {})
    active_leases = leases.get("active", 0)
    held_by_me = leases.get("held_by_me", 0)

    # Decisions from evidence ledger (if available)
    decisions = snapshot.get("decisions", [])
    decision_items = []
    for d in decisions[:5]:
        if isinstance(d, dict):
            ts = _format_ts_short(d.get("timestamp", ""))
            choice = _esc(d.get("choice", d.get("decision", "?"))[:50])
            confidence = d.get("confidence", 0)
            conf_pct = int(float(confidence) * 100) if confidence else 0
            conf_color = "#22c55e" if conf_pct >= 80 else "#eab308" if conf_pct >= 50 else "#ef4444"
            decision_items.append(
                f'<div class="decision-item">'
                f'<code class="feed-ts">{ts}</code>'
                f'<span class="decision-choice">{choice}</span>'
                f'<div class="confidence-bar"><div class="confidence-fill" style="width:{conf_pct}%;background:{conf_color}"></div></div>'
                f'</div>'
            )
    decisions_html = "".join(decision_items) if decision_items else '<div class="empty-state">No recent decisions</div>'

    # Rebuttal details
    rebuttal_details = snapshot.get("rebuttal_details", [])
    rebuttal_items = []
    for r in rebuttal_details[:3]:
        if isinstance(r, dict):
            topic = _esc(r.get("topic", "?")[:40])
            state = _esc(r.get("state", "active"))
            turns = r.get("turns", 0)
            state_color = "#eab308" if state == "active" else "#22c55e"
            rebuttal_items.append(
                f'<div class="rebuttal-item">'
                f'<span class="rebuttal-state" style="color:{state_color}">{state}</span>'
                f'<span class="rebuttal-topic">{topic}</span>'
                f'<span class="rebuttal-turns">{turns} turns</span></div>'
            )
    rebuttals_html = "".join(rebuttal_items) if rebuttal_items else (
        f'<div class="empty-state">{active_rb} active / {total_rb} total</div>' if total_rb else '<div class="empty-state">No rebuttals</div>'
    )

    return f"""<div id="zone3" class="zone-chief-synthesis">
  <div class="chief-header">
    <span class="chief-title">CHIEF SYNTHESIS</span>
  </div>
  <div class="chief-section">
    <div class="section-label">DECISIONS</div>
    {decisions_html}
  </div>
  <div class="chief-section">
    <div class="section-label">REBUTTALS</div>
    {rebuttals_html}
  </div>
  <div class="chief-section">
    <div class="section-label">LEASES</div>
    <div class="lease-grid">
      <div class="lease-stat"><span class="lease-val">{active_leases}</span><span class="lease-lbl">active</span></div>
      <div class="lease-stat"><span class="lease-val">{held_by_me}</span><span class="lease-lbl">mine</span></div>
    </div>
  </div>
</div>"""


# ── Zone 4: Packet Ledger ──


def _render_packet_ledger(snapshot: dict) -> str:
    """Bottom pane with message flow timeline and branch convergence."""
    events = snapshot.get("recent_events", [])

    # Message flow
    msg_items = []
    for ev in events[:10]:
        if isinstance(ev, dict):
            ts = _format_ts_short(ev.get("timestamp", ev.get("ts", "")))
            etype = _esc(ev.get("type", ev.get("event", "?")))
            source = _esc(ev.get("source", ev.get("from", "")))
            target = _esc(ev.get("target", ev.get("to", "")))
            detail = _esc(ev.get("detail", ev.get("message", ""))[:60])
            # Color by type
            type_colors = {
                "fleet": "#60a5fa", "surgeon": "#a78bfa", "probe": "#fbbf24",
                "ghost": "#2dd4bf", "phase": "#34d399",
            }
            color = "#94a3b8"
            for prefix, c in type_colors.items():
                if prefix in etype.lower():
                    color = c
                    break
            route = f"{source}\u2192{target}" if source and target else ""
            msg_items.append(
                f'<div class="packet-entry">'
                f'<code class="pkt-ts">{ts}</code>'
                f'<span class="pkt-route">{route}</span>'
                f'<span class="pkt-type" style="color:{color}">{etype}</span>'
                f'<span class="pkt-detail">{detail}</span></div>'
            )

    msgs_html = "".join(msg_items) if msg_items else '<div class="empty-state">No recent packets</div>'

    # Branch convergence (if available)
    branches = snapshot.get("branches", [])
    branch_items = []
    for b in branches[:6]:
        if isinstance(b, dict):
            bname = _esc(b.get("branch", "?"))
            machine = _esc(b.get("machine", "?"))
            ahead = b.get("ahead", 0)
            behind = b.get("behind", 0)
            files = b.get("files", 0)
            if isinstance(files, list):
                files = len(files)
            bar_w = min(ahead * 6, 120)
            conflict_class = " branch-conflict" if b.get("conflict_files", 0) > 0 else ""
            branch_items.append(
                f'<div class="branch-entry{conflict_class}">'
                f'<span class="branch-name">{bname}</span>'
                f'<span class="branch-machine">{machine}</span>'
                f'<span class="branch-stats">+{ahead}/-{behind}</span>'
                f'<div class="branch-bar" style="width:{bar_w}px"></div>'
                f'<span class="branch-files">{files}f</span></div>'
            )
    branches_html = "".join(branch_items) if branch_items else '<div class="empty-state">No diverged branches</div>'

    return f"""<div id="zone4" class="zone-packet-ledger">
  <div class="ledger-split">
    <div class="ledger-half">
      <div class="section-label">PACKET LEDGER</div>
      <div class="ledger-scroll">{msgs_html}</div>
    </div>
    <div class="ledger-half">
      <div class="section-label">BRANCH CONVERGENCE</div>
      <div class="ledger-scroll">{branches_html}</div>
    </div>
  </div>
</div>"""


# ── CSS ──


def _render_css() -> str:
    """Full inline CSS for the 5-zone theatrical layout."""
    return """<style>
  :root {
    --bg-base: #0b1120;
    --bg-surface: #111827;
    --bg-elevated: #1e293b;
    --bg-hover: #253348;
    --border: #1e293b;
    --border-bright: #334155;
    --text-primary: #e2e8f0;
    --text-secondary: #94a3b8;
    --text-muted: #64748b;
    --green: #22c55e;
    --yellow: #eab308;
    --red: #ef4444;
    --blue: #60a5fa;
    --purple: #a78bfa;
    --teal: #2dd4bf;
    --orange: #f97316;
    --font-mono: 'SF Mono', 'Cascadia Code', 'Fira Code', 'JetBrains Mono', monospace;
    --font-sans: -apple-system, system-ui, 'Segoe UI', sans-serif;
  }

  * { margin: 0; padding: 0; box-sizing: border-box; }

  body {
    font-family: var(--font-sans);
    background: var(--bg-base);
    color: var(--text-primary);
    height: 100vh;
    overflow: hidden;
    display: grid;
    grid-template-rows: auto 1fr auto;
    grid-template-columns: 220px 1fr 260px;
    grid-template-areas:
      "vitals vitals vitals"
      "rail   theater chief"
      "ledger ledger  ledger";
    gap: 1px;
    background-color: var(--border);
  }

  /* ── Zone 0: Vital Signs ── */
  .zone-vital-signs {
    grid-area: vitals;
    background: linear-gradient(180deg, #0f172a 0%, var(--bg-surface) 100%);
    padding: 0.5rem 1rem;
    display: flex;
    align-items: center;
    gap: 1rem;
    flex-wrap: wrap;
    border-bottom: 1px solid var(--border-bright);
  }
  .vital-header {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-shrink: 0;
  }
  .vital-dot-lg {
    width: 12px; height: 12px;
    border-radius: 50%;
    display: inline-block;
    flex-shrink: 0;
  }
  .vital-title {
    font-weight: 700;
    font-size: 0.85rem;
    letter-spacing: 0.1em;
    color: var(--text-primary);
  }
  .vital-node {
    font-family: var(--font-mono);
    font-size: 0.75rem;
    color: var(--text-secondary);
    background: var(--bg-elevated);
    padding: 0.15rem 0.5rem;
    border-radius: 4px;
  }
  .vital-ts {
    font-family: var(--font-mono);
    font-size: 0.7rem;
    color: var(--text-muted);
  }
  .vital-probes {
    display: flex;
    gap: 0.75rem;
    align-items: center;
  }
  .vital-item {
    display: flex;
    align-items: center;
    gap: 0.3rem;
    font-size: 0.75rem;
  }
  .vital-dot {
    width: 8px; height: 8px;
    border-radius: 50%;
    display: inline-block;
  }
  .vital-label {
    font-family: var(--font-mono);
    font-size: 0.65rem;
    color: var(--text-secondary);
  }
  .vital-badge {
    font-family: var(--font-mono);
    font-size: 0.6rem;
    font-weight: 700;
  }
  .vital-status {
    display: flex;
    gap: 0.75rem;
    align-items: center;
    margin-left: auto;
  }
  .vital-stat {
    font-family: var(--font-mono);
    font-size: 0.75rem;
    color: var(--text-secondary);
    display: flex;
    align-items: center;
    gap: 0.3rem;
  }
  .vital-stat.warn { color: var(--yellow); }
  .vital-tabs {
    display: flex;
    gap: 2px;
    margin-left: 1rem;
  }
  .tab {
    font-family: var(--font-mono);
    font-size: 0.7rem;
    padding: 0.25rem 0.75rem;
    background: var(--bg-elevated);
    color: var(--text-muted);
    border: 1px solid var(--border);
    border-radius: 4px 4px 0 0;
    cursor: pointer;
    transition: all 0.15s;
  }
  .tab:hover { background: var(--bg-hover); color: var(--text-secondary); }
  .tab.active {
    background: var(--bg-surface);
    color: var(--text-primary);
    border-bottom-color: var(--bg-surface);
  }

  /* ── Zone 1: Node Rail ── */
  .zone-node-rail {
    grid-area: rail;
    background: var(--bg-surface);
    padding: 0.5rem;
    overflow-y: auto;
    border-right: 1px solid var(--border);
  }
  .rail-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0.25rem 0.5rem;
    margin-bottom: 0.5rem;
  }
  .rail-title {
    font-size: 0.7rem;
    font-weight: 700;
    letter-spacing: 0.1em;
    color: var(--text-muted);
  }
  .rail-count {
    font-family: var(--font-mono);
    font-size: 0.7rem;
    color: var(--text-muted);
  }
  .node-card {
    background: var(--bg-elevated);
    border-radius: 6px;
    padding: 0.6rem;
    margin-bottom: 0.4rem;
    cursor: pointer;
    transition: background 0.15s;
  }
  .node-card:hover { background: var(--bg-hover); }
  .node-header {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    margin-bottom: 0.3rem;
  }
  .node-icon { font-size: 1rem; }
  .node-name {
    font-weight: 600;
    font-size: 0.85rem;
    flex: 1;
  }
  .node-dot {
    width: 8px; height: 8px;
    border-radius: 50%;
    display: inline-block;
  }
  .self-marker {
    font-size: 0.55rem;
    color: var(--blue);
    background: var(--blue)15;
    padding: 0.05rem 0.3rem;
    border-radius: 3px;
    font-weight: 700;
    vertical-align: middle;
  }
  .node-role {
    font-size: 0.7rem;
    color: var(--text-muted);
    margin-bottom: 0.2rem;
  }
  .node-meta {
    display: flex;
    justify-content: space-between;
    font-size: 0.7rem;
    color: var(--text-secondary);
    font-family: var(--font-mono);
  }

  /* ── Zone 2: Local Theater ── */
  .zone-local-theater {
    grid-area: theater;
    background: var(--bg-surface);
    overflow-y: auto;
    display: flex;
    flex-direction: column;
  }
  .theater-header {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.5rem 0.75rem;
    border-bottom: 1px solid var(--border);
    flex-shrink: 0;
  }
  .theater-title {
    font-size: 0.7rem;
    font-weight: 700;
    letter-spacing: 0.1em;
    color: var(--text-muted);
  }
  .theater-node {
    font-family: var(--font-mono);
    font-size: 0.7rem;
    color: var(--text-secondary);
  }
  .theater-section {
    padding: 0.5rem 0.75rem;
    border-bottom: 1px solid var(--border);
  }
  .theater-feed { flex: 1; overflow-y: auto; }
  .section-label {
    font-size: 0.65rem;
    font-weight: 700;
    letter-spacing: 0.1em;
    color: var(--text-muted);
    margin-bottom: 0.4rem;
  }
  .empty-state {
    font-size: 0.8rem;
    color: var(--text-muted);
    font-style: italic;
    padding: 0.3rem 0;
  }

  /* Session items */
  .session-item {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    padding: 0.2rem 0;
    font-size: 0.8rem;
  }
  .session-dot {
    width: 6px; height: 6px;
    border-radius: 50%;
    flex-shrink: 0;
  }
  .session-item code {
    font-family: var(--font-mono);
    font-size: 0.75rem;
    color: var(--text-secondary);
  }
  .session-state {
    font-family: var(--font-mono);
    font-size: 0.7rem;
    color: var(--text-muted);
  }
  .session-task {
    font-size: 0.75rem;
    color: var(--text-secondary);
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  /* Finding items */
  .finding-item {
    display: flex;
    align-items: baseline;
    gap: 0.5rem;
    padding: 0.2rem 0;
    font-size: 0.8rem;
  }
  .finding-sev {
    font-family: var(--font-mono);
    font-size: 0.65rem;
    font-weight: 700;
    flex-shrink: 0;
    min-width: 3.5rem;
  }
  .finding-probe {
    font-family: var(--font-mono);
    font-size: 0.7rem;
    color: var(--text-muted);
    flex-shrink: 0;
  }
  .finding-msg {
    font-size: 0.75rem;
    color: var(--text-secondary);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  /* Feed entries */
  .feed-entry {
    display: flex;
    align-items: baseline;
    gap: 0.5rem;
    padding: 0.15rem 0;
    font-family: var(--font-mono);
    font-size: 0.75rem;
    animation: finding-appear 0.3s ease-out;
  }
  .feed-ts {
    font-size: 0.7rem;
    color: var(--text-muted);
    flex-shrink: 0;
  }
  .feed-type {
    font-weight: 600;
    flex-shrink: 0;
  }
  .feed-detail {
    color: var(--text-secondary);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  /* ── Zone 3: Chief Synthesis ── */
  .zone-chief-synthesis {
    grid-area: chief;
    background: var(--bg-surface);
    overflow-y: auto;
    border-left: 1px solid var(--border);
  }
  .chief-header {
    padding: 0.5rem 0.75rem;
    border-bottom: 1px solid var(--border);
  }
  .chief-title {
    font-size: 0.7rem;
    font-weight: 700;
    letter-spacing: 0.1em;
    color: var(--text-muted);
  }
  .chief-section {
    padding: 0.5rem 0.75rem;
    border-bottom: 1px solid var(--border);
  }

  /* Decision items */
  .decision-item {
    padding: 0.3rem 0;
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
  }
  .decision-item > :first-child {
    display: flex;
    gap: 0.4rem;
    align-items: baseline;
  }
  .decision-choice {
    font-size: 0.8rem;
    color: var(--text-primary);
  }
  .confidence-bar {
    height: 3px;
    background: var(--bg-elevated);
    border-radius: 2px;
    overflow: hidden;
  }
  .confidence-fill {
    height: 100%;
    border-radius: 2px;
    transition: width 0.5s ease-out;
  }

  /* Rebuttal items */
  .rebuttal-item {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    padding: 0.2rem 0;
    font-size: 0.8rem;
  }
  .rebuttal-state {
    font-family: var(--font-mono);
    font-size: 0.65rem;
    font-weight: 700;
    min-width: 3.5rem;
  }
  .rebuttal-topic {
    flex: 1;
    color: var(--text-secondary);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .rebuttal-turns {
    font-family: var(--font-mono);
    font-size: 0.65rem;
    color: var(--text-muted);
  }

  /* Lease grid */
  .lease-grid {
    display: flex;
    gap: 1rem;
  }
  .lease-stat {
    display: flex;
    flex-direction: column;
    align-items: center;
  }
  .lease-val {
    font-size: 1.5rem;
    font-weight: 700;
    font-family: var(--font-mono);
    color: var(--text-primary);
  }
  .lease-lbl {
    font-size: 0.65rem;
    color: var(--text-muted);
    text-transform: uppercase;
  }

  /* ── Zone 4: Packet Ledger ── */
  .zone-packet-ledger {
    grid-area: ledger;
    background: var(--bg-surface);
    border-top: 1px solid var(--border-bright);
    max-height: 200px;
    overflow: hidden;
  }
  .ledger-split {
    display: grid;
    grid-template-columns: 1fr 1fr;
    height: 100%;
    gap: 1px;
    background: var(--border);
  }
  .ledger-half {
    background: var(--bg-surface);
    padding: 0.4rem 0.75rem;
    overflow-y: auto;
  }
  .ledger-scroll {
    overflow-y: auto;
    max-height: 150px;
  }

  /* Packet entries */
  .packet-entry {
    display: flex;
    align-items: baseline;
    gap: 0.4rem;
    padding: 0.1rem 0;
    font-family: var(--font-mono);
    font-size: 0.7rem;
  }
  .pkt-ts {
    color: var(--text-muted);
    font-size: 0.65rem;
    flex-shrink: 0;
  }
  .pkt-route {
    color: var(--text-secondary);
    flex-shrink: 0;
    min-width: 5rem;
  }
  .pkt-type { font-weight: 600; flex-shrink: 0; }
  .pkt-detail {
    color: var(--text-muted);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  /* Branch entries */
  .branch-entry {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    padding: 0.15rem 0;
    font-family: var(--font-mono);
    font-size: 0.7rem;
  }
  .branch-entry.branch-conflict { background: #ef444410; }
  .branch-name {
    color: var(--blue);
    flex-shrink: 0;
    min-width: 8rem;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .branch-machine {
    color: var(--text-muted);
    flex-shrink: 0;
    min-width: 3rem;
  }
  .branch-stats {
    color: var(--text-secondary);
    flex-shrink: 0;
    min-width: 4rem;
  }
  .branch-bar {
    height: 4px;
    background: var(--blue);
    border-radius: 2px;
    opacity: 0.6;
  }
  .branch-files {
    color: var(--text-muted);
    font-size: 0.6rem;
  }

  /* ── Animations ── */
  @keyframes finding-appear {
    0%   { opacity: 0; transform: translateY(4px); }
    100% { opacity: 1; transform: translateY(0); }
  }
  @keyframes vital-pulse {
    0%, 100% { opacity: 1; }
    50%      { opacity: 0.4; }
  }
  .vital-pulse .vital-dot {
    animation: vital-pulse 1.5s ease-in-out infinite;
  }

  /* ── Responsive ── */
  @media (max-width: 1200px) {
    body {
      grid-template-columns: 180px 1fr 200px;
    }
  }
  @media (max-width: 900px) {
    body {
      grid-template-columns: 1fr;
      grid-template-rows: auto auto 1fr auto auto;
      grid-template-areas:
        "vitals"
        "rail"
        "theater"
        "chief"
        "ledger";
    }
    .zone-node-rail {
      border-right: none;
      border-bottom: 1px solid var(--border);
      display: flex;
      gap: 0.5rem;
      overflow-x: auto;
      padding: 0.5rem;
    }
    .node-card { min-width: 160px; }
    .zone-chief-synthesis {
      border-left: none;
      border-top: 1px solid var(--border);
    }
    .zone-packet-ledger { max-height: 250px; }
  }

  /* ── Scrollbar ── */
  ::-webkit-scrollbar { width: 6px; height: 6px; }
  ::-webkit-scrollbar-track { background: var(--bg-base); }
  ::-webkit-scrollbar-thumb { background: var(--border-bright); border-radius: 3px; }
  ::-webkit-scrollbar-thumb:hover { background: var(--text-muted); }
</style>"""


# ── JavaScript ──


def _render_js() -> str:
    """Inline JS for auto-refresh and tab switching."""
    return """<script>
(function() {
  // Auto-refresh every 10s
  var refreshInterval = 10000;
  var refreshTimer = null;

  function scheduleRefresh() {
    refreshTimer = setTimeout(function() {
      // Graceful reload — keep scroll positions if possible
      var scrollPositions = {};
      document.querySelectorAll('[id^="zone"]').forEach(function(el) {
        scrollPositions[el.id] = el.scrollTop;
      });
      sessionStorage.setItem('_fleet_scroll', JSON.stringify(scrollPositions));
      sessionStorage.setItem('_fleet_tab', document.querySelector('.tab.active')?.dataset.tab || 'theater');
      location.reload();
    }, refreshInterval);
  }

  // Restore scroll positions after reload
  try {
    var saved = JSON.parse(sessionStorage.getItem('_fleet_scroll') || '{}');
    Object.keys(saved).forEach(function(id) {
      var el = document.getElementById(id);
      if (el) el.scrollTop = saved[id];
    });
    sessionStorage.removeItem('_fleet_scroll');
  } catch(e) {}

  // Restore active tab
  try {
    var savedTab = sessionStorage.getItem('_fleet_tab');
    if (savedTab) {
      document.querySelectorAll('.tab').forEach(function(t) {
        t.classList.toggle('active', t.dataset.tab === savedTab);
      });
      sessionStorage.removeItem('_fleet_tab');
    }
  } catch(e) {}

  // Tab switching
  document.querySelectorAll('.tab').forEach(function(tab) {
    tab.addEventListener('click', function() {
      document.querySelectorAll('.tab').forEach(function(t) { t.classList.remove('active'); });
      tab.classList.add('active');
    });
  });

  // Zone collapse on double-click headers
  document.querySelectorAll('.rail-header, .theater-header, .chief-header').forEach(function(hdr) {
    hdr.style.cursor = 'pointer';
    hdr.addEventListener('dblclick', function() {
      var zone = hdr.parentElement;
      if (zone.style.display === 'none') {
        zone.style.display = '';
      } else {
        // Toggle content visibility (keep header)
        var isCollapsed = zone.dataset.collapsed === 'true';
        Array.from(zone.children).forEach(function(child, i) {
          if (i > 0) child.style.display = isCollapsed ? '' : 'none';
        });
        zone.dataset.collapsed = isCollapsed ? 'false' : 'true';
      }
    });
  });

  // Start auto-refresh
  scheduleRefresh();

  // Update timestamp display
  var tsEl = document.getElementById('last-update');
  if (tsEl) {
    var now = new Date();
    tsEl.title = 'Last refresh: ' + now.toLocaleTimeString();
  }
})();
</script>"""


# ── Public API ──


def render_dashboard(snapshot: dict) -> str:
    """Render fleet status snapshot as a complete HTML page.

    5-zone theatrical layout with Superset-inspired dark theme.
    Self-contained HTML with inline CSS/JS, no external dependencies.

    Args:
        snapshot: Dict from FleetStatusAggregator.snapshot()

    Returns:
        Complete self-contained HTML string.
    """
    node_id = _esc(snapshot.get("node_id", "unknown"))

    vital_signs = _render_vital_signs(snapshot)
    node_rail = _render_node_rail(snapshot)
    theater = _render_local_theater(snapshot)
    chief = _render_chief_synthesis(snapshot)
    ledger = _render_packet_ledger(snapshot)
    css = _render_css()
    js = _render_js()

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="30">
<title>Fleet Dashboard \u2014 {node_id}</title>
{css}
</head>
<body>
{vital_signs}
{node_rail}
{theater}
{chief}
{ledger}
{js}
</body>
</html>"""
