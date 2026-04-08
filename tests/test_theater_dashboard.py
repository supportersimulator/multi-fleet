"""Tests for the 5-Zone Surgical Theater Dashboard.

Verifies: layout creation, graceful degradation when daemon is down,
render methods produce valid Rich renderables without crashing.
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

FLEET_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(FLEET_ROOT))

from multifleet.theater_dashboard import TheaterDashboard, TheaterDataFetcher


# ── Sample data ───────────────────────────────────────────────────────

SAMPLE_DATA = {
    "online": True,
    "timestamp": 1700000000,
    "health": {
        "nodeId": "mac1",
        "status": "ok",
        "uptime_s": 3600,
        "git": {"branch": "main", "commit": "abc123"},
        "activeSessions": 2,
        "nats_connected": True,
        "peers": {
            "mac2": {"lastSeen": 5, "sessions": 1, "transport": "nats"},
            "mac3": {"lastSeen": 90, "sessions": 0, "transport": "nats"},
        },
        "channel_health": {
            "mac2": {"score": "A", "broken": []},
            "mac3": {"score": "B", "broken": ["http"]},
        },
    },
    "nodes": [
        {"id": "mac1", "status": "self", "branch": "main", "sessions": 2, "score": "self"},
        {"id": "mac2", "status": "online", "branch": "feat/x", "sessions": 1, "score": "A"},
        {"id": "mac3", "status": "stale", "branch": "fix/y", "sessions": 0, "score": "B"},
    ],
    "races": [{"task": "Fix auth", "status": "racing", "participants": {
        "mac1": {"status": "dispatched"},
        "mac2": {"status": "completed", "result": {"findings": [{"x": 1}]}},
    }}],
    "evidence": [
        {"seq": 1, "event_type": "merge_decision", "subject": "test", "entry_hash": "abcdef1234567890"},
        {"seq": 2, "event_type": "gains_gate", "subject": "gate ok", "entry_hash": "1234567890abcdef"},
    ],
    "truth": {"inbox": 5, "vision": 3, "snapshot": 1, "dao": 0},
    "supervisor": {"tasks": [{"phase": "racing"}]},
    "probe_results": [
        {"severity": "high", "probe": "nats_check", "message": "latency 45ms", "node": "mac3"},
    ],
    "gains_gate": {"passed": True, "checks": [
        {"name": "redis", "passed": True, "message": "ok"},
        {"name": "llm", "passed": True, "message": "ok"},
    ]},
}

EMPTY_DATA = {
    "online": False, "timestamp": 0, "health": {},
    "nodes": [], "races": [], "evidence": [], "truth": {},
    "supervisor": {"tasks": []}, "probe_results": [], "gains_gate": {},
}


# ── Tests ─────────────────────────────────────────────────────────────

def test_layout_creation():
    """Layout should produce a valid Rich Layout with all 5 zones + header + footer."""
    dash = TheaterDashboard(daemon_port=19999)
    layout = dash._build_layout(SAMPLE_DATA)
    # Should have header, main (nodes+theater), bottom (evidence+truth+synthesis), footer
    assert layout is not None
    assert layout["header"] is not None
    assert layout["nodes"] is not None
    assert layout["theater"] is not None
    assert layout["evidence"] is not None
    assert layout["truth"] is not None
    assert layout["synthesis"] is not None
    assert layout["footer"] is not None


def test_data_fetch_graceful_daemon_down():
    """Fetcher should return empty/safe data when daemon is unreachable."""
    fetcher = TheaterDataFetcher(port=19999)  # nothing running here
    data = fetcher.fetch_all()
    assert data["online"] is False
    assert data["nodes"] == []
    assert data["evidence"] == []
    assert data["truth"] == {}
    assert data["races"] == []


def test_render_methods_no_crash():
    """Every render method should return a Panel without raising."""
    dash = TheaterDashboard(daemon_port=19999)

    # Full data
    header = dash._render_header(SAMPLE_DATA)
    assert header is not None

    nodes = dash._render_nodes(SAMPLE_DATA)
    assert nodes is not None

    theater = dash._render_theater(SAMPLE_DATA)
    assert theater is not None

    evidence = dash._render_evidence(SAMPLE_DATA)
    assert evidence is not None

    truth = dash._render_truth(SAMPLE_DATA)
    assert truth is not None

    synthesis = dash._render_synthesis(SAMPLE_DATA)
    assert synthesis is not None

    footer = dash._render_footer(SAMPLE_DATA)
    assert footer is not None


def test_render_empty_data_no_crash():
    """Render methods should handle empty/offline data without raising."""
    dash = TheaterDashboard(daemon_port=19999)

    # Offline layout
    offline = dash._build_offline_layout()
    assert offline is not None

    # Empty but online-shaped data
    minimal = {**EMPTY_DATA, "online": True, "health": {"nodeId": "test", "uptime_s": 0, "git": {}}}
    for method in [dash._render_header, dash._render_nodes, dash._render_theater,
                   dash._render_evidence, dash._render_truth, dash._render_synthesis,
                   dash._render_footer]:
        result = method(minimal)
        assert result is not None


def test_offline_layout():
    """When daemon is down, layout should show offline state."""
    dash = TheaterDashboard(daemon_port=19999)
    layout = dash._build_layout(EMPTY_DATA)
    # offline data -> offline layout (single panel, no zones)
    assert layout is not None
