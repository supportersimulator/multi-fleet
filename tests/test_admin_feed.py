"""Tests for AdminFeed — fleet event feed for admin panel."""

import time
import threading
import pytest

from multifleet.admin_feed import AdminFeed, AdminEvent, _SEVERITY_MAP


class TestAdminEvent:
    """Tests for AdminEvent dataclass."""

    def test_to_dict(self):
        evt = AdminEvent(
            id="abc123",
            event_type="fleet.peer.online",
            timestamp=1700000000.0,
            source="fleet-nerve:mac2",
            data={"peer_id": "mac1"},
            node_id="mac2",
            severity="info",
        )
        d = evt.to_dict()
        assert d["id"] == "abc123"
        assert d["event_type"] == "fleet.peer.online"
        assert d["timestamp"] == 1700000000.0
        assert d["source"] == "fleet-nerve:mac2"
        assert d["data"] == {"peer_id": "mac1"}
        assert d["node_id"] == "mac2"
        assert d["severity"] == "info"
        assert "ts_iso" in d

    def test_to_sse(self):
        evt = AdminEvent(
            id="sse1",
            event_type="fleet.peer.online",
            timestamp=1700000000.0,
            source="test",
            data={"peer_id": "mac1"},
        )
        sse = evt.to_sse()
        assert "event: fleet.peer.online\n" in sse
        assert "data: " in sse
        assert "id: sse1\n" in sse
        assert sse.endswith("\n\n")


class TestAdminFeed:
    """Tests for AdminFeed core functionality."""

    def test_record_and_retrieve(self):
        feed = AdminFeed(max_events=100)
        feed.record_event("fleet.peer.online", {"peer_id": "mac1"}, source="test", node_id="mac2")
        events = feed.get_recent(limit=10)
        assert len(events) == 1
        assert events[0]["event_type"] == "fleet.peer.online"
        assert events[0]["data"]["peer_id"] == "mac1"

    def test_newest_first(self):
        feed = AdminFeed(max_events=100)
        feed.record_event("type.a", {"seq": 1})
        feed.record_event("type.b", {"seq": 2})
        feed.record_event("type.c", {"seq": 3})
        events = feed.get_recent(limit=10)
        assert len(events) == 3
        assert events[0]["data"]["seq"] == 3  # newest
        assert events[2]["data"]["seq"] == 1  # oldest

    def test_ring_buffer_overflow(self):
        feed = AdminFeed(max_events=5)
        for i in range(10):
            feed.record_event("test.event", {"seq": i})
        events = feed.get_recent(limit=100)
        assert len(events) == 5
        # Should have the last 5 events (seq 5-9)
        seqs = [e["data"]["seq"] for e in events]
        assert seqs == [9, 8, 7, 6, 5]

    def test_filter_by_event_type(self):
        feed = AdminFeed(max_events=100)
        feed.record_event("fleet.peer.online", {"peer": "mac1"})
        feed.record_event("fleet.peer.offline", {"peer": "mac2"})
        feed.record_event("fleet.peer.online", {"peer": "mac3"})

        online = feed.get_recent(event_types=["fleet.peer.online"])
        assert len(online) == 2
        assert all(e["event_type"] == "fleet.peer.online" for e in online)

    def test_filter_by_severity(self):
        feed = AdminFeed(max_events=100)
        feed.record_event("fleet.peer.online", {})  # info
        feed.record_event("fleet.channel.broken", {})  # error
        feed.record_event("fleet.health.degraded", {})  # warning

        errors = feed.get_recent(severity="error")
        assert len(errors) == 1
        assert errors[0]["severity"] == "error"

    def test_filter_by_since(self):
        feed = AdminFeed(max_events=100)
        feed.record_event("old.event", {})
        cutoff = time.time() + 0.01
        time.sleep(0.02)
        feed.record_event("new.event", {})

        events = feed.get_recent(since=cutoff)
        assert len(events) == 1
        assert events[0]["event_type"] == "new.event"

    def test_limit(self):
        feed = AdminFeed(max_events=100)
        for i in range(20):
            feed.record_event("test.event", {"seq": i})
        events = feed.get_recent(limit=5)
        assert len(events) == 5

    def test_auto_severity_detection(self):
        feed = AdminFeed(max_events=100)
        evt = feed.record_event("fleet.channel.broken", {})
        assert evt.severity == "error"

        evt = feed.record_event("invariant.violated", {})
        assert evt.severity == "critical"

        evt = feed.record_event("fleet.peer.online", {})
        assert evt.severity == "info"

        evt = feed.record_event("unknown.type", {})
        assert evt.severity == "info"  # default

    def test_severity_override(self):
        feed = AdminFeed(max_events=100)
        evt = feed.record_event("fleet.peer.online", {}, severity="critical")
        assert evt.severity == "critical"

    def test_summary(self):
        feed = AdminFeed(max_events=100)
        feed.record_event("fleet.peer.online", {}, node_id="mac1")
        feed.record_event("fleet.peer.online", {}, node_id="mac2")
        feed.record_event("fleet.channel.broken", {}, node_id="mac1")

        summary = feed.get_summary()
        assert summary["node_count"] == 2
        assert "mac1" in summary["nodes"]
        assert "mac2" in summary["nodes"]
        assert summary["total_events"] == 3
        assert summary["buffer_size"] == 3
        assert summary["severity_counts"]["info"] == 2
        assert summary["severity_counts"]["error"] == 1
        assert len(summary["recent_important"]) == 1  # the broken channel
        assert summary["type_counts"]["fleet.peer.online"] == 2

    def test_subscribe_and_unsubscribe(self):
        feed = AdminFeed(max_events=100)
        received = []
        unsub = feed.subscribe(lambda evt: received.append(evt))

        feed.record_event("test.event", {"x": 1})
        assert len(received) == 1
        assert received[0].event_type == "test.event"

        unsub()
        feed.record_event("test.event", {"x": 2})
        assert len(received) == 1  # no new events after unsubscribe

    def test_record_from_envelope(self):
        feed = AdminFeed(max_events=100)
        envelope = {
            "id": "env-123",
            "type": "fleet.peer.online",
            "source": "fleet-nerve:mac2",
            "payload": {"peer_id": "mac1", "sessions": 3},
            "metadata": {"node_id": "mac2", "correlation_id": "corr-1"},
        }
        evt = feed.record_from_envelope(envelope)
        assert evt.id == "env-123"
        assert evt.event_type == "fleet.peer.online"
        assert evt.source == "fleet-nerve:mac2"
        assert evt.node_id == "mac2"
        assert evt.data["peer_id"] == "mac1"

    def test_stats(self):
        feed = AdminFeed(max_events=100)
        feed.record_event("a", {})
        feed.record_event("b", {})
        feed.get_recent(limit=10)

        stats = feed.stats
        assert stats["total_recorded"] == 2
        assert stats["total_served"] == 2

    def test_thread_safety(self):
        """Verify concurrent writes don't corrupt the buffer."""
        feed = AdminFeed(max_events=500)
        errors = []

        def writer(thread_id):
            try:
                for i in range(100):
                    feed.record_event("test.event", {"thread": thread_id, "seq": i})
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert feed.stats["total_recorded"] == 500
        events = feed.get_recent(limit=500)
        assert len(events) == 500

    def test_empty_feed(self):
        feed = AdminFeed(max_events=100)
        events = feed.get_recent()
        assert events == []
        summary = feed.get_summary()
        assert summary["total_events"] == 0
        assert summary["node_count"] == 0

    def test_sse_format(self):
        feed = AdminFeed(max_events=100)
        evt = feed.record_event("fleet.peer.online", {"peer": "mac1"})
        sse = evt.to_sse()
        lines = sse.strip().split("\n")
        assert lines[0] == "event: fleet.peer.online"
        assert lines[1].startswith("data: ")
        assert lines[2].startswith("id: ")


class TestEventBusIntegration:
    """Test that FleetEventBus wires into AdminFeed correctly."""

    def test_eventbus_emit_records_to_admin_feed(self):
        from multifleet.eventbus import FleetEventBus
        feed = AdminFeed(max_events=100)
        bus = FleetEventBus(node_id="mac2")
        bus.set_admin_feed(feed)

        # Emit an event (will fail EventBus delivery but should still record to admin feed)
        bus.emit("peer.online", {"peer_id": "mac1"})

        events = feed.get_recent(limit=10)
        assert len(events) == 1
        assert events[0]["event_type"] == "fleet.peer.online"
        assert events[0]["data"]["peer_id"] == "mac1"

    def test_eventbus_without_admin_feed(self):
        from multifleet.eventbus import FleetEventBus
        bus = FleetEventBus(node_id="mac2")
        # No admin feed set — should not raise
        result = bus.emit("peer.online", {"peer_id": "mac1"})
        # Should still return True (JSONL fallback)
        assert result is True
