"""Tests for the 6 vision packet schemas (task_brief, local_verdict, branch_status,
critique, merge_candidate, chief_decision).

Validates correct payloads pass and missing required fields are rejected.
"""

import sys
from pathlib import Path

import pytest

FLEET_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(FLEET_ROOT))

from multifleet.packet_registry import PacketRegistry

# Skip all tests if jsonschema not installed
jsonschema = pytest.importorskip("jsonschema")


@pytest.fixture
def registry():
    """Fresh registry with builtins loaded."""
    return PacketRegistry()


# ── Helpers ───────────────────────────────────────────────────────────────


def assert_valid(registry, type_name, payload):
    errors = registry.validate(type_name, payload)
    assert errors == [], f"Unexpected validation errors for {type_name}: {errors}"


def assert_invalid(registry, type_name, payload):
    errors = registry.validate(type_name, payload)
    assert len(errors) > 0, f"Expected validation errors for {type_name} but got none"


# ── Registration ──────────────────────────────────────────────────────────


class TestRegistration:
    def test_all_six_types_registered(self, registry):
        for t in ["task_brief", "local_verdict", "branch_status",
                   "critique", "merge_candidate", "chief_decision"]:
            assert registry.is_registered(t), f"{t} not registered"


# ── task_brief ────────────────────────────────────────────────────────────


class TestTaskBrief:
    def test_valid_full(self, registry):
        assert_valid(registry, "task_brief", {
            "task_id": "t-001",
            "objective": "Build rebuttal protocol",
            "deadline_minutes": 30,
            "scope": "multi-fleet",
            "constraints": ["no breaking changes"],
            "assigned_to": ["mac1", "mac2"],
        })

    def test_valid_minimal(self, registry):
        assert_valid(registry, "task_brief", {
            "task_id": "t-002",
            "objective": "Fix bug",
            "deadline_minutes": 5,
        })

    def test_missing_task_id(self, registry):
        assert_invalid(registry, "task_brief", {
            "objective": "Fix", "deadline_minutes": 5,
        })

    def test_missing_objective(self, registry):
        assert_invalid(registry, "task_brief", {
            "task_id": "t", "deadline_minutes": 5,
        })

    def test_invalid_deadline(self, registry):
        assert_invalid(registry, "task_brief", {
            "task_id": "t", "objective": "x", "deadline_minutes": 0,
        })


# ── local_verdict ─────────────────────────────────────────────────────────


class TestLocalVerdict:
    def test_valid(self, registry):
        assert_valid(registry, "local_verdict", {
            "task_id": "t-001",
            "machine_id": "mac1",
            "branch": "mac1/feat-rebuttal",
            "summary": "Implemented state machine",
            "confidence": 0.85,
            "files_touched": ["rebuttal.py", "test_rebuttal.py"],
            "risks": ["API change"],
            "dissent": [{"surgeon": "neurologist", "claim": "complexity high", "severity": "medium"}],
            "questions_for_chief": ["Merge strategy?"],
        })

    def test_missing_confidence(self, registry):
        assert_invalid(registry, "local_verdict", {
            "task_id": "t", "machine_id": "m", "branch": "b",
            "summary": "s", "files_touched": [], "risks": [],
            "dissent": [], "questions_for_chief": [],
        })

    def test_confidence_out_of_range(self, registry):
        assert_invalid(registry, "local_verdict", {
            "task_id": "t", "machine_id": "m", "branch": "b",
            "summary": "s", "confidence": 1.5,
            "files_touched": [], "risks": [],
            "dissent": [], "questions_for_chief": [],
        })

    def test_empty_arrays_valid(self, registry):
        assert_valid(registry, "local_verdict", {
            "task_id": "t", "machine_id": "m", "branch": "b",
            "summary": "s", "confidence": 0.0,
            "files_touched": [], "risks": [],
            "dissent": [], "questions_for_chief": [],
        })


# ── branch_status ─────────────────────────────────────────────────────────


class TestBranchStatus:
    def test_valid(self, registry):
        assert_valid(registry, "branch_status", {
            "machine_id": "mac2",
            "branch": "mac2/fix-123",
            "commit": "abc1234",
            "state": "working",
            "summary": "In progress",
            "files": ["foo.py"],
            "risks": [],
        })

    def test_invalid_state_enum(self, registry):
        assert_invalid(registry, "branch_status", {
            "machine_id": "m", "branch": "b", "commit": "c",
            "state": "invalid_state", "summary": "s",
            "files": [], "risks": [],
        })

    def test_missing_commit(self, registry):
        assert_invalid(registry, "branch_status", {
            "machine_id": "m", "branch": "b",
            "state": "working", "summary": "s",
            "files": [], "risks": [],
        })


# ── critique ──────────────────────────────────────────────────────────────


class TestCritique:
    def test_valid(self, registry):
        assert_valid(registry, "critique", {
            "task_id": "t-001",
            "from_machine": "mac2",
            "to_machine": "mac1",
            "target_branch": "mac1/feat",
            "target_commit": "def5678",
            "summary": "Looks solid with minor concerns",
            "confidence": 0.75,
            "blocking_concerns": [],
            "suggested_merge_action": "merge",
        })

    def test_invalid_merge_action(self, registry):
        assert_invalid(registry, "critique", {
            "task_id": "t", "from_machine": "m1", "to_machine": "m2",
            "target_branch": "b", "target_commit": "c",
            "summary": "s", "confidence": 0.5,
            "blocking_concerns": [],
            "suggested_merge_action": "delete",
        })

    def test_missing_from_machine(self, registry):
        assert_invalid(registry, "critique", {
            "task_id": "t", "to_machine": "m2",
            "target_branch": "b", "target_commit": "c",
            "summary": "s", "confidence": 0.5,
            "blocking_concerns": [],
            "suggested_merge_action": "merge",
        })


# ── merge_candidate ──────────────────────────────────────────────────────


class TestMergeCandidate:
    def test_valid(self, registry):
        assert_valid(registry, "merge_candidate", {
            "task_id": "t-001",
            "machine_id": "mac1",
            "branch": "mac1/feat",
            "commit": "abc1234",
            "summary": "Ready for merge",
            "test_results": {"passed": 42, "failed": 0, "skipped": 2},
            "recommendation": "Merge — all tests pass",
        })

    def test_missing_test_results(self, registry):
        assert_invalid(registry, "merge_candidate", {
            "task_id": "t", "machine_id": "m", "branch": "b",
            "commit": "c", "summary": "s",
            "recommendation": "r",
        })

    def test_minimal_test_results(self, registry):
        assert_valid(registry, "merge_candidate", {
            "task_id": "t", "machine_id": "m", "branch": "b",
            "commit": "c", "summary": "s",
            "test_results": {},
            "recommendation": "r",
        })


# ── chief_decision ────────────────────────────────────────────────────────


class TestChiefDecision:
    def test_valid(self, registry):
        assert_valid(registry, "chief_decision", {
            "task_id": "t-001",
            "decision": "cherry_pick",
            "winner_branch": "mac1/feat",
            "cherry_pick_from": [
                {"machine": "mac2", "commit": "xyz789", "reason": "better error handling"},
            ],
            "followups": ["Update docs"],
            "dissent_preserved": ["mac3 disagreed on API shape"],
        })

    def test_invalid_decision_enum(self, registry):
        assert_invalid(registry, "chief_decision", {
            "task_id": "t", "decision": "yolo",
            "winner_branch": "b",
            "cherry_pick_from": [], "followups": [],
            "dissent_preserved": [],
        })

    def test_missing_winner_branch(self, registry):
        assert_invalid(registry, "chief_decision", {
            "task_id": "t", "decision": "merge",
            "cherry_pick_from": [], "followups": [],
            "dissent_preserved": [],
        })

    def test_empty_arrays_valid(self, registry):
        assert_valid(registry, "chief_decision", {
            "task_id": "t", "decision": "reject",
            "winner_branch": "none",
            "cherry_pick_from": [], "followups": [],
            "dissent_preserved": [],
        })
