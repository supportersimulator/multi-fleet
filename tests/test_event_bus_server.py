"""Tests for multifleet.event_bus_server — EventBus core and WebSocket server."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from multifleet.event_bus_server import EventBus


# ═══════════════════════════════════════════════════════════════════
# EventBus core tests
# ═══════════════════════════════════════════════════════════════════


@pytest.fixture
def bus():
    return EventBus(ring_size=100)


class TestEmitDeliversToSubscriber:
    def test_handler_receives_event(self, bus):
        received = []
        bus.on("test.event", lambda e: received.append(e))
        bus.emit("test.event", {"key": "val"})
        assert len(received) == 1
        assert received[0]["payload"] == {"key": "val"}

    def test_multiple_handlers(self, bus):
        r1, r2 = [], []
        bus.on("test.event", lambda e: r1.append(e))
        bus.on("test.event", lambda e: r2.append(e))
        bus.emit("test.event", {"x": 1})
        assert len(r1) == 1
        assert len(r2) == 1


class TestWildcardSubscription:
    def test_star_wildcard(self, bus):
        received = []
        bus.on("injection.*", lambda e: received.append(e))
        bus.emit("injection.completed", {})
        bus.emit("injection.failed", {})
        bus.emit("health.check", {})  # should NOT match
        assert len(received) == 2
        assert received[0]["type"] == "injection.completed"
        assert received[1]["type"] == "injection.failed"

    def test_deep_wildcard(self, bus):
        received = []
        bus.on("fleet.peer.*", lambda e: received.append(e))
        bus.emit("fleet.peer.online", {})
        bus.emit("fleet.message.sent", {})
        assert len(received) == 1


class TestOnceFiresOnce:
    def test_once_handler_removed_after_fire(self, bus):
        received = []
        bus.once("test.once", lambda e: received.append(e))
        bus.emit("test.once", {"first": True})
        bus.emit("test.once", {"second": True})
        assert len(received) == 1
        assert received[0]["payload"]["first"] is True

    def test_once_with_wildcard(self, bus):
        received = []
        bus.once("skill.*", lambda e: received.append(e))
        bus.emit("skill.suggested", {})
        bus.emit("skill.activated", {})
        assert len(received) == 1


class TestOffUnsubscribes:
    def test_off_removes_handler(self, bus):
        received = []
        handler = lambda e: received.append(e)
        bus.on("test.off", handler)
        bus.emit("test.off", {})
        assert len(received) == 1

        bus.off("test.off", handler)
        bus.emit("test.off", {})
        assert len(received) == 1  # no new events

    def test_off_noop_when_not_subscribed(self, bus):
        # Should not raise
        bus.off("nonexistent.pattern", lambda e: None)


class TestRingBufferLimit:
    def test_ring_drops_oldest(self):
        bus = EventBus(ring_size=5)
        for i in range(10):
            bus.emit("test.ring", {"i": i})
        recent = bus.recent(limit=100)
        assert len(recent) == 5
        # newest first
        assert recent[0]["payload"]["i"] == 9
        assert recent[4]["payload"]["i"] == 5

    def test_dropped_counter(self):
        bus = EventBus(ring_size=3)
        for i in range(6):
            bus.emit("test.drop", {"i": i})
        # Ring was full at i=3,4,5 — dropped 3 events
        assert bus.stats["dropped"] == 3


class TestRecentReturnsEvents:
    def test_recent_newest_first(self, bus):
        bus.emit("a.event", {"i": 1})
        bus.emit("b.event", {"i": 2})
        bus.emit("c.event", {"i": 3})
        recent = bus.recent(limit=2)
        assert len(recent) == 2
        assert recent[0]["payload"]["i"] == 3
        assert recent[1]["payload"]["i"] == 2

    def test_recent_with_pattern_filter(self, bus):
        bus.emit("health.check", {})
        bus.emit("injection.done", {})
        bus.emit("health.degraded", {})
        recent = bus.recent(limit=10, pattern="health.*")
        assert len(recent) == 2
        assert all(e["type"].startswith("health.") for e in recent)


class TestMatchExact:
    def test_exact_match(self):
        assert EventBus._match("injection.completed", "injection.completed") is True
        assert EventBus._match("injection.completed", "injection.failed") is False


class TestMatchWildcard:
    def test_namespace_wildcard(self):
        assert EventBus._match("injection.*", "injection.completed") is True
        assert EventBus._match("injection.*", "injection.failed") is True
        assert EventBus._match("injection.*", "health.check") is False

    def test_question_mark_wildcard(self):
        assert EventBus._match("test.?", "test.a") is True
        assert EventBus._match("test.?", "test.ab") is False


class TestMatchStarAll:
    def test_star_matches_everything(self):
        assert EventBus._match("*", "injection.completed") is True
        assert EventBus._match("*", "health.check") is True
        assert EventBus._match("*", "anything.at.all") is True


class TestSourcePreserved:
    def test_default_source(self, bus):
        event = bus.emit("test.source", {})
        assert event["source"] == "python"

    def test_custom_source(self, bus):
        event = bus.emit("test.source", {}, source="typescript")
        assert event["source"] == "typescript"

    def test_handler_sees_source(self, bus):
        received = []
        bus.on("test.source", lambda e: received.append(e))
        bus.emit("test.source", {}, source="hook")
        assert received[0]["source"] == "hook"


class TestCorrelationId:
    def test_correlation_id_in_metadata(self, bus):
        event = bus.emit("test.corr", {}, correlation_id="abc-123")
        assert event["metadata"]["correlation_id"] == "abc-123"

    def test_no_correlation_id(self, bus):
        event = bus.emit("test.corr", {})
        # metadata exists but may not have correlation_id
        assert "metadata" in event


class TestHandlerExceptionDoesntCrashBus:
    def test_bad_handler_doesnt_block_others(self, bus):
        received = []

        def bad_handler(e):
            raise ValueError("boom")

        bus.on("test.error", bad_handler)
        bus.on("test.error", lambda e: received.append(e))
        bus.emit("test.error", {})

        # Second handler still received the event
        assert len(received) == 1
        assert bus.stats["errors"] == 1

    def test_bad_once_handler(self, bus):
        bus.once("test.error", lambda e: 1 / 0)
        # Should not raise
        bus.emit("test.error", {})
        assert bus.stats["errors"] == 1


class TestStatsTracking:
    def test_emitted_counter(self, bus):
        bus.emit("a.b", {})
        bus.emit("c.d", {})
        assert bus.stats["emitted"] == 2

    def test_delivered_counter(self, bus):
        bus.on("a.b", lambda e: None)
        bus.emit("a.b", {})
        assert bus.stats["delivered"] == 1

    def test_error_counter(self, bus):
        bus.on("a.b", lambda e: 1 / 0)
        bus.emit("a.b", {})
        assert bus.stats["errors"] == 1

    def test_stats_returns_copy(self, bus):
        s = bus.stats
        s["emitted"] = 999
        assert bus.stats["emitted"] != 999


class TestEventSchemaValid:
    def test_has_required_fields(self, bus):
        event = bus.emit("test.schema", {"data": 42}, source="test")
        assert "id" in event
        assert "version" in event
        assert event["version"] == 1
        assert "type" in event
        assert event["type"] == "test.schema"
        assert "source" in event
        assert event["source"] == "test"
        assert "timestamp" in event
        assert "payload" in event
        assert event["payload"] == {"data": 42}
        assert "metadata" in event
        assert isinstance(event["metadata"], dict)

    def test_id_is_uuid(self, bus):
        import uuid
        event = bus.emit("test.uuid", {})
        # Should parse as valid UUID
        uuid.UUID(event["id"])

    def test_timestamp_is_iso(self, bus):
        event = bus.emit("test.ts", {})
        ts = event["timestamp"]
        assert ts.endswith("Z")
        assert "T" in ts

    def test_session_id_in_metadata(self, bus):
        event = bus.emit("test.session", {}, session_id="sess-42")
        assert event["metadata"]["session_id"] == "sess-42"
