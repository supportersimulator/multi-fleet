"""Tests for INV-001/INV-004 hard-gate promotion (gap #7 from invariance audit).

When hard_gate=True (supervisor active), INV-001 and INV-004 failures become
blocking (critical + hard_gate=True). All other rules stay advisory regardless.
"""

import sys
from pathlib import Path

import pytest

FLEET_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(FLEET_ROOT))

from multifleet.invariance_rules import InvarianceEngine, InvarianceResult


@pytest.fixture
def engine():
    return InvarianceEngine()


# ── INV-001 hard-gate ──

class TestINV001HardGate:
    """INV-001 blocks execution without brainstorm when hard_gate=True."""

    def test_blocks_when_hard_gate_true(self, engine):
        """Violation with hard_gate=True -> critical + hard_gate flag."""
        r = engine.check_one("INV-001", {"supervisor_phase": "execution"}, hard_gate=True)
        assert not r.passed
        assert r.hard_gate is True
        assert r.severity == "critical"

    def test_advisory_when_hard_gate_false(self, engine):
        """Violation with hard_gate=False -> no hard_gate flag (backward compat)."""
        r = engine.check_one("INV-001", {"supervisor_phase": "execution"}, hard_gate=False)
        assert not r.passed
        assert r.hard_gate is False

    def test_passes_unaffected_by_hard_gate(self, engine):
        """Passing rule is unaffected by hard_gate flag."""
        r = engine.check_one("INV-001", {
            "supervisor_phase": "execution",
            "brainstorm_id": "abc",
        }, hard_gate=True)
        assert r.passed
        assert r.hard_gate is False

    def test_default_backward_compatible(self, engine):
        """No hard_gate arg -> same behavior as before."""
        r = engine.check_one("INV-001", {"supervisor_phase": "execution"})
        assert not r.passed
        assert r.hard_gate is False


# ── INV-004 hard-gate ──

class TestINV004HardGate:
    """INV-004 blocks incomplete evidence chain when hard_gate=True."""

    def test_blocks_when_hard_gate_true(self, engine):
        """Violation with hard_gate=True -> critical + hard_gate flag."""
        r = engine.check_one("INV-004", {
            "supervisor_phase": "complete",
            "vision_locked": False,
        }, hard_gate=True)
        assert not r.passed
        assert r.hard_gate is True
        assert r.severity == "critical"

    def test_advisory_when_hard_gate_false(self, engine):
        """Violation with hard_gate=False -> no hard_gate flag."""
        r = engine.check_one("INV-004", {
            "supervisor_phase": "complete",
            "vision_locked": False,
        }, hard_gate=False)
        assert not r.passed
        assert r.hard_gate is False

    def test_passes_unaffected_by_hard_gate(self, engine):
        """Full evidence chain passes regardless of hard_gate."""
        r = engine.check_one("INV-004", {
            "supervisor_phase": "complete",
            "vision_locked": True,
            "done_criteria": ["a"],
            "criteria_met": ["a"],
            "has_snapshot": True,
            "chief_accepted": True,
        }, hard_gate=True)
        assert r.passed
        assert r.hard_gate is False

    def test_multiple_missing_items(self, engine):
        """All missing items reported in violation when hard-gated."""
        r = engine.check_one("INV-004", {
            "supervisor_phase": "complete",
            "vision_locked": False,
            "has_snapshot": False,
            "chief_accepted": False,
        }, hard_gate=True)
        assert not r.passed
        assert r.hard_gate is True
        assert "locked VisionContract" in r.violation
        assert "SnapshotRecord" in r.violation
        assert "chief acceptance" in r.violation


# ── Other rules unaffected ──

class TestOtherRulesUnaffected:
    """INV-002, 003, 005, 006, 007 stay advisory regardless of hard_gate."""

    def test_inv002_not_promoted(self, engine):
        r = engine.check_one("INV-002", {"vision_locked": True}, hard_gate=True)
        assert not r.passed
        assert r.hard_gate is False

    def test_inv003_not_promoted(self, engine):
        r = engine.check_one("INV-003", {
            "surgeon_intensity": "standard",
            "surgeon_consensus": False,
            "escalation_sent": False,
        }, hard_gate=True)
        assert not r.passed
        assert r.hard_gate is False

    def test_inv005_not_promoted(self, engine):
        r = engine.check_one("INV-005", {
            "truth_ladder_path": ["inbox", "vision", "dao"],
        }, hard_gate=True)
        assert not r.passed
        assert r.hard_gate is False

    def test_inv006_not_promoted(self, engine):
        r = engine.check_one("INV-006", {
            "transport_channel": "P5_ssh",
            "repair_requested": False,
        }, hard_gate=True)
        assert not r.passed
        assert r.hard_gate is False

    def test_inv007_not_promoted(self, engine):
        r = engine.check_one("INV-007", {
            "overlay_surgeon_phase": "idle",
            "actual_surgeon_phase": "operate",
        }, hard_gate=True)
        assert not r.passed
        assert r.hard_gate is False


# ── check_all with hard_gate ──

class TestCheckAllHardGate:
    """check_all propagates hard_gate to relevant rules only."""

    def test_check_all_hard_gate_promotes_001(self, engine):
        state = {"supervisor_phase": "execution"}  # triggers INV-001
        results = engine.check_all(state, hard_gate=True)
        inv001 = next(r for r in results if r.rule_id == "INV-001")
        assert not inv001.passed
        assert inv001.hard_gate is True
        # Other rules that passed should not be hard-gated
        for r in results:
            if r.passed:
                assert r.hard_gate is False

    def test_check_all_default_no_hard_gate(self, engine):
        state = {"supervisor_phase": "execution"}
        results = engine.check_all(state)
        inv001 = next(r for r in results if r.rule_id == "INV-001")
        assert not inv001.passed
        assert inv001.hard_gate is False

    def test_check_all_hard_gate_false_explicit(self, engine):
        state = {"supervisor_phase": "execution"}
        results = engine.check_all(state, hard_gate=False)
        inv001 = next(r for r in results if r.rule_id == "INV-001")
        assert not inv001.passed
        assert inv001.hard_gate is False


# ── InvarianceResult.to_dict includes hard_gate ──

class TestResultSerialization:
    def test_to_dict_includes_hard_gate(self):
        r = InvarianceResult("INV-001", False, "test", "critical", hard_gate=True)
        d = r.to_dict()
        assert d["hard_gate"] is True

    def test_to_dict_hard_gate_default_false(self):
        r = InvarianceResult("INV-001", True)
        d = r.to_dict()
        assert d["hard_gate"] is False
