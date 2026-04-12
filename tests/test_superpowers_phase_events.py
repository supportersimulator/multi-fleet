"""Tests for superpowers.phase.changed event emission — gap #4 integration."""

import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from multifleet.event_bus import IDEEventBus, NS_SUPERPOWERS
from multifleet.event_schema import (
    EventType,
    superpowers_phase_event,
)
from multifleet.superpowers_adapter import SuperpowersAdapter


@pytest.fixture(autouse=True)
def reset_singleton():
    """Reset singleton before and after each test."""
    IDEEventBus.reset()
    yield
    IDEEventBus.reset()


@pytest.fixture
def bus():
    b = IDEEventBus(ring_size=100, namespace_ring_size=50)
    # Set as singleton so adapter's IDEEventBus.get() returns same instance
    IDEEventBus._instance = b
    return b


# ═══════════════════════════════════════════════════════════════════
# IDEEventBus typed helper
# ═══════════════════════════════════════════════════════════════════


class TestSuperpowersHelper:
    def test_phase_changed_event_structure(self, bus):
        event = bus.superpowers_phase_changed(
            old_phase="brainstorming",
            new_phase="writing-plans",
            node_id="mac1",
        )
        assert event["type"] == "superpowers.phase.changed"
        assert event["payload"]["old_phase"] == "brainstorming"
        assert event["payload"]["new_phase"] == "writing-plans"
        assert event["payload"]["node_id"] == "mac1"
        assert event["payload"]["timestamp"] > 0
        assert event["source"] == "superpowers-adapter"

    def test_phase_changed_custom_timestamp(self, bus):
        ts = 1700000000.0
        event = bus.superpowers_phase_changed(
            old_phase="signal", new_phase="brainstorming",
            node_id="mac2", timestamp=ts,
        )
        assert event["payload"]["timestamp"] == ts

    def test_phase_changed_extra_kwargs(self, bus):
        event = bus.superpowers_phase_changed(
            old_phase="a", new_phase="b", node_id="mac1",
            source_detail="webhook",
        )
        assert event["payload"]["source_detail"] == "webhook"

    def test_phase_changed_captured_in_namespace_ring(self, bus):
        bus.superpowers_phase_changed("a", "b", "mac1")
        history = bus.history(NS_SUPERPOWERS)
        assert len(history) == 1
        assert history[0]["type"] == "superpowers.phase.changed"

    def test_subscription_receives_phase_event(self, bus):
        received = []
        bus.on("superpowers.phase.*", lambda e: received.append(e))
        bus.superpowers_phase_changed("signal", "brainstorming", "mac1")
        assert len(received) == 1
        assert received[0]["payload"]["new_phase"] == "brainstorming"


# ═══════════════════════════════════════════════════════════════════
# SuperpowersAdapter emits events on phase change
# ═══════════════════════════════════════════════════════════════════


class TestAdapterEmitsEvents:
    def test_update_phase_emits_event(self, bus):
        received = []
        bus.on("superpowers.phase.changed", lambda e: received.append(e))

        adapter = SuperpowersAdapter()
        adapter.update_phase("brainstorming", source="webhook")

        assert len(received) == 1
        assert received[0]["payload"]["old_phase"] == "unknown"
        assert received[0]["payload"]["new_phase"] == "brainstorming"

    def test_same_phase_no_event(self, bus):
        received = []
        bus.on("superpowers.phase.changed", lambda e: received.append(e))

        adapter = SuperpowersAdapter()
        adapter.update_phase("brainstorming")
        adapter.update_phase("brainstorming")  # same phase — no event

        assert len(received) == 1

    def test_multiple_transitions(self, bus):
        received = []
        bus.on("superpowers.phase.changed", lambda e: received.append(e))

        adapter = SuperpowersAdapter()
        adapter.update_phase("signal")
        adapter.update_phase("brainstorming")
        adapter.update_phase("writing-plans")
        adapter.update_phase("executing-plans")

        assert len(received) == 4
        # Verify chain: unknown->signal->brainstorming->writing-plans->executing-plans
        assert received[0]["payload"]["old_phase"] == "unknown"
        assert received[0]["payload"]["new_phase"] == "signal"
        assert received[3]["payload"]["old_phase"] == "writing-plans"
        assert received[3]["payload"]["new_phase"] == "executing-plans"

    def test_event_includes_node_id(self, bus):
        received = []
        bus.on("superpowers.phase.changed", lambda e: received.append(e))

        with patch.dict(os.environ, {"MULTIFLEET_NODE_ID": "mac3"}):
            # Reset cached node id
            import multifleet.superpowers_adapter as sa
            sa._NODE_ID = None
            adapter = SuperpowersAdapter()
            adapter.update_phase("executing-plans")
            sa._NODE_ID = None  # clean up

        assert received[0]["payload"]["node_id"] == "mac3"

    def test_event_timestamp_matches_adapter(self, bus):
        received = []
        bus.on("superpowers.phase.changed", lambda e: received.append(e))

        adapter = SuperpowersAdapter()
        adapter.update_phase("brainstorming")

        assert received[0]["payload"]["timestamp"] == adapter._updated_at


# ═══════════════════════════════════════════════════════════════════
# Graceful degradation
# ═══════════════════════════════════════════════════════════════════


class TestGracefulDegradation:
    def test_adapter_works_without_eventbus(self):
        """Phase changes still work even if IDEEventBus.get() fails."""
        adapter = SuperpowersAdapter()

        # Reset singleton so get() creates a fresh one, then patch get to fail
        IDEEventBus.reset()
        with patch.object(IDEEventBus, "get", side_effect=RuntimeError("no bus")):
            # Should not raise — graceful degradation
            adapter.update_phase("brainstorming")

        assert adapter._current_phase == "brainstorming"

    def test_adapter_works_when_bus_emit_fails(self, bus):
        """Phase changes succeed even if event emission errors."""
        adapter = SuperpowersAdapter()

        with patch.object(bus, "superpowers_phase_changed", side_effect=RuntimeError("boom")):
            adapter.update_phase("executing-plans")

        assert adapter._current_phase == "executing-plans"
        ok, _ = adapter.can_dispatch_race()
        assert ok is True


# ═══════════════════════════════════════════════════════════════════
# EventSchema convenience builder
# ═══════════════════════════════════════════════════════════════════


class TestEventSchemaBuilder:
    def test_superpowers_phase_event(self):
        env = superpowers_phase_event("mac1", "signal", "brainstorming", timestamp=100.0)
        assert env.type == EventType.SUPERPOWERS_PHASE_CHANGED.value
        assert env.source == "superpowers"
        assert env.node_id == "mac1"
        assert env.payload["old_phase"] == "signal"
        assert env.payload["new_phase"] == "brainstorming"
        assert env.payload["timestamp"] == 100.0

    def test_superpowers_phase_event_auto_timestamp(self):
        before = time.time()
        env = superpowers_phase_event("mac2", "a", "b")
        after = time.time()
        assert before <= env.payload["timestamp"] <= after


# ═══════════════════════════════════════════════════════════════════
# Namespace constant
# ═══════════════════════════════════════════════════════════════════


class TestNamespaceConstant:
    def test_ns_superpowers_value(self):
        assert NS_SUPERPOWERS == "superpowers"

    def test_ns_superpowers_in_all_namespaces(self):
        from multifleet.event_bus import ALL_NAMESPACES
        assert "superpowers" in ALL_NAMESPACES

    def test_namespace_validates(self):
        from multifleet.event_types import validate_namespace
        assert validate_namespace("superpowers.phase.changed") is True
