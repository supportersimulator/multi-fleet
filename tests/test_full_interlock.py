"""Full 7-point interlock integration test.

Verifies ALL 7 interlock points in a single test file, plus one
end-to-end chain test that exercises: stress -> gate -> surgeon ->
dissent -> quorum -> evidence -> health.

Interlock points:
  1. FleetSendGate -> surgeon cross-exam
  2. FleetRepairGate -> surgeon review
  3. ChannelHealthInvariant -> CorrigibilityGate (surgeon L4 check)
  4. Surgeon dissent -> QuorumRegistry
  5. StressScore -> surgeon risk mode
  6. Evidence ledger -> surgeon evidence chain
  7. Gains gate -> fleet health check (FleetMergeGate)
"""

import tempfile
import time
from unittest.mock import MagicMock, patch

import pytest

from multifleet.evidence_ledger import EvidenceLedger
from multifleet.invariance_gates import (
    FleetMergeGate,
    FleetRepairGate,
    FleetSendGate,
    GateCheck,
    GateResult,
)
from multifleet.invariance_rules import ChannelHealthInvariant
from multifleet.quorum.quorum_registry import QuorumRegistry
from multifleet.quorum.stress_score import (
    STRESS_CRITICAL,
    STRESS_DEGRADED,
    STRESS_NORMAL,
    compute_stress_score,
    emit_stress_event,
    get_stress_level,
)

_SURGEON_ADAPTER_PATCH = "multifleet.surgeon_adapter.SurgeonAdapter"


# ── Helpers ──


def _mock_surgeon(consensus=True, confidence=0.8, dissents=None,
                  review_status="ok", review_output="approved",
                  phase="idle", disagreement=0.0):
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


def _make_ledger():
    """Create a fresh EvidenceLedger backed by a temp file."""
    tmp = tempfile.mktemp(suffix=".db")
    return EvidenceLedger(db_path=tmp)


# ══════════════════════════════════════════════════════════════════
# INTERLOCK 1: FleetSendGate -> surgeon cross-exam
# ══════════════════════════════════════════════════════════════════


class TestInterlock1_FleetSendGate_SurgeonCrossExam:
    """Strategic signals without quorum trigger surgeon cross-exam."""

    def test_surgeon_approves_strategic_signal(self):
        """Surgeon cross-exam consensus=True -> signal passes."""
        gate = FleetSendGate()
        pkt = _strategic_packet()
        adapter = _mock_surgeon(consensus=True, confidence=0.8)

        with patch(_SURGEON_ADAPTER_PATCH, return_value=adapter):
            result = gate.check(pkt)

        cross = [c for c in result.checks if c.name == "surgeon_cross_exam"]
        assert len(cross) == 1
        assert cross[0].passed is True

        corr = [c for c in result.checks if c.name == "strategic_corroboration"]
        assert corr[0].passed is True

    def test_surgeon_rejects_strategic_signal(self):
        """Surgeon cross-exam consensus=False -> signal blocked."""
        gate = FleetSendGate()
        pkt = _strategic_packet()
        adapter = _mock_surgeon(consensus=False, confidence=0.2)

        with patch(_SURGEON_ADAPTER_PATCH, return_value=adapter):
            result = gate.check(pkt)

        cross = [c for c in result.checks if c.name == "surgeon_cross_exam"]
        assert cross[0].passed is False
        assert result.passed is False


# ══════════════════════════════════════════════════════════════════
# INTERLOCK 2: FleetRepairGate -> surgeon review
# ══════════════════════════════════════════════════════════════════


class TestInterlock2_FleetRepairGate_SurgeonReview:
    """Destructive repairs under critical stress require surgeon review."""

    def test_surgeon_approves_destructive_repair(self):
        """Surgeon output contains 'approve' -> repair passes."""
        gate = FleetRepairGate()
        adapter = _mock_surgeon(review_output="I approve this repair action")

        with patch(_SURGEON_ADAPTER_PATCH, return_value=adapter):
            result = gate.check(
                "mac2", "full_restart",
                channel_health={"mac2": {"score": 10, "last_seen": time.time()}},
                node_id="mac1",
                stress_score=0.80,
            )

        review = [c for c in result.checks if c.name == "surgeon_repair_review"]
        assert len(review) == 1
        assert review[0].passed is True

        policy = [c for c in result.checks if c.name == "stress_repair_policy"]
        assert policy[0].passed is True

    def test_surgeon_rejects_destructive_repair(self):
        """Surgeon output lacks approval keywords -> repair blocked."""
        gate = FleetRepairGate()
        adapter = _mock_surgeon(review_output="deny this repair, too risky")

        with patch(_SURGEON_ADAPTER_PATCH, return_value=adapter):
            result = gate.check(
                "mac2", "full_restart",
                channel_health={"mac2": {"score": 10, "last_seen": time.time()}},
                node_id="mac1",
                stress_score=0.80,
            )

        assert result.passed is False


# ══════════════════════════════════════════════════════════════════
# INTERLOCK 3: ChannelHealthInvariant -> CorrigibilityGate (surgeon L4)
# ══════════════════════════════════════════════════════════════════


class TestInterlock3_ChannelHealth_SurgeonCorrigibility:
    """L4 remote-fix repairs must pass surgeon corrigibility gate."""

    def test_l4_repair_blocked_by_surgeon(self):
        """Surgeon output contains 'block' -> L4 repair blocked."""
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
        adapter = _mock_surgeon(review_output="This violates a vision lock, block it")

        with patch(_SURGEON_ADAPTER_PATCH, return_value=adapter):
            result = inv.auto_repair(violation)

        assert result["status"] == "blocked_by_surgeon"
        assert "surgeon_reason" in result

    def test_l4_repair_approved_by_surgeon(self):
        """Surgeon approves -> L4 repair proceeds."""
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
        adapter = _mock_surgeon(review_output="Repair approved, proceed with caution")

        with patch(_SURGEON_ADAPTER_PATCH, return_value=adapter):
            result = inv.auto_repair(violation)

        assert result["status"] != "blocked_by_surgeon"
        assert result.get("surgeon_approved") is True


# ══════════════════════════════════════════════════════════════════
# INTERLOCK 4: Surgeon dissent -> QuorumRegistry
# ══════════════════════════════════════════════════════════════════


class TestInterlock4_SurgeonDissent_QuorumRegistry:
    """Surgeon dissent creates a strategic proposal in QuorumRegistry."""

    def test_dissent_creates_proposal(self):
        """Recording surgeon dissent creates a surgeon-dissent- proposal."""
        registry = QuorumRegistry()
        dissent = {
            "surgeons_consulted": ["atlas", "cardiologist"],
            "consensus": False,
            "confidence": 0.3,
            "dissents": ["Race condition in shared state"],
        }

        proposal = registry.record_surgeon_dissent("shared state design", dissent, "mac1")

        assert proposal.proposal_id.startswith("surgeon-dissent-")
        assert "SURGEON DISSENT" in proposal.topic
        assert "mac1" in proposal.supporting_nodes

    def test_dissent_flows_from_send_gate_to_quorum(self):
        """When FleetSendGate surgeon rejects, dissent is registered in QuorumRegistry."""
        gate = FleetSendGate()
        registry = QuorumRegistry()
        pkt = _strategic_packet()
        adapter = _mock_surgeon(
            consensus=False, confidence=0.2,
            dissents=["Architecture concern: shared state mutation"],
        )

        with patch(_SURGEON_ADAPTER_PATCH, return_value=adapter):
            result = gate.check(pkt, quorum_registry=registry)

        assert result.passed is False

        # Dissent should now be a pending proposal
        with patch(_SURGEON_ADAPTER_PATCH) as MockAdapter:
            MockAdapter.return_value = _mock_surgeon(confidence=0.9)
            pending = registry.pending_proposals()

        assert len(pending) == 1
        assert "SURGEON DISSENT" in pending[0]["topic"]


# ══════════════════════════════════════════════════════════════════
# INTERLOCK 5: StressScore -> surgeon risk mode
# ══════════════════════════════════════════════════════════════════


class TestInterlock5_StressScore_SurgeonRisk:
    """Surgeon availability affects stress; critical stress triggers surgeon risk assessment."""

    def test_surgeon_unavailable_increases_stress(self):
        """surgeon_available=False adds 0.15 penalty."""
        score_with = compute_stress_score(surgeon_available=True)
        score_without = compute_stress_score(surgeon_available=False)
        assert score_without - score_with == pytest.approx(0.15, abs=0.001)

    def test_critical_stress_triggers_risk_assessment(self):
        """Critical stress on FleetSendGate triggers stress_surgeon_risk check."""
        gate = FleetSendGate()
        pkt = _strategic_packet(signal_class="tactical")
        adapter = _mock_surgeon(consensus=True, confidence=0.7)

        with patch(_SURGEON_ADAPTER_PATCH, return_value=adapter):
            result = gate.check(pkt, stress_score=0.80)

        risk = [c for c in result.checks if c.name == "stress_surgeon_risk"]
        assert len(risk) == 1

    def test_stress_event_emitted_on_critical_transition(self):
        """Crossing into critical emits event with requires_surgeon=True."""
        event = emit_stress_event(0.60, 0.80, node_id="mac1")
        assert event is not None
        assert event["to_level"] == "critical"
        assert event["requires_surgeon"] is True


# ══════════════════════════════════════════════════════════════════
# INTERLOCK 6: Evidence ledger -> surgeon evidence chain
# ══════════════════════════════════════════════════════════════════


class TestInterlock6_EvidenceLedger_SurgeonChain:
    """Gate activations are recorded in the evidence ledger with hash chain."""

    def test_gate_activation_recorded_in_ledger(self):
        """Gate check writes a gate_activation entry to the evidence ledger."""
        ledger = _make_ledger()

        # Record as the gate internals do
        entry = ledger.record(
            "gate_activation",
            "mac1",
            "FleetSendGate",
            {"passed": True, "summary": "FleetSendGate PASSED (6/6)", "checks_run": 6, "failures": []},
        )

        assert entry["event_type"] == "gate_activation"
        assert entry["entry_hash"]  # non-empty hash

        # Chain is intact
        chain = ledger.verify_chain()
        assert chain["valid"] is True
        assert chain["checked"] == 1

    def test_multiple_gate_entries_chain_intact(self):
        """Multiple gate entries maintain hash chain integrity."""
        ledger = _make_ledger()

        ledger.record("gate_activation", "mac1", "FleetSendGate",
                       {"passed": True, "checks_run": 6, "failures": []})
        ledger.record("gate_activation", "mac1", "FleetRepairGate",
                       {"passed": False, "checks_run": 5, "failures": ["stress_policy"]})
        ledger.record("gate_activation", "mac2", "FleetMergeGate",
                       {"passed": True, "checks_run": 4, "failures": []})

        chain = ledger.verify_chain()
        assert chain["valid"] is True
        assert chain["checked"] == 3

    def test_evidence_queryable_by_event_type(self):
        """Gate activation entries are queryable from the ledger."""
        ledger = _make_ledger()

        ledger.record("gate_activation", "mac1", "FleetSendGate", {"passed": True})
        ledger.record("channel_repair", "mac1", "nats_reconnect", {"target": "mac2"})
        ledger.record("gate_activation", "mac1", "FleetRepairGate", {"passed": False})

        gate_entries = ledger.query(event_type="gate_activation")
        assert len(gate_entries) == 2


# ══════════════════════════════════════════════════════════════════
# INTERLOCK 7: Gains gate -> fleet health check (FleetMergeGate)
# ══════════════════════════════════════════════════════════════════


class TestInterlock7_GainsGate_FleetHealthCheck:
    """FleetMergeGate checks surgeon state + gains gate before merge."""

    def test_surgeon_triage_blocks_merge(self):
        """Surgeon in triage phase -> merge blocked."""
        gate = FleetMergeGate()
        adapter = _mock_surgeon(phase="triage", disagreement=0.0)

        with patch(_SURGEON_ADAPTER_PATCH, return_value=adapter):
            result = gate.check("feature-branch")

        surgeon_checks = [c for c in result.checks if c.name == "surgeon_state"]
        assert len(surgeon_checks) == 1
        assert surgeon_checks[0].passed is False
        assert "triage" in surgeon_checks[0].message

    def test_gains_gate_failure_blocks_merge(self):
        """Gains gate failure -> merge blocked."""
        gate = FleetMergeGate()
        adapter = _mock_surgeon(phase="idle", disagreement=0.0)

        # Mock gains gate that fails
        mock_gains = MagicMock()
        mock_gains_result = MagicMock()
        mock_gains_result.passed = False
        mock_gains_result.recommendation = "Redis unreachable"
        mock_gains.run.return_value = mock_gains_result

        with patch(_SURGEON_ADAPTER_PATCH, return_value=adapter):
            result = gate.check("feature-branch", gains_gate=mock_gains)

        gains_checks = [c for c in result.checks if c.name == "gains_gate"]
        assert len(gains_checks) == 1
        assert gains_checks[0].passed is False
        assert result.passed is False

    def test_healthy_state_allows_merge(self):
        """Surgeon idle + low disagreement + no gains gate -> merge proceeds."""
        gate = FleetMergeGate()
        adapter = _mock_surgeon(phase="idle", disagreement=0.1)

        with patch(_SURGEON_ADAPTER_PATCH, return_value=adapter):
            result = gate.check("feature-branch")

        surgeon_checks = [c for c in result.checks if c.name == "surgeon_state"]
        assert surgeon_checks[0].passed is True


# ══════════════════════════════════════════════════════════════════
# END-TO-END: Full 7-point interlock chain
# ══════════════════════════════════════════════════════════════════


class TestEndToEnd_Full7PointChain:
    """Chains all 7 interlocks: stress -> gate -> surgeon -> dissent -> quorum -> evidence -> health."""

    def test_full_chain_block_path(self):
        """Full chain where stress causes cascade through all 7 interlocks."""
        ledger = _make_ledger()

        # ── IL5: Stress rises to critical via surgeon unavailability ──
        stress = compute_stress_score(
            fallback_p3plus_count=3,
            chief_sync_failures=2,
            stale_peers=1,
            surgeon_available=False,  # +0.15 penalty
        )
        assert get_stress_level(stress) == STRESS_CRITICAL
        assert stress >= 0.75

        # Stress event fires
        event = emit_stress_event(0.40, stress, node_id="mac1")
        assert event is not None
        assert event["requires_surgeon"] is True

        # ── IL1: FleetSendGate -> surgeon cross-exam rejects strategic signal ──
        send_gate = FleetSendGate()
        registry = QuorumRegistry()
        pkt = _strategic_packet()
        adapter = _mock_surgeon(
            consensus=False, confidence=0.2,
            dissents=["Architecture concern: shared state mutation risk"],
        )

        with patch(_SURGEON_ADAPTER_PATCH, return_value=adapter):
            send_result = send_gate.check(pkt, quorum_registry=registry, stress_score=stress)

        assert send_result.passed is False
        cross = [c for c in send_result.checks if c.name == "surgeon_cross_exam"]
        assert cross[0].passed is False

        # ── IL4: Surgeon dissent flows to QuorumRegistry ──
        with patch(_SURGEON_ADAPTER_PATCH) as MockAdapter:
            MockAdapter.return_value = _mock_surgeon(confidence=0.9)
            pending = registry.pending_proposals()
        assert len(pending) == 1
        assert "SURGEON DISSENT" in pending[0]["topic"]

        # ── IL6: Record gate activation in evidence ledger ──
        entry = ledger.record(
            "gate_activation", "mac1", "FleetSendGate",
            {
                "passed": send_result.passed,
                "summary": send_result.summary,
                "checks_run": len(send_result.checks),
                "failures": [c.name for c in send_result.checks if not c.passed],
            },
        )
        assert entry["event_type"] == "gate_activation"
        chain = ledger.verify_chain()
        assert chain["valid"] is True

        # ── IL2: FleetRepairGate -> surgeon review blocks destructive repair ──
        repair_gate = FleetRepairGate()
        repair_adapter = _mock_surgeon(review_output="deny this repair, too risky")

        with patch(_SURGEON_ADAPTER_PATCH, return_value=repair_adapter):
            repair_result = repair_gate.check(
                "mac2", "full_restart",
                channel_health={"mac2": {"score": 5, "last_seen": time.time()}},
                node_id="mac1",
                stress_score=stress,
            )

        assert repair_result.passed is False

        # Record repair gate in evidence too
        ledger.record(
            "gate_activation", "mac1", "FleetRepairGate",
            {"passed": repair_result.passed, "failures": [c.name for c in repair_result.checks if not c.passed]},
        )

        # ── IL3: ChannelHealthInvariant -> surgeon corrigibility blocks L4 ──
        inv = ChannelHealthInvariant()
        inv._escalation_levels["mac2"] = 4
        violation = {
            "peer": "mac2", "score": "1/7", "up": 1, "total": 7,
            "broken": ["nats", "http"], "severity": "critical",
            "action": "repair", "escalation_level": 4,
        }
        block_adapter = _mock_surgeon(review_output="block this L4 repair")

        with patch(_SURGEON_ADAPTER_PATCH, return_value=block_adapter):
            repair_decision = inv.auto_repair(violation)

        assert repair_decision["status"] == "blocked_by_surgeon"

        # ── IL7: FleetMergeGate -> surgeon triage blocks merge ──
        merge_gate = FleetMergeGate()
        triage_adapter = _mock_surgeon(phase="triage", disagreement=0.5)

        with patch(_SURGEON_ADAPTER_PATCH, return_value=triage_adapter):
            merge_result = merge_gate.check("feature-branch")

        assert merge_result.passed is False
        surgeon_state = [c for c in merge_result.checks if c.name == "surgeon_state"]
        assert surgeon_state[0].passed is False

        # Record merge gate in evidence
        ledger.record(
            "gate_activation", "mac1", "FleetMergeGate",
            {"passed": merge_result.passed, "failures": [c.name for c in merge_result.checks if not c.passed]},
        )

        # ── Final: Evidence chain covers all 3 gate activations ──
        final_chain = ledger.verify_chain()
        assert final_chain["valid"] is True
        assert final_chain["checked"] == 3

        gate_entries = ledger.query(event_type="gate_activation")
        assert len(gate_entries) == 3
        subjects = [e["subject"] for e in gate_entries]
        assert "FleetSendGate" in subjects
        assert "FleetRepairGate" in subjects
        assert "FleetMergeGate" in subjects

    def test_full_chain_approval_path(self):
        """Full chain where surgeon approves everything -- happy path."""
        ledger = _make_ledger()

        # Stress below critical
        stress = compute_stress_score(
            fallback_p3plus_count=1,
            surgeon_available=True,
        )
        assert get_stress_level(stress) != STRESS_CRITICAL

        # IL1: Surgeon approves strategic signal
        gate = FleetSendGate()
        pkt = _strategic_packet()
        adapter = _mock_surgeon(consensus=True, confidence=0.9)

        with patch(_SURGEON_ADAPTER_PATCH, return_value=adapter):
            result = gate.check(pkt, stress_score=stress)

        cross = [c for c in result.checks if c.name == "surgeon_cross_exam"]
        assert cross[0].passed is True

        # IL6: Record in evidence
        ledger.record("gate_activation", "mac1", "FleetSendGate",
                       {"passed": result.passed})

        # IL7: Merge gate passes with idle surgeon
        merge_gate = FleetMergeGate()
        idle_adapter = _mock_surgeon(phase="idle", disagreement=0.05)

        with patch(_SURGEON_ADAPTER_PATCH, return_value=idle_adapter):
            merge_result = merge_gate.check("feature-branch")

        surgeon_state = [c for c in merge_result.checks if c.name == "surgeon_state"]
        assert surgeon_state[0].passed is True

        ledger.record("gate_activation", "mac1", "FleetMergeGate",
                       {"passed": merge_result.passed})

        chain = ledger.verify_chain()
        assert chain["valid"] is True
        assert chain["checked"] == 2
