"""Tests for multifleet.eventbus — Fleet EventBus adapter."""

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from multifleet.eventbus import FleetEventBus, JSONL_FALLBACK, MAX_JSONL_SIZE


@pytest.fixture
def bus():
    """EventBus with EventBus marked unavailable (forces JSONL fallback)."""
    b = FleetEventBus(node_id="mac2")
    b._eventbus_available = False
    b._last_availability_check = 1e12  # far future — no re-probe
    return b


@pytest.fixture
def bus_with_eventbus():
    """EventBus with EventBus marked available (mocked POST)."""
    b = FleetEventBus(node_id="mac2")
    b._eventbus_available = True
    b._last_availability_check = 1e12
    return b


class TestEmitCreatesValidEvent:
    def test_event_structure(self, bus, tmp_path):
        jsonl = tmp_path / "events.jsonl"
        with patch("multifleet.eventbus.JSONL_FALLBACK", jsonl):
            bus.emit("test.event", {"key": "val"})
        event = json.loads(jsonl.read_text().strip())
        assert "id" in event
        assert event["version"] == 1
        assert event["type"] == "fleet.test.event"
        assert event["source"] == "fleet-nerve:mac2"
        assert "timestamp" in event
        assert event["payload"] == {"key": "val"}
        assert event["metadata"]["node_id"] == "mac2"
        assert "correlation_id" in event["metadata"]


class TestFleetNamespace:
    def test_all_events_prefixed(self, bus, tmp_path):
        jsonl = tmp_path / "events.jsonl"
        with patch("multifleet.eventbus.JSONL_FALLBACK", jsonl):
            bus.emit("peer.online", {})
            bus.emit("message.sent", {})
            bus.emit("health.degraded", {})
        lines = jsonl.read_text().strip().split("\n")
        for line in lines:
            event = json.loads(line)
            assert event["type"].startswith("fleet."), f"Missing fleet. prefix: {event['type']}"


class TestJsonlFallback:
    def test_writes_when_eventbus_unavailable(self, bus, tmp_path):
        jsonl = tmp_path / "events.jsonl"
        with patch("multifleet.eventbus.JSONL_FALLBACK", jsonl):
            result = bus.emit("test.fallback", {"x": 1})
        assert result is True
        assert jsonl.exists()
        event = json.loads(jsonl.read_text().strip())
        assert event["payload"] == {"x": 1}

    def test_creates_parent_dirs(self, bus, tmp_path):
        jsonl = tmp_path / "deep" / "nested" / "events.jsonl"
        with patch("multifleet.eventbus.JSONL_FALLBACK", jsonl):
            bus.emit("test.dirs", {})
        assert jsonl.exists()

    def test_appends_multiple_events(self, bus, tmp_path):
        jsonl = tmp_path / "events.jsonl"
        with patch("multifleet.eventbus.JSONL_FALLBACK", jsonl):
            bus.emit("a", {})
            bus.emit("b", {})
            bus.emit("c", {})
        lines = jsonl.read_text().strip().split("\n")
        assert len(lines) == 3


class TestJsonlRotation:
    def test_rotation_when_over_limit(self, bus, tmp_path):
        """JSONL file rotates when it exceeds MAX_JSONL_SIZE."""
        jsonl = tmp_path / "events.jsonl"
        # Create a file just over the limit
        jsonl.parent.mkdir(parents=True, exist_ok=True)
        jsonl.write_text("x" * (MAX_JSONL_SIZE + 1))

        with patch("multifleet.eventbus.JSONL_FALLBACK", jsonl):
            bus.emit("after.rotation", {"rotated": True})

        # Original should now be small (just the new event)
        assert jsonl.exists()
        assert jsonl.stat().st_size < MAX_JSONL_SIZE

        # .jsonl.1 should have the old content
        rotated = tmp_path / "events.jsonl.1"
        assert rotated.exists()
        assert rotated.stat().st_size > MAX_JSONL_SIZE

    def test_rotation_shifts_existing(self, bus, tmp_path):
        """Existing .jsonl.1 gets shifted to .jsonl.2."""
        jsonl = tmp_path / "events.jsonl"
        rot1 = tmp_path / "events.jsonl.1"
        rot2 = tmp_path / "events.jsonl.2"

        jsonl.write_text("x" * (MAX_JSONL_SIZE + 1))
        rot1.write_text("old-rotation-1")

        with patch("multifleet.eventbus.JSONL_FALLBACK", jsonl):
            bus.emit("after.rotation2", {})

        assert rot2.exists()
        assert rot2.read_text() == "old-rotation-1"
        assert rot1.exists()  # now contains the previously-current file

    def test_oldest_rotation_deleted(self, bus, tmp_path):
        """When .jsonl.2 already exists, it gets deleted on rotation."""
        jsonl = tmp_path / "events.jsonl"
        rot1 = tmp_path / "events.jsonl.1"
        rot2 = tmp_path / "events.jsonl.2"

        jsonl.write_text("x" * (MAX_JSONL_SIZE + 1))
        rot1.write_text("rotation-1-content")
        rot2.write_text("rotation-2-to-delete")

        with patch("multifleet.eventbus.JSONL_FALLBACK", jsonl):
            bus.emit("after.rotation3", {})

        # rot2 should now have rot1's old content
        assert rot2.read_text() == "rotation-1-content"

    def test_no_rotation_under_limit(self, bus, tmp_path):
        """No rotation when file is under the size limit."""
        jsonl = tmp_path / "events.jsonl"
        jsonl.write_text("small-file")

        with patch("multifleet.eventbus.JSONL_FALLBACK", jsonl):
            bus.emit("no.rotation", {})

        rot1 = tmp_path / "events.jsonl.1"
        assert not rot1.exists()


class TestBroadcast:
    def test_broadcast_event_types_auto_broadcast(self, bus, tmp_path):
        """Important events (peer.online, lease.acquired, etc.) auto-broadcast."""
        jsonl = tmp_path / "events.jsonl"
        with patch("multifleet.eventbus.JSONL_FALLBACK", jsonl), \
             patch("urllib.request.urlopen", side_effect=Exception("no daemon")) as mock_urlopen:
            bus.peer_online("mac1", sessions=2)

        # Should have tried both JSONL and broadcast
        # broadcast_sent stays 0 because daemon is unreachable
        assert bus.stats["broadcast_sent"] == 0
        assert bus.stats["jsonl_fallback"] == 1

    def test_broadcast_not_for_high_freq(self, bus, tmp_path):
        """message.sent and message.received do NOT broadcast."""
        jsonl = tmp_path / "events.jsonl"
        with patch("multifleet.eventbus.JSONL_FALLBACK", jsonl), \
             patch("urllib.request.urlopen") as mock_urlopen:
            bus.message_sent("mac3", "P1", "context", 0.1)
            bus.message_received("mac3", "context")

        # urlopen should NOT have been called for broadcast (only JSONL path)
        # (eventbus is unavailable, so urlopen is not called for eventbus either)
        assert bus.stats["broadcast_sent"] == 0

    def test_explicit_broadcast_param(self, bus, tmp_path):
        """broadcast=True forces broadcast even for custom events."""
        jsonl = tmp_path / "events.jsonl"
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{}'
        with patch("multifleet.eventbus.JSONL_FALLBACK", jsonl), \
             patch("urllib.request.urlopen", return_value=mock_resp):
            bus.emit("custom.event", {"data": 1}, broadcast=True)

        assert bus.stats["broadcast_sent"] == 1

    def test_broadcast_includes_stats(self, bus):
        """Stats dict includes broadcast_sent counter."""
        assert "broadcast_sent" in bus.stats


class TestPeerOnlineEvent:
    def test_payload(self, bus, tmp_path):
        jsonl = tmp_path / "events.jsonl"
        with patch("multifleet.eventbus.JSONL_FALLBACK", jsonl):
            bus.peer_online("mac1", sessions=3)
        event = json.loads(jsonl.read_text().strip())
        assert event["type"] == "fleet.peer.online"
        assert event["payload"] == {"peer_id": "mac1", "sessions": 3}


class TestMessageSentEvent:
    def test_payload(self, bus, tmp_path):
        jsonl = tmp_path / "events.jsonl"
        with patch("multifleet.eventbus.JSONL_FALLBACK", jsonl):
            bus.message_sent("mac3", "P1_nats", "context", 0.42)
        event = json.loads(jsonl.read_text().strip())
        assert event["type"] == "fleet.message.sent"
        assert event["payload"]["target"] == "mac3"
        assert event["payload"]["channel"] == "P1_nats"
        assert event["payload"]["type"] == "context"
        assert event["payload"]["elapsed_s"] == 0.42


class TestStatsTracking:
    def test_counters_increment(self, bus, tmp_path):
        jsonl = tmp_path / "events.jsonl"
        with patch("multifleet.eventbus.JSONL_FALLBACK", jsonl):
            bus.emit("a", {})
            bus.emit("b", {})
        assert bus.stats["emitted"] == 2
        assert bus.stats["jsonl_fallback"] == 2
        assert bus.stats["eventbus_delivered"] == 0
        assert bus.stats["errors"] == 0

    def test_eventbus_delivered_counter(self, bus_with_eventbus):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            bus_with_eventbus.emit("test", {"ok": True})
        assert bus_with_eventbus.stats["eventbus_delivered"] == 1
        assert bus_with_eventbus.stats["jsonl_fallback"] == 0


class TestEventBusUnavailableGraceful:
    def test_no_crash_when_both_fail(self):
        bus = FleetEventBus(node_id="mac2")
        bus._eventbus_available = False
        bus._last_availability_check = 1e12
        # Make JSONL also fail
        with patch("multifleet.eventbus.JSONL_FALLBACK", Path("/nonexistent/readonly/x.jsonl")):
            result = bus.emit("test.fail", {})
        assert result is False
        assert bus.stats["errors"] == 1

    def test_check_eventbus_returns_false_when_down(self):
        bus = FleetEventBus(node_id="mac2")
        with patch("urllib.request.urlopen", side_effect=Exception("conn refused")):
            assert bus.check_eventbus() is False
