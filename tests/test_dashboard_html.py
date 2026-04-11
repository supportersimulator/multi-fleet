"""Tests for dashboard_html module."""

import sys
from pathlib import Path

# Ensure multifleet is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from multifleet.dashboard_html import render_dashboard, _render_static_dashboard


def _make_snapshot(**overrides):
    """Build a minimal valid snapshot."""
    base = {
        "timestamp": 1712300000.0,
        "node_id": "mac1",
        "daemon": {"status": "ok", "nats": {"connected": True}, "branch": "main", "sessions": 2},
        "peers": {"total": 2, "reachable": 1, "peers": ["mac2", "mac3"]},
        "liaison": {"pending_reviews": 1, "active_rebuttals": 0, "unread": 3, "summary": ""},
        "rebuttals": {"active": 1, "total": 2},
        "leases": {"active": 1, "held_by_me": 1},
        "recent_events": [
            {"timestamp": 1712300000.0, "type": "heartbeat", "detail": "mac2 online"},
        ],
        "health_dot": "green",
    }
    base.update(overrides)
    return base


def test_render_returns_complete_html():
    html = render_dashboard(_make_snapshot(), prefer_web=False)
    assert html.startswith("<!DOCTYPE html>")
    assert "</html>" in html
    assert "<title>" in html


def test_auto_refresh_meta_tag():
    html = render_dashboard(_make_snapshot(), prefer_web=False)
    assert 'http-equiv="refresh" content="15"' in html


def test_node_grid_shows_peers():
    html = render_dashboard(_make_snapshot(), prefer_web=False)
    assert "mac1" in html
    assert "mac2" in html
    assert "mac3" in html
    assert "Reachable: 1/2" in html


def test_rebuttals_section():
    html = render_dashboard(_make_snapshot(rebuttals={"active": 3, "total": 5}), prefer_web=False)
    assert "<strong>3</strong> active / 5 total" in html

    html_none = render_dashboard(_make_snapshot(rebuttals={"active": 0, "total": 0}), prefer_web=False)
    assert "None" in html_none


def test_xss_escape():
    html = _render_static_dashboard(_make_snapshot(node_id="<script>alert(1)</script>"))
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_web_dashboard_preferred():
    """render_dashboard defaults to the real-time web dashboard."""
    html = render_dashboard(_make_snapshot())
    assert html.startswith("<!DOCTYPE html>")
    # Web dashboard uses JS polling, not meta refresh
    assert "setInterval(poll" in html
    assert "EventBus" in html


def test_web_dashboard_html():
    """Web dashboard contains all required panel sections."""
    from multifleet.web_dashboard import build_dashboard_html
    html = build_dashboard_html()
    # All card sections present
    for section in ["Nodes", "Messages", "Gains Gate", "Races", "Evidence Ledger",
                    "Truth Ladder", "Probe Findings", "Node Overlays", "EventBus Live"]:
        assert section in html, f"Missing section: {section}"
    # Superset-inspired dark theme
    assert "#1a1a2e" in html
    # WebSocket connection
    assert "ws://127.0.0.1:8031" in html
    # Daemon connection
    assert "http://127.0.0.1:8855" in html


if __name__ == "__main__":
    test_render_returns_complete_html()
    test_auto_refresh_meta_tag()
    test_node_grid_shows_peers()
    test_rebuttals_section()
    test_xss_escape()
    test_web_dashboard_preferred()
    test_web_dashboard_html()
    print("All 7 tests passed.")
