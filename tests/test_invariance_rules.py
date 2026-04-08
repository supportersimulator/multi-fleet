"""Tests for InvarianceEngine — 7 GhostScan protocol enforcement rules."""

import sys
from pathlib import Path

import pytest

FLEET_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(FLEET_ROOT))

from multifleet.invariance_rules import InvarianceEngine, InvarianceResult


@pytest.fixture
def engine():
    return InvarianceEngine()


# ── INV-001: No execution without brainstorm ──

class TestRule001:
    def test_passes_when_not_in_execution(self, engine):
        r = engine.check_one("INV-001", {"supervisor_phase": "brainstorming"})
        assert r.passed

    def test_passes_with_brainstorm_id(self, engine):
        r = engine.check_one("INV-001", {
            "supervisor_phase": "execution",
            "brainstorm_id": "abc123",
        })
        assert r.passed

    def test_passes_with_bypass_reason(self, engine):
        r = engine.check_one("INV-001", {
            "supervisor_phase": "execution",
            "bypass_reason": "hotfix",
        })
        assert r.passed

    def test_fails_in_execution_without_brainstorm(self, engine):
        r = engine.check_one("INV-001", {"supervisor_phase": "execution"})
        assert not r.passed
        assert r.severity == "critical"

    def test_fails_in_review_without_brainstorm(self, engine):
        r = engine.check_one("INV-001", {"supervisor_phase": "review"})
        assert not r.passed


# ── INV-002: No vision lock without surgeon ──

class TestRule002:
    def test_passes_when_not_locked(self, engine):
        r = engine.check_one("INV-002", {"vision_locked": False})
        assert r.passed

    def test_passes_with_surgeon_decision(self, engine):
        r = engine.check_one("INV-002", {
            "vision_locked": True,
            "surgeon_decision_id": "sdp-001",
        })
        assert r.passed

    def test_fails_locked_without_surgeon(self, engine):
        r = engine.check_one("INV-002", {"vision_locked": True})
        assert not r.passed
        assert r.severity == "critical"


# ── INV-003: Missing consensus escalation ──

class TestRule003:
    def test_passes_on_light_intensity(self, engine):
        r = engine.check_one("INV-003", {
            "surgeon_intensity": "light",
            "surgeon_consensus": False,
        })
        assert r.passed

    def test_passes_when_consensus_reached(self, engine):
        r = engine.check_one("INV-003", {
            "surgeon_intensity": "standard",
            "surgeon_consensus": True,
        })
        assert r.passed

    def test_passes_with_escalation(self, engine):
        r = engine.check_one("INV-003", {
            "surgeon_intensity": "full",
            "surgeon_consensus": False,
            "escalation_sent": True,
        })
        assert r.passed

    def test_fails_no_consensus_no_escalation(self, engine):
        r = engine.check_one("INV-003", {
            "surgeon_intensity": "standard",
            "surgeon_consensus": False,
            "escalation_sent": False,
        })
        assert not r.passed


# ── INV-004: No COMPLETE without full chain ──

class TestRule004:
    def test_passes_when_not_complete(self, engine):
        r = engine.check_one("INV-004", {"supervisor_phase": "execution"})
        assert r.passed

    def test_passes_with_full_chain(self, engine):
        r = engine.check_one("INV-004", {
            "supervisor_phase": "complete",
            "vision_locked": True,
            "done_criteria": ["a", "b"],
            "criteria_met": ["a", "b"],
            "has_snapshot": True,
            "chief_accepted": True,
        })
        assert r.passed

    def test_fails_missing_vision_lock(self, engine):
        r = engine.check_one("INV-004", {
            "supervisor_phase": "complete",
            "vision_locked": False,
            "has_snapshot": True,
            "chief_accepted": True,
        })
        assert not r.passed
        assert "locked VisionContract" in r.violation

    def test_fails_unsatisfied_criteria(self, engine):
        r = engine.check_one("INV-004", {
            "supervisor_phase": "complete",
            "vision_locked": True,
            "done_criteria": ["a", "b", "c"],
            "criteria_met": ["a"],
            "has_snapshot": True,
            "chief_accepted": True,
        })
        assert not r.passed
        assert "unsatisfied criteria" in r.violation

    def test_fails_missing_snapshot(self, engine):
        r = engine.check_one("INV-004", {
            "supervisor_phase": "complete",
            "vision_locked": True,
            "has_snapshot": False,
            "chief_accepted": True,
        })
        assert not r.passed
        assert "SnapshotRecord" in r.violation


# ── INV-005: No DAO skip snapshot ──

class TestRule005:
    def test_passes_no_dao(self, engine):
        r = engine.check_one("INV-005", {"truth_ladder_path": ["inbox", "vision"]})
        assert r.passed

    def test_passes_correct_path(self, engine):
        r = engine.check_one("INV-005", {
            "truth_ladder_path": ["inbox", "vision", "snapshot", "dao"],
        })
        assert r.passed

    def test_fails_dao_without_snapshot(self, engine):
        r = engine.check_one("INV-005", {
            "truth_ladder_path": ["inbox", "vision", "dao"],
        })
        assert not r.passed
        assert r.severity == "critical"

    def test_passes_empty_path(self, engine):
        r = engine.check_one("INV-005", {"truth_ladder_path": []})
        assert r.passed


# ── INV-006: Transport fallback repair ──

class TestRule006:
    def test_passes_on_p1(self, engine):
        r = engine.check_one("INV-006", {"transport_channel": "P1_nats"})
        assert r.passed

    def test_passes_fallback_with_repair(self, engine):
        r = engine.check_one("INV-006", {
            "transport_channel": "P3_chief",
            "repair_requested": True,
        })
        assert r.passed

    def test_fails_fallback_without_repair(self, engine):
        r = engine.check_one("INV-006", {
            "transport_channel": "P5_ssh",
            "repair_requested": False,
        })
        assert not r.passed
        assert "P5_ssh" in r.violation


# ── INV-007: Overlay reflects state ──

class TestRule007:
    def test_passes_when_matched(self, engine):
        r = engine.check_one("INV-007", {
            "overlay_surgeon_phase": "operate",
            "actual_surgeon_phase": "operate",
            "overlay_execution_phase": "execution",
            "actual_execution_phase": "execution",
        })
        assert r.passed

    def test_fails_surgeon_mismatch(self, engine):
        r = engine.check_one("INV-007", {
            "overlay_surgeon_phase": "idle",
            "actual_surgeon_phase": "operate",
        })
        assert not r.passed
        assert "surgeon phase" in r.violation

    def test_fails_chief_sync_drift(self, engine):
        r = engine.check_one("INV-007", {
            "overlay_chief_sync_age_s": 10.0,
            "actual_chief_sync_age_s": 200.0,
        })
        assert not r.passed
        assert "drift" in r.violation

    def test_passes_no_overlay_data(self, engine):
        r = engine.check_one("INV-007", {})
        assert r.passed


# ── Engine-level tests ──

class TestEngine:
    def test_check_all_returns_7_results(self, engine):
        results = engine.check_all({})
        assert len(results) == 7

    def test_check_all_all_pass_on_empty_state(self, engine):
        results = engine.check_all({})
        assert all(r.passed for r in results)

    def test_check_one_unknown_rule(self, engine):
        r = engine.check_one("INV-999", {})
        assert not r.passed
        assert "Unknown rule" in r.violation

    def test_rule_ids(self, engine):
        ids = engine.rule_ids
        assert len(ids) == 7
        assert ids[0] == "INV-001"
        assert ids[-1] == "INV-007"

    def test_result_to_dict(self):
        r = InvarianceResult("INV-001", False, "test violation", "critical")
        d = r.to_dict()
        assert d["rule_id"] == "INV-001"
        assert d["passed"] is False
        assert d["violation"] == "test violation"
        assert d["severity"] == "critical"

    def test_check_all_with_violations(self, engine):
        state = {
            "supervisor_phase": "execution",
            # No brainstorm_id or bypass — triggers INV-001
        }
        results = engine.check_all(state)
        inv001 = next(r for r in results if r.rule_id == "INV-001")
        assert not inv001.passed
