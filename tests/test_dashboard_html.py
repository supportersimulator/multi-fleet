"""Tests for enhanced dashboard_html module — 5-zone theatrical layout."""

import sys
from pathlib import Path

# Ensure multifleet is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from multifleet.dashboard_html import render_dashboard


def _make_snapshot(**overrides):
    """Build a minimal valid snapshot."""
    base = {
        "timestamp": 1712300000.0,
        "node_id": "mac1",
        "daemon": {"status": "ok", "nats": {"connected": True}, "branch": "main", "sessions": 2, "role": "chief"},
        "peers": {"total": 2, "reachable": 1, "peers": ["mac2", "mac3"]},
        "liaison": {"pending_reviews": 1, "active_rebuttals": 0, "unread": 3, "summary": ""},
        "rebuttals": {"active": 1, "total": 2},
        "leases": {"active": 1, "held_by_me": 1},
        "recent_events": [
            {"timestamp": 1712300000.0, "type": "heartbeat", "detail": "mac2 online",
             "source": "mac2", "target": "mac1"},
        ],
        "health_dot": "green",
    }
    base.update(overrides)
    return base


# ── Test 1: Complete HTML structure ──

def test_render_returns_complete_html():
    """Dashboard returns valid HTML5 document with all zones."""
    html = render_dashboard(_make_snapshot())
    assert html.startswith("<!DOCTYPE html>")
    assert "</html>" in html
    assert "<title>" in html
    assert "Fleet Dashboard" in html


# ── Test 2: 5-zone layout present ──

def test_five_zones_present():
    """All 5 zones are rendered in the output."""
    html = render_dashboard(_make_snapshot())
    assert 'id="zone0"' in html, "Zone 0 (vital signs) missing"
    assert 'id="zone1"' in html, "Zone 1 (node rail) missing"
    assert 'id="zone2"' in html, "Zone 2 (local theater) missing"
    assert 'id="zone3"' in html, "Zone 3 (chief synthesis) missing"
    assert 'id="zone4"' in html, "Zone 4 (packet ledger) missing"


# ── Test 3: CSS grid layout ──

def test_css_grid_layout():
    """CSS uses grid with proper zone areas."""
    html = render_dashboard(_make_snapshot())
    assert "grid-template-areas" in html
    assert "vitals" in html
    assert "rail" in html
    assert "theater" in html
    assert "chief" in html
    assert "ledger" in html


# ── Test 4: Dark theme colors ──

def test_dark_theme():
    """Background uses dark theme colors."""
    html = render_dashboard(_make_snapshot())
    assert "#0b1120" in html or "#0f172a" in html, "Dark bg missing"
    assert "#111827" in html, "Surface bg missing"
    assert "#1e293b" in html, "Elevated bg missing"


# ── Test 5: Node cards show peers ──

def test_node_rail_shows_all_nodes():
    """Node rail shows self + peer cards."""
    html = render_dashboard(_make_snapshot())
    assert "mac1" in html
    assert "mac2" in html
    assert "mac3" in html
    assert "YOU" in html, "Self-marker missing on own node"
    assert "1/2" in html, "Reachable count missing"


# ── Test 6: Auto-refresh ──

def test_auto_refresh():
    """Dashboard has both meta refresh fallback and JS refresh."""
    html = render_dashboard(_make_snapshot())
    assert 'http-equiv="refresh"' in html, "Meta refresh missing"
    assert "setTimeout" in html or "setInterval" in html, "JS refresh missing"
    assert "refreshInterval" in html, "JS refresh timer missing"


# ── Test 7: XSS protection ──

def test_xss_escape():
    """User-provided strings are HTML-escaped."""
    html = render_dashboard(_make_snapshot(node_id="<script>alert(1)</script>"))
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


# ── Test 8: Responsive CSS ──

def test_responsive_media_queries():
    """CSS includes responsive breakpoints."""
    html = render_dashboard(_make_snapshot())
    assert "@media" in html, "No responsive breakpoints"
    assert "max-width:" in html


# ── Test 9: Health color coding ──

def test_health_color_coding():
    """Health dot maps to correct colors with glow."""
    for dot, expected_color in [("green", "#22c55e"), ("yellow", "#eab308"), ("red", "#ef4444")]:
        html = render_dashboard(_make_snapshot(health_dot=dot))
        assert expected_color in html, f"Color for {dot} health missing"


# ── Test 10: Optional data graceful degradation ──

def test_optional_data_graceful():
    """Dashboard renders cleanly with minimal/missing optional data."""
    minimal = {
        "timestamp": 1712300000.0,
        "node_id": "test",
        "daemon": {"status": "ok"},
        "peers": {"total": 0, "reachable": 0, "peers": []},
        "liaison": {},
        "rebuttals": {},
        "leases": {},
        "recent_events": [],
        "health_dot": "red",
    }
    html = render_dashboard(minimal)
    assert "<!DOCTYPE html>" in html
    assert 'id="zone0"' in html
    assert 'id="zone2"' in html
    # Should show empty states, not crash
    assert "No recent" in html or "No active" in html or "No probe" in html


# ── Test 11: Branch convergence in ledger ──

def test_branch_convergence_section():
    """Branch data renders in the packet ledger zone."""
    snap = _make_snapshot(branches=[
        {"branch": "fleet/mac2-fix", "machine": "mac2", "ahead": 3, "behind": 1, "files": 5},
    ])
    html = render_dashboard(snap)
    assert "BRANCH CONVERGENCE" in html
    assert "fleet/mac2-fix" in html
    assert "+3/-1" in html


# ── Test 12: Probe findings display ──

def test_probe_findings_display():
    """Probe findings render with severity coloring."""
    snap = _make_snapshot(probe_findings=[
        {"probe_id": "mirror_drift", "severity": "high", "message": "Drift detected in auth.py"},
    ])
    html = render_dashboard(snap)
    assert "mirror_drift" in html
    assert "HIGH" in html
    assert "Drift detected" in html


# ── Test 13: Agent session items ──

def test_agent_sessions_display():
    """Agent sessions render in the local theater."""
    snap = _make_snapshot(agent_sessions=[
        {"id": "abc12345-def", "state": "running", "task": "Fix auth module"},
    ])
    html = render_dashboard(snap)
    assert "abc12345" in html
    assert "running" in html
    assert "Fix auth" in html


# ── Test 14: Decision items with confidence bars ──

def test_decisions_display():
    """Decisions render with confidence bars."""
    snap = _make_snapshot(decisions=[
        {"timestamp": 1712300000.0, "choice": "Use adapter pattern", "confidence": 0.85},
    ])
    html = render_dashboard(snap)
    assert "Use adapter pattern" in html
    assert "confidence-bar" in html
    assert "85%" in html


# ── Test 15: Workspace tabs ──

def test_workspace_tabs():
    """Workspace tabs are present in vital signs bar."""
    html = render_dashboard(_make_snapshot())
    assert "Theater" in html
    assert "Metrics" in html
    assert "Branches" in html
    assert 'data-tab=' in html


# ── Test 16: Monospace font for terminal content ──

def test_monospace_terminal_style():
    """Terminal/code content uses monospace font."""
    html = render_dashboard(_make_snapshot())
    assert "font-mono" in html or "monospace" in html


# ── Test 17: Animations defined ──

def test_animations_defined():
    """CSS animations for findings and vital pulse are present."""
    html = render_dashboard(_make_snapshot())
    assert "@keyframes finding-appear" in html
    assert "@keyframes vital-pulse" in html


# ── Test 18: Collapsible zones ──

def test_collapsible_zones():
    """JS includes zone collapse on double-click."""
    html = render_dashboard(_make_snapshot())
    assert "dblclick" in html
    assert "collapsed" in html


# ── Test 19: Self-contained (no external deps) ──

def test_no_external_dependencies():
    """HTML has no external CSS/JS links."""
    html = render_dashboard(_make_snapshot())
    # No <link rel="stylesheet" href="http...">
    assert 'rel="stylesheet"' not in html
    # No <script src="http...">
    assert 'script src=' not in html.lower()
    # All CSS is inline
    assert "<style>" in html
    assert "</style>" in html


# ── Test 20: Packet ledger shows event routing ──

def test_packet_ledger_routing():
    """Packet ledger shows source->target routing."""
    snap = _make_snapshot(recent_events=[
        {"timestamp": 1712300000.0, "type": "fleet.heartbeat",
         "source": "mac1", "target": "all", "detail": "alive"},
    ])
    html = render_dashboard(snap)
    assert "PACKET LEDGER" in html
    # The arrow character between source and target
    assert "mac1" in html
    assert "all" in html


if __name__ == "__main__":
    tests = [
        test_render_returns_complete_html,
        test_five_zones_present,
        test_css_grid_layout,
        test_dark_theme,
        test_node_rail_shows_all_nodes,
        test_auto_refresh,
        test_xss_escape,
        test_responsive_media_queries,
        test_health_color_coding,
        test_optional_data_graceful,
        test_branch_convergence_section,
        test_probe_findings_display,
        test_agent_sessions_display,
        test_decisions_display,
        test_workspace_tabs,
        test_monospace_terminal_style,
        test_animations_defined,
        test_collapsible_zones,
        test_no_external_dependencies,
        test_packet_ledger_routing,
    ]
    for t in tests:
        t()
        print(f"  PASS: {t.__name__}")
    print(f"\nAll {len(tests)} tests passed.")
