"""Tests for AcceptanceGate — corrigibility-as-code."""

from multifleet.acceptance_gate import AcceptanceGate, AcceptanceDecision
from multifleet.probe_engine import ProbeResult, ProbeFinding


class TestAcceptanceGate:

    def test_all_clear_accepts(self):
        gate = AcceptanceGate()
        decision = gate.evaluate(
            probe_results=[],
            surgeon_resolved=True,
            invariants_satisfied=True,
            evidence_count=1,
        )
        assert decision.accepted is True
        assert len(decision.blocking_issues) == 0

    def test_blocking_probe_rejects(self):
        gate = AcceptanceGate()
        result = ProbeResult(
            probe_id="test", category="structure", status="warning",
            confidence=0.9, severity="high",
            findings=[ProbeFinding(title="Critical drift", blast_radius="high")],
        )
        decision = gate.evaluate(probe_results=[result])
        assert decision.accepted is False
        assert "Blocking probe" in decision.blocking_issues[0]

    def test_unresolved_surgeon_blocks(self):
        gate = AcceptanceGate()
        decision = gate.evaluate(
            surgeon_intensity="full",
            surgeon_resolved=False,
        )
        assert decision.accepted is False
        assert any("Surgeon review" in b for b in decision.blocking_issues)

    def test_light_surgeon_no_block(self):
        gate = AcceptanceGate()
        decision = gate.evaluate(
            surgeon_intensity="light",
            surgeon_resolved=False,
        )
        assert decision.accepted is True

    def test_invariant_failure_blocks(self):
        gate = AcceptanceGate()
        decision = gate.evaluate(invariants_satisfied=False)
        assert decision.accepted is False

    def test_strict_mode_blocks_on_dissent(self):
        gate = AcceptanceGate(strict=True)
        decision = gate.evaluate(
            surgeon_resolved=True,
            surgeon_dissent_count=2,
        )
        assert decision.accepted is False
        assert any("dissent" in b.lower() for b in decision.blocking_issues)

    def test_normal_mode_allows_dissent(self):
        gate = AcceptanceGate(strict=False)
        decision = gate.evaluate(
            surgeon_resolved=True,
            surgeon_dissent_count=2,
        )
        assert decision.accepted is True
        assert len(decision.unresolved_objection_ids) > 0

    def test_strict_no_evidence_blocks(self):
        gate = AcceptanceGate(strict=True)
        decision = gate.evaluate(
            surgeon_intensity="standard",
            surgeon_resolved=True,
            evidence_count=0,
        )
        assert decision.accepted is False

    def test_permissive_policy_only_invariant_blocks(self):
        gate = AcceptanceGate()
        result = ProbeResult(
            probe_id="test", category="change", status="warning",
            confidence=0.8, severity="high",
            findings=[ProbeFinding(title="Scope creep", blast_radius="high")],
        )
        decision = gate.evaluate(
            probe_results=[result],
            epistemic_packet={"acceptance_policy": "permissive"},
        )
        # Permissive: non-invariant blockers cleared
        assert decision.accepted is True

    def test_dict_probe_results(self):
        gate = AcceptanceGate()
        decision = gate.evaluate(
            probe_results=[{
                "probe_id": "test",
                "findings": [{"title": "Bad thing", "blast_radius": "high"}],
            }],
        )
        assert decision.accepted is False

    def test_quick_check_true(self):
        gate = AcceptanceGate()
        assert gate.quick_check([]) is True

    def test_quick_check_false(self):
        gate = AcceptanceGate()
        result = ProbeResult(
            probe_id="test", category="reliability", status="critical",
            confidence=0.95, severity="critical",
            findings=[ProbeFinding(title="Blocker", blast_radius="high")],
        )
        assert gate.quick_check([result]) is False

    def test_decision_summary(self):
        d = AcceptanceDecision(
            accepted=False, reason="test",
            blocking_issues=["issue1", "issue2"],
        )
        assert "BLOCKED" in d.summary()
        assert "2 blockers" in d.summary()

    def test_decision_to_dict(self):
        d = AcceptanceDecision(accepted=True, reason="All good")
        data = d.to_dict()
        assert data["accepted"] is True
        assert data["reason"] == "All good"
