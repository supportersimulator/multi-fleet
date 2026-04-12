"""Tests for typed fleet events — verifying enum coverage, emit_typed wiring,
and migration of ad-hoc string callers to the EventType system.

Covers: event_schema new EventType values, health_monitor typed emit,
fleet_race typed emit, ghost modules typed emit, review_flow typed emit.
"""

import json
from unittest.mock import MagicMock, patch

from multifleet.event_schema import (
    EventEnvelope,
    EventSource,
    EventType,
    create_event,
)
from multifleet.event_emitter import EventEmitter


# ── 1. New EventType enum values exist and have correct dot-notation ──

class TestNewEventTypeValues:

    def test_health_event_types_exist(self):
        assert EventType.HEALTH_TRANSITION.value == "health.transition"
        assert EventType.HEALTH_ANOMALY.value == "health.anomaly"
        assert EventType.HEALTH_REPAIR.value == "health.repair"
        assert EventType.HEALTH_CHANNEL_BROKEN.value == "health.channel.broken"
        assert EventType.HEALTH_CHANNEL_HEALING.value == "health.channel.healing"
        assert EventType.HEALTH_CHANNEL_RESTORED.value == "health.channel.restored"

    def test_supervisor_event_type_exists(self):
        assert EventType.SUPERVISOR_TRANSITION.value == "supervisor.transition"

    def test_ghost_extended_event_types_exist(self):
        assert EventType.GHOST_CORROBORATE_COMPLETED.value == "ghost.corroborate.completed"
        assert EventType.GHOST_MIRROR_SCANNED.value == "ghost.mirror.scanned"
        assert EventType.GHOST_MIRROR_DRIFT.value == "ghost.mirror.drift_detected"
        assert EventType.GHOST_ANTICIPATE_COMPLETED.value == "ghost.anticipate.completed"

    def test_race_arbiter_pending_exists(self):
        assert EventType.RACE_ARBITER_PENDING.value == "race.arbiter.pending"

    def test_synthesis_event_types_exist(self):
        assert EventType.SYNTHESIS_MERGE_DECISION.value == "synthesis.merge_decision"
        assert EventType.SYNTHESIS_DECISION.value == "synthesis.decision"

    def test_review_event_types_exist(self):
        assert EventType.REVIEW_STARTED.value == "review.started"
        assert EventType.REVIEW_VERDICT_RECEIVED.value == "review.verdict_received"
        assert EventType.REVIEW_COMPLETED.value == "review.completed"

    def test_all_new_types_have_dot_notation(self):
        """Every EventType value must use dot notation."""
        for et in EventType:
            assert "." in et.value, f"{et.name} = '{et.value}' missing dot notation"

    def test_total_event_type_count_increased(self):
        """After adding new types, enum should have at least 45 members."""
        assert len(EventType) >= 45


# ── 2. create_event produces correct envelopes for new types ──

class TestCreateEventNewTypes:

    def test_create_health_transition_event(self):
        evt = create_event(
            EventType.HEALTH_TRANSITION,
            EventSource.MULTI_FLEET,
            "mac1",
            payload={"node": "mac2", "from": "online", "to": "degraded"},
        )
        assert isinstance(evt, EventEnvelope)
        assert evt.type == "health.transition"
        assert evt.source == "multi-fleet"
        assert evt.payload["node"] == "mac2"

    def test_create_ghost_corroborate_event(self):
        evt = create_event(
            EventType.GHOST_CORROBORATE_COMPLETED,
            EventSource.GHOST,
            "mac1",
            payload={"strengthened": 3, "weakened": 1, "novel": 2},
        )
        assert evt.type == "ghost.corroborate.completed"
        assert evt.payload["strengthened"] == 3

    def test_create_review_started_event(self):
        evt = create_event(
            EventType.REVIEW_STARTED,
            EventSource.MULTI_FLEET,
            "mac1",
            payload={"task_id": "task-99"},
            task_id="task-99",
        )
        assert evt.type == "review.started"
        assert evt.task_id == "task-99"

    def test_create_race_arbiter_pending_event(self):
        evt = create_event(
            EventType.RACE_ARBITER_PENDING,
            EventSource.MULTI_FLEET,
            "mac1",
            payload={"decision_id": "d-1", "race_id": "r-1", "confidence": 0.6},
        )
        assert evt.type == "race.arbiter.pending"
        assert evt.payload["confidence"] == 0.6

    def test_envelope_round_trip_new_type(self):
        evt = create_event(
            EventType.HEALTH_CHANNEL_BROKEN,
            EventSource.MULTI_FLEET,
            "mac2",
            payload={"peer": "mac1", "score": "2/7"},
        )
        j = evt.to_json()
        restored = EventEnvelope.from_json(j)
        assert restored.type == "health.channel.broken"
        assert restored.payload["peer"] == "mac1"


# ── 3. EventEmitter.emit_typed works with new types ──

class TestEmitTypedNewTypes:

    def test_emit_typed_health_transition(self):
        emitter = EventEmitter(node_id="mac1")
        # Patch the sinks so nothing actually sends
        emitter._emit_to_eventbus = MagicMock(return_value=False)
        emitter._emit_to_log = MagicMock(return_value=True)

        result = emitter.emit_typed(
            EventType.HEALTH_TRANSITION,
            EventSource.MULTI_FLEET,
            payload={"node": "mac2", "from": "online", "to": "offline"},
        )
        assert result["log"] is True
        assert emitter.stats["emitted"] == 1

    def test_emit_typed_ghost_anticipate(self):
        emitter = EventEmitter(node_id="mac1")
        emitter._emit_to_eventbus = MagicMock(return_value=False)
        emitter._emit_to_log = MagicMock(return_value=True)

        result = emitter.emit_typed(
            EventType.GHOST_ANTICIPATE_COMPLETED,
            EventSource.GHOST,
            payload={"predictions": 5, "backlog_added": 2},
        )
        assert result["log"] is True

    def test_emit_typed_review_completed(self):
        emitter = EventEmitter(node_id="mac1")
        emitter._emit_to_eventbus = MagicMock(return_value=False)
        emitter._emit_to_log = MagicMock(return_value=True)

        result = emitter.emit_typed(
            EventType.REVIEW_COMPLETED,
            EventSource.MULTI_FLEET,
            payload={"verdict": "approved"},
            task_id="task-42",
        )
        assert result["log"] is True


# ── 4. HealthMonitor._emit_event uses typed path ──

class TestHealthMonitorTypedEmit:

    def test_health_monitor_maps_transition_to_typed(self):
        """HealthMonitor._emit_event should route known types through emit_typed."""
        from multifleet.health_monitor import FleetHealthMonitor

        hm = FleetHealthMonitor.__new__(FleetHealthMonitor)
        hm._eventbus = None  # no FleetEventBus fallback

        with patch("multifleet.health_monitor.FleetHealthMonitor._get_event_type_map") as mock_map:
            mock_map.return_value = {
                "health.transition": EventType.HEALTH_TRANSITION,
            }
            with patch("multifleet.event_emitter.emit_typed") as mock_emit:
                hm._emit_event("health.transition", {"node": "mac2"})
                mock_emit.assert_called_once()
                call_args = mock_emit.call_args
                assert call_args[1].get("event_type", call_args[0][0]) == EventType.HEALTH_TRANSITION

    def test_health_monitor_falls_back_for_unknown(self):
        """Unknown event types should fall back to FleetEventBus."""
        from multifleet.health_monitor import FleetHealthMonitor

        hm = FleetHealthMonitor.__new__(FleetHealthMonitor)
        mock_bus = MagicMock()
        hm._eventbus = mock_bus

        # Force typed path to not find the key
        with patch("multifleet.health_monitor.FleetHealthMonitor._get_event_type_map") as mock_map:
            mock_map.return_value = {}
            hm._emit_event("unknown.event", {"data": 1})
            mock_bus.emit.assert_called_once_with("unknown.event", {"data": 1})


# ── 5. FleetRace._emit_event uses typed path ──

class TestFleetRaceTypedEmit:

    def test_fleet_race_maps_arbiter_pending(self):
        """fleet_race._emit_event should route fleet.arbiter.pending to typed emit."""
        from multifleet.fleet_race import FleetRace

        frm = FleetRace.__new__(FleetRace)
        frm._node_id = "mac1"
        frm._port = 8855

        with patch("multifleet.event_emitter.emit_typed") as mock_emit:
            frm._emit_event("fleet.arbiter.pending", {
                "decision_id": "d-1",
                "race_id": "r-1",
            })
            mock_emit.assert_called_once()
            call_args = mock_emit.call_args
            assert call_args[1].get("event_type", call_args[0][0]) == EventType.RACE_ARBITER_PENDING
