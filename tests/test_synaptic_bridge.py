"""Tests for multifleet.synaptic_bridge — Synaptic 8th Intelligence fleet awareness."""

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

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
# Test: Surgeon event processing
# ═══════════════════════════════════════════════════════════════════

class TestSurgeonEvents:
    def test_low_confidence_verdict_generates_insight(self, bridge):
        """Low-confidence surgeon verdict surfaces a warning."""
        bridge._on_surgeon_event(_make_event(
            "surgeon.verdict",
            {"surgeon": "cardiologist", "verdict": "uncertain", "confidence": 0.3},
        ))
        insights = bridge.get_active_insights()
        surgeon_insights = [i for i in insights if i["category"] == "surgeon"]
        assert len(surgeon_insights) == 1
        assert "cardiologist" in surgeon_insights[0]["message"]
        assert "30%" in surgeon_insights[0]["message"]

    def test_high_confidence_verdict_no_insight(self, bridge):
        """High-confidence verdict does not generate an insight."""
        bridge._on_surgeon_event(_make_event(
            "surgeon.verdict",
            {"surgeon": "atlas", "verdict": "approve", "confidence": 0.95},
        ))
        insights = bridge.get_active_insights()
        surgeon_insights = [i for i in insights if i["category"] == "surgeon"]
        assert len(surgeon_insights) == 0

    def test_cross_exam_disagreement_generates_insight(self, bridge):
        """Cross-exam without consensus generates a warning."""
        bridge._on_surgeon_event(_make_event(
            "surgeon.cross_exam.complete",
            {"topic": "schema migration", "consensus": False, "summary": "split"},
        ))
        insights = bridge.get_active_insights()
        surgeon_insights = [i for i in insights if i["category"] == "surgeon"]
        assert len(surgeon_insights) == 1
        assert "disagreement" in surgeon_insights[0]["message"].lower()
        assert "schema migration" in surgeon_insights[0]["message"]

    def test_cross_exam_consensus_no_insight(self, bridge):
        """Cross-exam with consensus does not generate an insight."""
        bridge._on_surgeon_event(_make_event(
            "surgeon.cross_exam.complete",
            {"topic": "deploy plan", "consensus": True},
        ))
        insights = bridge.get_active_insights()
        surgeon_insights = [i for i in insights if i["category"] == "surgeon"]
        assert len(surgeon_insights) == 0

    def test_surgeon_events_tracked_in_stats(self, bridge):
        """Surgeon events are counted in stats."""
        bridge._on_surgeon_event(_make_event(
            "surgeon.verdict",
            {"surgeon": "atlas", "verdict": "ok", "confidence": 0.9},
        ))
        bridge._on_surgeon_event(_make_event(
            "surgeon.cross_exam.complete",
            {"topic": "test", "consensus": True},
        ))
        s = bridge.stats
        assert s["surgeon_verdicts_tracked"] == 1
        assert s["cross_exams_seen"] == 1
        assert s["total_events_processed"] == 2


# ═══════════════════════════════════════════════════════════════════
# Test: Evidence event processing
# ═══════════════════════════════════════════════════════════════════

class TestEvidenceEvents:
    def test_chain_break_generates_critical_insight(self, bridge):
        """Evidence chain break is always critical."""
        bridge._on_evidence_event(_make_event(
            "evidence.chain_broken",
            {"broken_at_seq": 42, "detail": "hash mismatch"},
        ))
        insights = bridge.get_active_insights()
        evidence_insights = [i for i in insights if i["category"] == "evidence"]
        assert len(evidence_insights) == 1
        assert evidence_insights[0]["severity"] == "critical"
        assert "42" in evidence_insights[0]["message"]

    def test_evidence_recorded_tracked(self, bridge):
        """Evidence recorded entries are tracked in stats."""
        bridge._on_evidence_event(_make_event(
            "evidence.recorded",
            {"entry_id": "e1", "event_type": "probe.finding", "node_id": "mac1"},
        ))
        s = bridge.stats
        assert s["evidence_entries_tracked"] == 1
        assert s["total_events_processed"] == 1

    def test_chain_breaks_counted(self, bridge):
        """Chain breaks are counted in stats."""
        bridge._on_evidence_event(_make_event(
            "evidence.chain_broken", {"broken_at_seq": 1, "detail": "x"},
        ))
        bridge._on_evidence_event(_make_event(
            "evidence.chain_broken", {"broken_at_seq": 2, "detail": "y"},
        ))
        s = bridge.stats
        assert s["chain_breaks"] == 2


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

    def test_evidence_chain_break_triggers_alert(self, bridge):
        """Evidence chain break (critical) triggers alert."""
        bridge._on_evidence_event(_make_event(
            "evidence.chain_broken", {"broken_at_seq": 1, "detail": "hash"},
        ))
        assert bridge.should_alert_aaron() is True


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
    def test_start_subscribes_to_all_namespaces(self, bridge_with_bus):
        bridge, bus = bridge_with_bus
        bridge.start()
        # Should subscribe to fleet.*, surgeon.*, evidence.*
        call_args = [c[0] for c in bus.on.call_args_list]
        patterns = [args[0] for args in call_args]
        assert "fleet.*" in patterns
        assert "surgeon.*" in patterns
        assert "evidence.*" in patterns
        assert bridge._started is True

    def test_start_is_idempotent(self, bridge_with_bus):
        bridge, bus = bridge_with_bus
        bridge.start()
        bridge.start()
        assert bus.on.call_count == 3  # fleet.* + surgeon.* + evidence.*

    def test_stop_unsubscribes_all(self, bridge_with_bus):
        bridge, bus = bridge_with_bus
        bridge.start()
        bridge.stop()
        call_args = [c[0] for c in bus.off.call_args_list]
        patterns = [args[0] for args in call_args]
        assert "fleet.*" in patterns
        assert "surgeon.*" in patterns
        assert "evidence.*" in patterns
        assert bridge._started is False

    def test_start_without_bus_is_noop(self):
        bridge = SynapticBridge()
        bridge.start()  # should not raise
        assert bridge._started is False


# ═══════════════════════════════════════════════════════════════════
# Test: emit_insight
# ═══════════════════════════════════════════════════════════════════

class TestEmitInsight:
    def test_emit_insight_records_locally(self, bridge):
        """emit_insight adds to local insight buffer."""
        bridge.emit_insight("Test insight", ["ev1", "ev2"])
        insights = bridge.get_active_insights()
        assert len(insights) == 1
        assert insights[0]["message"] == "Test insight"
        assert insights[0]["category"] == "pattern"

    def test_emit_insight_emits_to_bus(self, bridge_with_bus):
        """emit_insight emits synaptic.insight event on the bus."""
        bridge, bus = bridge_with_bus
        result = bridge.emit_insight("Bus insight", ["evidence1"])
        bus.emit.assert_called_once()
        call_args = bus.emit.call_args
        assert call_args[0][0] == "synaptic.insight"
        assert call_args[0][1]["message"] == "Bus insight"
        assert call_args[0][1]["evidence"] == ["evidence1"]

    def test_emit_insight_no_bus_returns_none(self, bridge):
        """emit_insight without bus returns None (no crash)."""
        result = bridge.emit_insight("No bus insight")
        assert result is None
        # But insight is still recorded locally
        assert len(bridge.get_active_insights()) == 1


# ═══════════════════════════════════════════════════════════════════
# Test: get_personality_state
# ═══════════════════════════════════════════════════════════════════

class TestGetPersonalityState:
    def test_degraded_when_module_unavailable(self, bridge):
        """Returns degraded state when memory module is not importable."""
        # Force modules_probed so it doesn't try to import
        bridge._modules_probed = True
        bridge._personality_factory = None
        state = bridge.get_personality_state()
        assert state["available"] is False
        assert state["mode"] == "degraded"

    def test_full_when_module_available(self, bridge):
        """Returns full state when personality module works."""
        mock_personality = MagicMock()
        mock_personality.get_personality_state.return_value = {
            "voice": {"warmth": 0.8},
            "themes": ["async patterns"],
        }
        bridge._modules_probed = True
        bridge._personality_factory = lambda: mock_personality
        state = bridge.get_personality_state()
        assert state["available"] is True
        assert state["mode"] == "full"
        assert state["voice"] == {"warmth": 0.8}

    def test_graceful_on_personality_exception(self, bridge):
        """Returns degraded if personality.get_personality_state() throws."""
        mock_personality = MagicMock()
        mock_personality.get_personality_state.side_effect = RuntimeError("db locked")
        bridge._modules_probed = True
        bridge._personality_factory = lambda: mock_personality
        state = bridge.get_personality_state()
        assert state["available"] is False
        assert "db locked" in state["reason"]


# ═══════════════════════════════════════════════════════════════════
# Test: feed_pattern
# ═══════════════════════════════════════════════════════════════════

class TestFeedPattern:
    def test_feed_pattern_no_engine_no_crash(self, bridge):
        """feed_pattern without pattern engine does not crash."""
        bridge._modules_probed = True
        bridge._pattern_engine_factory = None
        bridge.feed_pattern("surgeon.verdict", {"verdict": "ok"})
        # No exception = pass

    def test_feed_pattern_calls_engine(self, bridge):
        """feed_pattern calls engine.feed_event when available."""
        mock_engine = MagicMock()
        mock_engine.feed_event = MagicMock()
        bridge._modules_probed = True
        bridge._pattern_engine_factory = lambda: mock_engine
        bridge.feed_pattern("surgeon.verdict", {"verdict": "ok"})
        mock_engine.feed_event.assert_called_once_with(
            "surgeon.verdict", {"verdict": "ok"}
        )

    def test_feed_pattern_graceful_on_exception(self, bridge):
        """feed_pattern handles engine exceptions gracefully."""
        mock_engine = MagicMock()
        mock_engine.feed_event.side_effect = RuntimeError("scan failed")
        bridge._modules_probed = True
        bridge._pattern_engine_factory = lambda: mock_engine
        bridge.feed_pattern("evidence.recorded", {"entry_id": "e1"})
        # No exception = pass


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

    def test_stats_include_new_fields(self, bridge):
        """Stats include surgeon/evidence/personality tracking."""
        s = bridge.stats
        assert "surgeon_verdicts_tracked" in s
        assert "cross_exams_seen" in s
        assert "evidence_entries_tracked" in s
        assert "chain_breaks" in s
        assert "personality_available" in s
        assert "pattern_engine_available" in s

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


# ═══════════════════════════════════════════════════════════════════
# Test: Auto-emit on insight creation
# ═══════════════════════════════════════════════════════════════════

class TestAutoEmit:
    def test_fleet_insight_emitted_to_bus(self, bridge_with_bus):
        """When fleet events generate insights, synaptic.insight is emitted."""
        bridge, bus = bridge_with_bus
        bridge.on_fleet_event(_make_event(
            "fleet.peer.offline", {}, node_id="mac1",
        ))
        # bus.emit should have been called with synaptic.insight
        emit_calls = [c for c in bus.emit.call_args_list
                      if c[0][0] == "synaptic.insight"]
        assert len(emit_calls) == 1
        assert emit_calls[0][0][1]["category"] == "drift"

    def test_surgeon_insight_emitted_to_bus(self, bridge_with_bus):
        """Surgeon disagreement insight is emitted to bus."""
        bridge, bus = bridge_with_bus
        bridge._on_surgeon_event(_make_event(
            "surgeon.cross_exam.complete",
            {"topic": "deploy", "consensus": False, "summary": "split"},
        ))
        emit_calls = [c for c in bus.emit.call_args_list
                      if c[0][0] == "synaptic.insight"]
        assert len(emit_calls) == 1
        assert emit_calls[0][0][1]["category"] == "surgeon"


# ═══════════════════════════════════════════════════════════════════
# Test: Scheduled pattern scan
# ═══════════════════════════════════════════════════════════════════

class TestPatternScan:
    def test_start_scan_without_engine_returns_false(self):
        """Cannot start scan if pattern engine is unavailable."""
        bridge = SynapticBridge()
        bridge._modules_probed = True
        bridge._pattern_engine_factory = None
        assert bridge.start_pattern_scan() is False

    def test_start_scan_with_engine(self):
        """Scan thread starts when engine is available."""
        bridge = SynapticBridge()
        mock_engine = MagicMock()
        bridge._modules_probed = True
        bridge._pattern_engine_factory = lambda: mock_engine
        try:
            assert bridge.start_pattern_scan(interval_s=3600) is True
            assert bridge._scan_thread is not None
            assert bridge._scan_thread.is_alive()
        finally:
            bridge.stop_pattern_scan()

    def test_start_scan_idempotent(self):
        """Starting scan twice returns False on second call."""
        bridge = SynapticBridge()
        mock_engine = MagicMock()
        bridge._modules_probed = True
        bridge._pattern_engine_factory = lambda: mock_engine
        try:
            assert bridge.start_pattern_scan(interval_s=3600) is True
            assert bridge.start_pattern_scan(interval_s=3600) is False
        finally:
            bridge.stop_pattern_scan()

    def test_stop_scan_safe_when_not_running(self):
        """stop_pattern_scan is safe to call when no scan is active."""
        bridge = SynapticBridge()
        bridge.stop_pattern_scan()  # should not raise

    def test_run_single_scan_emits_events(self):
        """_run_single_scan emits pattern events when patterns found."""
        from multifleet.synaptic_bridge import Insight
        from dataclasses import dataclass

        bus = MagicMock()
        bridge = SynapticBridge(event_bus=bus)

        mock_engine = MagicMock()
        # scan() returns a result with new patterns
        mock_result = MagicMock()
        mock_result.scan_id = "test123"
        mock_result.patterns_found = 1
        mock_result.new_patterns = 1
        mock_result.reinforced_patterns = 0
        mock_result.scan_duration_ms = 42.0
        mock_engine.scan.return_value = mock_result

        # get_recent returns a mock pattern
        mock_pattern = MagicMock()
        mock_pattern.pattern_type = "recurring_error"
        mock_pattern.title = "Test Pattern"
        mock_pattern.confidence = 0.8
        mock_pattern.description = "Test description"
        mock_pattern.actionable = True
        mock_pattern.suggested_action = "Fix it"
        mock_engine.get_recent.return_value = [mock_pattern]
        mock_engine.surface_to_outbox.return_value = 1

        bridge._modules_probed = True
        bridge._pattern_engine_factory = lambda: mock_engine

        bridge._run_single_scan()

        # Verify scan was called
        mock_engine.scan.assert_called_once()
        # Verify pattern event emitted
        pattern_events = [
            c for c in bus.emit.call_args_list
            if c[0][0] == "synaptic.pattern.detected"
        ]
        assert len(pattern_events) == 1
        payload = pattern_events[0][0][1]
        assert payload["pattern_type"] == "recurring_error"
        assert payload["title"] == "Test Pattern"
        assert payload["confidence"] == 0.8
        # Verify outbox surfacing
        mock_engine.surface_to_outbox.assert_called_once()

    def test_stats_include_scan_fields(self):
        """Stats dict includes pattern_scan_active and failure count."""
        bridge = SynapticBridge()
        s = bridge.stats
        assert "pattern_scan_active" in s
        assert "pattern_scan_failures" in s
        assert s["pattern_scan_active"] is False
        assert s["pattern_scan_failures"] == 0

    def test_interval_floor_at_30s(self):
        """Scan interval cannot go below 30s."""
        bridge = SynapticBridge()
        mock_engine = MagicMock()
        bridge._modules_probed = True
        bridge._pattern_engine_factory = lambda: mock_engine
        try:
            bridge.start_pattern_scan(interval_s=5)
            assert bridge._scan_interval_s == 30
        finally:
            bridge.stop_pattern_scan()

    def test_bridge_start_auto_starts_scan(self):
        """SynapticBridge.start() auto-starts pattern scan."""
        bus = MagicMock()
        bridge = SynapticBridge(event_bus=bus)
        mock_engine = MagicMock()
        bridge._modules_probed = True
        bridge._pattern_engine_factory = lambda: mock_engine
        try:
            bridge.start()
            assert bridge._scan_thread is not None
            assert bridge._scan_thread.is_alive()
        finally:
            bridge.stop()

    def test_bridge_stop_stops_scan(self):
        """SynapticBridge.stop() stops the pattern scan thread."""
        bus = MagicMock()
        bridge = SynapticBridge(event_bus=bus)
        mock_engine = MagicMock()
        bridge._modules_probed = True
        bridge._pattern_engine_factory = lambda: mock_engine
        bridge.start()
        assert bridge._scan_thread is not None
        bridge.stop()
        assert bridge._scan_thread is None
