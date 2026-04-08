"""Tests for FleetHealthMonitor -- anomaly detection, auto-repair, event emission."""

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from multifleet.health_monitor import FleetHealthMonitor


@pytest.fixture
def monitor():
    m = FleetHealthMonitor(daemon_port=8855, check_interval=1, node_id="mac2")
    m.wire_eventbus(MagicMock())
    m.wire_ledger(MagicMock())
    return m


MOCK_HEALTH = {
    "nodeId": "mac2",
    "status": "ok",
    "uptime_s": 3600,
    "activeSessions": 1,
    "peers": {
        "mac1": {"lastSeen": 5, "sessions": 2},
        "mac3": {"lastSeen": 200, "sessions": 0},
    },
    "channel_health": {
        "mac1": {"score": "6/7", "broken": ["P6_wol"]},
        "mac3": {"score": "2/7", "broken": ["P1_nats", "P2_http", "P3_chief", "P5_ssh", "P6_wol"]},
    },
}


class TestCheckFleet:
    def test_parses_healthy_and_degraded(self, monitor):
        with patch.object(monitor, "_fetch_daemon_health", return_value=MOCK_HEALTH):
            result = monitor.check_fleet()

        assert result["status"] == "ok"
        assert "mac1" in result["nodes"]
        assert result["nodes"]["mac1"]["status"] == "healthy"
        assert result["nodes"]["mac3"]["status"] == "offline"  # lastSeen > 120

    def test_unreachable_daemon(self, monitor):
        with patch.object(monitor, "_fetch_daemon_health", return_value=None):
            result = monitor.check_fleet()
        assert result["status"] == "unreachable"

    def test_detects_transition(self, monitor):
        # Seed history so there's a "previous" status
        monitor._record_snapshot("mac1", {"status": "healthy", "timestamp": time.time() - 60})
        health_degraded = {**MOCK_HEALTH}
        health_degraded["peers"] = {"mac1": {"lastSeen": 5, "sessions": 1}}
        health_degraded["channel_health"] = {
            "mac1": {"score": "2/7", "broken": ["P1_nats", "P2_http", "P3_chief", "P5_ssh", "P6_wol"]},
        }
        with patch.object(monitor, "_fetch_daemon_health", return_value=health_degraded):
            result = monitor.check_fleet()

        transitions = [a for a in result["alerts"] if a["type"] == "transition"]
        assert len(transitions) == 1
        assert transitions[0]["from"] == "healthy"
        assert transitions[0]["to"] == "degraded"


class TestDetectAnomalies:
    def test_score_drop(self, monitor):
        ts = time.time()
        # Seed history with high score
        monitor._record_snapshot("mac1", {"score": 7, "status": "healthy", "timestamp": ts - 60})
        monitor._record_snapshot("mac1", {"score": 7, "status": "healthy", "timestamp": ts - 30})
        nodes = {"mac2": {"status": "ok"}, "mac1": {"score": 4, "status": "degraded", "timestamp": ts}}
        anomalies = monitor.detect_anomalies(nodes)
        assert any(a["type"] == "score_drop" for a in anomalies)

    def test_all_peers_offline(self, monitor):
        nodes = {
            "mac2": {"status": "ok"},
            "mac1": {"status": "offline"},
            "mac3": {"status": "offline"},
        }
        anomalies = monitor.detect_anomalies(nodes)
        assert any(a["type"] == "all_peers_offline" for a in anomalies)


class TestAutoRepair:
    def test_records_repair_in_ledger(self, monitor):
        with patch("urllib.request.urlopen") as mock_url:
            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_url.return_value = mock_resp
            result = monitor.auto_repair("mac1", "degraded: broken channels ['P1_nats']")

        assert result["status"] == "sent"
        assert monitor._ledger.record.called

    def test_handles_connection_error(self, monitor):
        with patch("urllib.request.urlopen", side_effect=ConnectionError("refused")):
            result = monitor.auto_repair("mac1", "test issue")
        assert result["status"] == "error"


class TestFleetStatus:
    def test_returns_status_dict(self, monitor):
        status = monitor.get_fleet_status()
        assert status["node_id"] == "mac2"
        assert "checks_run" in status
        assert "active_alerts" in status


class TestContinuousLoop:
    def test_max_checks_stops(self, monitor):
        with patch.object(monitor, "_fetch_daemon_health", return_value=MOCK_HEALTH):
            monitor.run_continuous(max_checks=2)
        assert monitor._checks_run == 2
        assert not monitor._running
