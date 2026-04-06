"""Tests for durable task state machine."""

import time
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from multifleet.task_state import TaskStateMachine, TaskState, BACKOFF_BASE_S


@pytest.fixture
def tsm(tmp_path):
    """Fresh task state machine with temp DB."""
    return TaskStateMachine(db_path=str(tmp_path / "test.db"))


class TestCreateTask:
    def test_create_returns_uuid(self, tsm):
        tid = tsm.create_task("mac2", "mac1", {"prompt": "test"})
        assert len(tid) == 36  # UUID format

    def test_created_state(self, tsm):
        tid = tsm.create_task("mac2", "mac1", {"prompt": "test"})
        task = tsm.get(tid)
        assert task["state"] == "created"
        assert task["node_id"] == "mac2"
        assert task["origin_node"] == "mac1"

    def test_payload_stored(self, tsm):
        import json
        tid = tsm.create_task("mac2", "mac1", {"prompt": "fix auth", "timeout": 300})
        task = tsm.get(tid)
        payload = json.loads(task["payload"])
        assert payload["prompt"] == "fix auth"

    def test_chain_fields(self, tsm):
        tid = tsm.create_task("mac2", "mac1", {}, chain_id="chain-1", chain_step=2)
        task = tsm.get(tid)
        assert task["chain_id"] == "chain-1"
        assert task["chain_step"] == 2

    def test_custom_lease_and_retries(self, tsm):
        tid = tsm.create_task("mac2", "mac1", {}, lease_s=600, max_retries=5)
        task = tsm.get(tid)
        assert task["lease_s"] == 600
        assert task["max_retries"] == 5

    def test_audit_on_create(self, tsm):
        tid = tsm.create_task("mac2", "mac1", {})
        log = tsm.audit_log(tid)
        assert len(log) == 1
        assert log[0]["new_state"] == "created"
        assert log[0]["old_state"] is None


class TestTransitions:
    def test_created_to_assigned(self, tsm):
        tid = tsm.create_task("mac2", "mac1", {})
        assert tsm.transition(tid, TaskState.ASSIGNED, "dispatched")
        assert tsm.get(tid)["state"] == "assigned"

    def test_assigned_to_in_progress(self, tsm):
        tid = tsm.create_task("mac2", "mac1", {})
        tsm.transition(tid, TaskState.ASSIGNED)
        assert tsm.transition(tid, TaskState.IN_PROGRESS, "agent started")
        task = tsm.get(tid)
        assert task["state"] == "in_progress"
        assert task["leased_at"] is not None

    def test_in_progress_to_completed(self, tsm):
        tid = tsm.create_task("mac2", "mac1", {})
        tsm.transition(tid, TaskState.ASSIGNED)
        tsm.transition(tid, TaskState.IN_PROGRESS)
        assert tsm.transition(tid, TaskState.COMPLETED, "done")
        task = tsm.get(tid)
        assert task["state"] == "completed"
        assert task["completed_at"] is not None

    def test_in_progress_to_failed(self, tsm):
        tid = tsm.create_task("mac2", "mac1", {})
        tsm.transition(tid, TaskState.ASSIGNED)
        tsm.transition(tid, TaskState.IN_PROGRESS)
        assert tsm.transition(tid, TaskState.FAILED, "agent crashed")
        assert tsm.get(tid)["state"] == "failed"

    def test_invalid_transition_rejected(self, tsm):
        tid = tsm.create_task("mac2", "mac1", {})
        assert not tsm.transition(tid, TaskState.COMPLETED)  # can't skip to completed
        assert tsm.get(tid)["state"] == "created"  # unchanged

    def test_terminal_state_blocks_transitions(self, tsm):
        tid = tsm.create_task("mac2", "mac1", {})
        tsm.transition(tid, TaskState.ASSIGNED)
        tsm.transition(tid, TaskState.IN_PROGRESS)
        tsm.transition(tid, TaskState.COMPLETED)
        assert not tsm.transition(tid, TaskState.ASSIGNED)  # can't leave terminal

    def test_stale_to_reassigned_increments_retries(self, tsm):
        tid = tsm.create_task("mac2", "mac1", {})
        tsm.transition(tid, TaskState.ASSIGNED)
        tsm.transition(tid, TaskState.IN_PROGRESS)
        tsm.transition(tid, TaskState.STALE, "lease expired")
        tsm.transition(tid, TaskState.ASSIGNED, "retry_1")
        task = tsm.get(tid)
        assert task["retries"] == 1

    def test_nonexistent_task_returns_false(self, tsm):
        assert not tsm.transition("nonexistent-id", TaskState.ASSIGNED)

    def test_full_audit_trail(self, tsm):
        tid = tsm.create_task("mac2", "mac1", {})
        tsm.transition(tid, TaskState.ASSIGNED, "dispatched")
        tsm.transition(tid, TaskState.IN_PROGRESS, "started")
        tsm.transition(tid, TaskState.COMPLETED, "done")
        log = tsm.audit_log(tid)
        assert len(log) == 4
        states = [(e["old_state"], e["new_state"]) for e in log]
        assert states == [
            (None, "created"),
            ("created", "assigned"),
            ("assigned", "in_progress"),
            ("in_progress", "completed"),
        ]


class TestListActive:
    def test_list_excludes_terminal(self, tsm):
        t1 = tsm.create_task("mac2", "mac1", {})
        t2 = tsm.create_task("mac2", "mac1", {})
        tsm.transition(t1, TaskState.ASSIGNED)
        tsm.transition(t1, TaskState.IN_PROGRESS)
        tsm.transition(t1, TaskState.COMPLETED)
        active = tsm.list_active()
        assert len(active) == 1
        assert active[0]["id"] == t2

    def test_filter_by_node(self, tsm):
        tsm.create_task("mac2", "mac1", {})
        tsm.create_task("mac3", "mac1", {})
        active = tsm.list_active(node_id="mac2")
        assert len(active) == 1


class TestResumeStale:
    def test_resume_expired_lease(self, tsm):
        tid = tsm.create_task("mac2", "mac1", {}, lease_s=1)
        tsm.transition(tid, TaskState.ASSIGNED)
        tsm.transition(tid, TaskState.IN_PROGRESS)
        # Manually backdate leased_at
        conn = tsm._conn()
        conn.execute("UPDATE task_states SET leased_at = ? WHERE id = ?",
                     (time.time() - 10, tid))
        conn.commit()
        conn.close()

        reassigned = tsm.resume_stale()
        assert tid in reassigned
        assert tsm.get(tid)["state"] == "assigned"

    def test_max_retries_causes_expiry(self, tsm):
        tid = tsm.create_task("mac2", "mac1", {}, lease_s=1, max_retries=0)
        tsm.transition(tid, TaskState.ASSIGNED)
        tsm.transition(tid, TaskState.IN_PROGRESS)
        conn = tsm._conn()
        conn.execute("UPDATE task_states SET leased_at = ? WHERE id = ?",
                     (time.time() - 10, tid))
        conn.commit()
        conn.close()

        reassigned = tsm.resume_stale()
        assert tid not in reassigned
        assert tsm.get(tid)["state"] == "expired"

    def test_expire_old_created(self, tsm):
        tid = tsm.create_task("mac2", "mac1", {})
        conn = tsm._conn()
        conn.execute("UPDATE task_states SET created_at = ? WHERE id = ?",
                     (time.time() - 7200, tid))
        conn.commit()
        conn.close()

        count = tsm.expire_old_created(max_age_s=3600)
        assert count == 1
        assert tsm.get(tid)["state"] == "expired"


class TestBackoff:
    def test_exponential_backoff(self):
        assert TaskStateMachine.next_retry_delay(0) == 5
        assert TaskStateMachine.next_retry_delay(1) == 10
        assert TaskStateMachine.next_retry_delay(2) == 20
        assert TaskStateMachine.next_retry_delay(3) == 40

    def test_backoff_cap(self):
        assert TaskStateMachine.next_retry_delay(10) == 120  # capped


class TestStats:
    def test_empty_stats(self, tsm):
        assert tsm.stats() == {}

    def test_stats_counts(self, tsm):
        tsm.create_task("mac2", "mac1", {})
        tsm.create_task("mac3", "mac1", {})
        t3 = tsm.create_task("mac2", "mac1", {})
        tsm.transition(t3, TaskState.ASSIGNED)
        s = tsm.stats()
        assert s["created"] == 2
        assert s["assigned"] == 1


class TestGetNonexistent:
    def test_get_missing_returns_none(self, tsm):
        assert tsm.get("nonexistent") is None
