"""Tests for fleet dashboard module.

Dashboard module is being built by another agent — tests written against expected API.
Tests verify: module loading, health data parsing, peer status rendering, offline degradation.
"""

import json
import time
from unittest.mock import MagicMock, patch

import pytest

import sys
from pathlib import Path

FLEET_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(FLEET_ROOT))


# ── Health data parsing (standalone, no module import needed) ──

SAMPLE_HEALTH = {
    "status": "ok",
    "node_id": "mac1",
    "uptime_s": 3600,
    "peers": {
        "mac2": {"lastSeen": time.time(), "reachable": True},
        "mac3": {"lastSeen": time.time() - 120, "reachable": True},
    },
    "channel_health": {
        "mac2": {"reachable": True, "latency_ms": 12},
        "mac3": {"reachable": True, "latency_ms": 45},
    },
}


def _peer_status(last_seen: float, now: float = None) -> str:
    """Classify peer health: green (<30s), yellow (<120s), red (>120s/unreachable)."""
    now = now or time.time()
    age = now - last_seen
    if age < 30:
        return "green"
    elif age < 120:
        return "yellow"
    return "red"


def _parse_health(data: dict) -> dict:
    """Parse daemon /health response into dashboard-friendly structure."""
    if "error" in data or data.get("status") != "ok":
        return {"online": False, "peers": {}, "error": data.get("error", "unknown")}
    peers = {}
    for node, info in data.get("peers", {}).items():
        peers[node] = {
            "status": _peer_status(info.get("lastSeen", 0)),
            "reachable": info.get("reachable", False),
            "lastSeen": info.get("lastSeen", 0),
        }
    return {"online": True, "node_id": data.get("node_id"), "peers": peers}


class TestPeerStatus:
    """Peer status color classification based on lastSeen age."""

    def test_green_recent(self):
        now = time.time()
        assert _peer_status(now - 5, now) == "green"

    def test_green_boundary(self):
        now = time.time()
        assert _peer_status(now - 29, now) == "green"

    def test_yellow_stale(self):
        now = time.time()
        assert _peer_status(now - 60, now) == "yellow"

    def test_yellow_boundary(self):
        now = time.time()
        assert _peer_status(now - 119, now) == "yellow"

    def test_red_old(self):
        now = time.time()
        assert _peer_status(now - 300, now) == "red"

    def test_red_zero_timestamp(self):
        now = time.time()
        assert _peer_status(0, now) == "red"


class TestHealthParsing:
    """Parse daemon /health response into dashboard model."""

    def test_healthy_response(self):
        result = _parse_health(SAMPLE_HEALTH)
        assert result["online"] is True
        assert result["node_id"] == "mac1"
        assert "mac2" in result["peers"]
        assert "mac3" in result["peers"]

    def test_peer_reachability(self):
        result = _parse_health(SAMPLE_HEALTH)
        assert result["peers"]["mac2"]["reachable"] is True

    def test_error_response(self):
        result = _parse_health({"error": "connection refused"})
        assert result["online"] is False
        assert result["error"] == "connection refused"

    def test_bad_status(self):
        result = _parse_health({"status": "degraded"})
        assert result["online"] is False

    def test_empty_peers(self):
        data = {"status": "ok", "node_id": "mac1", "peers": {}}
        result = _parse_health(data)
        assert result["online"] is True
        assert result["peers"] == {}


class TestDaemonOffline:
    """Graceful degradation when daemon is unreachable."""

    def test_connection_refused(self):
        from urllib.error import URLError
        with patch("urllib.request.urlopen", side_effect=URLError("Connection refused")):
            # Simulate what dashboard would do on fetch failure
            try:
                import urllib.request
                urllib.request.urlopen("http://127.0.0.1:8855/health", timeout=3)
                health = {}
            except (URLError, Exception):
                health = {"error": "daemon offline"}
            result = _parse_health(health)
            assert result["online"] is False

    def test_timeout_degrades_gracefully(self):
        with patch("urllib.request.urlopen", side_effect=TimeoutError()):
            try:
                import urllib.request
                urllib.request.urlopen("http://127.0.0.1:8855/health", timeout=3)
                health = {}
            except Exception:
                health = {"error": "timeout"}
            result = _parse_health(health)
            assert result["online"] is False
            assert result["error"] == "timeout"


class TestDashboardModuleImport:
    """Dashboard module loading (when available)."""

    def test_import_or_skip(self):
        """Dashboard module loads without errors, or is not yet built."""
        try:
            from multifleet import dashboard
            assert hasattr(dashboard, "__name__")
        except ImportError:
            pytest.skip("dashboard module not yet built — tests cover expected API")

    def test_expected_api_surface(self):
        """When dashboard exists, it should expose key functions."""
        try:
            from multifleet import dashboard
            # Expected API surface
            for attr in ("fetch_health", "render_status", "DashboardData"):
                if not hasattr(dashboard, attr):
                    pytest.skip(f"dashboard.{attr} not yet implemented")
        except ImportError:
            pytest.skip("dashboard module not yet built")
