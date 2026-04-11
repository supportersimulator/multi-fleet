"""Tests for supervisor.py — Dev Squad execution continuity loop."""

import pytest
from multifleet.supervisor import Supervisor, SupervisorPhase, SupervisorState


@pytest.fixture
def sup():
    return Supervisor(node_id="mac1")


class TestStartTask:
    def test_start_task(self, sup):
        state = sup.start_task("t1", "Build feature X", ["tests pass", "deployed"])
        assert state.task_id == "t1"
        assert state.phase == SupervisorPhase.INBOX_CAPTURE
        assert state.vision_contract is not None
        assert len(state.vision_contract.done_criteria) == 2
        assert len(state.history) == 1

    def test_duplicate_task_raises(self, sup):
        sup.start_task("t1", "Task", ["done"])
        with pytest.raises(ValueError, match="already exists"):
            sup.start_task("t1", "Same ID", ["done"])


class TestAdvancePhases:
    def test_advance_through_phases(self, sup):
        state = sup.start_task("t1", "Task", ["criterion"])
        # Advance through the first several phases
        phases_seen = [state.phase]
        for _ in range(5):
            state = sup.advance("t1")
            phases_seen.append(state.phase)

        assert phases_seen[0] == SupervisorPhase.INBOX_CAPTURE
        assert phases_seen[1] == SupervisorPhase.BRAINSTORMING
        assert phases_seen[2] == SupervisorPhase.SURGEON_ADJUDICATION
        assert phases_seen[3] == SupervisorPhase.CHIEF_ESCALATION
        assert phases_seen[4] == SupervisorPhase.VISION_LOCK
        assert phases_seen[5] == SupervisorPhase.EXECUTION

    def test_advance_to_complete(self, sup):
        state = sup.start_task("t1", "Task", ["criterion"])
        # Advance all the way (mark criterion met at REVIEW)
        for _ in range(6):  # to REVIEW
            state = sup.advance("t1")
        assert state.phase == SupervisorPhase.REVIEW
        state = sup.advance("t1", {"criteria_satisfied": ["criterion"]})
        # With criteria met, REVIEW jumps to SNAPSHOT_RECORD
        assert state.phase == SupervisorPhase.SNAPSHOT_RECORD

    def test_no_advance_past_complete(self, sup):
        state = sup.start_task("t1", "Task", ["c"])
        # Fast-track through all phases
        for _ in range(6):
            state = sup.advance("t1")
        state = sup.advance("t1", {"criteria_satisfied": ["c"]})  # -> SNAPSHOT_RECORD
        state = sup.advance("t1")  # -> DAO_CANDIDATE
        state = sup.advance("t1")  # -> CHIEF_ACCEPTANCE
        state = sup.advance("t1")  # -> COMPLETE
        assert state.phase == SupervisorPhase.COMPLETE
        # Further advance does nothing
        state = sup.advance("t1")
        assert state.phase == SupervisorPhase.COMPLETE


class TestBlockedState:
    def test_block_task(self, sup):
        sup.start_task("t1", "Task", ["c"])
        state = sup.block("t1", "Missing dependency")
        assert state.phase == SupervisorPhase.BLOCKED
        assert state.blocked_reason == "Missing dependency"
        assert state.is_terminal

    def test_advance_blocked_noop(self, sup):
        sup.start_task("t1", "Task", ["c"])
        sup.block("t1", "Stuck")
        state = sup.advance("t1")
        assert state.phase == SupervisorPhase.BLOCKED


class TestMaxIterationsSafety:
    def test_max_iterations_blocks(self, sup):
        state = sup.start_task("t1", "Task", ["c"])
        state.max_iterations = 3
        sup.advance("t1")  # iter 1
        sup.advance("t1")  # iter 2
        sup.advance("t1")  # iter 3
        state = sup.advance("t1")  # iter 4 -> blocked
        assert state.phase == SupervisorPhase.BLOCKED
        assert "Max iterations" in state.blocked_reason


class TestHistoryTracking:
    def test_history_recorded(self, sup):
        sup.start_task("t1", "Task", ["c"])
        sup.advance("t1")
        sup.advance("t1")
        state = sup.get_state("t1")
        assert len(state.history) == 3  # start + 2 advances
        assert state.history[0]["from"] is None  # initial
        assert state.history[0]["to"] == "inbox_capture"
        assert state.history[1]["from"] == "inbox_capture"
        assert state.history[1]["to"] == "brainstorming"


class TestVisionContract:
    def test_is_done(self, sup):
        state = sup.start_task("t1", "Task", ["a", "b", "c"])
        assert not sup.is_done("t1")
        state.criteria_met.append("a")
        assert not sup.is_done("t1")
        state.criteria_met.extend(["b", "c"])
        assert sup.is_done("t1")

    def test_criteria_tracked_via_evidence(self, sup):
        sup.start_task("t1", "Task", ["x", "y"])
        for _ in range(6):  # advance to REVIEW
            sup.advance("t1")
        state = sup.advance("t1", {"criteria_satisfied": ["x"]})
        assert "x" in state.criteria_met
        assert "y" not in state.criteria_met


class TestFixLoop:
    def test_review_fix_loop(self, sup):
        sup.start_task("t1", "Task", ["c"])
        for _ in range(6):  # advance to REVIEW
            sup.advance("t1")
        state = sup.get_state("t1")
        assert state.phase == SupervisorPhase.REVIEW
        # Trigger fix loop
        state = sup.advance("t1", {"fix_needed": True})
        assert state.phase == SupervisorPhase.FIX_LOOP
        # FIX_LOOP returns to REVIEW
        state = sup.advance("t1")
        assert state.phase == SupervisorPhase.REVIEW


class TestListTasks:
    def test_list_active(self, sup):
        sup.start_task("t1", "Task 1", ["c"])
        sup.start_task("t2", "Task 2", ["c"])
        sup.block("t1", "stuck")
        active = sup.list_active()
        assert len(active) == 1
        assert active[0].task_id == "t2"

    def test_list_all(self, sup):
        sup.start_task("t1", "Task 1", ["c"])
        sup.start_task("t2", "Task 2", ["c"])
        assert len(sup.list_all()) == 2

    def test_unknown_task_raises(self, sup):
        with pytest.raises(ValueError, match="Unknown task"):
            sup.get_state("nonexistent")
