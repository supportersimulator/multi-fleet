"""Tests for EventEnvelope — the shared bloodstream of the 5-plane architecture."""

import json

from multifleet.event_schema import (
    EventEnvelope,
    EventSource,
    EventType,
    create_event,
    ghost_digest_event,
    probe_finding_event,
    race_event,
    surgeon_phase_event,
    task_accepted_event,
    task_blocked_event,
)


class TestEventEnvelope:

    def test_create_basic(self):
        e = EventEnvelope(type="test.event", node_id="mac1", source="test")
        assert e.id  # UUID generated
        assert e.ts  # Timestamp set
        assert e.type == "test.event"
        assert e.node_id == "mac1"

    def test_to_dict(self):
        e = EventEnvelope(type="test", node_id="mac1", source="test", payload={"key": "val"})
        d = e.to_dict()
        assert d["type"] == "test"
        assert d["payload"]["key"] == "val"
        assert "id" in d
        assert "ts" in d

    def test_to_json(self):
        e = EventEnvelope(type="test", node_id="mac1", source="test")
        j = e.to_json()
        parsed = json.loads(j)
        assert parsed["type"] == "test"

    def test_from_dict(self):
        d = {"id": "abc", "type": "test", "node_id": "mac2", "source": "test", "ts": "2026-01-01"}
        e = EventEnvelope.from_dict(d)
        assert e.id == "abc"
        assert e.node_id == "mac2"

    def test_from_json(self):
        j = '{"id": "xyz", "type": "probe.finding", "node_id": "mac3", "source": "probe-engine"}'
        e = EventEnvelope.from_json(j)
        assert e.id == "xyz"
        assert e.source == "probe-engine"

    def test_optional_fields(self):
        e = EventEnvelope(
            type="test", node_id="mac1", source="test",
            task_id="task-1", session_id="sess-1",
        )
        d = e.to_dict()
        assert d["task_id"] == "task-1"
        assert d["session_id"] == "sess-1"

    def test_optional_fields_excluded_when_none(self):
        e = EventEnvelope(type="test", node_id="mac1", source="test")
        d = e.to_dict()
        assert "task_id" not in d
        assert "session_id" not in d

    def test_round_trip(self):
        e1 = EventEnvelope(type="test", node_id="mac1", source="test", payload={"x": 1})
        j = e1.to_json()
        e2 = EventEnvelope.from_json(j)
        assert e2.type == e1.type
        assert e2.node_id == e1.node_id
        assert e2.payload["x"] == 1


class TestEventFactory:

    def test_create_event(self):
        e = create_event(EventType.PROBE_FINDING_RAISED, EventSource.PROBE_ENGINE, "mac1")
        assert e.type == "probe.finding.raised"
        assert e.source == "probe-engine"

    def test_probe_finding_event(self):
        e = probe_finding_event("mac1", "mirror_drift", "high", "Architecture drift")
        assert e.type == "probe.finding.raised"
        assert e.payload["probe_id"] == "mirror_drift"
        assert e.payload["severity"] == "high"

    def test_surgeon_phase_event(self):
        e = surgeon_phase_event("mac1", "review", "full", 0.3)
        assert e.type == "surgeon.phase.changed"
        assert e.payload["phase"] == "review"
        assert e.payload["disagreement_score"] == 0.3

    def test_ghost_digest_event(self):
        e = ghost_digest_event("mac1", "task-123", candidates=5, objections=2)
        assert e.type == "ghost.session.digested"
        assert e.task_id == "task-123"
        assert e.payload["candidates"] == 5

    def test_task_accepted_event(self):
        e = task_accepted_event("mac1", "task-abc", "All checks passed")
        assert e.type == "task.accepted"
        assert e.payload["reason"] == "All checks passed"

    def test_task_blocked_event(self):
        e = task_blocked_event("mac1", "task-xyz", ["Blocking probe: mirror_drift"])
        assert e.type == "task.blocked"
        assert len(e.payload["blocking_issues"]) == 1

    def test_race_event(self):
        e = race_event(EventType.RACE_STARTED, "mac1", "race-001", participants=3)
        assert e.type == "race.started"
        assert e.payload["race_id"] == "race-001"
        assert e.payload["participants"] == 3


class TestEventTypes:

    def test_all_event_types_have_values(self):
        for et in EventType:
            assert "." in et.value  # All types use dot notation

    def test_all_sources_have_values(self):
        for src in EventSource:
            assert src.value  # All sources are non-empty

    def test_event_type_count(self):
        assert len(EventType) >= 25  # At least 25 event types defined
