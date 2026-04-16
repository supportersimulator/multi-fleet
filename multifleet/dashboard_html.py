"""Fleet Dashboard -- renders fleet status as self-contained HTML."""

import html
import time
from datetime import datetime, timezone


def _esc(text: str) -> str:
    """HTML-escape a string."""
    return html.escape(str(text)) if text else ""


def _health_dot(color: str) -> str:
    """Return colored circle for health status."""
    colors = {"green": "#22c55e", "yellow": "#eab308", "red": "#ef4444"}
    c = colors.get(color, "#6b7280")
    return f'<span style="color:{c};font-size:1.4em;">&#9679;</span>'


def _format_ts(ts) -> str:
    """Format unix timestamp to human-readable."""
    if not ts:
        return "—"
    try:
        dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        return dt.strftime("%H:%M:%S UTC")
    except (ValueError, TypeError, OSError):
        return str(ts)


def _render_node_grid(snapshot: dict) -> str:
    """Render node grid section."""
    daemon = snapshot.get("daemon", {})
    peers_info = snapshot.get("peers", {})
    node_id = _esc(snapshot.get("node_id", "unknown"))
    health_dot = snapshot.get("health_dot", "red")

    # This node row
    status = _esc(daemon.get("status", "unknown"))
    nats = daemon.get("nats", {})
    nats_ok = nats.get("connected", False) if isinstance(nats, dict) else daemon.get("nats_connected", False)
    branch = _esc(daemon.get("branch", "—"))
    sessions = daemon.get("sessions", "—")

    rows = [f"""<tr>
        <td>{_health_dot(health_dot)}</td>
        <td><strong>{node_id}</strong></td>
        <td>{status}</td>
        <td>{"yes" if nats_ok else "no"}</td>
        <td>{_esc(str(branch))}</td>
        <td>{_esc(str(sessions))}</td>
    </tr>"""]

    # Peer rows
    peer_names = peers_info.get("peers", [])
    for name in peer_names:
        rows.append(f"""<tr>
        <td>{_health_dot("green")}</td>
        <td>{_esc(name)}</td>
        <td>peer</td><td>—</td><td>—</td><td>—</td>
    </tr>""")

    return f"""<section>
    <h2>Nodes</h2>
    <table>
        <thead><tr><th></th><th>Node</th><th>Status</th><th>NATS</th><th>Branch</th><th>Sessions</th></tr></thead>
        <tbody>{"".join(rows)}</tbody>
    </table>
    <p class="muted">Reachable: {peers_info.get("reachable", 0)}/{peers_info.get("total", 0)} peers</p>
</section>"""


def _render_rebuttals(snapshot: dict) -> str:
    """Render active rebuttals section."""
    rb = snapshot.get("rebuttals", {})
    active = rb.get("active", 0)
    total = rb.get("total", 0)
    if not total:
        return '<section><h2>Rebuttals</h2><p class="muted">None</p></section>'
    return f"""<section>
    <h2>Rebuttals</h2>
    <p><strong>{active}</strong> active / {total} total</p>
</section>"""


def _render_events(snapshot: dict) -> str:
    """Render recent events section."""
    events = snapshot.get("recent_events", [])
    if not events:
        return '<section><h2>Recent Events</h2><p class="muted">No recent events</p></section>'
    items = []
    for ev in events[:8]:
        if isinstance(ev, dict):
            ts = _format_ts(ev.get("timestamp", ev.get("ts", "")))
            desc = _esc(ev.get("type", ev.get("event", ev.get("description", "?"))))
            detail = _esc(ev.get("detail", ev.get("message", "")))
            items.append(f"<li><code>{ts}</code> <strong>{desc}</strong> {detail}</li>")
        else:
            items.append(f"<li>{_esc(str(ev))}</li>")
    return f'<section><h2>Recent Events</h2><ul>{"".join(items)}</ul></section>'


def _render_liaison(snapshot: dict) -> str:
    """Render liaison/channel health."""
    li = snapshot.get("liaison", {})
    reviews = li.get("pending_reviews", 0)
    unread = li.get("unread", 0)
    leases = snapshot.get("leases", {})
    active_leases = leases.get("active", 0)
    return f"""<section>
    <h2>Channel Health</h2>
    <table>
        <tr><td>Pending reviews</td><td><strong>{reviews}</strong></td></tr>
        <tr><td>Unread messages</td><td><strong>{unread}</strong></td></tr>
        <tr><td>Active leases</td><td><strong>{active_leases}</strong></td></tr>
    </table>
</section>"""


def render_dashboard(snapshot: dict = None, *, prefer_web: bool = True) -> str:
    """Render fleet status as a complete HTML page.

    When prefer_web=True (default), delegates to the real-time web dashboard
    (web_dashboard.py) which provides WebSocket EventBus integration and
    auto-refreshing HTTP data.

    Falls back to the static snapshot renderer if the web dashboard is unavailable.

    Args:
        snapshot: Dict from FleetStatusAggregator.snapshot(). If None,
            auto-fetches from FleetStatusAggregator (sibling module).
        prefer_web: If True, use the real-time web dashboard when available

    Returns:
        Complete self-contained HTML string with inline CSS + JS.
    """
    if prefer_web:
        try:
            from multifleet.web_dashboard import build_dashboard_html
            return build_dashboard_html()
        except ImportError:
            pass

    # Auto-fetch snapshot from FleetStatusAggregator if not provided
    if snapshot is None:
        try:
            from multifleet.status_aggregator import FleetStatusAggregator
            snapshot = FleetStatusAggregator().snapshot()
        except Exception:
            snapshot = {}

    # Fallback: static snapshot (original implementation)
    return _render_static_dashboard(snapshot)


def _render_probe_review(snapshot: dict) -> str:
    """Render probe review results section (Zone 2 — Local Theater)."""
    pr = snapshot.get("probe_review", {})
    if not pr or pr.get("error"):
        error_msg = pr.get("error", "") if pr else ""
        note = f" ({_esc(error_msg)})" if error_msg else ""
        return f'<section><h2>Probe Review</h2><p class="muted">No probe review data{note}</p></section>'

    # Probe findings
    probes = pr.get("probes", {})
    total_probes = probes.get("total", 0)
    findings_count = probes.get("findings", 0)
    blocking = probes.get("blocking", 0)
    intensity = _esc(probes.get("surgeon_intensity", "unknown"))

    findings_html = ""
    for result in probes.get("results", [])[:5]:
        for finding in result.get("findings", [])[:3]:
            severity = finding.get("severity", finding.get("blast_radius", "low"))
            title = _esc(finding.get("title", "untitled"))
            sev_colors = {"critical": "#ef4444", "high": "#f97316", "medium": "#eab308", "low": "#22c55e"}
            color = sev_colors.get(severity, "#6b7280")
            findings_html += f'<li><span style="color:{color};font-weight:600;">[{_esc(severity)}]</span> {title}</li>'

    # Surgeon verdict
    verdict = pr.get("surgeon_verdict", {})
    verdict_html = ""
    if verdict and not verdict.get("skipped"):
        v = _esc(str(verdict.get("verdict", "unknown")))
        conf = verdict.get("confidence", 0.0)
        verdict_html = f'<p>Surgeon verdict: <strong>{v}</strong> (confidence: {conf:.0%})</p>'
    elif verdict.get("skipped"):
        reason = _esc(verdict.get("reason", ""))
        verdict_html = f'<p class="muted">Surgeon review skipped: {reason}</p>'

    # Acceptance gate
    acceptance = pr.get("acceptance", {})
    gate_html = ""
    if acceptance and not acceptance.get("error"):
        decision = acceptance.get("decision", acceptance.get("accepted", "unknown"))
        gate_color = "#22c55e" if str(decision).lower() in ("accept", "true", "pass") else "#ef4444"
        gate_html = f'<p>Acceptance gate: <span style="color:{gate_color};font-weight:600;">{_esc(str(decision).upper())}</span></p>'
    elif acceptance.get("error"):
        gate_html = f'<p class="muted">Acceptance gate error: {_esc(acceptance["error"])}</p>'

    elapsed = pr.get("elapsed_s", "?")

    return f"""<section>
    <h2>Probe Review</h2>
    <table>
        <tr><td>Probes run</td><td><strong>{total_probes}</strong></td></tr>
        <tr><td>Findings</td><td><strong>{findings_count}</strong> ({blocking} blocking)</td></tr>
        <tr><td>Surgeon intensity</td><td><code>{intensity}</code></td></tr>
        <tr><td>Elapsed</td><td>{_esc(str(elapsed))}s</td></tr>
    </table>
    {f'<ul>{findings_html}</ul>' if findings_html else ''}
    {verdict_html}
    {gate_html}
</section>"""


def _render_surgeon_status(snapshot: dict) -> str:
    """Render surgeon adapter status section (Zone 3 — Chief Synthesis)."""
    ss = snapshot.get("surgeon_status", {})
    if not ss:
        return '<section><h2>3-Surgeon Status</h2><p class="muted">No surgeon data</p></section>'

    available = ss.get("available", False)
    state = ss.get("state", {})
    counters = ss.get("counters", {})
    phase = _esc(state.get("phase", "unknown"))
    disagreement = state.get("disagreement_score", 0.0)

    avail_dot = _health_dot("green" if available else "red")

    # Surgeon availability rows
    surgeons = ss.get("surgeons", {})
    surgeon_rows = ""
    for name in ("atlas", "cardiologist", "neurologist"):
        info = surgeons.get(name, {})
        s_avail = info.get("available", False)
        dot = _health_dot("green" if s_avail else "red")
        surgeon_rows += f"<tr><td>{dot}</td><td>{_esc(name)}</td><td>{'ready' if s_avail else 'offline'}</td></tr>"

    # If no individual surgeon data, show aggregate
    if not surgeons:
        surgeon_rows = f"""<tr><td>{avail_dot}</td><td>bridge</td><td>{'connected' if available else 'unavailable'}</td></tr>"""

    # Recent cross-exam / counters
    queries = counters.get("queries", 0)
    reviews = counters.get("reviews", 0)
    errors = counters.get("bridge_errors", 0)
    fallbacks = counters.get("degraded_fallbacks", 0)

    # Acceptance gate history
    gate_history = ss.get("gate_history", [])
    gate_html = ""
    if gate_history:
        items = []
        for entry in gate_history[:5]:
            decision = _esc(str(entry.get("decision", "?")))
            ts = _format_ts(entry.get("timestamp", ""))
            color = "#22c55e" if decision.lower() in ("accept", "pass") else "#ef4444"
            items.append(f'<li><code>{ts}</code> <span style="color:{color};">{decision}</span></li>')
        gate_html = f'<p style="margin-top:0.5rem;"><strong>Gate History</strong></p><ul>{"".join(items)}</ul>'

    return f"""<section>
    <h2>3-Surgeon Status</h2>
    <table>
        <thead><tr><th></th><th>Surgeon</th><th>Status</th></tr></thead>
        <tbody>{surgeon_rows}</tbody>
    </table>
    <table style="margin-top:0.5rem;">
        <tr><td>Phase</td><td><code>{phase}</code></td></tr>
        <tr><td>Disagreement</td><td>{disagreement:.0%}</td></tr>
        <tr><td>Queries</td><td>{queries}</td></tr>
        <tr><td>Reviews</td><td>{reviews}</td></tr>
        <tr><td>Errors</td><td>{errors}</td></tr>
        <tr><td>Fallbacks</td><td>{fallbacks}</td></tr>
    </table>
    {gate_html}
</section>"""


def _render_arbiter_pending(snapshot: dict) -> str:
    """Render pending arbiter decisions badge (Zone 3 — Chief Synthesis)."""
    arbiter = snapshot.get("arbiter_pending", [])
    if not arbiter:
        return ""

    count = len(arbiter)
    items = ""
    for d in arbiter[:5]:
        task_id = _esc(d.get("task_id", "?"))
        confidence = d.get("confidence", 0.0)
        dissent_count = len(d.get("dissent", []))
        conf_color = "#22c55e" if confidence >= 0.7 else "#eab308" if confidence >= 0.4 else "#ef4444"
        items += f"""<tr>
        <td><a href="/arbiter/{task_id}" style="color:#60a5fa;">{task_id[:12]}</a></td>
        <td><span style="color:{conf_color};font-weight:600;">{confidence:.0%}</span></td>
        <td>{dissent_count}</td>
    </tr>"""

    return f"""<section>
    <h2 style="color:#f59e0b;">DECISIONS AWAITING REVIEW ({count})</h2>
    <table>
        <thead><tr><th>Task</th><th>Confidence</th><th>Dissent</th></tr></thead>
        <tbody>{items}</tbody>
    </table>
</section>"""


def _render_static_dashboard(snapshot: dict) -> str:
    """Original static dashboard renderer (fallback)."""
    ts = _format_ts(snapshot.get("timestamp", time.time()))
    node_id = _esc(snapshot.get("node_id", "unknown"))
    health = snapshot.get("health_dot", "red")

    node_grid = _render_node_grid(snapshot)
    liaison = _render_liaison(snapshot)
    rebuttals = _render_rebuttals(snapshot)
    events = _render_events(snapshot)
    probe_review = _render_probe_review(snapshot)
    surgeon_status = _render_surgeon_status(snapshot)
    arbiter_pending = _render_arbiter_pending(snapshot)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="15">
<title>Fleet Dashboard — {node_id}</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; background: #0f172a; color: #e2e8f0; margin: 0; padding: 1rem; }}
  h1 {{ margin: 0 0 0.5rem; font-size: 1.4rem; }}
  h2 {{ font-size: 1rem; border-bottom: 1px solid #334155; padding-bottom: 0.3rem; margin: 1rem 0 0.5rem; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ text-align: left; padding: 0.3rem 0.6rem; border-bottom: 1px solid #1e293b; }}
  th {{ color: #94a3b8; font-weight: 600; font-size: 0.85rem; }}
  ul {{ padding-left: 1.2rem; }}
  li {{ margin: 0.2rem 0; font-size: 0.9rem; }}
  code {{ background: #1e293b; padding: 0.1rem 0.3rem; border-radius: 3px; font-size: 0.85rem; }}
  .muted {{ color: #64748b; font-size: 0.85rem; }}
  .header {{ display: flex; align-items: center; gap: 0.5rem; }}
  section {{ margin-bottom: 0.5rem; }}
</style>
</head>
<body>
<div class="header">
  {_health_dot(health)}
  <h1>Fleet Dashboard</h1>
  <span class="muted">node: {node_id} &middot; {ts}</span>
</div>
{node_grid}
{liaison}
{probe_review}
{rebuttals}
{surgeon_status}
{arbiter_pending}
{events}
</body>
</html>"""
