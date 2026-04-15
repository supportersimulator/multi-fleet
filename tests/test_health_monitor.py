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


class TestPartitionHealReconciliation:
    """Tests for automatic lease reconciliation on partition heal."""

    def test_partition_heal_triggers_reconcile(self, monitor):
        """When a peer transitions from offline to healthy, reconcile() is called."""
        mock_lm = MagicMock()
        mock_lm.list_provisional.return_value = [
            {"resource_id": "task-1", "node_id": "mac1", "fence_token": 1,
             "partition_id": "part-abc", "lease_type": "short_task"},
        ]
        mock_lm.reconcile.return_value = [
            {"resource_id": "task-1", "verdict": "accepted", "node_id": "mac1"},
        ]
        monitor.wire_lease_manager(mock_lm)

        # Seed peer as offline
        monitor._prev_peer_status["mac1"] = "offline"

        # Health response shows mac1 now reachable
        health = {**MOCK_HEALTH}
        health["peers"] = {"mac1": {"lastSeen": 5, "sessions": 1}}
        health["channel_health"] = {"mac1": {"score": "6/7", "broken": ["P6_wol"]}}

        with patch.object(monitor, "_fetch_daemon_health", return_value=health):
            result = monitor.check_fleet()

        mock_lm.list_provisional.assert_called_once_with(node_id="mac1")
        mock_lm.reconcile.assert_called_once()
        heal_alerts = [a for a in result["alerts"] if a["type"] == "partition_healed"]
        assert len(heal_alerts) == 1
        assert heal_alerts[0]["peer"] == "mac1"
        assert heal_alerts[0]["provisional_count"] == 1

    def test_reconcile_failure_does_not_crash(self, monitor):
        """Reconciliation errors are caught — health monitoring continues."""
        mock_lm = MagicMock()
        mock_lm.list_provisional.side_effect = RuntimeError("DB locked")
        monitor.wire_lease_manager(mock_lm)

        monitor._prev_peer_status["mac1"] = "offline"

        health = {**MOCK_HEALTH}
        health["peers"] = {"mac1": {"lastSeen": 5, "sessions": 1}}
        health["channel_health"] = {"mac1": {"score": "6/7", "broken": ["P6_wol"]}}

        with patch.object(monitor, "_fetch_daemon_health", return_value=health):
            result = monitor.check_fleet()

        # Should not raise — check completed
        assert result["status"] == "ok"
        heal_alerts = [a for a in result["alerts"] if a["type"] == "partition_healed"]
        assert len(heal_alerts) == 1

    def test_partition_heal_emits_event(self, monitor):
        """health.partition.healed event is emitted on partition heal."""
        mock_lm = MagicMock()
        mock_lm.list_provisional.return_value = []
        monitor.wire_lease_manager(mock_lm)

        monitor._prev_peer_status["mac1"] = "offline"

        health = {**MOCK_HEALTH}
        health["peers"] = {"mac1": {"lastSeen": 5, "sessions": 1}}
        health["channel_health"] = {"mac1": {"score": "6/7", "broken": ["P6_wol"]}}

        emitted_events = []
        original_emit = monitor._emit_event

        def capture_emit(event_type, payload):
            emitted_events.append((event_type, payload))
            original_emit(event_type, payload)

        with patch.object(monitor, "_emit_event", side_effect=capture_emit):
            with patch.object(monitor, "_fetch_daemon_health", return_value=health):
                monitor.check_fleet()

        partition_events = [e for e in emitted_events if e[0] == "health.partition.healed"]
        assert len(partition_events) == 1
        assert partition_events[0][1]["peer"] == "mac1"

    def test_no_reconcile_without_lease_manager(self, monitor):
        """Without a wired LeaseManager, partition heal is detected but no reconcile."""
        monitor._prev_peer_status["mac1"] = "offline"

        health = {**MOCK_HEALTH}
        health["peers"] = {"mac1": {"lastSeen": 5, "sessions": 1}}
        health["channel_health"] = {"mac1": {"score": "6/7", "broken": ["P6_wol"]}}

        with patch.object(monitor, "_fetch_daemon_health", return_value=health):
            result = monitor.check_fleet()

        # No crash, no partition_healed alert (no lease manager to trigger it)
        heal_alerts = [a for a in result["alerts"] if a["type"] == "partition_healed"]
        assert len(heal_alerts) == 0

    def test_no_reconcile_when_peer_stays_healthy(self, monitor):
        """No reconciliation triggered when peer was already healthy."""
        mock_lm = MagicMock()
        monitor.wire_lease_manager(mock_lm)

        # Peer was already healthy
        monitor._prev_peer_status["mac1"] = "healthy"

        health = {**MOCK_HEALTH}
        health["peers"] = {"mac1": {"lastSeen": 5, "sessions": 1}}
        health["channel_health"] = {"mac1": {"score": "6/7", "broken": ["P6_wol"]}}

        with patch.object(monitor, "_fetch_daemon_health", return_value=health):
            monitor.check_fleet()

        mock_lm.list_provisional.assert_not_called()
        mock_lm.reconcile.assert_not_called()
