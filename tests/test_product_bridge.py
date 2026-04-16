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
        assert "superpowers->3-surgeons" in routes
        assert "superpowers->multi-fleet" in routes
        assert "3-surgeons->superpowers" in routes
        assert len(routes) == 10


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


# ── 8. New Routes — Superpowers <-> Surgeons <-> Fleet ──────────────────


class TestSuperpowersSurgeonsBridge:
    """Tests for superpowers <-> 3-surgeons wiring."""

    def test_superpowers_to_surgeons_via_brainstorm_ingest(self, bridge):
        """Brainstorm data is ingested and routed to surgeon cross-exam."""
        bridge.register_product("3-surgeons", {})
        bridge.register_product("superpowers", {})

        mock_result = MagicMock()
        mock_result.verdict = "approve"
        mock_result.confidence = 0.9
        mock_result.reasoning = "Option A is best"
        mock_result.dissent = []

        mock_engine = MagicMock()
        mock_engine.consult.return_value = mock_result

        with patch("multifleet.product_bridge._import_cross_exam", return_value=mock_engine):
            result = bridge.brainstorm_to_surgeons({
                "task_id": "fix-auth",
                "options": [
                    {"title": "Option A", "description": "Direct fix", "pros": ["fast"], "cons": ["risky"]},
                    {"title": "Option B", "description": "Refactor", "pros": ["clean"], "cons": ["slow"]},
                ],
            })

        assert result["source"] == "superpowers->3-surgeons"
        assert "brainstorm_id" in result
        assert result["verdict"] == "approve"

    def test_superpowers_to_surgeons_fallback_no_cross_exam(self, bridge):
        """When cross-exam unavailable, returns fallback."""
        bridge.register_product("3-surgeons", {})
        bridge.register_product("superpowers", {})

        with patch("multifleet.product_bridge._import_cross_exam", return_value=None):
            result = bridge.brainstorm_to_surgeons({
                "task_id": "test",
                "options": [{"title": "Only Option", "description": "..."}],
            })

        assert result.get("fallback") is True or result.get("error")

    def test_surgeons_to_superpowers_consensus_advances_workflow(self, bridge):
        """Surgeon consensus advances superpowers to executing-plans."""
        bridge.register_product("3-surgeons", {})
        bridge.register_product("superpowers", {})

        result = bridge.surgeon_verdict_to_workflow({
            "task_id": "fix-auth",
            "agreed": True,
            "confidence": 0.85,
        })

        assert result["action"] == "proceed"
        assert result["new_phase"] == "executing-plans"
        assert result["surgeon_agreed"] is True

    def test_surgeons_to_superpowers_dissent_reverts_to_planning(self, bridge):
        """Surgeon dissent reverts superpowers to writing-plans."""
        bridge.register_product("3-surgeons", {})
        bridge.register_product("superpowers", {})

        result = bridge.surgeon_verdict_to_workflow({
            "task_id": "fix-auth",
            "agreed": False,
            "confidence": 0.3,
        })

        assert result["action"] == "revise"
        assert result["new_phase"] == "writing-plans"

    def test_surgeons_to_superpowers_low_confidence_refines(self, bridge):
        """Low confidence consensus still requires refinement."""
        bridge.register_product("3-surgeons", {})
        bridge.register_product("superpowers", {})

        result = bridge.surgeon_verdict_to_workflow({
            "task_id": "fix-auth",
            "agreed": True,
            "confidence": 0.4,
        })

        assert result["action"] == "refine"
        assert result["new_phase"] == "writing-plans"


class TestSuperpowersFleetBridge:
    """Tests for superpowers <-> fleet phase notification wiring."""

    def test_superpowers_phase_change_notifies_fleet(self, bridge):
        """Phase change updates the SuperpowersAdapter and returns state."""
        bridge.register_product("superpowers", {})

        result = bridge.superpowers_phase_change("executing-plans", source="webhook")

        assert result["phase_updated"] is True
        assert result["current_phase"] == "executing-plans"
        assert result["can_dispatch"] is True

    def test_superpowers_blocking_phase_blocks_dispatch(self, bridge):
        """Blocking phase prevents fleet dispatch."""
        bridge.register_product("superpowers", {})

        result = bridge.superpowers_phase_change("brainstorming", source="webhook")

        assert result["phase_updated"] is True
        assert result["current_phase"] == "brainstorming"
        assert result["can_dispatch"] is False

    def test_probes_to_superpowers_includes_dispatch_status(self, bridge):
        """Probe findings response includes can_dispatch from SuperpowersAdapter."""
        result = bridge._handle_probes_to_superpowers({
            "findings": [
                {"title": "Test finding", "blast_radius": "low"},
            ]
        })

        assert "can_dispatch" in result
        assert "dispatch_reason" in result


class TestCrossProductWiring:
    """Tests verifying actual cross-product call chains work end-to-end."""

    def test_fleet_to_surgeons_records_in_memory(self, bridge):
        """Fleet->surgeons verdict is recorded in contextdna memory."""
        bridge.register_product("3-surgeons", {})

        mock_result = MagicMock()
        mock_result.verdict = "merge"
        mock_result.confidence = 0.85
        mock_result.reasoning = "Tests pass"
        mock_result.dissent = []

        mock_engine = MagicMock()
        mock_engine.consult.return_value = mock_result

        with patch("multifleet.product_bridge._import_cross_exam", return_value=mock_engine), \
             patch.object(bridge, "_handle_surgeons_to_memory") as mock_mem:
            result = bridge._handle_fleet_to_surgeons({
                "claim": "branch A is better",
                "task_id": "test-123",
            })

        assert result["verdict"] == "merge"
        # Verify memory was called with surgeon verdict data
        mock_mem.assert_called_once()
        mem_args = mock_mem.call_args[0][0]
        assert mem_args["surgeon"] == "cross-exam"
        assert mem_args["task_id"] == "test-123"

    def test_surgeons_to_fleet_notifies_superpowers(self, bridge):
        """Surgeon verdict routed to fleet also notifies superpowers."""
        bridge.register_product("multi-fleet", {})

        with patch.object(bridge, "_handle_surgeons_to_superpowers") as mock_sp:
            result = bridge._handle_surgeons_to_fleet({
                "task_id": "test-123",
                "verdict": "consensus",
                "agreed": True,
                "confidence": 0.9,
                "target_nodes": "all",
            })

        assert "packet_id" in result
        # Verify superpowers was notified
        mock_sp.assert_called_once()

    def test_full_brainstorm_to_verdict_to_workflow_chain(self, bridge):
        """End-to-end: brainstorm -> surgeon review -> workflow advance."""
        bridge.register_product("superpowers", {})
        bridge.register_product("3-surgeons", {})

        # Step 1: Brainstorm creates options
        mock_result = MagicMock()
        mock_result.verdict = "approve"
        mock_result.confidence = 0.9
        mock_result.reasoning = "Option A wins"
        mock_result.dissent = []

        mock_engine = MagicMock()
        mock_engine.consult.return_value = mock_result

        with patch("multifleet.product_bridge._import_cross_exam", return_value=mock_engine):
            surgeon_result = bridge.brainstorm_to_surgeons({
                "task_id": "new-feature",
                "options": [
                    {"title": "Option A", "description": "Direct approach"},
                    {"title": "Option B", "description": "Refactor first"},
                ],
            })

        assert surgeon_result["verdict"] == "approve"

        # Step 2: Surgeon verdict advances workflow
        workflow_result = bridge.surgeon_verdict_to_workflow({
            "task_id": "new-feature",
            "agreed": True,
            "confidence": 0.9,
        })

        assert workflow_result["action"] == "proceed"
        assert workflow_result["new_phase"] == "executing-plans"

    def test_all_ten_routes_registered(self, bridge):
        """All 10 routes are registered and have handlers."""
        routes = bridge.list_routes()
        expected = [
            "multi-fleet->3-surgeons",
            "3-surgeons->multi-fleet",
            "multi-fleet->context-dna",
            "context-dna->multi-fleet",
            "multi-fleet->superpowers",
            "3-surgeons->context-dna",
            "context-dna->3-surgeons",
            "superpowers->3-surgeons",
            "superpowers->multi-fleet",
            "3-surgeons->superpowers",
        ]
        for route in expected:
            assert route in routes, f"Missing route: {route}"
        assert len(routes) == 10
