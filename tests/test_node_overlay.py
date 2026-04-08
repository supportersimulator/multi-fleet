"""Tests for NodeOverlay and AcceptanceGate."""

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

FLEET_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(FLEET_ROOT))

from multifleet.node_overlay import (
    EpistemicStrip,
    EvidenceStrip,
    InvariantStrip,
    NodeOverlay,
    NodeOverlayManager,
    ProbeStrip,
    SurgeonStrip,
)
from multifleet.acceptance_gate import AcceptanceGate, AcceptanceResult


# ═══════════════════════════════════════════════════════════════════
# NodeOverlay tests
# ═══════════════════════════════════════════════════════════════════


class TestNodeOverlayCreation:
    def test_default_overlay(self):
        o = NodeOverlay(node_id="mac1")
        assert o.node_id == "mac1"
        assert o.epistemic.certainty_band == 0.5
        assert o.surgeon.phase == "idle"
        assert o.invariant.active_count == 0
        assert o.evidence.candidate_count == 0
        assert o.probe.blocking_count == 0

    def test_to_dict_has_all_strips(self):
        o = NodeOverlay(node_id="mac2")
        d = o.to_dict()
        assert d["node_id"] == "mac2"
        assert "health_score" in d
        assert "epistemic" in d
        assert "surgeon" in d
        assert "invariant" in d
        assert "evidence" in d
        assert "probe" in d

    def test_strips_serialize(self):
        ep = EpistemicStrip(certainty_band=0.9, contradiction_count=2)
        d = ep.to_dict()
        assert d["certainty_band"] == 0.9
        assert d["contradiction_count"] == 2


class TestOverlayHealthScore:
    def test_perfect_health(self):
        o = NodeOverlay(node_id="mac1")
        o.epistemic.certainty_band = 1.0
        o.epistemic.contradiction_count = 0
        o.epistemic.stale_context_risk = 0.0
        o.surgeon.disagreement_score = 0.0
        o.surgeon.correction_loop_count = 0
        o.invariant.active_count = 5
        o.invariant.violated_count = 0
        o.evidence.candidate_count = 3
        o.evidence.promoted_count = 3
        o.probe.blocking_count = 0
        o.probe.warning_count = 0
        score = o.health_score()
        assert score >= 0.9

    def test_degraded_health_from_violations(self):
        o = NodeOverlay(node_id="mac1")
        o.invariant.active_count = 4
        o.invariant.violated_count = 4  # all violated
        score = o.health_score()
        # Should be noticeably lower
        assert score < 0.8

    def test_degraded_health_from_blocking_probes(self):
        o = NodeOverlay(node_id="mac1")
        o.probe.blocking_count = 3
        score = o.health_score()
        assert score < 0.9

    def test_health_score_clamped(self):
        o = NodeOverlay(node_id="mac1")
        o.epistemic.certainty_band = 0.0
        o.probe.blocking_count = 10
        o.invariant.active_count = 10
        o.invariant.violated_count = 10
        score = o.health_score()
        assert 0.0 <= score <= 1.0


class TestOverlayUpdateFromHealth:
    def test_ok_status_high_certainty(self):
        mgr = NodeOverlayManager()
        health = {"status": "ok", "peers": {"mac2": {}, "mac3": {}}}
        overlay = mgr.update_from_health("mac1", health)
        assert overlay.epistemic.certainty_band > 0.8

    def test_degraded_status_low_certainty(self):
        mgr = NodeOverlayManager()
        health = {"status": "degraded", "peers": {}}
        overlay = mgr.update_from_health("mac1", health)
        assert overlay.epistemic.certainty_band < 0.5

    def test_channel_health_maps_to_stale_risk(self):
        mgr = NodeOverlayManager()
        health = {
            "status": "ok",
            "peers": {},
            "channel_health": {"mac2": {"score": 50}},
        }
        overlay = mgr.update_from_health("mac1", health)
        assert overlay.epistemic.stale_context_risk > 0.4


class TestOverlayManager:
    def test_update_creates_overlay(self):
        mgr = NodeOverlayManager()
        overlay = mgr.update("mac1", epistemic={"certainty_band": 0.9})
        assert overlay.epistemic.certainty_band == 0.9

    def test_get_returns_none_for_unknown(self):
        mgr = NodeOverlayManager()
        assert mgr.get("nonexistent") is None

    def test_all_overlays(self):
        mgr = NodeOverlayManager()
        mgr.update("mac1")
        mgr.update("mac2")
        assert len(mgr.all_overlays()) == 2

    def test_update_from_probe(self):
        mgr = NodeOverlayManager()
        probes = [
            {"status": "critical", "severity": "critical", "findings": [{"title": "Bad import"}]},
            {"status": "ok", "severity": "info", "findings": []},
            {"status": "warning", "severity": "medium", "findings": [{"title": "Stale docs"}]},
        ]
        overlay = mgr.update_from_probe("mac1", probes)
        assert overlay.probe.blocking_count == 1
        assert overlay.probe.warning_count == 1
        assert overlay.probe.info_count == 1
        assert "Bad import" in overlay.probe.top_findings

    def test_update_from_surgeon(self):
        mgr = NodeOverlayManager()
        state = {"phase": "operate", "disagreement_score": 0.4, "correction_loops": 2}
        overlay = mgr.update_from_surgeon("mac1", state)
        assert overlay.surgeon.phase == "operate"
        assert overlay.surgeon.disagreement_score == 0.4
        assert overlay.surgeon.correction_loop_count == 2


# ═══════════════════════════════════════════════════════════════════
# AcceptanceGate tests
# ═══════════════════════════════════════════════════════════════════


class TestAcceptanceGateAllPass:
    def test_all_pass_no_subsystems(self):
        """Gate passes when no subsystems configured (all checks skip)."""
        gate = AcceptanceGate()
        result = gate.evaluate("task-001", "feature/x")
        assert result.accepted is True
        assert len(result.blocking_reasons) == 0
        assert len(result.checks) == 6


class TestAcceptanceGateBlockingProbe:
    def test_blocking_probe_fails_gate(self):
        """Critical probe findings block acceptance."""
        mock_engine = MagicMock()
        mock_result = MagicMock()
        mock_result.status = "critical"
        mock_result.severity = "critical"
        mock_engine.run_cheap.return_value = [mock_result]
        gate = AcceptanceGate(probe_engine=mock_engine)
        result = gate.evaluate("task-002", "feature/y", context={"probe_context": {}})
        assert result.accepted is False
        assert any("critical probe" in r.lower() for r in result.blocking_reasons)


class TestAcceptanceGateNoEvidence:
    def test_no_evidence_blocks_gate(self):
        """Missing evidence ledger entries block acceptance."""
        mock_ledger = MagicMock()
        mock_ledger.query.return_value = []  # no entries
        gate = AcceptanceGate(evidence_ledger=mock_ledger)
        result = gate.evaluate("task-003", "feature/z")
        assert result.accepted is False
        assert any("evidence" in r.lower() for r in result.blocking_reasons)

    def test_evidence_present_passes(self):
        """Evidence entries present allows gate to pass."""
        mock_ledger = MagicMock()
        mock_ledger.query.return_value = [{"entry_id": "ev-001", "subject": "task-004"}]
        gate = AcceptanceGate(evidence_ledger=mock_ledger)
        result = gate.evaluate("task-004", "feature/w")
        assert result.accepted is True


class TestAcceptanceGateSurgeonResolution:
    def test_unresolved_surgeon_blocks(self):
        """Surgeon still in operate phase blocks acceptance."""
        gate = AcceptanceGate()
        ctx = {"surgeon_state": {"phase": "operate", "disagreement_score": 0.5}}
        result = gate.evaluate("task-005", "feature/v", context=ctx)
        assert result.accepted is False
        assert any("surgeon" in r.lower() for r in result.blocking_reasons)

    def test_high_disagreement_blocks(self):
        """High surgeon disagreement blocks even if phase is closed."""
        gate = AcceptanceGate()
        ctx = {"surgeon_state": {"phase": "close", "disagreement_score": 0.9}}
        result = gate.evaluate("task-006", "feature/u", context=ctx)
        assert result.accepted is False


class TestAcceptanceGateMirrorDrift:
    def test_unacknowledged_drift_blocks(self):
        gate = AcceptanceGate()
        ctx = {"mirror_drift_detected": True, "mirror_drift_acknowledged": False}
        result = gate.evaluate("task-007", "feature/t", context=ctx)
        assert result.accepted is False

    def test_acknowledged_drift_passes(self):
        gate = AcceptanceGate()
        ctx = {"mirror_drift_detected": True, "mirror_drift_acknowledged": True}
        result = gate.evaluate("task-008", "feature/s", context=ctx)
        assert result.accepted is True
