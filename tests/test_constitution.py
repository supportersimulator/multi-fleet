"""Tests for Constitutional Physics — machine-enforceable principles."""

import pytest

from multifleet.constitution import (
    CheckResult,
    ConstitutionalGate,
    GateResult,
    Principle,
    check_acontext_logic,
    check_determinism,
    check_evidence_backing,
    check_injection_purity,
    check_minimalism,
    check_reversibility,
    check_sop_integrity,
    explain_principle,
    get_constitution,
    get_principle,
    validate_before_action,
)


# ---------------------------------------------------------------------------
# Constitution completeness
# ---------------------------------------------------------------------------

class TestConstitutionCompleteness:
    """All 7 principles must be present and correctly structured."""

    def test_seven_principles(self):
        principles = get_constitution()
        assert len(principles) == 7

    def test_ids_sequential(self):
        principles = get_constitution()
        ids = [p.id for p in principles]
        assert ids == ["CP-1", "CP-2", "CP-3", "CP-4", "CP-5", "CP-6", "CP-7"]

    def test_all_have_names(self):
        for p in get_constitution():
            assert p.name, f"{p.id} missing name"
            assert p.description, f"{p.id} missing description"

    def test_all_have_checks(self):
        for p in get_constitution():
            assert callable(p.check), f"{p.id} check not callable"

    def test_severity_values(self):
        for p in get_constitution():
            assert p.severity in ("hard-gate", "advisory"), f"{p.id} bad severity"

    def test_hard_gate_principles(self):
        hard = [p for p in get_constitution() if p.severity == "hard-gate"]
        ids = {p.id for p in hard}
        assert ids == {"CP-1", "CP-2", "CP-5", "CP-6"}

    def test_advisory_principles(self):
        advisory = [p for p in get_constitution() if p.severity == "advisory"]
        ids = {p.id for p in advisory}
        assert ids == {"CP-3", "CP-4", "CP-7"}

    def test_get_principle_by_id(self):
        p = get_principle("CP-1")
        assert p is not None
        assert p.name == "Preserve Determinism"

    def test_get_principle_unknown(self):
        assert get_principle("CP-99") is None

    def test_principles_are_frozen(self):
        p = get_principle("CP-1")
        with pytest.raises(AttributeError):
            p.name = "tampered"


# ---------------------------------------------------------------------------
# CP-1: Preserve Determinism
# ---------------------------------------------------------------------------

class TestDeterminism:
    def test_matching_outputs(self):
        r = check_determinism({"output1": "hello", "output2": "hello"})
        assert r.passed
        assert r.principle_id == "CP-1"

    def test_mismatched_outputs(self):
        r = check_determinism({"output1": "hello", "output2": "world"})
        assert not r.passed
        assert "violation" in r.message.lower()

    def test_single_output(self):
        r = check_determinism({"output1": "hello"})
        assert r.passed

    def test_no_outputs(self):
        r = check_determinism({})
        assert r.passed

    def test_bytes_outputs(self):
        r = check_determinism({"output1": b"data", "output2": b"data"})
        assert r.passed

    def test_bytes_mismatch(self):
        r = check_determinism({"output1": b"a", "output2": b"b"})
        assert not r.passed


# ---------------------------------------------------------------------------
# CP-2: No Discovery at Injection
# ---------------------------------------------------------------------------

class TestInjectionPurity:
    def test_pure_ops(self):
        r = check_injection_purity({
            "operation_log": ["retrieve_context", "assemble_payload", "query_store"]
        })
        assert r.passed

    def test_forbidden_learn(self):
        r = check_injection_purity({
            "operation_log": ["retrieve", "learn_from_prompt"]
        })
        assert not r.passed
        assert "learn" in r.message.lower()

    def test_forbidden_promote(self):
        r = check_injection_purity({
            "operation_log": ["promote_to_gold"]
        })
        assert not r.passed

    def test_forbidden_discover(self):
        r = check_injection_purity({
            "operation_log": ["discover_new_pattern"]
        })
        assert not r.passed

    def test_forbidden_write_memory(self):
        r = check_injection_purity({
            "operation_log": ["write_memory"]
        })
        assert not r.passed

    def test_no_ops(self):
        r = check_injection_purity({})
        assert r.passed

    def test_empty_log(self):
        r = check_injection_purity({"operation_log": []})
        assert r.passed


# ---------------------------------------------------------------------------
# CP-3: Respect SOP Integrity
# ---------------------------------------------------------------------------

class TestSOPIntegrity:
    def test_no_sop_change(self):
        r = check_sop_integrity({"sop_changed": False})
        assert r.passed

    def test_sop_change_with_evidence(self):
        r = check_sop_integrity({"sop_changed": True, "evidence_count": 3})
        assert r.passed

    def test_sop_change_no_evidence(self):
        r = check_sop_integrity({"sop_changed": True, "evidence_count": 0})
        assert not r.passed
        assert r.severity == "advisory"

    def test_sop_change_missing_evidence_key(self):
        r = check_sop_integrity({"sop_changed": True})
        assert not r.passed


# ---------------------------------------------------------------------------
# CP-4: Respect acontext Logic
# ---------------------------------------------------------------------------

class TestAcontextLogic:
    def test_referenced(self):
        r = check_acontext_logic({"acontext_referenced": True})
        assert r.passed

    def test_not_referenced(self):
        r = check_acontext_logic({"acontext_referenced": False})
        assert not r.passed

    def test_full_expansion_after_failures(self):
        r = check_acontext_logic({
            "acontext_referenced": True,
            "full_expansion": True,
            "failure_count": 3,
        })
        assert r.passed

    def test_full_expansion_too_early(self):
        r = check_acontext_logic({
            "acontext_referenced": True,
            "full_expansion": True,
            "failure_count": 1,
        })
        assert not r.passed

    def test_defaults(self):
        r = check_acontext_logic({})
        assert r.passed


# ---------------------------------------------------------------------------
# CP-5: Evidence Over Confidence
# ---------------------------------------------------------------------------

class TestEvidenceBacking:
    def test_evidence_with_confidence(self):
        r = check_evidence_backing({
            "claim": "this works", "evidence_count": 5, "confidence": 0.9
        })
        assert r.passed

    def test_high_confidence_no_evidence(self):
        r = check_evidence_backing({
            "claim": "trust me", "evidence_count": 0, "confidence": 0.95
        })
        assert not r.passed
        assert r.severity == "hard-gate"

    def test_low_confidence_no_evidence(self):
        r = check_evidence_backing({
            "claim": "maybe", "evidence_count": 0, "confidence": 0.3
        })
        assert r.passed

    def test_boundary_confidence(self):
        # 0.7 is the boundary — at 0.7 exactly, should pass
        r = check_evidence_backing({
            "claim": "borderline", "evidence_count": 0, "confidence": 0.7
        })
        assert r.passed

    def test_just_over_boundary(self):
        r = check_evidence_backing({
            "claim": "borderline", "evidence_count": 0, "confidence": 0.71
        })
        assert not r.passed


# ---------------------------------------------------------------------------
# CP-6: Prefer Reversible Actions
# ---------------------------------------------------------------------------

class TestReversibility:
    def test_non_destructive(self):
        r = check_reversibility({"destructive": False})
        assert r.passed

    def test_destructive_with_checkpoint(self):
        r = check_reversibility({"destructive": True, "has_checkpoint": True})
        assert r.passed

    def test_destructive_no_checkpoint(self):
        r = check_reversibility({"destructive": True, "has_checkpoint": False})
        assert not r.passed
        assert r.severity == "hard-gate"

    def test_defaults_non_destructive(self):
        r = check_reversibility({})
        assert r.passed


# ---------------------------------------------------------------------------
# CP-7: Minimalism
# ---------------------------------------------------------------------------

class TestMinimalism:
    def test_minimal_scope(self):
        r = check_minimalism({"scope": "minimal", "files_changed": 2, "lines_changed": 50})
        assert r.passed

    def test_broad_many_files(self):
        r = check_minimalism({"scope": "broad", "files_changed": 25, "lines_changed": 500})
        assert not r.passed
        assert r.severity == "advisory"

    def test_broad_few_files(self):
        r = check_minimalism({"scope": "broad", "files_changed": 5, "lines_changed": 100})
        assert r.passed

    def test_many_lines_moderate(self):
        r = check_minimalism({"scope": "moderate", "files_changed": 5, "lines_changed": 1500})
        assert not r.passed

    def test_many_lines_minimal(self):
        # Even 1500 lines is ok if scope is explicitly "minimal"
        r = check_minimalism({"scope": "minimal", "files_changed": 5, "lines_changed": 1500})
        assert r.passed

    def test_defaults(self):
        r = check_minimalism({})
        assert r.passed


# ---------------------------------------------------------------------------
# ConstitutionalGate
# ---------------------------------------------------------------------------

class TestConstitutionalGate:
    def test_all_pass(self):
        ctx = {
            "output1": "x", "output2": "x",
            "operation_log": ["retrieve"],
            "sop_changed": False,
            "acontext_referenced": True,
            "evidence_count": 3, "confidence": 0.8,
            "destructive": False,
            "scope": "minimal", "files_changed": 1, "lines_changed": 10,
        }
        gate = ConstitutionalGate()
        result = gate.validate("test-action", ctx)
        assert result.passed
        assert len(result.checks) == 7
        assert "PASSED" in result.summary

    def test_hard_gate_blocks(self):
        ctx = {
            "destructive": True, "has_checkpoint": False,  # CP-6 fails
        }
        result = ConstitutionalGate().validate("risky-action", ctx)
        assert not result.passed
        assert "BLOCKED" in result.summary
        assert "CP-6" in result.summary

    def test_advisory_only_does_not_block(self):
        ctx = {
            # All hard-gates pass
            "output1": "a", "output2": "a",
            "operation_log": [],
            "evidence_count": 1, "confidence": 0.5,
            "destructive": False,
            # Advisory fails
            "sop_changed": True, "evidence_count": 0,
            "scope": "broad", "files_changed": 50, "lines_changed": 3000,
        }
        # evidence_count=0 with confidence=0.5 passes CP-5 (low confidence ok)
        # but sop_changed=True with evidence_count=0 fails CP-3 (advisory)
        result = ConstitutionalGate().validate("advisory-test", ctx)
        # CP-5 check: evidence_count=0, confidence=0.5 -> passes
        # CP-3: sop_changed=True, evidence_count=0 -> advisory fail
        # CP-7: broad, 50 files -> advisory fail
        # All hard gates should pass -> overall passes
        assert result.passed
        assert len(result.advisories) >= 1

    def test_check_exception_recorded(self):
        """Check functions that crash produce failures, not silent swallowing."""
        def bad_check(ctx):
            raise ValueError("boom")

        bad_principle = Principle(
            id="CP-X", name="Bad", description="Crashes",
            check=bad_check, severity="hard-gate",
        )
        gate = ConstitutionalGate(principles=[bad_principle])
        result = gate.validate("crash-test", {})
        assert not result.passed
        assert "exception" in result.checks[0].message.lower()

    def test_duration_recorded(self):
        result = ConstitutionalGate().validate("timing", {})
        assert result.duration_ms >= 0

    def test_to_dict(self):
        result = ConstitutionalGate().validate("dict-test", {})
        d = result.to_dict()
        assert "passed" in d
        assert "checks" in d
        assert "summary" in d
        assert isinstance(d["checks"], list)


# ---------------------------------------------------------------------------
# validate_before_action convenience function
# ---------------------------------------------------------------------------

class TestValidateBeforeAction:
    def test_passes_clean_context(self):
        result = validate_before_action("merge", {
            "destructive": False,
            "evidence_count": 1,
            "confidence": 0.5,
        })
        assert result.passed

    def test_blocks_on_hard_gate(self):
        result = validate_before_action("deploy", {
            "evidence_count": 0,
            "confidence": 0.99,  # CP-5: high confidence, zero evidence
        })
        assert not result.passed

    def test_empty_context(self):
        # Defaults should be safe — no action is destructive by default
        result = validate_before_action("noop", {})
        assert result.passed


# ---------------------------------------------------------------------------
# explain_principle
# ---------------------------------------------------------------------------

class TestExplainPrinciple:
    def test_known_principle(self):
        text = explain_principle("CP-1")
        assert "Determinism" in text
        assert len(text) > 50

    def test_all_principles_have_explanations(self):
        for p in get_constitution():
            text = explain_principle(p.id)
            assert len(text) > 20, f"{p.id} explanation too short"

    def test_unknown_principle(self):
        text = explain_principle("CP-99")
        assert "Unknown" in text


# ---------------------------------------------------------------------------
# GateResult properties
# ---------------------------------------------------------------------------

class TestGateResult:
    def test_hard_failures_property(self):
        r = GateResult(passed=False, checks=[
            CheckResult("CP-1", False, "fail", "hard-gate"),
            CheckResult("CP-3", False, "warn", "advisory"),
        ])
        assert len(r.hard_failures) == 1
        assert r.hard_failures[0].principle_id == "CP-1"

    def test_advisories_property(self):
        r = GateResult(passed=True, checks=[
            CheckResult("CP-1", True, "ok", "hard-gate"),
            CheckResult("CP-3", False, "warn", "advisory"),
        ])
        assert len(r.advisories) == 1
        assert r.advisories[0].principle_id == "CP-3"

    def test_summary_passed_no_advisories(self):
        r = GateResult(passed=True, checks=[
            CheckResult("CP-1", True, "ok", "hard-gate"),
        ])
        assert "PASSED (1/1)" in r.summary

    def test_summary_passed_with_advisories(self):
        r = GateResult(passed=True, checks=[
            CheckResult("CP-1", True, "ok", "hard-gate"),
            CheckResult("CP-3", False, "warn", "advisory"),
        ])
        assert "PASSED" in r.summary
        assert "CP-3" in r.summary

    def test_summary_blocked(self):
        r = GateResult(passed=False, checks=[
            CheckResult("CP-6", False, "no checkpoint", "hard-gate"),
        ])
        assert "BLOCKED" in r.summary
        assert "CP-6" in r.summary
