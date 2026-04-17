"""Tests for 7-point invariance interlock between 3-Surgeons and Multi-Fleet.

Each wiring point is tested for:
  - Interlock fires when conditions are met
  - Graceful degradation when surgeon is unavailable
  - Existing behavior preserved when interlock doesn't apply

Integration tests verify end-to-end flows across multiple interlocks.
"""

import time
from unittest.mock import MagicMock, patch

import pytest

from multifleet.invariance_gates import (
    FleetMergeGate,
    FleetRepairGate,
    FleetSendGate,
    GateCheck,
    GateResult,
)
from multifleet.invariance_rules import ChannelHealthInvariant
from multifleet.quorum.quorum_registry import QuorumRegistry
from multifleet.quorum.stress_score import compute_stress_score

# Patch targets — SurgeonAdapter is lazily imported inside functions
# so we patch at the source module, not the importing module
_SURGEON_ADAPTER_PATCH = "multifleet.surgeon_adapter.SurgeonAdapter"


# ── Helpers ──


def _strategic_packet(**overrides):
    """Build a minimal strategic packet for testing."""
    pkt = {
        "from_node": "mac1",
        "to_node": "mac2",
        "type": "architecture_proposal",
        "id": "test-proposal-1",
        "signal_class": "strategic",
        "payload": {"subject": "test"},
        "meta": {"corroboration_count": 0},
    }
    pkt.update(overrides)
    return pkt


def _mock_surgeon_adapter(consensus=True, confidence=0.8, dissents=None,
                          phase="idle", disagreement=0.0,
                          review_status="ok", review_output="approved"):
    """Create a mock SurgeonAdapter with configurable behavior."""
    adapter = MagicMock()
    adapter.request_cross_exam.return_value = {
        "surgeons_consulted": ["atlas", "cardiologist"],
        "consensus": consensus,
        "dissents": dissents or [],
        "confidence": confidence,
        "output": "cross-exam result",
        "source": "test",
    }
    adapter.check_consensus.return_value = {
        "resolved": consensus,
        "confidence": confidence,
        "dissent": dissents or [],
        "source": "test",
        "cached": False,
    }
    adapter.get_state.return_value = {
        "phase": phase,
        "disagreement_score": disagreement,
        "correction_loops": 0,
        "abstention": False,
    }
    adapter.request_review.return_value = {
        "status": review_status,
        "output": review_output,
        "path": "test",
        "review_type": "cardio_review",
    }
    adapter.is_available.return_value = True
    return adapter


# ══════════════════════════════════════════════════════════════════
# INTERLOCK 1: FleetSendGate → Surgeon cross-exam for strategic signals
# ══════════════════════════════════════════════════════════════════


class TestInterlock1_SendGateSurgeonCrossExam:
    """FleetSendGate requests surgeon cross-exam for strategic signals without quorum."""

    def test_strategic_signal_surgeon_approves(self):
        """Surgeon cross-exam approves → strategic signal passes."""
        gate = FleetSendGate()
        pkt = _strategic_packet()
        adapter = _mock_surgeon_adapter(consensus=True, confidence=0.8)

        with patch(_SURGEON_ADAPTER_PATCH, return_value=adapter):
            result = gate.check(pkt)

        # Should have surgeon_cross_exam check that passed
        cross_exam_checks = [c for c in result.checks if c.name == "surgeon_cross_exam"]
        assert len(cross_exam_checks) == 1
        assert cross_exam_checks[0].passed is True

        # Strategic corroboration should pass (approved via surgeon)
        corr_checks = [c for c in result.checks if c.name == "strategic_corroboration"]
        assert len(corr_checks) == 1
        assert corr_checks[0].passed is True

    def test_strategic_signal_surgeon_rejects(self):
        """Surgeon cross-exam rejects → strategic signal fails."""
        gate = FleetSendGate()
        pkt = _strategic_packet()
        adapter = _mock_surgeon_adapter(consensus=False, confidence=0.2)

        with patch(_SURGEON_ADAPTER_PATCH, return_value=adapter):
            result = gate.check(pkt)

        cross_exam_checks = [c for c in result.checks if c.name == "surgeon_cross_exam"]
        assert len(cross_exam_checks) == 1
        assert cross_exam_checks[0].passed is False
        assert result.passed is False

    def test_strategic_signal_surgeon_unavailable_degrades(self):
        """Surgeon unavailable → graceful degradation, existing behavior preserved."""
        gate = FleetSendGate()
        pkt = _strategic_packet()

        with patch(_SURGEON_ADAPTER_PATCH, side_effect=ImportError("no bridge")):
            result = gate.check(pkt)

        # Should have degraded surgeon check (non-critical)
        cross_exam_checks = [c for c in result.checks if c.name == "surgeon_cross_exam"]
        assert len(cross_exam_checks) == 1
        assert cross_exam_checks[0].passed is True  # degraded = non-blocking
        assert cross_exam_checks[0].critical is False

        # But strategic corroboration still fails (no quorum, no surgeon)
        corr_checks = [c for c in result.checks if c.name == "strategic_corroboration"]
        assert len(corr_checks) == 1
        assert corr_checks[0].passed is False

    def test_tactical_signal_no_surgeon_check(self):
        """Tactical signals skip surgeon cross-exam entirely."""
        gate = FleetSendGate()
        pkt = _strategic_packet(signal_class="tactical")

        result = gate.check(pkt)

        cross_exam_checks = [c for c in result.checks if c.name == "surgeon_cross_exam"]
        assert len(cross_exam_checks) == 0

    def test_strategic_with_quorum_skips_surgeon(self):
        """Strategic signal with quorum in registry skips surgeon cross-exam."""
        gate = FleetSendGate()
        registry = QuorumRegistry(min_nodes=1)
        registry.record_support("test-proposal-1", "test", "mac1")
        registry.record_chief_confirmation("test-proposal-1")

        pkt = _strategic_packet()

        # Need to patch surgeon confidence check in has_quorum too
        with patch(_SURGEON_ADAPTER_PATCH) as MockAdapter:
            mock_adapter = _mock_surgeon_adapter(confidence=0.9)
            MockAdapter.return_value = mock_adapter
            result = gate.check(pkt, quorum_registry=registry)

        # Should pass via quorum, no surgeon cross-exam needed
        corr_checks = [c for c in result.checks if c.name == "strategic_corroboration"]
        assert len(corr_checks) == 1
        assert corr_checks[0].passed is True
        assert "quorum" in corr_checks[0].message.lower()


# ══════════════════════════════════════════════════════════════════
# INTERLOCK 2: FleetRepairGate → Surgeon review under critical stress
# ══════════════════════════════════════════════════════════════════


class TestInterlock2_RepairGateSurgeonReview:
    """FleetRepairGate requests surgeon review for destructive repairs under critical stress."""

    def test_destructive_repair_surgeon_approves(self):
        """Surgeon approves destructive repair → repair passes."""
        gate = FleetRepairGate()
        adapter = _mock_surgeon_adapter(review_output="I approve this repair action")

        with patch(_SURGEON_ADAPTER_PATCH, return_value=adapter):
            result = gate.check(
                "mac2", "full_restart",
                channel_health={"mac2": {"score": 10, "last_seen": time.time()}},
                node_id="mac1",
                stress_score=0.80,
            )

        review_checks = [c for c in result.checks if c.name == "surgeon_repair_review"]
        assert len(review_checks) == 1
        assert review_checks[0].passed is True

        policy_checks = [c for c in result.checks if c.name == "stress_repair_policy"]
        assert len(policy_checks) == 1
        assert policy_checks[0].passed is True

    def test_destructive_repair_surgeon_rejects(self):
        """Surgeon rejects destructive repair → repair blocked."""
        gate = FleetRepairGate()
        adapter = _mock_surgeon_adapter(review_output="This repair is too risky, deny it")

        with patch(_SURGEON_ADAPTER_PATCH, return_value=adapter):
            result = gate.check(
                "mac2", "full_restart",
                channel_health={"mac2": {"score": 10, "last_seen": time.time()}},
                node_id="mac1",
                stress_score=0.80,
            )

        # Surgeon review fails, stress_repair_policy also fails
        assert result.passed is False

    def test_destructive_repair_surgeon_unavailable(self):
        """Surgeon unavailable → falls through to existing chief-review-required block."""
        gate = FleetRepairGate()

        with patch(_SURGEON_ADAPTER_PATCH, side_effect=Exception("no bridge")):
            result = gate.check(
                "mac2", "full_restart",
                channel_health={"mac2": {"score": 10, "last_seen": time.time()}},
                node_id="mac1",
                stress_score=0.80,
            )

        # Surgeon review degraded (non-critical), but stress_repair_policy still blocks
        review_checks = [c for c in result.checks if c.name == "surgeon_repair_review"]
        assert len(review_checks) == 1
        assert review_checks[0].critical is False

    def test_non_destructive_repair_no_surgeon(self):
        """Non-destructive repairs skip surgeon review even under critical stress."""
        gate = FleetRepairGate()
        result = gate.check(
            "mac2", "reconnect_nats",
            channel_health={"mac2": {"score": 10, "last_seen": time.time()}},
            node_id="mac1",
            stress_score=0.80,
        )

        review_checks = [c for c in result.checks if c.name == "surgeon_repair_review"]
        assert len(review_checks) == 0

    def test_low_stress_no_surgeon_review(self):
        """Below critical stress threshold → no surgeon review for destructive repairs."""
        gate = FleetRepairGate()
        result = gate.check(
            "mac2", "full_restart",
            channel_health={"mac2": {"score": 10, "last_seen": time.time()}},
            node_id="mac1",
            stress_score=0.40,
        )

        review_checks = [c for c in result.checks if c.name == "surgeon_repair_review"]
        assert len(review_checks) == 0


# ══════════════════════════════════════════════════════════════════
# INTERLOCK 3: ChannelHealthInvariant → Surgeon corrigibility before L4
# ══════════════════════════════════════════════════════════════════


class TestInterlock3_ChannelHealthSurgeonCorrigibility:
    """ChannelHealthInvariant validates L4 repairs against surgeon corrigibility gate."""

    def test_l4_repair_surgeon_blocks(self):
        """Surgeon corrigibility gate blocks L4 repair → status = blocked_by_surgeon."""
        inv = ChannelHealthInvariant()
        inv._escalation_levels["mac2"] = 4
        violation = {
            "peer": "mac2",
            "score": "1/7",
            "up": 1,
            "total": 7,
            "broken": ["nats", "http"],
            "severity": "critical",
            "action": "repair",
            "escalation_level": 4,
        }

        adapter = _mock_surgeon_adapter(review_output="This violates a vision lock, block it")

        with patch(_SURGEON_ADAPTER_PATCH, return_value=adapter):
            result = inv.auto_repair(violation)

        assert result["status"] == "blocked_by_surgeon"
        assert "surgeon_reason" in result

    def test_l4_repair_surgeon_approves(self):
        """Surgeon approves L4 repair → repair proceeds normally."""
        inv = ChannelHealthInvariant()
        inv._escalation_levels["mac2"] = 4
        violation = {
            "peer": "mac2",
            "score": "1/7",
            "up": 1,
            "total": 7,
            "broken": ["nats", "http"],
            "severity": "critical",
            "action": "repair",
            "escalation_level": 4,
        }

        adapter = _mock_surgeon_adapter(review_output="Repair approved, proceed with caution")

        with patch(_SURGEON_ADAPTER_PATCH, return_value=adapter):
            result = inv.auto_repair(violation)

        assert result["status"] != "blocked_by_surgeon"
        assert result.get("surgeon_approved") is True

    def test_l4_repair_surgeon_unavailable(self):
        """Surgeon unavailable → L4 repair proceeds (graceful degradation)."""
        inv = ChannelHealthInvariant()
        inv._escalation_levels["mac2"] = 4
        violation = {
            "peer": "mac2",
            "score": "1/7",
            "up": 1,
            "total": 7,
            "broken": ["nats", "http"],
            "severity": "critical",
            "action": "repair",
            "escalation_level": 4,
        }

        with patch(_SURGEON_ADAPTER_PATCH, side_effect=ImportError("no bridge")):
            result = inv.auto_repair(violation)

        # Should not be blocked — degraded gracefully
        assert result["status"] != "blocked_by_surgeon"
        assert "surgeon_check_error" in result

    def test_l3_repair_no_surgeon_check(self):
        """L1-L3 repairs skip surgeon corrigibility check."""
        inv = ChannelHealthInvariant()
        violation = {
            "peer": "mac2",
            "score": "2/7",
            "up": 2,
            "total": 7,
            "broken": ["nats"],
            "severity": "high",
            "action": "repair",
            "escalation_level": 3,
        }

        result = inv.auto_repair(violation)

        assert "surgeon_reason" not in result
        assert "surgeon_approved" not in result


# ══════════════════════════════════════════════════════════════════
# INTERLOCK 4: Surgeon dissent → QuorumRegistry strategic proposals
# ══════════════════════════════════════════════════════════════════


class TestInterlock4_SurgeonDissentQuorum:
    """Surgeon dissent becomes a strategic proposal in QuorumRegistry."""

    def test_dissent_creates_proposal(self):
        """Recording surgeon dissent creates a strategic proposal."""
        registry = QuorumRegistry()
        dissent = {
            "surgeons_consulted": ["atlas", "cardiologist"],
            "consensus": False,
            "confidence": 0.3,
            "dissents": ["Architecture concern: shared state mutation"],
        }

        proposal = registry.record_surgeon_dissent("shared state design", dissent, "mac1")

        assert proposal.proposal_id.startswith("surgeon-dissent-")
        assert "SURGEON DISSENT" in proposal.topic
        assert "mac1" in proposal.supporting_nodes

    def test_dissent_proposal_needs_quorum(self):
        """Surgeon dissent proposal doesn't auto-reach quorum — needs fleet support."""
        registry = QuorumRegistry()
        dissent = {"confidence": 0.3, "dissents": ["concern"]}

        proposal = registry.record_surgeon_dissent("test topic", dissent, "mac1")

        with patch(_SURGEON_ADAPTER_PATCH) as MockAdapter:
            mock_adapter = _mock_surgeon_adapter(confidence=0.9)
            MockAdapter.return_value = mock_adapter
            assert registry.has_quorum(proposal.proposal_id) is False

    def test_dissent_reaches_quorum_with_support(self):
        """Surgeon dissent can reach quorum when other nodes support it."""
        registry = QuorumRegistry()
        dissent = {"confidence": 0.3, "dissents": ["concern"]}

        proposal = registry.record_surgeon_dissent("test topic", dissent, "mac1")
        registry.record_support(proposal.proposal_id, "test topic", "mac2")
        registry.record_chief_confirmation(proposal.proposal_id)

        with patch(_SURGEON_ADAPTER_PATCH) as MockAdapter:
            mock_adapter = _mock_surgeon_adapter(confidence=0.9)
            MockAdapter.return_value = mock_adapter
            assert registry.has_quorum(proposal.proposal_id) is True

    def test_dissent_appears_in_pending(self):
        """Surgeon dissent appears in pending proposals list."""
        registry = QuorumRegistry()
        dissent = {"confidence": 0.5, "dissents": ["issue"]}

        registry.record_surgeon_dissent("pending test", dissent, "mac1")

        with patch(_SURGEON_ADAPTER_PATCH) as MockAdapter:
            mock_adapter = _mock_surgeon_adapter(confidence=0.9)
            MockAdapter.return_value = mock_adapter
            pending = registry.pending_proposals()

        assert len(pending) == 1
        assert "SURGEON DISSENT" in pending[0]["topic"]


# ══════════════════════════════════════════════════════════════════
# INTERLOCK 5: Surgeon availability → stress score feedback
# ══════════════════════════════════════════════════════════════════


class TestInterlock5_SurgeonStressScore:
    """Surgeon availability affects fleet stress score."""

    def test_surgeon_available_no_penalty(self):
        """Surgeon available → no stress penalty."""
        score_with = compute_stress_score(surgeon_available=True)
        score_default = compute_stress_score()
        assert score_with == 0.0
        assert score_default == 0.0

    def test_surgeon_unavailable_adds_penalty(self):
        """Surgeon unavailable → +0.15 to stress score."""
        score = compute_stress_score(surgeon_available=False)
        assert score == pytest.approx(0.15, abs=0.001)

    def test_surgeon_unavailable_combines_with_other_stress(self):
        """Surgeon unavailability stacks with other stress factors."""
        score_with_surgeon = compute_stress_score(
            fallback_p3plus_count=3,
            surgeon_available=True,
        )
        score_without_surgeon = compute_stress_score(
            fallback_p3plus_count=3,
            surgeon_available=False,
        )
        assert score_without_surgeon - score_with_surgeon == pytest.approx(0.15, abs=0.001)

    def test_surgeon_unavailable_can_push_to_critical(self):
        """Surgeon unavailability can push stress from degraded to critical."""
        # Get to 0.65 with other factors (degraded level)
        base_score = compute_stress_score(
            fallback_p3plus_count=3,  # 0.30
            chief_sync_failures=2,   # 0.25 (capped)
            stale_peers=1,           # 0.10
        )
        assert base_score >= 0.50  # degraded

        critical_score = compute_stress_score(
            fallback_p3plus_count=3,
            chief_sync_failures=2,
            stale_peers=1,
            surgeon_available=False,  # +0.15
        )
        assert critical_score >= 0.75  # critical


# ══════════════════════════════════════════════════════════════════
# INTERLOCK 6: CardioGate → FleetMergeGate signal
# ══════════════════════════════════════════════════════════════════


class TestInterlock6_MergeGateSurgeonState:
    """FleetMergeGate checks surgeon state before allowing merge."""

    def test_surgeon_triage_blocks_merge(self):
        """Surgeon in triage phase → merge blocked."""
        gate = FleetMergeGate()
        adapter = _mock_surgeon_adapter(phase="triage", disagreement=0.0)

        with patch(_SURGEON_ADAPTER_PATCH, return_value=adapter):
            result = gate.check("feature-branch")

        surgeon_checks = [c for c in result.checks if c.name == "surgeon_state"]
        assert len(surgeon_checks) == 1
        assert surgeon_checks[0].passed is False
        assert "triage" in surgeon_checks[0].message

    def test_high_disagreement_blocks_merge(self):
        """Surgeon disagreement > 0.3 → merge blocked."""
        gate = FleetMergeGate()
        adapter = _mock_surgeon_adapter(phase="review", disagreement=0.5)

        with patch(_SURGEON_ADAPTER_PATCH, return_value=adapter):
            result = gate.check("feature-branch")

        surgeon_checks = [c for c in result.checks if c.name == "surgeon_state"]
        assert len(surgeon_checks) == 1
        assert surgeon_checks[0].passed is False
        assert "disagreement" in surgeon_checks[0].message

    def test_surgeon_idle_allows_merge(self):
        """Surgeon idle with low disagreement → merge allowed."""
        gate = FleetMergeGate()
        adapter = _mock_surgeon_adapter(phase="idle", disagreement=0.1)

        with patch(_SURGEON_ADAPTER_PATCH, return_value=adapter):
            result = gate.check("feature-branch")

        surgeon_checks = [c for c in result.checks if c.name == "surgeon_state"]
        assert len(surgeon_checks) == 1
        assert surgeon_checks[0].passed is True

    def test_surgeon_unavailable_degrades(self):
        """Surgeon unavailable → merge gate degrades gracefully."""
        gate = FleetMergeGate()

        with patch(_SURGEON_ADAPTER_PATCH, side_effect=Exception("no bridge")):
            result = gate.check("feature-branch")

        surgeon_checks = [c for c in result.checks if c.name == "surgeon_state"]
        assert len(surgeon_checks) == 1
        assert surgeon_checks[0].passed is True
        assert surgeon_checks[0].critical is False


# ══════════════════════════════════════════════════════════════════
# INTERLOCK 7: QuorumRegistry.has_quorum() requires surgeon confidence
# ══════════════════════════════════════════════════════════════════


class TestInterlock7_QuorumSurgeonConfidence:
    """QuorumRegistry requires surgeon confidence >= 0.6 for quorum."""

    def test_high_confidence_quorum_passes(self):
        """Surgeon confidence >= 0.6 → quorum valid."""
        registry = QuorumRegistry(min_nodes=2)
        registry.record_support("prop-1", "test", "mac1")
        registry.record_support("prop-1", "test", "mac2")
        registry.record_chief_confirmation("prop-1")

        with patch(_SURGEON_ADAPTER_PATCH) as MockAdapter:
            mock_adapter = _mock_surgeon_adapter(confidence=0.8)
            MockAdapter.return_value = mock_adapter
            assert registry.has_quorum("prop-1") is True

    def test_low_confidence_quorum_revoked(self):
        """Surgeon confidence < 0.6 → quorum provisional (revoked)."""
        registry = QuorumRegistry(min_nodes=2)
        registry.record_support("prop-1", "test", "mac1")
        registry.record_support("prop-1", "test", "mac2")
        registry.record_chief_confirmation("prop-1")

        with patch(_SURGEON_ADAPTER_PATCH) as MockAdapter:
            mock_adapter = _mock_surgeon_adapter(confidence=0.3)
            MockAdapter.return_value = mock_adapter
            assert registry.has_quorum("prop-1") is False

    def test_surgeon_unavailable_quorum_stands(self):
        """Surgeon unavailable → quorum stands on node support + authority."""
        registry = QuorumRegistry(min_nodes=2)
        registry.record_support("prop-1", "test", "mac1")
        registry.record_support("prop-1", "test", "mac2")
        registry.record_chief_confirmation("prop-1")

        with patch(_SURGEON_ADAPTER_PATCH, side_effect=ImportError("no bridge")):
            assert registry.has_quorum("prop-1") is True

    def test_quenched_proposal_still_blocked(self):
        """Quenched proposals remain blocked regardless of surgeon confidence."""
        registry = QuorumRegistry(min_nodes=2)
        registry.record_support("prop-1", "test", "mac1")
        registry.record_support("prop-1", "test", "mac2")
        registry.record_chief_confirmation("prop-1")
        registry.quench("prop-1", "evidence contradicts")

        with patch(_SURGEON_ADAPTER_PATCH) as MockAdapter:
            mock_adapter = _mock_surgeon_adapter(confidence=0.9)
            MockAdapter.return_value = mock_adapter
            assert registry.has_quorum("prop-1") is False


# ══════════════════════════════════════════════════════════════════
# INTEGRATION TESTS — cross-interlock flows
# ══════════════════════════════════════════════════════════════════


class TestIntegration_StrategicSignalFlow:
    """Strategic signal → surgeon cross-exam → quorum check → send/block."""

    def test_strategic_signal_full_approval_flow(self):
        """Strategic signal approved by surgeon and quorum → send allowed."""
        gate = FleetSendGate()
        registry = QuorumRegistry(min_nodes=1)
        registry.record_support("test-proposal-1", "test", "mac1")
        registry.record_chief_confirmation("test-proposal-1")
        pkt = _strategic_packet()

        with patch(_SURGEON_ADAPTER_PATCH) as MockAdapter:
            mock_adapter = _mock_surgeon_adapter(confidence=0.9)
            MockAdapter.return_value = mock_adapter
            result = gate.check(pkt, quorum_registry=registry)

        assert result.passed is True

    def test_strategic_signal_surgeon_block_flow(self):
        """Strategic signal with no quorum + surgeon reject → send blocked."""
        gate = FleetSendGate()
        pkt = _strategic_packet()
        adapter = _mock_surgeon_adapter(consensus=False, confidence=0.1)

        with patch(_SURGEON_ADAPTER_PATCH, return_value=adapter):
            result = gate.check(pkt)

        assert result.passed is False
        # Both surgeon_cross_exam and strategic_corroboration should fail
        failed_names = {c.name for c in result.checks if not c.passed and c.critical}
        assert "surgeon_cross_exam" in failed_names
        assert "strategic_corroboration" in failed_names


class TestIntegration_CriticalStressRepairFlow:
    """Critical stress → surgeon review → repair allowed/blocked."""

    def test_critical_stress_surgeon_approved_repair(self):
        """Under critical stress, surgeon-approved destructive repair proceeds."""
        gate = FleetRepairGate()
        adapter = _mock_surgeon_adapter(review_output="proceed with the repair, approved")

        with patch(_SURGEON_ADAPTER_PATCH, return_value=adapter):
            result = gate.check(
                "mac2", "full_restart",
                channel_health={"mac2": {"score": 5, "last_seen": time.time()}},
                node_id="mac1",
                stress_score=0.85,
            )

        policy_checks = [c for c in result.checks if c.name == "stress_repair_policy"]
        assert len(policy_checks) == 1
        assert policy_checks[0].passed is True


class TestIntegration_SurgeonDissentPropagation:
    """Surgeon dissent → quorum proposal → fleet-wide propagation."""

    def test_dissent_propagates_through_quorum(self):
        """Surgeon dissent recorded as proposal, visible to fleet, reaches quorum with support."""
        registry = QuorumRegistry()
        dissent = {
            "surgeons_consulted": ["atlas", "cardiologist", "neurologist"],
            "consensus": False,
            "confidence": 0.2,
            "dissents": ["Memory bridge design has race condition"],
        }

        # mac1 raises dissent
        proposal = registry.record_surgeon_dissent("memory bridge race", dissent, "mac1")

        # mac2 agrees
        registry.record_support(proposal.proposal_id, proposal.topic, "mac2")

        # Chief confirms the concern
        registry.record_chief_confirmation(proposal.proposal_id)

        # With high surgeon confidence, quorum is reached
        with patch(_SURGEON_ADAPTER_PATCH) as MockAdapter:
            mock_adapter = _mock_surgeon_adapter(confidence=0.9)
            MockAdapter.return_value = mock_adapter
            assert registry.has_quorum(proposal.proposal_id) is True


class TestIntegration_MergeBlockedBySurgeonDisagreement:
    """Merge blocked when surgeon disagreement is high."""

    def test_merge_blocked_full_flow(self):
        """High disagreement from recent cross-exam → merge gate blocks."""
        gate = FleetMergeGate()

        # Surgeon in review phase with high disagreement
        adapter = _mock_surgeon_adapter(phase="review", disagreement=0.7)

        with patch(_SURGEON_ADAPTER_PATCH, return_value=adapter):
            result = gate.check("feature-branch")

        assert result.passed is False
        surgeon_checks = [c for c in result.checks if c.name == "surgeon_state"]
        assert surgeon_checks[0].passed is False
        assert "disagreement" in surgeon_checks[0].message


# ══════════════════════════════════════════════════════════════════
# INTERLOCK WIRING: Cross-system connection points
# ══════════════════════════════════════════════════════════════════


class TestWiring_DissentFlowsToQuorum:
    """INTERLOCK 1→4: Surgeon dissent in FleetSendGate registers in QuorumRegistry."""

    def test_dissent_registered_in_quorum_registry(self):
        """When surgeon rejects a strategic signal, dissent flows to quorum registry."""
        gate = FleetSendGate()
        registry = QuorumRegistry()
        pkt = _strategic_packet()
        adapter = _mock_surgeon_adapter(
            consensus=False, confidence=0.2,
            dissents=["Architecture concern: shared state mutation"],
        )

        with patch(_SURGEON_ADAPTER_PATCH, return_value=adapter):
            result = gate.check(pkt, quorum_registry=registry)

        assert result.passed is False

        # Dissent should have been registered as a proposal
        pending = []
        with patch(_SURGEON_ADAPTER_PATCH) as MockAdapter:
            mock = _mock_surgeon_adapter(confidence=0.9)
            MockAdapter.return_value = mock
            pending = registry.pending_proposals()

        assert len(pending) == 1
        assert "SURGEON DISSENT" in pending[0]["topic"]
        assert "mac1" in pending[0]["supporting_nodes"]

    def test_dissent_not_registered_without_registry(self):
        """Without quorum_registry, dissent is not registered (no crash)."""
        gate = FleetSendGate()
        pkt = _strategic_packet()
        adapter = _mock_surgeon_adapter(
            consensus=False, confidence=0.2,
            dissents=["concern"],
        )

        with patch(_SURGEON_ADAPTER_PATCH, return_value=adapter):
            result = gate.check(pkt, quorum_registry=None)

        assert result.passed is False
        # No crash, just no registration

    def test_approval_does_not_register_dissent(self):
        """When surgeon approves, no dissent is registered in quorum registry."""
        gate = FleetSendGate()
        registry = QuorumRegistry()
        pkt = _strategic_packet()
        adapter = _mock_surgeon_adapter(consensus=True, confidence=0.8)

        with patch(_SURGEON_ADAPTER_PATCH, return_value=adapter):
            result = gate.check(pkt, quorum_registry=registry)

        with patch(_SURGEON_ADAPTER_PATCH) as MockAdapter:
            mock = _mock_surgeon_adapter(confidence=0.9)
            MockAdapter.return_value = mock
            pending = registry.pending_proposals()

        assert len(pending) == 0


class TestWiring_CriticalStressSurgeonRisk:
    """INTERLOCK 5→1: Critical stress triggers surgeon risk assessment in FleetSendGate."""

    def test_critical_stress_triggers_risk_assessment(self):
        """Critical stress score triggers surgeon risk assessment check."""
        gate = FleetSendGate()
        pkt = _strategic_packet(signal_class="tactical")
        adapter = _mock_surgeon_adapter(consensus=True, confidence=0.7)
        adapter.is_available.return_value = True

        with patch(_SURGEON_ADAPTER_PATCH, return_value=adapter):
            result = gate.check(pkt, stress_score=0.80)

        risk_checks = [c for c in result.checks if c.name == "stress_surgeon_risk"]
        assert len(risk_checks) == 1

    def test_non_critical_stress_no_risk_assessment(self):
        """Below critical stress, no surgeon risk assessment."""
        gate = FleetSendGate()
        pkt = _strategic_packet(signal_class="tactical")

        result = gate.check(pkt, stress_score=0.40)

        risk_checks = [c for c in result.checks if c.name == "stress_surgeon_risk"]
        assert len(risk_checks) == 0

    def test_critical_stress_surgeon_unavailable_degrades(self):
        """Surgeon unavailable under critical stress degrades gracefully."""
        gate = FleetSendGate()
        pkt = _strategic_packet(signal_class="tactical")

        with patch(_SURGEON_ADAPTER_PATCH, side_effect=ImportError("no bridge")):
            result = gate.check(pkt, stress_score=0.80)

        # Should not crash; risk check absent (degraded)
        risk_checks = [c for c in result.checks if c.name == "stress_surgeon_risk"]
        assert len(risk_checks) == 0


class TestWiring_StressEventEmission:
    """INTERLOCK 5: Stress level transitions emit events for surgeon protocol."""

    def test_transition_to_critical_emits_event(self):
        """Crossing into critical emits event with requires_surgeon=True."""
        from multifleet.quorum.stress_score import emit_stress_event
        event = emit_stress_event(0.60, 0.80, node_id="mac1")

        assert event is not None
        assert event["from_level"] == "degraded"
        assert event["to_level"] == "critical"
        assert event["requires_surgeon"] is True
        assert event["node_id"] == "mac1"

    def test_same_level_no_event(self):
        """No event when stress stays within the same level."""
        from multifleet.quorum.stress_score import emit_stress_event
        event = emit_stress_event(0.30, 0.40, node_id="mac1")

        assert event is None

    def test_transition_from_critical_emits_event(self):
        """Leaving critical also emits event (requires_surgeon=False)."""
        from multifleet.quorum.stress_score import emit_stress_event
        event = emit_stress_event(0.80, 0.50, node_id="mac2")

        assert event is not None
        assert event["from_level"] == "critical"
        assert event["to_level"] == "degraded"
        assert event["requires_surgeon"] is False

    def test_normal_to_guarded_no_surgeon(self):
        """Non-critical transitions don't require surgeon."""
        from multifleet.quorum.stress_score import emit_stress_event
        event = emit_stress_event(0.10, 0.30, node_id="mac3")

        assert event is not None
        assert event["requires_surgeon"] is False
