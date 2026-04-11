"""Tests for channel health invariance — auto-repair of broken channels.

12 tests covering:
- Healthy channels produce no violations
- Half channels down -> high severity
- All channels down -> critical severity
- Auto-repair triggered on violation
- EventBus events emitted on state change
- Repair escalation L1 -> L2 -> L3 -> L4
- Graceful when daemon unreachable
- Multiple peers with different health
"""

import time
from unittest.mock import MagicMock, call, patch

import pytest

from multifleet.invariance_rules import ChannelHealthInvariant
from multifleet.health_monitor import FleetHealthMonitor


# ── Fixtures ──

@pytest.fixture
def invariant():
    return ChannelHealthInvariant()


@pytest.fixture
def monitor():
    m = FleetHealthMonitor(daemon_port=8855, check_interval=1, node_id="mac2")
    m.wire_eventbus(MagicMock())
    m.wire_ledger(MagicMock())
    return m


# ── ChannelHealthInvariant.check() ──

class TestChannelHealthCheck:
    def test_healthy_channels_no_violations(self, invariant):
        """All channels up -> no violations."""
        health = {
            "mac1": {"score": "7/7", "broken": []},
            "mac3": {"score": "5/7", "broken": ["P6_wol", "P8_keystroke"]},
        }
        violations = invariant.check(health)
        assert violations == []

    def test_half_channels_down_high_severity(self, invariant):
        """Less than half channels up -> high severity violation."""
        health = {
            "mac1": {"score": "2/7", "broken": ["P1_nats", "P2_http", "P3_chief", "P5_ssh", "P6_wol"]},
        }
        violations = invariant.check(health)
        assert len(violations) == 1
        assert violations[0]["peer"] == "mac1"
        assert violations[0]["severity"] == "high"
        assert violations[0]["action"] == "repair"
        assert violations[0]["up"] == 2

    def test_all_channels_down_critical_severity(self, invariant):
        """Zero channels up -> critical severity."""
        health = {
            "mac1": {"score": "0/7", "broken": ["P1_nats", "P2_http", "P3_chief", "P4_seed", "P5_ssh", "P6_wol", "P7_git"]},
        }
        violations = invariant.check(health)
        assert len(violations) == 1
        assert violations[0]["severity"] == "critical"
        assert violations[0]["up"] == 0

    def test_string_score_format(self, invariant):
        """Handles string shorthand format (no dict wrapper)."""
        health = {"mac1": "1/7"}
        violations = invariant.check(health)
        assert len(violations) == 1
        assert violations[0]["score"] == "1/7"


# ── ChannelHealthInvariant.auto_repair() ──

class TestAutoRepair:
    def test_repair_triggered_on_violation(self, invariant):
        """auto_repair calls health_monitor.auto_repair with correct args."""
        mock_monitor = MagicMock()
        mock_monitor.auto_repair.return_value = {"status": "sent"}

        violation = {
            "peer": "mac1",
            "score": "1/7",
            "up": 1,
            "total": 7,
            "broken": ["P1_nats", "P2_http"],
            "severity": "high",
            "action": "repair",
            "escalation_level": 1,
        }
        result = invariant.auto_repair(violation, health_monitor=mock_monitor)
        assert result["status"] == "sent"
        assert result["level"] == 1
        assert result["level_name"] == "notify"
        mock_monitor.auto_repair.assert_called_once()

    def test_repair_without_monitor(self, invariant):
        """auto_repair returns no_monitor when health_monitor is None."""
        violation = {
            "peer": "mac1", "score": "0/7", "up": 0, "total": 7,
            "broken": [], "severity": "critical", "action": "repair",
            "escalation_level": 1,
        }
        result = invariant.auto_repair(violation, health_monitor=None)
        assert result["status"] == "no_monitor"


# ── Escalation L1 -> L2 -> L3 -> L4 ──

class TestEscalation:
    def test_escalation_levels(self, invariant):
        """Escalation bumps from L1 to L4 after cooldown expires."""
        mock_monitor = MagicMock()
        mock_monitor.auto_repair.return_value = {"status": "sent"}

        violation = {
            "peer": "mac1", "score": "0/7", "up": 0, "total": 7,
            "broken": ["P1_nats"], "severity": "critical",
            "action": "repair", "escalation_level": 1,
        }

        # First attempt: L1
        r1 = invariant.auto_repair(violation, health_monitor=mock_monitor)
        assert r1["level"] == 1
        assert r1["level_name"] == "notify"

        # Simulate cooldown expired by backdating last_repair_attempt
        invariant._last_repair_attempt["mac1"] = time.time() - 400

        # Second attempt: should escalate to L2
        violation["escalation_level"] = invariant.get_escalation_level("mac1")
        r2 = invariant.auto_repair(violation, health_monitor=mock_monitor)
        assert r2["level"] == 2
        assert r2["level_name"] == "guide"

        # Simulate another cooldown
        invariant._last_repair_attempt["mac1"] = time.time() - 400
        violation["escalation_level"] = invariant.get_escalation_level("mac1")
        r3 = invariant.auto_repair(violation, health_monitor=mock_monitor)
        assert r3["level"] == 3
        assert r3["level_name"] == "assist"

        # One more
        invariant._last_repair_attempt["mac1"] = time.time() - 400
        violation["escalation_level"] = invariant.get_escalation_level("mac1")
        r4 = invariant.auto_repair(violation, health_monitor=mock_monitor)
        assert r4["level"] == 4
        assert r4["level_name"] == "remote_fix"

        # L4 is max — stays at L4
        invariant._last_repair_attempt["mac1"] = time.time() - 400
        violation["escalation_level"] = invariant.get_escalation_level("mac1")
        r5 = invariant.auto_repair(violation, health_monitor=mock_monitor)
        assert r5["level"] == 4

    def test_clear_escalation(self, invariant):
        """clear_escalation resets peer back to L1."""
        invariant._escalation_levels["mac1"] = 3
        invariant._last_repair_attempt["mac1"] = time.time()
        invariant.clear_escalation("mac1")
        assert invariant.get_escalation_level("mac1") == 1
        assert "mac1" not in invariant._last_repair_attempt


# ── EventBus events via health monitor integration ──

class TestEventBusIntegration:
    def test_broken_and_healing_events_emitted(self, monitor):
        """check_fleet emits channel.broken and channel.healing events."""
        health = {
            "nodeId": "mac2",
            "status": "ok",
            "uptime_s": 100,
            "activeSessions": 1,
            "peers": {"mac1": {"lastSeen": 5, "sessions": 1}},
            "channel_health": {
                "mac1": {"score": "1/7", "broken": ["P1_nats", "P2_http", "P3_chief", "P4_seed", "P5_ssh", "P6_wol"]},
            },
        }
        with patch.object(monitor, "_fetch_daemon_health", return_value=health):
            result = monitor.check_fleet()

        # Check events were emitted
        emit_calls = monitor._eventbus.emit.call_args_list
        event_types = [c[0][0] for c in emit_calls]
        assert "channel.broken" in event_types
        assert "channel.healing" in event_types

        # Check alert recorded
        inv_alerts = [a for a in result["alerts"] if a["type"] == "channel_invariance"]
        assert len(inv_alerts) == 1
        assert inv_alerts[0]["peer"] == "mac1"

    def test_restored_event_emitted(self, monitor):
        """channel.restored emitted when peer recovers from broken state."""
        # Set up broken state
        monitor._prev_channel_state["mac1"] = "broken"
        monitor._channel_invariant._escalation_levels["mac1"] = 2

        health = {
            "nodeId": "mac2",
            "status": "ok",
            "uptime_s": 100,
            "activeSessions": 1,
            "peers": {"mac1": {"lastSeen": 5, "sessions": 1}},
            "channel_health": {
                "mac1": {"score": "6/7", "broken": ["P6_wol"]},
            },
        }
        with patch.object(monitor, "_fetch_daemon_health", return_value=health):
            monitor.check_fleet()

        emit_calls = monitor._eventbus.emit.call_args_list
        event_types = [c[0][0] for c in emit_calls]
        assert "channel.restored" in event_types
        # Escalation should be cleared
        assert monitor._channel_invariant.get_escalation_level("mac1") == 1


# ── Edge cases ──

class TestEdgeCases:
    def test_graceful_when_daemon_unreachable(self, monitor):
        """No crash when daemon is unreachable — channel invariance skipped."""
        with patch.object(monitor, "_fetch_daemon_health", return_value=None):
            result = monitor.check_fleet()
        assert result["status"] == "unreachable"
        # No events emitted (no channel_health data to check)
        monitor._eventbus.emit.assert_not_called()

    def test_multiple_peers_different_health(self, invariant):
        """Multiple peers: only degraded ones produce violations."""
        health = {
            "mac1": {"score": "6/7", "broken": ["P6_wol"]},
            "mac2": {"score": "1/7", "broken": ["P1_nats", "P2_http", "P3_chief", "P4_seed", "P5_ssh", "P6_wol"]},
            "mac3": {"score": "0/7", "broken": ["P1_nats", "P2_http", "P3_chief", "P4_seed", "P5_ssh", "P6_wol", "P7_git"]},
        }
        violations = invariant.check(health)
        assert len(violations) == 2
        peers = {v["peer"] for v in violations}
        assert peers == {"mac2", "mac3"}
        # mac3 should be critical (0 channels), mac2 high
        by_peer = {v["peer"]: v for v in violations}
        assert by_peer["mac3"]["severity"] == "critical"
        assert by_peer["mac2"]["severity"] == "high"
