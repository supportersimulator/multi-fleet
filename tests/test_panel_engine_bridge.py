"""Tests for multifleet.panel_engine_bridge -- Python-to-TS panel format bridge."""

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from multifleet.event_bus import IDEEventBus
from multifleet.panel_bus_bridge import PanelBusBridge
from multifleet.panel_engine_bridge import (
    PanelEngineBridge,
    _PANEL_REGISTRY,
    _NAMESPACE_TO_PANEL_ID,
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
def bus_bridge(bus):
    return PanelBusBridge(bus)


@pytest.fixture
def mock_panel_server():
    server = MagicMock()
    server.all_panels.return_value = {
        "panels": [
            {"panel_id": "fleet-status", "title": "Fleet Status",
             "updated_at": "2026-04-15T00:00:00Z", "data": [{"id": "mac1"}],
             "summary": "1/1 nodes"},
        ],
        "generated_at": "2026-04-15T00:00:00Z",
    }
    return server


@pytest.fixture
def bridge(mock_panel_server, bus_bridge):
    return PanelEngineBridge(
        ide_panel_server=mock_panel_server,
        panel_bus_bridge=bus_bridge,
    )


# ---------------------------------------------------------------------------
# Manifest generation
# ---------------------------------------------------------------------------

class TestGenerateManifests:
    def test_returns_all_panels(self, bridge):
        manifests = bridge.generate_manifests()
        assert len(manifests) == len(_PANEL_REGISTRY)

    def test_manifest_has_required_fields(self, bridge):
        manifests = bridge.generate_manifests()
        required = {
            "panelId", "version", "displayName", "description",
            "source", "entry", "capabilities", "layout",
            "activationEvents",
        }
        for m in manifests:
            missing = required - set(m.keys())
            assert not missing, f"Missing fields in {m['panelId']}: {missing}"

    def test_manifest_source_is_mcp(self, bridge):
        for m in bridge.generate_manifests():
            assert m["source"] == "mcp"

    def test_manifest_layout_has_required_fields(self, bridge):
        layout_required = {"defaultMode", "resizable"}
        for m in bridge.generate_manifests():
            layout = m["layout"]
            missing = layout_required - set(layout.keys())
            assert not missing, f"Missing layout fields in {m['panelId']}: {missing}"

    def test_manifest_capabilities_are_strings(self, bridge):
        valid = {
            "repo_read", "repo_write", "agent_call", "container_exec",
            "evidence_read", "evidence_write", "bus_publish", "bus_subscribe",
        }
        for m in bridge.generate_manifests():
            for cap in m["capabilities"]:
                assert cap in valid, f"Invalid capability {cap} in {m['panelId']}"

    def test_single_manifest_lookup(self, bridge):
        m = bridge.generate_manifest("fleet-status")
        assert m is not None
        assert m["panelId"] == "fleet.status"

    def test_single_manifest_unknown_returns_none(self, bridge):
        assert bridge.generate_manifest("nonexistent") is None

    def test_manifests_are_copies(self, bridge):
        """Mutations to returned manifests must not affect registry."""
        m1 = bridge.generate_manifests()
        m1[0]["panelId"] = "MUTATED"
        m2 = bridge.generate_manifests()
        assert m2[0]["panelId"] != "MUTATED"


# ---------------------------------------------------------------------------
# Descriptor generation
# ---------------------------------------------------------------------------

class TestGenerateDescriptors:
    def test_returns_all_panels(self, bridge):
        descriptors = bridge.generate_descriptors()
        assert len(descriptors) == len(_PANEL_REGISTRY)

    def test_descriptor_has_required_fields(self, bridge):
        required = {
            "manifest", "state", "layoutMode", "permissions",
            "registeredAt", "lastStateChange", "activatedAt", "errorInfo",
        }
        for d in bridge.generate_descriptors():
            missing = required - set(d.keys())
            assert not missing, f"Missing: {missing}"

    def test_inactive_panel_state_is_registered(self, bridge):
        """Panels with no bus events should be 'registered'."""
        for d in bridge.generate_descriptors():
            assert d["state"] == "registered"
            assert d["activatedAt"] is None

    def test_active_panel_after_event(self, bridge, bus):
        """Panel with bus events should be 'active'."""
        bus.emit("fleet.node_update", {"node": "mac1"})
        time.sleep(0.05)
        descriptors = bridge.generate_descriptors()
        fleet_desc = next(
            d for d in descriptors
            if d["manifest"]["panelId"] == "fleet.status"
        )
        assert fleet_desc["state"] == "active"
        assert fleet_desc["activatedAt"] is not None

    def test_permissions_match_capabilities(self, bridge):
        for d in bridge.generate_descriptors():
            caps = d["manifest"]["capabilities"]
            granted = d["permissions"]["granted"]
            assert set(granted) == set(caps)
            assert d["permissions"]["denied"] == []

    def test_error_info_is_none(self, bridge):
        for d in bridge.generate_descriptors():
            assert d["errorInfo"] is None


# ---------------------------------------------------------------------------
# Event translation
# ---------------------------------------------------------------------------

class TestTranslateEvent:
    def test_fleet_event(self, bridge):
        result = bridge.translate_event(
            "fleet.node_update", {"node": "mac1"}, source="test"
        )
        assert result is not None
        assert result["type"] == "panel.activated"
        assert result["panelId"] == "fleet.status"
        assert result["source"] == "test"
        assert result["payload"] == {"node": "mac1"}
        assert "correlationId" in result
        assert "timestamp" in result

    def test_probe_event(self, bridge):
        result = bridge.translate_event("probe.scan_complete", {"findings": 3})
        assert result["panelId"] == "fleet.probe-findings"

    def test_surgeon_event(self, bridge):
        result = bridge.translate_event("surgeon.verdict", {"ok": True})
        assert result["panelId"] == "fleet.surgeon-feed"

    def test_evidence_event(self, bridge):
        result = bridge.translate_event("evidence.new_entry", {"seq": 42})
        assert result["panelId"] == "fleet.evidence-timeline"

    def test_quorum_event(self, bridge):
        result = bridge.translate_event("quorum.vote", {"result": "accept"})
        assert result["panelId"] == "fleet.quorum-pulse"

    def test_unknown_namespace_returns_none(self, bridge):
        assert bridge.translate_event("unknown.event", {}) is None

    def test_all_namespaces_mapped(self):
        """Every namespace in the mapping has a valid panel ID."""
        for ns, panel_id in _NAMESPACE_TO_PANEL_ID.items():
            found = any(
                reg["panelId"] == panel_id
                for reg in _PANEL_REGISTRY.values()
            )
            assert found, f"Namespace {ns} maps to unknown panel {panel_id}"


# ---------------------------------------------------------------------------
# Bus event translation
# ---------------------------------------------------------------------------

class TestTranslateBusEvent:
    def test_fleet_bus_event(self, bridge):
        result = bridge.translate_bus_event(
            "fleet.heartbeat", {"ts": 123}, source="daemon"
        )
        assert result is not None
        assert result["channel"] == "panel.events"
        assert result["key"] == "fleet.status:panel.activated"
        assert result["value"]["panelId"] == "fleet.status"
        assert result["value"]["payload"] == {"ts": 123}
        assert "timestamp" in result

    def test_unknown_bus_event_returns_none(self, bridge):
        assert bridge.translate_bus_event("nope.foo", {}) is None


# ---------------------------------------------------------------------------
# engine_format endpoint
# ---------------------------------------------------------------------------

class TestEngineFormat:
    def test_contains_all_sections(self, bridge):
        result = bridge.engine_format()
        assert "manifests" in result
        assert "descriptors" in result
        assert "panelData" in result
        assert "busCache" in result
        assert "generatedAt" in result

    def test_manifests_count(self, bridge):
        result = bridge.engine_format()
        assert len(result["manifests"]) == len(_PANEL_REGISTRY)

    def test_panel_data_from_server(self, bridge):
        result = bridge.engine_format()
        assert "fleet-status" in result["panelData"]

    def test_bus_cache_present(self, bridge, bus):
        bus.emit("fleet.test", {"x": 1})
        time.sleep(0.05)
        result = bridge.engine_format()
        assert "fleet-status" in result["busCache"]

    def test_handles_no_server(self, bus_bridge):
        """Works with only bus bridge, no panel server."""
        b = PanelEngineBridge(
            ide_panel_server=None,
            panel_bus_bridge=bus_bridge,
        )
        result = b.engine_format()
        assert len(result["manifests"]) == len(_PANEL_REGISTRY)
        assert result["panelData"] == {}

    def test_handles_no_bus_bridge(self, mock_panel_server):
        """Works with only panel server, no bus bridge."""
        b = PanelEngineBridge(
            ide_panel_server=mock_panel_server,
            panel_bus_bridge=None,
        )
        result = b.engine_format()
        assert len(result["manifests"]) == len(_PANEL_REGISTRY)
        assert result["busCache"] == {}


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

class TestHealth:
    def test_healthy_with_both(self, bridge):
        h = bridge.health()
        assert h["status"] == "ok"
        assert h["ide_panel_server"] is True
        assert h["panel_bus_bridge"] is True
        assert h["registered_panels"] == len(_PANEL_REGISTRY)

    def test_degraded_with_neither(self):
        b = PanelEngineBridge(ide_panel_server=None, panel_bus_bridge=None)
        h = b.health()
        assert h["status"] == "degraded"
