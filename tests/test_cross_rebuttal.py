"""Tests for the cross-machine rebuttal state machine."""

import sys
import time
from pathlib import Path

import pytest

FLEET_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(FLEET_ROOT))

from multifleet.cross_rebuttal import (
    VALID_TRANSITIONS,
    RebuttalManager,
    RebuttalPhase,
    RebuttalSession,
)


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def mgr():
    return RebuttalManager()


@pytest.fixture
def task_brief():
    return {
        "task_id": "task-001",
        "objective": "Implement cross-machine rebuttal",
        "deadline_minutes": 30,
        "assigned_to": ["mac1", "mac2", "mac3"],
        "constraints": ["no breaking changes"],
    }


@pytest.fixture
def session(mgr, task_brief):
    return mgr.start_session(task_brief)


# ── Phase transition validation ───────────────────────────────────────────


class TestPhaseTransitions:
    def test_valid_forward_transitions(self, session):
        """Each phase can advance to its valid successor."""
        phases = [
            RebuttalPhase.LOCAL_SURGERY,
            RebuttalPhase.VERDICTS_COLLECTED,
            RebuttalPhase.UNDER_REVIEW,
            RebuttalPhase.CRITIQUES_COLLECTED,
            RebuttalPhase.SYNTHESIS,
            RebuttalPhase.RESOLVED,
        ]
        for phase in phases:
            assert session.advance(phase) is True
            assert session.phase == phase

    def test_invalid_skip_transition(self, session):
        """Cannot skip phases."""
        assert session.advance(RebuttalPhase.VERDICTS_COLLECTED) is False
        assert session.phase == RebuttalPhase.ASSIGNED

    def test_invalid_backward_transition(self, session):
        session.advance(RebuttalPhase.LOCAL_SURGERY)
        assert session.advance(RebuttalPhase.ASSIGNED) is False
        assert session.phase == RebuttalPhase.LOCAL_SURGERY

    def test_resolved_is_terminal(self, session):
        # Walk to resolved
        for phase in [
            RebuttalPhase.LOCAL_SURGERY,
            RebuttalPhase.VERDICTS_COLLECTED,
            RebuttalPhase.UNDER_REVIEW,
            RebuttalPhase.CRITIQUES_COLLECTED,
            RebuttalPhase.SYNTHESIS,
            RebuttalPhase.RESOLVED,
        ]:
            session.advance(phase)
        # No transitions from resolved
        assert session.advance(RebuttalPhase.ASSIGNED) is False
        assert session.advance(RebuttalPhase.SYNTHESIS) is False
        assert session.phase == RebuttalPhase.RESOLVED

    def test_all_phases_covered_in_transitions(self):
        """Every non-terminal phase has at least one valid transition."""
        for phase in RebuttalPhase:
            if phase == RebuttalPhase.RESOLVED:
                assert phase not in VALID_TRANSITIONS
            else:
                assert phase in VALID_TRANSITIONS
                assert len(VALID_TRANSITIONS[phase]) >= 1


# ── Deadline expiry ───────────────────────────────────────────────────────


class TestDeadlineExpiry:
    def test_not_expired_initially(self, session):
        assert session.is_expired() is False

    def test_expired_after_deadline(self, session):
        session.created_at = time.time() - (31 * 60)
        assert session.is_expired() is True

    def test_not_expired_within_deadline(self, session):
        session.created_at = time.time() - (29 * 60)
        assert session.is_expired() is False


# ── Verdict collection ────────────────────────────────────────────────────


class TestVerdictCollection:
    def test_add_verdict(self, session):
        session.add_verdict("mac1", {"summary": "looks good", "confidence": 0.9})
        assert "mac1" in session.verdicts
        assert session.verdicts["mac1"]["confidence"] == 0.9

    def test_all_verdicts_in(self, session):
        assert session.all_verdicts_in() is False
        session.add_verdict("mac1", {"summary": "ok"})
        assert session.all_verdicts_in() is False
        session.add_verdict("mac2", {"summary": "ok"})
        assert session.all_verdicts_in() is False
        session.add_verdict("mac3", {"summary": "ok"})
        assert session.all_verdicts_in() is True

    def test_all_verdicts_empty_machines(self):
        s = RebuttalSession(
            task_id="t", phase=RebuttalPhase.LOCAL_SURGERY,
            created_at=time.time(), deadline_minutes=10,
            expected_machines=[],
        )
        assert s.all_verdicts_in() is False

    def test_verdict_overwrites(self, session):
        session.add_verdict("mac1", {"v": 1})
        session.add_verdict("mac1", {"v": 2})
        assert session.verdicts["mac1"]["v"] == 2


# ── Critique collection ──────────────────────────────────────────────────


class TestCritiqueCollection:
    def test_add_critique(self, session):
        session.add_critique("mac1", {"summary": "concern", "confidence": 0.7})
        assert "mac1" in session.critiques

    def test_all_critiques_in(self, session):
        assert session.all_critiques_in() is False
        for m in ["mac1", "mac2", "mac3"]:
            session.add_critique(m, {"summary": "ok"})
        assert session.all_critiques_in() is True


# ── Session lifecycle ─────────────────────────────────────────────────────


class TestSessionLifecycle:
    def test_full_lifecycle(self, mgr, task_brief):
        session = mgr.start_session(task_brief)
        assert session.phase == RebuttalPhase.ASSIGNED

        # Phase 1: assign -> local surgery
        assert mgr.advance_session("task-001") is True
        assert session.phase == RebuttalPhase.LOCAL_SURGERY

        # Phase 2: collect verdicts
        for m in ["mac1", "mac2", "mac3"]:
            session.add_verdict(m, {"summary": f"{m} done", "confidence": 0.8})
        assert session.all_verdicts_in() is True
        assert mgr.advance_session("task-001") is True
        assert session.phase == RebuttalPhase.VERDICTS_COLLECTED

        # Phase 3: cross-machine review
        assert mgr.advance_session("task-001") is True
        assert session.phase == RebuttalPhase.UNDER_REVIEW
        for m in ["mac1", "mac2", "mac3"]:
            session.add_critique(m, {"summary": f"{m} review"})
        assert session.all_critiques_in() is True
        assert mgr.advance_session("task-001") is True
        assert session.phase == RebuttalPhase.CRITIQUES_COLLECTED

        # Phase 4: synthesis -> resolved
        assert mgr.advance_session("task-001") is True
        assert session.phase == RebuttalPhase.SYNTHESIS
        session.set_decision({"decision": "merge", "winner_branch": "mac1/feat"})
        assert session.chief_decision is not None
        assert mgr.advance_session("task-001") is True
        assert session.phase == RebuttalPhase.RESOLVED

    def test_start_session_generates_id(self, mgr):
        s = mgr.start_session({"objective": "test", "deadline_minutes": 5})
        assert s.task_id is not None
        assert len(s.task_id) > 0


# ── Serialization roundtrip ──────────────────────────────────────────────


class TestSerialization:
    def test_roundtrip(self, session):
        session.advance(RebuttalPhase.LOCAL_SURGERY)
        session.add_verdict("mac1", {"summary": "ok"})
        d = session.to_dict()
        restored = RebuttalSession.from_dict(d)
        assert restored.task_id == session.task_id
        assert restored.phase == session.phase
        assert restored.verdicts == session.verdicts
        assert restored.expected_machines == session.expected_machines
        assert restored.deadline_minutes == session.deadline_minutes

    def test_roundtrip_with_decision(self, session):
        session.set_decision({"decision": "merge"})
        d = session.to_dict()
        restored = RebuttalSession.from_dict(d)
        assert restored.chief_decision == {"decision": "merge"}

    def test_roundtrip_preserves_all_fields(self, session):
        session.advance(RebuttalPhase.LOCAL_SURGERY)
        session.add_verdict("mac2", {"x": 1})
        session.add_critique("mac3", {"y": 2})
        d = session.to_dict()
        assert d["phase"] == "local_surgery"
        assert d["verdicts"]["mac2"]["x"] == 1
        assert d["critiques"]["mac3"]["y"] == 2


# ── Cleanup expired ──────────────────────────────────────────────────────


class TestCleanupExpired:
    def test_cleanup_removes_expired(self, mgr):
        s = mgr.start_session({
            "task_id": "old",
            "objective": "stale",
            "deadline_minutes": 1,
        })
        s.created_at = time.time() - 120  # 2 min ago, deadline 1 min
        count = mgr.cleanup_expired()
        assert count == 1
        assert mgr.get_session("old") is None

    def test_cleanup_keeps_fresh(self, mgr, task_brief):
        mgr.start_session(task_brief)
        count = mgr.cleanup_expired()
        assert count == 0
        assert mgr.get_session("task-001") is not None

    def test_cleanup_skips_resolved(self, mgr):
        s = mgr.start_session({
            "task_id": "done",
            "objective": "finished",
            "deadline_minutes": 1,
        })
        # Walk to resolved
        for phase in [
            RebuttalPhase.LOCAL_SURGERY,
            RebuttalPhase.VERDICTS_COLLECTED,
            RebuttalPhase.UNDER_REVIEW,
            RebuttalPhase.CRITIQUES_COLLECTED,
            RebuttalPhase.SYNTHESIS,
            RebuttalPhase.RESOLVED,
        ]:
            s.advance(phase)
        s.created_at = time.time() - 120
        count = mgr.cleanup_expired()
        assert count == 0
        assert mgr.get_session("done") is not None


# ── Multiple concurrent sessions ─────────────────────────────────────────


class TestConcurrentSessions:
    def test_multiple_sessions(self, mgr):
        s1 = mgr.start_session({"task_id": "t1", "objective": "a", "deadline_minutes": 30})
        s2 = mgr.start_session({"task_id": "t2", "objective": "b", "deadline_minutes": 30})
        s3 = mgr.start_session({"task_id": "t3", "objective": "c", "deadline_minutes": 30})

        assert len(mgr.active_sessions()) == 3
        assert len(mgr.all_sessions()) == 3

        # Advance only s1
        mgr.advance_session("t1")
        assert s1.phase == RebuttalPhase.LOCAL_SURGERY
        assert s2.phase == RebuttalPhase.ASSIGNED
        assert s3.phase == RebuttalPhase.ASSIGNED

    def test_active_excludes_resolved(self, mgr):
        s = mgr.start_session({"task_id": "r1", "objective": "x", "deadline_minutes": 30})
        for phase in [
            RebuttalPhase.LOCAL_SURGERY,
            RebuttalPhase.VERDICTS_COLLECTED,
            RebuttalPhase.UNDER_REVIEW,
            RebuttalPhase.CRITIQUES_COLLECTED,
            RebuttalPhase.SYNTHESIS,
            RebuttalPhase.RESOLVED,
        ]:
            s.advance(phase)
        assert len(mgr.active_sessions()) == 0
        assert len(mgr.all_sessions()) == 1

    def test_advance_nonexistent_session(self, mgr):
        assert mgr.advance_session("nope") is False
