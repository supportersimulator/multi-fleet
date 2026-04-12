"""Tests for multifleet.panel_bus_bridge -- event bus to panel cache bridge."""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from multifleet.event_bus import IDEEventBus
from multifleet.panel_bus_bridge import (
    PanelBusBridge,
    PANEL_FLEET_STATUS,
    PANEL_PROBE_FINDINGS,
    PANEL_SURGEON_FEED,
    PANEL_EVIDENCE_TIMELINE,
    PANEL_QUORUM_PULSE,
    ALL_PANEL_IDS,
)


@pytest.fixture(autouse=True)
def reset_singletons():
    """Reset singletons before each test."""
    IDEEventBus.reset()
    PanelBusBridge.reset()
    yield
    IDEEventBus.reset()
    PanelBusBridge.reset()


@pytest.fixture
def bus():
    return IDEEventBus()


@pytest.fixture
def bridge(bus):
    return PanelBusBridge(bus)


class TestSubscriptions:
    """Bridge subscribes to correct namespaces."""

    def test_fleet_events_reach_fleet_panel(self, bus, bridge):
        bus.emit("fleet.peer.online", {"peer_id": "mac2"})
        data = bridge.get_panel(PANEL_FLEET_STATUS)
        assert data["event_count"] == 1
        assert len(data["events"]) == 1
        assert data["events"][0]["payload"]["peer_id"] == "mac2"

    def test_probe_events_reach_probe_panel(self, bus, bridge):
        bus.emit("probe.finding", {"probe_id": "p1", "severity": "high"})
        data = bridge.get_panel(PANEL_PROBE_FINDINGS)
        assert data["event_count"] == 1
        assert data["events"][0]["payload"]["probe_id"] == "p1"

    def test_surgeon_events_reach_surgeon_panel(self, bus, bridge):
        bus.emit("surgeon.verdict", {"surgeon": "atlas", "verdict": "approve"})
        data = bridge.get_panel(PANEL_SURGEON_FEED)
        assert data["event_count"] == 1

    def test_evidence_events_reach_evidence_panel(self, bus, bridge):
        bus.emit("evidence.recorded", {"entry_id": "e1", "node_id": "mac1"})
        data = bridge.get_panel(PANEL_EVIDENCE_TIMELINE)
        assert data["event_count"] == 1

    def test_quorum_events_reach_quorum_panel(self, bus, bridge):
        bus.emit("quorum.proposed", {"proposal_id": "q1", "topic": "test"})
        data = bridge.get_panel(PANEL_QUORUM_PULSE)
        assert data["event_count"] == 1

    def test_unrelated_events_dont_reach_panels(self, bus, bridge):
        bus.emit("panel.registered", {"panel_id": "x"})
        for pid in ALL_PANEL_IDS:
            assert bridge.get_panel(pid)["event_count"] == 0


class TestCacheUpdates:
    """Events update panel cache correctly."""

    def test_multiple_events_accumulate(self, bus, bridge):
        bus.emit("fleet.peer.online", {"peer_id": "mac1"})
        bus.emit("fleet.peer.online", {"peer_id": "mac2"})
        bus.emit("fleet.peer.offline", {"peer_id": "mac3"})
        data = bridge.get_panel(PANEL_FLEET_STATUS)
        assert data["event_count"] == 3
        assert len(data["events"]) == 3

    def test_events_newest_first(self, bus, bridge):
        bus.emit("probe.finding", {"probe_id": "first"})
        bus.emit("probe.finding", {"probe_id": "second"})
        data = bridge.get_panel(PANEL_PROBE_FINDINGS)
        assert data["events"][0]["payload"]["probe_id"] == "second"
        assert data["events"][1]["payload"]["probe_id"] == "first"

    def test_cache_respects_max_size(self, bus):
        small_bridge = PanelBusBridge(bus, cache_size=3)
        for i in range(5):
            bus.emit("fleet.peer.online", {"peer_id": f"mac{i}"})
        data = small_bridge.get_panel(PANEL_FLEET_STATUS)
        assert len(data["events"]) == 3
        assert data["event_count"] == 5  # total count tracks all

    def test_updated_at_advances(self, bus, bridge):
        before = bridge.get_panel(PANEL_FLEET_STATUS)["updated_at"]
        assert before == 0.0
        bus.emit("fleet.peer.online", {"peer_id": "mac1"})
        after = bridge.get_panel(PANEL_FLEET_STATUS)["updated_at"]
        assert after > before


class TestPanelUpdateEmission:
    """emit_panel_update fires panel.updated events on cache changes."""

    def test_panel_updated_emitted_on_event(self, bus, bridge):
        received = []
        bus.on("panel.updated", lambda e: received.append(e))
        bus.emit("fleet.peer.online", {"peer_id": "mac1"})
        assert len(received) == 1
        assert received[0]["payload"]["panel_id"] == PANEL_FLEET_STATUS

    def test_panel_updated_contains_metadata(self, bus, bridge):
        received = []
        bus.on("panel.updated", lambda e: received.append(e))
        bus.emit("surgeon.verdict", {"surgeon": "atlas"})
        payload = received[0]["payload"]
        assert payload["panel_id"] == PANEL_SURGEON_FEED
        assert payload["event_count"] == 1
        assert payload["updated_at"] > 0

    def test_manual_emit_panel_update(self, bus, bridge):
        received = []
        bus.on("panel.updated", lambda e: received.append(e))
        bridge.emit_panel_update(PANEL_QUORUM_PULSE, extra={"reason": "manual"})
        assert len(received) == 1
        assert received[0]["payload"]["reason"] == "manual"


class TestDataRetrieval:
    """Panel data is retrievable after events."""

    def test_get_all_panels(self, bus, bridge):
        bus.emit("fleet.peer.online", {"peer_id": "mac1"})
        bus.emit("probe.finding", {"probe_id": "p1"})
        result = bridge.get_all_panels()
        assert "panels" in result
        assert "generated_at" in result
        assert result["panels"][PANEL_FLEET_STATUS]["event_count"] == 1
        assert result["panels"][PANEL_PROBE_FINDINGS]["event_count"] == 1
        assert result["panels"][PANEL_SURGEON_FEED]["event_count"] == 0

    def test_get_timestamps(self, bus, bridge):
        bus.emit("fleet.peer.online", {"peer_id": "mac1"})
        ts = bridge.get_timestamps()
        assert ts["panels"][PANEL_FLEET_STATUS]["updated_at"] > 0
        assert ts["panels"][PANEL_PROBE_FINDINGS]["updated_at"] == 0

    def test_get_panel_for_component(self, bus, bridge):
        bus.emit("fleet.peer.online", {"peer_id": "mac1"})
        data = bridge.get_panel_for_component("vitals")
        assert data["panel_id"] == PANEL_FLEET_STATUS
        assert data["event_count"] == 1

    def test_get_panel_for_unknown_component(self, bus, bridge):
        data = bridge.get_panel_for_component("nonexistent")
        assert "error" in data

    def test_component_mapping_complete(self, bus, bridge):
        """All four dashboard components map to valid panels."""
        for name in ("vitals", "surgeon-feed", "quorum-pulse", "evidence-timeline"):
            data = bridge.get_panel_for_component(name)
            assert "error" not in data
            assert data["panel_id"] in ALL_PANEL_IDS

    def test_empty_panel_returns_zero_state(self, bus, bridge):
        data = bridge.get_panel(PANEL_FLEET_STATUS)
        assert data["events"] == []
        assert data["event_count"] == 0
        assert data["updated_at"] == 0.0


class TestStats:
    """Stats and health checks."""

    def test_stats_structure(self, bus, bridge):
        s = bridge.stats
        assert "panels" in s
        assert "cache_size" in s
        for pid in ALL_PANEL_IDS:
            assert pid in s["panels"]

    def test_health_reports_active_panels(self, bus, bridge):
        bus.emit("fleet.peer.online", {"peer_id": "mac1"})
        h = bridge.health()
        assert h["status"] == "ok"
        assert PANEL_FLEET_STATUS in h["active_panels"]
        assert h["total_events"] == 1


class TestSingleton:
    """Singleton pattern works correctly."""

    def test_get_returns_same_instance(self):
        a = PanelBusBridge.get()
        b = PanelBusBridge.get()
        assert a is b

    def test_reset_clears_singleton(self):
        a = PanelBusBridge.get()
        PanelBusBridge.reset()
        b = PanelBusBridge.get()
        assert a is not b
