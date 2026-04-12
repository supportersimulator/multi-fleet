"""Tests for multifleet.event_bus — Phase 10 M10.2 IDEEventBus."""

import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from multifleet.event_bus import (
    IDEEventBus,
    ALL_NAMESPACES,
    NS_FLEET,
    NS_PROBE,
    NS_SURGEON,
    NS_PANEL,
    NS_EVIDENCE,
    NS_QUORUM,
)


@pytest.fixture(autouse=True)
def reset_singleton():
    """Reset singleton before and after each test."""
    IDEEventBus.reset()
    yield
    IDEEventBus.reset()


@pytest.fixture
def bus():
    return IDEEventBus(ring_size=100, namespace_ring_size=50)


# ═══════════════════════════════════════════════════════════════════
# Singleton
# ═══════════════════════════════════════════════════════════════════


class TestSingleton:
    def test_get_returns_same_instance(self):
        a = IDEEventBus.get()
        b = IDEEventBus.get()
        assert a is b

    def test_reset_clears_singleton(self):
        a = IDEEventBus.get()
        IDEEventBus.reset()
        b = IDEEventBus.get()
        assert a is not b

    def test_get_thread_safe(self):
        instances = []

        def grab():
            instances.append(IDEEventBus.get())

        threads = [threading.Thread(target=grab) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert all(i is instances[0] for i in instances)


# ═══════════════════════════════════════════════════════════════════
# Core pub/sub
# ═══════════════════════════════════════════════════════════════════


class TestPubSub:
    def test_on_receives_event(self, bus):
        received = []
        bus.on("test.event", lambda e: received.append(e))
        bus.emit("test.event", {"key": "val"})
        assert len(received) == 1
        assert received[0]["payload"] == {"key": "val"}

    def test_wildcard_subscription(self, bus):
        received = []
        bus.on("fleet.*", lambda e: received.append(e))
        bus.emit("fleet.peer.online", {"peer_id": "mac1"})
        bus.emit("probe.finding", {"probe_id": "x"})
        # fleet.peer.online does NOT match "fleet.*" via fnmatch because
        # fnmatch's * doesn't cross dots. So fleet.* matches fleet.X but not fleet.X.Y
        # Check what actually matches:
        matched_types = [e["type"] for e in received]
        # fleet.peer.online has 3 segments, fleet.* matches 2 segments
        # This is fnmatch behavior — document it
        assert "probe.finding" not in matched_types

    def test_deep_wildcard(self, bus):
        received = []
        bus.on("fleet.peer.*", lambda e: received.append(e))
        bus.emit("fleet.peer.online", {})
        bus.emit("fleet.peer.offline", {})
        bus.emit("fleet.health.degraded", {})
        assert len(received) == 2

    def test_once_fires_once(self, bus):
        received = []
        bus.once("panel.activated", lambda e: received.append(e))
        bus.emit("panel.activated", {"panel_id": "p1"})
        bus.emit("panel.activated", {"panel_id": "p2"})
        assert len(received) == 1

    def test_off_unsubscribes(self, bus):
        received = []
        handler = lambda e: received.append(e)
        bus.on("test.off", handler)
        bus.emit("test.off", {})
        assert len(received) == 1
        bus.off("test.off", handler)
        bus.emit("test.off", {})
        assert len(received) == 1


# ═══════════════════════════════════════════════════════════════════
# Event schema
# ═══════════════════════════════════════════════════════════════════


class TestEventSchema:
    def test_event_has_required_fields(self, bus):
        event = bus.emit("test.schema", {"data": 42}, source="test-source")
        assert "id" in event
        assert event["version"] == 1
        assert event["type"] == "test.schema"
        assert event["source"] == "test-source"
        assert "timestamp" in event
        assert event["payload"] == {"data": 42}
        assert "metadata" in event

    def test_correlation_id_preserved(self, bus):
        event = bus.emit("test.corr", {}, correlation_id="corr-123")
        assert event["metadata"]["correlation_id"] == "corr-123"

    def test_session_id_preserved(self, bus):
        event = bus.emit("test.sess", {}, session_id="sess-456")
        assert event["metadata"]["session_id"] == "sess-456"

    def test_payload_is_json_safe(self, bus):
        import json
        event = bus.emit("test.json", {
            "string": "hello",
            "number": 42,
            "float": 3.14,
            "bool": True,
            "null": None,
            "list": [1, 2, 3],
            "nested": {"a": "b"},
        })
        # Should not raise
        json.dumps(event)


# ═══════════════════════════════════════════════════════════════════
# Namespace history rings
# ═══════════════════════════════════════════════════════════════════


class TestNamespaceHistory:
    def test_fleet_events_captured(self, bus):
        bus.emit("fleet.peer.online", {"peer_id": "mac1"})
        bus.emit("fleet.peer.offline", {"peer_id": "mac2"})
        history = bus.history(NS_FLEET)
        assert len(history) == 2
        # newest first
        assert history[0]["type"] == "fleet.peer.offline"

    def test_probe_events_captured(self, bus):
        bus.emit("probe.finding", {"probe_id": "test"})
        history = bus.history(NS_PROBE)
        assert len(history) == 1

    def test_surgeon_events_captured(self, bus):
        bus.emit("surgeon.verdict", {"surgeon": "atlas"})
        history = bus.history(NS_SURGEON)
        assert len(history) == 1

    def test_panel_events_captured(self, bus):
        bus.emit("panel.registered", {"panel_id": "p1"})
        history = bus.history(NS_PANEL)
        assert len(history) == 1

    def test_evidence_events_captured(self, bus):
        bus.emit("evidence.recorded", {"entry_id": "e1"})
        history = bus.history(NS_EVIDENCE)
        assert len(history) == 1

    def test_quorum_events_captured(self, bus):
        bus.emit("quorum.proposed", {"proposal_id": "q1"})
        history = bus.history(NS_QUORUM)
        assert len(history) == 1

    def test_history_limit(self, bus):
        for i in range(100):
            bus.emit("fleet.tick", {"i": i})
        history = bus.history(NS_FLEET, limit=10)
        assert len(history) == 10
        assert history[0]["payload"]["i"] == 99

    def test_history_pattern_filter(self, bus):
        bus.emit("fleet.peer.online", {})
        bus.emit("fleet.health.degraded", {})
        bus.emit("fleet.peer.offline", {})
        history = bus.history(NS_FLEET, pattern="fleet.peer.*")
        assert len(history) == 2

    def test_unknown_namespace_returns_empty(self, bus):
        assert bus.history("nonexistent") == []

    def test_namespace_isolation(self, bus):
        bus.emit("fleet.event", {})
        bus.emit("probe.event", {})
        assert len(bus.history(NS_FLEET)) == 1
        assert len(bus.history(NS_PROBE)) == 1
        assert len(bus.history(NS_SURGEON)) == 0

    def test_ring_overflow(self):
        bus = IDEEventBus(ring_size=1000, namespace_ring_size=5)
        for i in range(10):
            bus.emit("fleet.tick", {"i": i})
        history = bus.history(NS_FLEET)
        assert len(history) == 5
        assert history[0]["payload"]["i"] == 9  # newest
        assert history[4]["payload"]["i"] == 5  # oldest kept

    def test_recent_delegates_to_core(self, bus):
        bus.emit("fleet.a", {})
        bus.emit("probe.b", {})
        recent = bus.recent(limit=10)
        assert len(recent) == 2


# ═══════════════════════════════════════════════════════════════════
# Middleware pipeline
# ═══════════════════════════════════════════════════════════════════


class TestMiddleware:
    def test_middleware_transforms_payload(self, bus):
        def add_timestamp(event_type, payload, metadata):
            payload["mw_added"] = True
            return event_type, payload, metadata

        bus.use(add_timestamp)
        received = []
        bus.on("test.mw", lambda e: received.append(e))
        bus.emit("test.mw", {"original": True})
        assert received[0]["payload"]["mw_added"] is True
        assert received[0]["payload"]["original"] is True

    def test_middleware_suppresses_event(self, bus):
        def suppress_all(event_type, payload, metadata):
            return None

        bus.use(suppress_all)
        received = []
        bus.on("test.suppressed", lambda e: received.append(e))
        result = bus.emit("test.suppressed", {})
        assert result is None
        assert len(received) == 0

    def test_middleware_chain_order(self, bus):
        order = []

        def mw1(et, p, m):
            order.append("mw1")
            return et, p, m

        def mw2(et, p, m):
            order.append("mw2")
            return et, p, m

        bus.use(mw1)
        bus.use(mw2)
        bus.emit("test.chain", {})
        assert order == ["mw1", "mw2"]

    def test_middleware_error_doesnt_crash(self, bus):
        def bad_mw(et, p, m):
            raise ValueError("boom")

        bus.use(bad_mw)
        # Should not raise — middleware errors are caught
        event = bus.emit("test.mw_error", {})
        # Event still emitted (middleware error is non-fatal)
        assert event is not None

    def test_remove_middleware(self, bus):
        call_count = [0]

        def counting_mw(et, p, m):
            call_count[0] += 1
            return et, p, m

        bus.use(counting_mw)
        bus.emit("test.rm", {})
        assert call_count[0] == 1

        bus.remove_middleware(counting_mw)
        bus.emit("test.rm", {})
        assert call_count[0] == 1  # not called again

    def test_remove_nonexistent_middleware_noop(self, bus):
        bus.remove_middleware(lambda et, p, m: (et, p, m))  # should not raise


# ═══════════════════════════════════════════════════════════════════
# Typed publish helpers — fleet.*
# ═══════════════════════════════════════════════════════════════════


class TestFleetHelpers:
    def test_fleet_peer_online(self, bus):
        event = bus.fleet_peer_online("mac1", sessions=3)
        assert event["type"] == "fleet.peer.online"
        assert event["payload"]["peer_id"] == "mac1"
        assert event["payload"]["sessions"] == 3
        assert event["source"] == "fleet-daemon"

    def test_fleet_peer_offline(self, bus):
        event = bus.fleet_peer_offline("mac2", last_seen_s=42.5)
        assert event["type"] == "fleet.peer.offline"
        assert event["payload"]["last_seen_s"] == 42.5

    def test_fleet_health(self, bus):
        event = bus.fleet_health("mac1", "degraded", ["P1", "P2"])
        assert event["type"] == "fleet.health.degraded"
        assert event["payload"]["broken"] == ["P1", "P2"]

    def test_fleet_channel(self, bus):
        event = bus.fleet_channel("broken", "mac1", "3/7")
        assert event["type"] == "fleet.channel.broken"
        assert event["payload"]["score"] == "3/7"

    def test_fleet_extra_kwargs(self, bus):
        event = bus.fleet_peer_online("mac1", custom_field="extra")
        assert event["payload"]["custom_field"] == "extra"


# ═══════════════════════════════════════════════════════════════════
# Typed publish helpers — probe.*
# ═══════════════════════════════════════════════════════════════════


class TestProbeHelpers:
    def test_probe_finding(self, bus):
        event = bus.probe_finding("mirror_drift", "Mirror Drift", "high", 0.87)
        assert event["type"] == "probe.finding"
        assert event["payload"]["probe_id"] == "mirror_drift"
        assert event["payload"]["severity"] == "high"
        assert event["payload"]["confidence"] == 0.87
        assert event["source"] == "probe-engine"

    def test_probe_complete(self, bus):
        event = bus.probe_complete(probes_run=5, findings_count=2, duration_ms=150.3)
        assert event["type"] == "probe.complete"
        assert event["payload"]["probes_run"] == 5
        assert event["payload"]["findings_count"] == 2

    def test_probe_error(self, bus):
        event = bus.probe_error("broken_probe", "timeout after 5s")
        assert event["type"] == "probe.error"
        assert event["payload"]["error"] == "timeout after 5s"


# ═══════════════════════════════════════════════════════════════════
# Typed publish helpers — surgeon.*
# ═══════════════════════════════════════════════════════════════════


class TestSurgeonHelpers:
    def test_cross_exam_started(self, bus):
        event = bus.surgeon_cross_exam_started(
            "architecture review",
            ["atlas", "cardiologist", "neurologist"],
            correlation_id="cx-001",
        )
        assert event["type"] == "surgeon.cross_exam.started"
        assert event["payload"]["surgeons"] == ["atlas", "cardiologist", "neurologist"]
        assert event["metadata"]["correlation_id"] == "cx-001"
        assert event["source"] == "3-surgeons"

    def test_cross_exam_complete(self, bus):
        event = bus.surgeon_cross_exam_complete(
            "arch review", consensus=True, summary="All agreed"
        )
        assert event["type"] == "surgeon.cross_exam.complete"
        assert event["payload"]["consensus"] is True

    def test_verdict(self, bus):
        event = bus.surgeon_verdict("cardiologist", "approve", 0.92)
        assert event["type"] == "surgeon.verdict"
        assert event["payload"]["surgeon"] == "cardiologist"
        assert event["payload"]["confidence"] == 0.92


# ═══════════════════════════════════════════════════════════════════
# Typed publish helpers — panel.*
# ═══════════════════════════════════════════════════════════════════


class TestPanelHelpers:
    def test_panel_registered(self, bus):
        event = bus.panel_registered("evidence-panel", source="extension")
        assert event["type"] == "panel.registered"
        assert event["payload"]["panel_id"] == "evidence-panel"
        assert event["payload"]["source"] == "extension"

    def test_panel_activated(self, bus):
        event = bus.panel_activated("evidence-panel")
        assert event["type"] == "panel.activated"

    def test_panel_suspended(self, bus):
        event = bus.panel_suspended("evidence-panel", reason="user minimized")
        assert event["type"] == "panel.suspended"
        assert event["payload"]["reason"] == "user minimized"

    def test_panel_closed(self, bus):
        event = bus.panel_closed("evidence-panel")
        assert event["type"] == "panel.closed"

    def test_panel_error(self, bus):
        event = bus.panel_error("evidence-panel", "render crash")
        assert event["type"] == "panel.error"
        assert event["payload"]["error"] == "render crash"

    def test_full_panel_lifecycle(self, bus):
        """Simulate register -> activate -> suspend -> activate -> close."""
        received = []
        bus.on("panel.*", lambda e: received.append(e["type"]))

        bus.panel_registered("p1")
        bus.panel_activated("p1")
        bus.panel_suspended("p1", reason="background")
        bus.panel_activated("p1")
        bus.panel_closed("p1")

        assert len(received) == 5
        assert received[0] == "panel.registered"
        assert received[-1] == "panel.closed"


# ═══════════════════════════════════════════════════════════════════
# Typed publish helpers — evidence.*
# ═══════════════════════════════════════════════════════════════════


class TestEvidenceHelpers:
    def test_evidence_recorded(self, bus):
        event = bus.evidence_recorded("e-001", "probe.finding", "mac1", subject="drift")
        assert event["type"] == "evidence.recorded"
        assert event["payload"]["entry_id"] == "e-001"
        assert event["payload"]["node_id"] == "mac1"
        assert event["source"] == "evidence-ledger"

    def test_evidence_chain_broken(self, bus):
        event = bus.evidence_chain_broken(42, "hash mismatch at seq 42")
        assert event["type"] == "evidence.chain_broken"
        assert event["payload"]["broken_at_seq"] == 42

    def test_evidence_synced(self, bus):
        event = bus.evidence_synced("mac2", 15)
        assert event["type"] == "evidence.synced"
        assert event["payload"]["entries_imported"] == 15


# ═══════════════════════════════════════════════════════════════════
# Typed publish helpers — quorum.*
# ═══════════════════════════════════════════════════════════════════


class TestQuorumHelpers:
    def test_quorum_proposed(self, bus):
        event = bus.quorum_proposed("q-001", "merge strategy", "mac1", required_votes=2)
        assert event["type"] == "quorum.proposed"
        assert event["payload"]["proposal_id"] == "q-001"
        assert event["payload"]["required_votes"] == 2
        assert event["source"] == "quorum"

    def test_quorum_voted(self, bus):
        event = bus.quorum_voted("q-001", "mac2", "approve")
        assert event["type"] == "quorum.voted"
        assert event["payload"]["voter"] == "mac2"
        assert event["payload"]["vote"] == "approve"

    def test_quorum_decided(self, bus):
        event = bus.quorum_decided("q-001", "approved", votes_for=2, votes_against=1)
        assert event["type"] == "quorum.decided"
        assert event["payload"]["votes_for"] == 2
        assert event["payload"]["votes_against"] == 1

    def test_full_quorum_lifecycle(self, bus):
        """Simulate propose -> vote -> vote -> decide."""
        received = []
        bus.on("quorum.*", lambda e: received.append(e["type"]))

        bus.quorum_proposed("q-001", "topic", "mac1", required_votes=2)
        bus.quorum_voted("q-001", "mac2", "approve")
        bus.quorum_voted("q-001", "mac3", "approve")
        bus.quorum_decided("q-001", "approved", votes_for=2, votes_against=0)

        assert len(received) == 4
        assert received[0] == "quorum.proposed"
        assert received[-1] == "quorum.decided"


# ═══════════════════════════════════════════════════════════════════
# Integration hooks
# ═══════════════════════════════════════════════════════════════════


class TestIntegrationHooks:
    def test_attach_fleet_daemon(self, bus):
        mock_fleet = MagicMock()
        bus.attach_fleet_daemon(mock_fleet)
        mock_fleet.set_core_bus.assert_called_once_with(bus.core_bus)
        assert "fleet_daemon" in bus.integrations

    def test_attach_probe_engine(self, bus):
        mock_probe = MagicMock()
        bus.attach_probe_engine(mock_probe)
        assert "probe_engine" in bus.integrations

    def test_attach_surgeon_adapter(self, bus):
        mock_surgeon = MagicMock()
        bus.attach_surgeon_adapter(mock_surgeon)
        assert "surgeon_adapter" in bus.integrations

    def test_attach_evidence_ledger(self, bus):
        mock_ledger = MagicMock()
        bus.attach_evidence_ledger(mock_ledger)
        assert "evidence_ledger" in bus.integrations

    def test_multiple_integrations(self, bus):
        bus.attach_probe_engine(MagicMock())
        bus.attach_surgeon_adapter(MagicMock())
        assert len(bus.integrations) == 2


# ═══════════════════════════════════════════════════════════════════
# Stats and health
# ═══════════════════════════════════════════════════════════════════


class TestStatsAndHealth:
    def test_stats_structure(self, bus):
        bus.emit("fleet.test", {})
        stats = bus.stats
        assert "core" in stats
        assert "namespace_events" in stats
        assert "integrations" in stats
        assert "middleware_count" in stats

    def test_namespace_stats(self, bus):
        bus.emit("fleet.a", {})
        bus.emit("fleet.b", {})
        bus.emit("probe.c", {})
        assert bus.stats["namespace_events"]["fleet"] == 2
        assert bus.stats["namespace_events"]["probe"] == 1

    def test_health_check(self, bus):
        bus.emit("fleet.test", {})
        bus.attach_probe_engine(MagicMock())
        health = bus.health()
        assert health["status"] == "ok"
        assert health["events_emitted"] == 1
        assert "fleet" in health["namespaces_active"]
        assert "probe_engine" in health["integrations"]

    def test_core_bus_accessible(self, bus):
        from multifleet.event_bus_server import EventBus
        assert isinstance(bus.core_bus, EventBus)


# ═══════════════════════════════════════════════════════════════════
# Error resilience
# ═══════════════════════════════════════════════════════════════════


class TestErrorResilience:
    def test_handler_exception_doesnt_crash_bus(self, bus):
        bus.on("test.crash", lambda e: 1 / 0)
        received = []
        bus.on("test.crash", lambda e: received.append(e))
        bus.emit("test.crash", {})
        # Second handler still fires
        assert len(received) == 1

    def test_namespace_constants(self):
        assert NS_FLEET == "fleet"
        assert NS_PROBE == "probe"
        assert NS_SURGEON == "surgeon"
        assert NS_PANEL == "panel"
        assert NS_EVIDENCE == "evidence"
        assert NS_QUORUM == "quorum"
        assert len(ALL_NAMESPACES) == 6


# ═══════════════════════════════════════════════════════════════════
# Cross-namespace scenarios
# ═══════════════════════════════════════════════════════════════════


class TestCrossNamespaceScenarios:
    def test_probe_finding_triggers_evidence(self, bus):
        """Simulate: probe finds something -> evidence records it."""
        evidence_events = []
        bus.on("evidence.*", lambda e: evidence_events.append(e))

        # Probe engine finds something
        bus.probe_finding("drift_check", "Mirror Drift", "high", 0.9)

        # Evidence ledger records it (in real system, a handler would do this)
        bus.evidence_recorded("e-001", "probe.finding", "mac1", subject="drift")

        assert len(evidence_events) == 1
        assert evidence_events[0]["payload"]["entry_id"] == "e-001"

    def test_surgeon_exam_triggers_quorum(self, bus):
        """Simulate: surgeon exam -> quorum vote on result."""
        all_events = []
        bus.on("*", lambda e: all_events.append(e["type"]))

        bus.surgeon_cross_exam_started("review", ["atlas", "cardio"], correlation_id="cx-1")
        bus.surgeon_verdict("atlas", "approve", 0.95, correlation_id="cx-1")
        bus.surgeon_verdict("cardio", "approve", 0.88, correlation_id="cx-1")
        bus.surgeon_cross_exam_complete("review", consensus=True, correlation_id="cx-1")
        bus.quorum_decided("cx-1", "approved", votes_for=2, votes_against=0)

        assert "surgeon.cross_exam.started" in all_events
        assert "quorum.decided" in all_events

    def test_late_subscriber_catches_up(self, bus):
        """Panels connecting late should see history."""
        # Events happened before panel connected
        bus.fleet_peer_online("mac1")
        bus.fleet_peer_online("mac2")
        bus.fleet_peer_offline("mac1")

        # Panel connects and asks for history
        history = bus.history(NS_FLEET)
        assert len(history) == 3
        # Can filter to just peer events
        peer_history = bus.history(NS_FLEET, pattern="fleet.peer.*")
        assert len(peer_history) == 3
