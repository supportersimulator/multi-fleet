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


def render_dashboard(snapshot: dict) -> str:
    """Render fleet status snapshot as a complete HTML page.

    Args:
        snapshot: Dict from FleetStatusAggregator.snapshot()

    Returns:
        Complete self-contained HTML string with inline CSS.
    """
    ts = _format_ts(snapshot.get("timestamp", time.time()))
    node_id = _esc(snapshot.get("node_id", "unknown"))
    health = snapshot.get("health_dot", "red")

    node_grid = _render_node_grid(snapshot)
    liaison = _render_liaison(snapshot)
    rebuttals = _render_rebuttals(snapshot)
    events = _render_events(snapshot)

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
{rebuttals}
{events}
</body>
</html>"""
