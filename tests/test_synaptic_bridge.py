"""Tests for multifleet.synaptic_bridge — Synaptic 8th Intelligence fleet awareness."""

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from multifleet.synaptic_bridge import (
    SynapticBridge,
    Insight,
    ALERT_THRESHOLD_FAILURES,
    INSIGHT_TTL_S,
    PATTERN_WINDOW_S,
)


# ═══════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════

def _make_event(event_type: str, payload: dict = None, node_id: str = "") -> dict:
    """Create a minimal fleet event dict."""
    meta = {}
    if node_id:
        meta["node_id"] = node_id
    return {
        "type": event_type,
        "payload": payload or {},
        "metadata": meta,
    }


@pytest.fixture
def bridge():
    """SynapticBridge without EventBus (direct feed via on_fleet_event)."""
    return SynapticBridge()


@pytest.fixture
def bridge_with_bus():
    """SynapticBridge wired to a mock EventBus."""
    bus = MagicMock()
    b = SynapticBridge(event_bus=bus)
    return b, bus


# ═══════════════════════════════════════════════════════════════════
# Test: Insight dataclass
# ═══════════════════════════════════════════════════════════════════

class TestInsight:
    def test_to_dict_has_all_fields(self):
        i = Insight(
            category="pattern",
            severity="warning",
            message="test message",
            source_event="fleet.test",
            node_id="mac1",
        )
        d = i.to_dict()
        assert d["category"] == "pattern"
        assert d["severity"] == "warning"
        assert d["message"] == "test message"
        assert d["source_event"] == "fleet.test"
        assert d["node_id"] == "mac1"
        assert "timestamp" in d
        assert "evidence" in d

    def test_age_and_expiry(self):
        i = Insight(
            category="test", severity="info", message="old",
            timestamp=time.time() - INSIGHT_TTL_S - 1,
        )
        assert i.is_expired is True
        fresh = Insight(category="test", severity="info", message="new")
        assert fresh.is_expired is False
        assert fresh.age_s < 2  # just created


# ═══════════════════════════════════════════════════════════════════
# Test: Event processing and insight generation
# ═══════════════════════════════════════════════════════════════════

class TestEventProcessing:
    def test_failure_events_generate_insight_after_threshold(self, bridge):
        """Repeated failures within window trigger a pattern insight."""
        for _ in range(ALERT_THRESHOLD_FAILURES):
            bridge.on_fleet_event(_make_event(
                "fleet.channel.failure",
                {"error": "NATS timeout"},
                node_id="mac2",
            ))
        insights = bridge.get_active_insights()
        assert len(insights) >= 1
        pattern = [i for i in insights if i["category"] == "pattern"]
        assert len(pattern) == 1
        assert "Repeated failures" in pattern[0]["message"]
        assert "mac2" in pattern[0]["message"]

    def test_single_failure_no_insight(self, bridge):
        """One failure is not a pattern — no insight generated."""
        bridge.on_fleet_event(_make_event(
            "fleet.channel.failure", {"error": "timeout"}, node_id="mac1",
        ))
        insights = bridge.get_active_insights()
        pattern = [i for i in insights if i["category"] == "pattern"]
        assert len(pattern) == 0

    def test_peer_offline_generates_drift_insight(self, bridge):
        bridge.on_fleet_event(_make_event(
            "fleet.peer.offline", {}, node_id="mac3",
        ))
        insights = bridge.get_active_insights()
        assert any(i["category"] == "drift" for i in insights)
        drift = [i for i in insights if i["category"] == "drift"][0]
        assert "mac3" in drift["message"]
        assert "offline" in drift["message"]

    def test_peer_online_generates_info_insight(self, bridge):
        bridge.on_fleet_event(_make_event(
            "fleet.peer.online", {}, node_id="mac2",
        ))
        insights = bridge.get_active_insights()
        assert any(i["severity"] == "info" for i in insights)

    def test_health_degraded_generates_warning(self, bridge):
        bridge.on_fleet_event(_make_event(
            "fleet.health.check", {"status": "degraded", "reason": "high latency"},
        ))
        insights = bridge.get_active_insights()
        health = [i for i in insights if i["category"] == "health"]
        assert len(health) == 1
        assert health[0]["severity"] == "warning"

    def test_health_critical_generates_critical(self, bridge):
        bridge.on_fleet_event(_make_event(
            "fleet.health.check", {"status": "critical", "reason": "node down"},
        ))
        insights = bridge.get_active_insights()
        health = [i for i in insights if i["category"] == "health"]
        assert len(health) == 1
        assert health[0]["severity"] == "critical"

    def test_quorum_low_confidence_generates_warning(self, bridge):
        bridge.on_fleet_event(_make_event(
            "fleet.quorum.decided",
            {"decision": "merge-branch-x", "confidence": 0.3},
        ))
        insights = bridge.get_active_insights()
        coord = [i for i in insights if i["category"] == "coordination"]
        assert len(coord) == 1
        assert "Low-confidence" in coord[0]["message"]

    def test_sync_hold_message_generates_coordination_insight(self, bridge):
        bridge.on_fleet_event(_make_event(
            "fleet.message.received",
            {"subject": "SYNC_HOLD: schema migration", "from": "mac1"},
        ))
        insights = bridge.get_active_insights()
        coord = [i for i in insights if i["category"] == "coordination"]
        assert len(coord) == 1
        assert "SYNC HOLD" in coord[0]["message"]


# ═══════════════════════════════════════════════════════════════════
# Test: Alert thresholds
# ═══════════════════════════════════════════════════════════════════

class TestAlertThresholds:
    def test_no_alert_when_idle(self, bridge):
        assert bridge.should_alert_aaron() is False

    def test_alert_on_critical_insight(self, bridge):
        bridge.on_fleet_event(_make_event(
            "fleet.health.check", {"status": "critical"},
        ))
        assert bridge.should_alert_aaron() is True

    def test_alert_on_multiple_warnings(self, bridge):
        """3+ warnings within PATTERN_WINDOW_S triggers alert."""
        bridge.on_fleet_event(_make_event("fleet.peer.offline", {}, "mac1"))
        bridge.on_fleet_event(_make_event("fleet.peer.offline", {}, "mac2"))
        bridge.on_fleet_event(_make_event("fleet.peer.offline", {}, "mac3"))
        assert bridge.should_alert_aaron() is True

    def test_no_alert_on_two_warnings(self, bridge):
        bridge.on_fleet_event(_make_event("fleet.peer.offline", {}, "mac1"))
        bridge.on_fleet_event(_make_event("fleet.peer.offline", {}, "mac2"))
        assert bridge.should_alert_aaron() is False


# ═══════════════════════════════════════════════════════════════════
# Test: Deduplication
# ═══════════════════════════════════════════════════════════════════

class TestDeduplication:
    def test_duplicate_insights_suppressed(self, bridge):
        """Same category+message within 60s is deduplicated."""
        for _ in range(5):
            bridge.on_fleet_event(_make_event(
                "fleet.peer.offline", {}, node_id="mac2",
            ))
        insights = bridge.get_active_insights()
        drift = [i for i in insights if i["category"] == "drift"]
        assert len(drift) == 1  # only one, not five


# ═══════════════════════════════════════════════════════════════════
# Test: EventBus integration
# ═══════════════════════════════════════════════════════════════════

class TestEventBusIntegration:
    def test_start_subscribes_to_fleet_events(self, bridge_with_bus):
        bridge, bus = bridge_with_bus
        bridge.start()
        bus.on.assert_called_once_with("fleet.*", bridge._on_fleet_event)
        assert bridge._started is True

    def test_start_is_idempotent(self, bridge_with_bus):
        bridge, bus = bridge_with_bus
        bridge.start()
        bridge.start()
        assert bus.on.call_count == 1

    def test_stop_unsubscribes(self, bridge_with_bus):
        bridge, bus = bridge_with_bus
        bridge.start()
        bridge.stop()
        bus.off.assert_called_once_with("fleet.*", bridge._on_fleet_event)
        assert bridge._started is False

    def test_start_without_bus_is_noop(self):
        bridge = SynapticBridge()
        bridge.start()  # should not raise
        assert bridge._started is False


# ═══════════════════════════════════════════════════════════════════
# Test: Stats and expiry
# ═══════════════════════════════════════════════════════════════════

class TestStatsAndExpiry:
    def test_stats_reflect_processed_events(self, bridge):
        bridge.on_fleet_event(_make_event("fleet.peer.online", {}, "mac1"))
        bridge.on_fleet_event(_make_event("fleet.peer.online", {}, "mac2"))
        s = bridge.stats
        assert s["total_events_processed"] == 2
        assert s["nodes_tracked"] == 2
        assert s["started"] is False

    def test_expired_insights_excluded_from_active(self, bridge):
        """Insights older than TTL are filtered out."""
        bridge.on_fleet_event(_make_event("fleet.peer.offline", {}, "mac1"))
        assert len(bridge.get_active_insights()) == 1

        # Manually expire the insight
        with bridge._lock:
            bridge._insights[0].timestamp = time.time() - INSIGHT_TTL_S - 1

        assert len(bridge.get_active_insights()) == 0

    def test_rolling_buffer_respects_max(self):
        bridge = SynapticBridge(max_insights=3)
        # Generate 5 distinct insights (different node_ids to avoid dedup)
        for i in range(5):
            bridge.on_fleet_event(_make_event(
                "fleet.peer.offline", {}, node_id=f"mac{i}",
            ))
        # Buffer maxlen=3, so only 3 survive
        with bridge._lock:
            assert len(bridge._insights) == 3


# ═══════════════════════════════════════════════════════════════════
# Test: IDE Panel integration
# ═══════════════════════════════════════════════════════════════════

class TestIDEPanelIntegration:
    def test_synaptic_panel_with_bridge(self):
        from multifleet.ide_panels import IDEPanelServer
        bridge = SynapticBridge()
        bridge.on_fleet_event(_make_event("fleet.peer.offline", {}, "mac2"))
        server = IDEPanelServer(synaptic_bridge=bridge)
        panel = server.synaptic_panel()
        assert panel["panel_id"] == "synaptic"
        assert panel["title"] == "Synaptic Consciousness"
        assert len(panel["data"]) >= 1
        assert "1 warning" in panel["summary"]

    def test_synaptic_panel_without_bridge(self):
        from multifleet.ide_panels import IDEPanelServer
        server = IDEPanelServer(synaptic_bridge=None)
        # Patch import to fail
        with pytest.MonkeyPatch.context() as m:
            m.setattr(
                "multifleet.ide_panels.IDEPanelServer._get_synaptic_bridge",
                lambda self: None,
            )
            panel = server.synaptic_panel()
        assert panel["panel_id"] == "synaptic"
        assert "unavailable" in panel["summary"]

    def test_synaptic_panel_in_all_panels(self):
        from multifleet.ide_panels import IDEPanelServer
        bridge = SynapticBridge()
        server = IDEPanelServer(synaptic_bridge=bridge)
        result = server.all_panels()
        panel_ids = [p["panel_id"] for p in result["panels"]]
        assert "synaptic" in panel_ids
