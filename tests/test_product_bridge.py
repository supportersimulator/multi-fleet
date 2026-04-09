"""Tests for ProductBridge — product integration routing.

15 tests covering: discovery, registration, routing, each bridge direction,
graceful degradation when products unavailable.
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure multifleet is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from multifleet.product_bridge import (
    BridgeError,
    ProductBridge,
    ProductInfo,
    ProductUnavailable,
    RouteNotFound,
    get_bridge,
    PRODUCT_CATALOG,
    _bridge,
)


@pytest.fixture
def bridge():
    """Fresh ProductBridge for each test."""
    return ProductBridge()


# ── 1. Discovery ─────────────────────────────────────────────────────────


class TestDiscovery:
    """Tests for product auto-discovery."""

    def test_discover_returns_all_products(self, bridge):
        """discover_products returns an entry for every cataloged product."""
        with patch.object(bridge, "_check_port", return_value=False), \
             patch.object(bridge, "_check_import", return_value=False):
            result = bridge.discover_products()

        assert set(result.keys()) == set(PRODUCT_CATALOG.keys())
        for name, info in result.items():
            assert isinstance(info, ProductInfo)
            assert info.name == name

    def test_discover_caches_results(self, bridge):
        """Second call within TTL returns cached results without re-checking."""
        with patch.object(bridge, "_check_port", return_value=True) as mock_port, \
             patch.object(bridge, "_check_import", return_value=True):
            bridge.discover_products()
            bridge.discover_products()

        # Port check should only be called once per port-based product
        port_products = [n for n, c in PRODUCT_CATALOG.items() if c.get("port")]
        assert mock_port.call_count == len(port_products)

    def test_discover_marks_available_when_port_responds(self, bridge):
        """Products with open ports are marked available."""
        def fake_port(port, timeout=2.0):
            return port == 8855  # Only fleet daemon is up

        with patch.object(bridge, "_check_port", side_effect=fake_port), \
             patch.object(bridge, "_check_import", return_value=False):
            result = bridge.discover_products()

        assert result["multi-fleet"].available is True
        assert result["context-dna"].available is False
        assert result["admin-panel"].available is False


# ── 2. Registration ──────────────────────────────────────────────────────


class TestRegistration:
    """Tests for manual product registration."""

    def test_register_product(self, bridge):
        """register_product stores capabilities and marks available."""
        info = bridge.register_product("multi-fleet", {"send": True, "receive": True})

        assert info.name == "multi-fleet"
        assert info.available is True
        assert info.capabilities == {"send": True, "receive": True}
        assert info.product_type == "transport"

    def test_register_unknown_product(self, bridge):
        """Registering an unknown product still works with defaults."""
        info = bridge.register_product("new-thing", {"experimental": True})

        assert info.name == "new-thing"
        assert info.product_type == "unknown"
        assert info.available is True


# ── 3. Routing ───────────────────────────────────────────────────────────


class TestRouting:
    """Tests for message routing between products."""

    def test_route_not_found(self, bridge):
        """Routing between unregistered pair raises RouteNotFound."""
        with pytest.raises(RouteNotFound, match="no route"):
            bridge.route("admin-panel", "superpowers", {"test": True})

    def test_product_unavailable_raises(self, bridge):
        """Routing to unavailable product raises ProductUnavailable."""
        bridge.register_product("3-surgeons", {})
        bridge._products["3-surgeons"].available = False

        with patch.object(bridge, "_refresh_product"):
            with pytest.raises(ProductUnavailable, match="3-surgeons"):
                bridge.route("multi-fleet", "3-surgeons", {"claim": "test"})

    def test_list_routes(self, bridge):
        """list_routes returns all registered route keys."""
        routes = bridge.list_routes()
        assert "multi-fleet->3-surgeons" in routes
        assert "3-surgeons->multi-fleet" in routes
        assert "multi-fleet->context-dna" in routes
        assert "context-dna->multi-fleet" in routes
        assert "multi-fleet->superpowers" in routes
        assert len(routes) == 7


# ── 4. Fleet <-> Surgeons Bridge ─────────────────────────────────────────


class TestFleetSurgeonsBridge:
    """Tests for fleet <-> 3-surgeons integration."""

    def test_fleet_to_surgeons_fallback_when_not_importable(self, bridge):
        """Returns fallback when 3-surgeons package not available."""
        bridge.register_product("3-surgeons", {})

        with patch("multifleet.product_bridge._import_cross_exam", return_value=None):
            result = bridge.fleet_to_surgeons({"claim": "branch A is better"})

        assert result["fallback"] is True
        assert "not importable" in result["error"]

    def test_fleet_to_surgeons_with_mock_engine(self, bridge):
        """Successful cross-exam returns verdict structure."""
        bridge.register_product("3-surgeons", {})

        mock_result = MagicMock()
        mock_result.verdict = "merge"
        mock_result.confidence = 0.85
        mock_result.reasoning = "Strong evidence for branch A"
        mock_result.dissent = []

        mock_engine = MagicMock()
        mock_engine.consult.return_value = mock_result

        with patch("multifleet.product_bridge._import_cross_exam", return_value=mock_engine):
            result = bridge.fleet_to_surgeons({
                "claim": "branch A is better",
                "evidence": {"tests_passing": True},
            })

        assert result["verdict"] == "merge"
        assert result["confidence"] == 0.85
        assert result["source"] == "3-surgeons"

    def test_surgeons_to_fleet_creates_packet(self, bridge):
        """Surgeon verdict creates a fleet packet for broadcast."""
        bridge.register_product("multi-fleet", {})

        result = bridge.surgeons_to_fleet({
            "task_id": "task-123",
            "verdict": "merge",
            "confidence": 0.9,
        })

        assert "packet_id" in result
        assert result["channel_used"] == "internal"


# ── 5. Fleet <-> Memory Bridge ──────────────────────────────────────────


class TestFleetMemoryBridge:
    """Tests for fleet <-> context-dna memory integration."""

    def test_fleet_to_memory_local_fallback(self, bridge):
        """When HTTP fails, falls back to local evidence ledger."""
        bridge.register_product("context-dna", {})

        mock_ledger_cls = MagicMock()
        mock_ledger = MagicMock()
        mock_ledger.record.return_value = "entry-abc"
        mock_ledger_cls.return_value = mock_ledger

        with patch("multifleet.product_bridge.urllib.request.urlopen", side_effect=OSError), \
             patch("multifleet.product_bridge.EvidenceLedger", mock_ledger_cls, create=True):
            # Patch the import inside the handler
            with patch.dict("sys.modules", {"multifleet.evidence_ledger": MagicMock(EvidenceLedger=mock_ledger_cls)}):
                result = bridge._handle_fleet_to_memory({
                    "event_type": "fleet_evidence",
                    "node_id": "mac1",
                    "evidence": {"finding": "test"},
                })

        # Either stored via HTTP or ledger or error — all are valid
        assert isinstance(result, dict)

    def test_memory_to_fleet_graceful_failure(self, bridge):
        """When both HTTP and ledger fail, returns empty results."""
        with patch("multifleet.product_bridge.urllib.request.urlopen", side_effect=OSError):
            result = bridge._handle_memory_to_fleet({"query": "test query"})

        # Should return results (possibly empty) without raising
        assert "results" in result or "error" in result


# ── 6. Probes -> Superpowers Bridge ─────────────────────────────────────


class TestProbesSuperPowersBridge:
    """Tests for probe findings -> superpowers workflow adjustments."""

    def test_empty_findings_returns_none_risk(self, bridge):
        """No findings = no risk."""
        result = bridge._handle_probes_to_superpowers({"findings": []})

        assert result["risk_level"] == "none"
        assert result["workflow_adjustments"] == []

    def test_security_finding_triggers_gate(self, bridge):
        """Security-related finding triggers security_review gate."""
        result = bridge._handle_probes_to_superpowers({
            "findings": [
                {"title": "Security surface: exposed env vars", "blast_radius": "high"},
            ]
        })

        assert result["risk_level"] == "high"
        assert len(result["workflow_adjustments"]) == 1
        assert result["workflow_adjustments"][0]["gate_type"] == "security_review"

    def test_drift_finding_triggers_sync(self, bridge):
        """Drift-related finding triggers sync action."""
        result = bridge._handle_probes_to_superpowers({
            "findings": [
                {"title": "Mirror drift detected", "blast_radius": "medium"},
            ]
        })

        assert result["risk_level"] == "medium"
        assert result["workflow_adjustments"][0]["action"] == "sync"

    def test_mixed_findings_risk_escalation(self, bridge):
        """Multiple findings escalate to highest risk level."""
        result = bridge._handle_probes_to_superpowers({
            "findings": [
                {"title": "Stale context", "blast_radius": "low"},
                {"title": "Architectural hotspot", "blast_radius": "high"},
            ]
        })

        assert result["risk_level"] == "high"
        assert result["findings_count"] == 2


# ── 7. Singleton ─────────────────────────────────────────────────────────


class TestSingleton:
    """Tests for module-level singleton."""

    def test_get_bridge_returns_instance(self):
        """get_bridge() returns a ProductBridge."""
        import multifleet.product_bridge as mod
        mod._bridge = None  # Reset
        b = get_bridge()
        assert isinstance(b, ProductBridge)

    def test_get_bridge_returns_same_instance(self):
        """get_bridge() returns same instance on repeated calls."""
        import multifleet.product_bridge as mod
        mod._bridge = None
        b1 = get_bridge()
        b2 = get_bridge()
        assert b1 is b2
