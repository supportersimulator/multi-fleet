"""EventBus full wiring verification tests.

Proves the entire event pipeline: EventBus pub/sub, IDE bridge translation,
SSE streaming, extension endpoints, subscriber lifecycle, and burst handling.
"""

import asyncio
import json
import queue
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from multifleet.event_bus_server import EventBus, EventBusServer, SSEHandler
from multifleet.event_types import create_event, validate_namespace
from multifleet.eventbus import FleetEventBus
from multifleet.ide_bridge import IDEBridge, _EVENT_MAP
from multifleet.event_stream import push_event, _recent_events, _event_queues, _event_queues_lock


# ═══════════════════════════════════════════════════════════════════
# EventBus core pub/sub
# ═══════════════════════════════════════════════════════════════════


class TestEventBusCore:
    def test_event_published_reaches_handler(self):
        """Event published -> reaches subscribed handler."""
        bus = EventBus(ring_size=100)
        received = []
        bus.on("fleet.peer.*", lambda e: received.append(e))

        bus.emit("fleet.peer.online", {"peer_id": "mac2"}, source="test")

        assert len(received) == 1
        assert received[0]["type"] == "fleet.peer.online"
        assert received[0]["payload"]["peer_id"] == "mac2"

    def test_wildcard_star_catches_all(self):
        """Wildcard '*' handler receives every event."""
        bus = EventBus(ring_size=100)
        received = []
        bus.on("*", lambda e: received.append(e))

        bus.emit("fleet.peer.online", {})
        bus.emit("injection.completed", {})
        bus.emit("health.check", {})

        assert len(received) == 3

    def test_no_subscribers_graceful(self):
        """EventBus handles emit with zero subscribers without error."""
        bus = EventBus(ring_size=100)
        event = bus.emit("fleet.peer.online", {"peer_id": "mac1"})
        assert event["type"] == "fleet.peer.online"
        assert bus.stats["emitted"] == 1
        assert bus.stats["delivered"] == 0

    def test_once_handler_fires_once(self):
        """Once-handler fires exactly once then auto-unsubscribes."""
        bus = EventBus(ring_size=100)
        received = []
        bus.once("fleet.peer.online", lambda e: received.append(e))

        bus.emit("fleet.peer.online", {})
        bus.emit("fleet.peer.online", {})

        assert len(received) == 1

    def test_off_removes_handler(self):
        """off() correctly removes a handler."""
        bus = EventBus(ring_size=100)
        received = []
        handler = lambda e: received.append(e)
        bus.on("fleet.*", handler)
        bus.emit("fleet.peer.online", {})
        assert len(received) == 1

        bus.off("fleet.*", handler)
        bus.emit("fleet.peer.online", {})
        assert len(received) == 1  # no new events

    def test_handler_exception_does_not_crash_bus(self):
        """A failing handler does not prevent other handlers from firing."""
        bus = EventBus(ring_size=100)
        received = []

        def bad_handler(e):
            raise RuntimeError("boom")

        bus.on("fleet.*", bad_handler)
        bus.on("fleet.*", lambda e: received.append(e))

        bus.emit("fleet.test", {})

        assert len(received) == 1
        assert bus.stats["errors"] == 1

    def test_multiple_events_quick_succession_no_drops(self):
        """Rapid emission of many events does not drop any."""
        bus = EventBus(ring_size=500)
        received = []
        bus.on("fleet.burst.*", lambda e: received.append(e))

        count = 200
        for i in range(count):
            bus.emit(f"fleet.burst.event", {"seq": i})

        assert len(received) == count
        assert bus.stats["emitted"] == count

    def test_ring_buffer_stores_events(self):
        """Ring buffer retains recent events for replay."""
        bus = EventBus(ring_size=10)
        for i in range(15):
            bus.emit("fleet.test", {"seq": i})

        recent = bus.recent(limit=20)
        assert len(recent) == 10  # ring_size caps it
        # newest first
        assert recent[0]["payload"]["seq"] == 14

    def test_recent_with_pattern_filter(self):
        """recent() filters by pattern correctly."""
        bus = EventBus(ring_size=100)
        bus.emit("fleet.peer.online", {})
        bus.emit("fleet.health.degraded", {})
        bus.emit("fleet.peer.offline", {})

        peer_events = bus.recent(pattern="fleet.peer.*")
        assert len(peer_events) == 2


# ═══════════════════════════════════════════════════════════════════
# IDE Bridge translation
# ═══════════════════════════════════════════════════════════════════


class TestIDEBridgeTranslation:
    def test_peer_online_translates_to_status_bar(self):
        """fleet.peer.online -> IDE bridge -> statusBar command."""
        bridge = IDEBridge()
        result = bridge.translate_event("fleet.peer.online", {"peer_id": "mac2"})
        assert result["command"] == "statusBar"
        assert result["payload"]["action"] == "online"

    def test_peer_offline_translates_to_status_bar(self):
        """fleet.peer.offline -> IDE bridge -> statusBar command."""
        bridge = IDEBridge()
        result = bridge.translate_event("fleet.peer.offline", {"peer_id": "mac2"})
        assert result["command"] == "statusBar"
        assert result["payload"]["action"] == "offline"

    def test_probe_finding_queues_notification(self):
        """fleet.probe.finding -> notification queued."""
        bridge = IDEBridge()
        result = bridge.translate_event(
            "fleet.probe.finding",
            {"title": "Stale doc detected", "severity": "warning"},
        )
        assert result["command"] == "notification"
        notifications = bridge.get_pending_notifications()
        assert len(notifications) == 1
        assert "Stale doc" in notifications[0]["message"]
        assert notifications[0]["severity"] == "warning"

    def test_health_degraded_generates_warning(self):
        """fleet.health.degraded -> warning + notification queued."""
        bridge = IDEBridge()
        result = bridge.translate_event(
            "fleet.health.degraded",
            {"node_id": "mac3", "score": "0.4"},
        )
        assert result["command"] == "warning"
        notifications = bridge.get_pending_notifications()
        assert len(notifications) == 1
        assert notifications[0]["severity"] == "warning"

    def test_rebuttal_proposed_badges_activity_bar(self):
        """fleet.rebuttal.proposed -> badge command + notification."""
        bridge = IDEBridge()
        result = bridge.translate_event(
            "fleet.rebuttal.proposed",
            {"subject": "Schema change proposal"},
        )
        assert result["command"] == "badge"
        notifications = bridge.get_pending_notifications()
        assert len(notifications) == 1
        assert "Schema change" in notifications[0]["message"]

    def test_get_pending_notifications_clears_after_read(self):
        """get_pending_notifications returns and clears the queue."""
        bridge = IDEBridge()
        bridge.translate_event("fleet.probe.finding", {"title": "test"})
        first = bridge.get_pending_notifications()
        assert len(first) == 1
        second = bridge.get_pending_notifications()
        assert len(second) == 0

    def test_format_for_extension_produces_v1_schema(self):
        """format_for_extension returns ContextDNA Event v1 shape."""
        bridge = IDEBridge()
        translated = bridge.translate_event("fleet.peer.online", {"peer_id": "mac1"})
        formatted = bridge.format_for_extension(translated)
        assert "type" in formatted
        assert "payload" in formatted
        assert "source" in formatted
        assert "timestamp" in formatted
        assert formatted["source"] == "fleet-bridge"


# ═══════════════════════════════════════════════════════════════════
# FleetEventBus (producer adapter)
# ═══════════════════════════════════════════════════════════════════


class TestFleetEventBusProducer:
    def test_emit_with_core_bus_delivers_directly(self):
        """When core_bus is set, events go through in-process path."""
        core = EventBus(ring_size=100)
        received = []
        core.on("fleet.*", lambda e: received.append(e))

        feb = FleetEventBus(node_id="mac1")
        feb.set_core_bus(core)
        result = feb.emit("peer.online", {"peer_id": "mac2"})

        assert result is True
        assert len(received) == 1
        assert received[0]["type"] == "fleet.peer.online"
        assert feb.stats["eventbus_delivered"] == 1

    def test_emit_falls_back_to_jsonl(self, tmp_path):
        """Without core_bus or HTTP, falls back to JSONL file."""
        import multifleet.eventbus as eb_mod

        original_fallback = eb_mod.JSONL_FALLBACK
        eb_mod.JSONL_FALLBACK = tmp_path / "events.jsonl"
        try:
            feb = FleetEventBus(node_id="mac1")
            # No core bus, no HTTP endpoint
            feb._eventbus_available = False
            feb._last_availability_check = time.time()

            result = feb.emit("test.event", {"key": "val"})
            assert result is True
            assert feb.stats["jsonl_fallback"] == 1
            assert eb_mod.JSONL_FALLBACK.exists()
            lines = eb_mod.JSONL_FALLBACK.read_text().strip().split("\n")
            assert len(lines) == 1
            data = json.loads(lines[0])
            assert data["type"] == "fleet.test.event"
        finally:
            eb_mod.JSONL_FALLBACK = original_fallback

    def test_convenience_peer_online(self):
        """peer_online() convenience method emits correct event."""
        core = EventBus(ring_size=100)
        received = []
        core.on("*", lambda e: received.append(e))

        feb = FleetEventBus(node_id="mac1")
        feb.set_core_bus(core)
        feb.peer_online("mac2", sessions=3)

        assert len(received) == 1
        assert received[0]["payload"]["peer_id"] == "mac2"
        assert received[0]["payload"]["sessions"] == 3


# ═══════════════════════════════════════════════════════════════════
# SSE push_event (event_stream module)
# ═══════════════════════════════════════════════════════════════════


class TestEventStreamPush:
    def test_push_event_reaches_connected_queue(self):
        """push_event delivers to all registered SSE client queues."""
        q = queue.Queue(maxsize=10)
        with _event_queues_lock:
            _event_queues.append(q)
        try:
            push_event({"type": "fleet.test", "payload": {}})
            item = q.get(timeout=1)
            assert item["type"] == "fleet.test"
        finally:
            with _event_queues_lock:
                _event_queues.remove(q)

    def test_push_event_no_queues_no_error(self):
        """push_event with no SSE clients does not raise."""
        # Just make sure no exception
        push_event({"type": "fleet.noop", "payload": {}})


# ═══════════════════════════════════════════════════════════════════
# Extension endpoints (IDEBridge data shapes)
# ═══════════════════════════════════════════════════════════════════


class TestExtensionEndpoints:
    def test_status_bar_without_aggregator(self):
        """Status bar returns sensible defaults when aggregator is None."""
        bridge = IDEBridge(aggregator=None)
        data = bridge.get_status_bar_data()
        assert data["node_count"] == 0
        assert data["health_dot"] == "red"
        assert "off" in data["text"].lower()

    def test_activity_bar_without_aggregator(self):
        """Activity bar returns empty sections when aggregator is None."""
        bridge = IDEBridge(aggregator=None)
        data = bridge.get_activity_bar_data()
        assert data["sections"] == []
        assert data["connected"] is False

    def test_status_bar_with_aggregator(self):
        """Status bar returns node count from aggregator snapshot."""
        agg = MagicMock()
        agg.snapshot.return_value = {
            "peers": {"reachable": 2, "total": 3},
            "health_dot": "green",
        }
        agg.status_bar_text.return_value = "Fleet: 3/3"
        bridge = IDEBridge(aggregator=agg)
        data = bridge.get_status_bar_data()
        assert data["node_count"] == 3  # self + 2 reachable
        assert data["health_dot"] == "green"

    def test_activity_bar_with_peers(self):
        """Activity bar builds peer tree from aggregator snapshot."""
        agg = MagicMock()
        agg.snapshot.return_value = {
            "daemon": {
                "status": "ok",
                "peers": {
                    "mac2": {"status": "online", "sessions": 2},
                    "mac3": {"status": "offline", "last_seen": 60},
                },
            },
            "rebuttals": {"active": 1, "total": 3},
            "leases": {"active": 0, "held_by_me": 0},
            "liaison": {"unread": 0},
        }
        bridge = IDEBridge(aggregator=agg)
        data = bridge.get_activity_bar_data()
        assert data["connected"] is True
        peer_section = next(s for s in data["sections"] if s["id"] == "peers")
        assert peer_section["count"] == 2
        rebuttal_section = next(s for s in data["sections"] if s["id"] == "rebuttals")
        assert rebuttal_section["count"] == 1

    def test_notifications_endpoint_returns_correct_data(self):
        """Extension notifications endpoint returns list of notification dicts."""
        bridge = IDEBridge()
        bridge.translate_event("fleet.probe.finding", {"title": "Issue A"})
        bridge.translate_event("fleet.health.degraded", {"node_id": "mac2"})

        notifications = bridge.get_pending_notifications()
        assert len(notifications) == 2
        assert all("severity" in n for n in notifications)
        assert all("message" in n for n in notifications)
        assert all("timestamp" in n for n in notifications)
