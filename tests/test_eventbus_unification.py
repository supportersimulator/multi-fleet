"""Tests for event bus unification — legacy FleetEventBus → IDEEventBus migration.

Verifies:
1. Bridge: events emitted on FleetEventBus (via set_core_bus) are visible on IDEEventBus
2. IDEEventBus singleton works as drop-in replacement in migrated modules
3. FleetEventBus emits deprecation warning
4. Both create_event factories produce compatible structures
5. Namespace history captures bridged events
6. Wildcard subscriptions see bridged fleet events
7. Convenience helpers on IDEEventBus match old FleetEventBus semantics
"""

import sys
import warnings
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from multifleet.event_bus import IDEEventBus
from multifleet.event_bus_server import EventBus
from multifleet.eventbus import FleetEventBus


@pytest.fixture(autouse=True)
def reset_singleton():
    """Reset IDEEventBus singleton between tests."""
    IDEEventBus.reset()
    yield
    IDEEventBus.reset()


# ── 1. Bridge: set_core_bus makes legacy events visible on new bus ──


class TestBridgeVisibility:
    def test_fleet_event_visible_on_ide_bus(self):
        """Events emitted via FleetEventBus.emit() with set_core_bus bridge
        are received by IDEEventBus subscribers."""
        ide_bus = IDEEventBus.get()
        received = []
        ide_bus.on("fleet.*", lambda e: received.append(e))

        # Create legacy bus and wire bridge
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            legacy = FleetEventBus(node_id="mac1")
        legacy.set_core_bus(ide_bus.core_bus)

        # Emit via legacy bus
        legacy.emit("peer.online", {"peer_id": "mac2", "sessions": 1})

        assert len(received) == 1
        assert received[0]["type"] == "fleet.peer.online"
        assert received[0]["payload"]["peer_id"] == "mac2"

    def test_bridge_events_in_namespace_history(self):
        """Bridged fleet events appear in IDEEventBus namespace history."""
        ide_bus = IDEEventBus.get()

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            legacy = FleetEventBus(node_id="mac1")
        legacy.set_core_bus(ide_bus.core_bus)

        legacy.emit("health.degraded", {"peer_id": "mac2", "score": "C", "broken": ["P1"]})

        history = ide_bus.history("fleet", limit=10)
        assert len(history) >= 1
        assert history[0]["type"] == "fleet.health.degraded"

    def test_attach_fleet_daemon_wires_bridge(self):
        """IDEEventBus.attach_fleet_daemon() calls set_core_bus on FleetEventBus."""
        ide_bus = IDEEventBus.get()
        received = []
        ide_bus.on("fleet.message.*", lambda e: received.append(e))

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            legacy = FleetEventBus(node_id="mac3")
        ide_bus.attach_fleet_daemon(legacy)

        legacy.message_sent("mac1", "P2", "context", 0.5)

        assert len(received) == 1
        assert received[0]["payload"]["target"] == "mac1"


# ── 2. IDEEventBus as drop-in replacement ──


class TestIDEEventBusDropIn:
    def test_singleton_returns_same_instance(self):
        a = IDEEventBus.get()
        b = IDEEventBus.get()
        assert a is b

    def test_emit_and_subscribe(self):
        """Basic emit/subscribe works on IDEEventBus."""
        bus = IDEEventBus.get()
        received = []
        bus.on("probe.finding", lambda e: received.append(e))
        bus.emit("probe.finding", {"probe_id": "test", "severity": "high"},
                 source="probe-engine")
        assert len(received) == 1
        assert received[0]["payload"]["probe_id"] == "test"

    def test_wildcard_subscription(self):
        """Wildcard patterns work on IDEEventBus."""
        bus = IDEEventBus.get()
        received = []
        bus.on("supervisor.*", lambda e: received.append(e))
        bus.emit("supervisor.phase_transition", {"task_id": "t1", "to": "coding"})
        assert len(received) == 1


# ── 3. Deprecation warning on FleetEventBus ──


class TestDeprecationWarning:
    def test_fleet_eventbus_warns(self):
        """Importing and instantiating FleetEventBus emits DeprecationWarning."""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            FleetEventBus(node_id="mac1")
        deprecations = [x for x in w if issubclass(x.category, DeprecationWarning)]
        assert len(deprecations) >= 1
        assert "IDEEventBus" in str(deprecations[0].message)


# ── 4. Both create_event factories produce compatible structures ──


class TestCreateEventFactories:
    def test_event_types_create_event_returns_dict(self):
        from multifleet.event_types import create_event
        event = create_event("fleet.peer.online", {"peer_id": "mac1"}, source="test")
        assert isinstance(event, dict)
        assert event["type"] == "fleet.peer.online"
        assert "id" in event
        assert "version" in event
        assert "timestamp" in event
        assert event["payload"] == {"peer_id": "mac1"}

    def test_event_schema_create_event_returns_envelope(self):
        from multifleet.event_schema import (
            create_event, EventType, EventSource, EventEnvelope,
        )
        envelope = create_event(
            EventType.FLEET_NODE_HEARTBEAT,
            EventSource.MULTI_FLEET,
            node_id="mac1",
            payload={"uptime": 3600},
        )
        assert isinstance(envelope, EventEnvelope)
        assert envelope.type == "fleet.node.heartbeat"
        d = envelope.to_dict()
        assert "id" in d
        assert d["payload"] == {"uptime": 3600}

    def test_both_factories_share_field_names(self):
        """Both dict and envelope events have the same core fields."""
        from multifleet.event_types import create_event as create_dict
        from multifleet.event_schema import (
            create_event as create_envelope, EventType, EventSource,
        )
        d = create_dict("fleet.test", {"x": 1})
        e = create_envelope(
            EventType.FLEET_NODE_HEARTBEAT, EventSource.MULTI_FLEET,
            node_id="n", payload={"x": 1},
        ).to_dict()
        # Both must have: id, type, payload
        for key in ("id", "type", "payload"):
            assert key in d, f"dict event missing {key}"
            assert key in e, f"envelope event missing {key}"


# ── 5. Namespace history ring captures events ──


class TestNamespaceHistory:
    def test_fleet_namespace_ring(self):
        bus = IDEEventBus.get()
        bus.emit("fleet.peer.online", {"peer_id": "mac1"}, source="fleet-daemon")
        bus.emit("fleet.peer.offline", {"peer_id": "mac2"}, source="fleet-daemon")
        history = bus.history("fleet")
        assert len(history) == 2
        # Newest first
        assert history[0]["type"] == "fleet.peer.offline"

    def test_probe_namespace_ring(self):
        bus = IDEEventBus.get()
        bus.probe_finding("p1", "test finding", "high", 0.9)
        history = bus.history("probe")
        assert len(history) == 1
        assert history[0]["payload"]["probe_id"] == "p1"


# ── 6. Wildcard subscriptions see bridged fleet events ──


class TestWildcardBridged:
    def test_star_catches_all_bridged(self):
        ide_bus = IDEEventBus.get()
        all_events = []
        ide_bus.on("*", lambda e: all_events.append(e))

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            legacy = FleetEventBus(node_id="mac1")
        legacy.set_core_bus(ide_bus.core_bus)

        legacy.peer_online("mac2")
        legacy.channel_broken("mac3", score="D", broken=["P1", "P2"])

        fleet_events = [e for e in all_events if e["type"].startswith("fleet.")]
        assert len(fleet_events) == 2

    def test_fleet_channel_star_pattern(self):
        ide_bus = IDEEventBus.get()
        channel_events = []
        ide_bus.on("fleet.channel.*", lambda e: channel_events.append(e))

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            legacy = FleetEventBus(node_id="mac1")
        legacy.set_core_bus(ide_bus.core_bus)

        legacy.channel_broken("mac2", score="D", broken=["P1"])
        legacy.channel_healing("mac2", score="C", escalation_level=2)
        legacy.channel_restored("mac2", score="A")

        assert len(channel_events) == 3
        types = [e["type"] for e in channel_events]
        assert "fleet.channel.broken" in types
        assert "fleet.channel.healing" in types
        assert "fleet.channel.restored" in types


# ── 7. IDEEventBus typed helpers match old semantics ──


class TestTypedHelpers:
    def test_fleet_peer_online_helper(self):
        bus = IDEEventBus.get()
        received = []
        bus.on("fleet.peer.online", lambda e: received.append(e))
        bus.fleet_peer_online("mac1", sessions=3)
        assert received[0]["payload"]["peer_id"] == "mac1"
        assert received[0]["payload"]["sessions"] == 3

    def test_fleet_health_helper(self):
        bus = IDEEventBus.get()
        received = []
        bus.on("fleet.health.degraded", lambda e: received.append(e))
        bus.fleet_health("mac1", score="D", broken=["P1"])
        assert received[0]["payload"]["score"] == "D"

    def test_evidence_recorded_helper(self):
        bus = IDEEventBus.get()
        received = []
        bus.on("evidence.recorded", lambda e: received.append(e))
        bus.evidence_recorded("e1", "probe_finding", "mac1", subject="drift")
        assert received[0]["payload"]["entry_id"] == "e1"
