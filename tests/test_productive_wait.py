"""Tests for productive waiting — sessions never idle."""

import time

import pytest

from multifleet.productive_wait import ProductiveWaiter, WaitState


@pytest.fixture
def waiter():
    return ProductiveWaiter(node_id="mac1")


# ── WaitState basics ──

class TestWaitState:
    def test_defaults(self):
        ws = WaitState(waiting_for="task-1")
        assert ws.waiting_for == "task-1"
        assert ws.reply_received is False
        assert ws.reply_data is None
        assert ws.productive_task is None
        assert ws.productive_started_at is None
        assert isinstance(ws.started_at, float)


# ── Start wait / notify reply / clear ──

class TestWaitLifecycle:
    def test_start_wait(self, waiter):
        state = waiter.start_wait("task-1")
        assert state.waiting_for == "task-1"
        assert waiter.is_waiting()
        assert "task-1" in waiter.pending_replies()

    def test_notify_reply(self, waiter):
        waiter.start_wait("task-1")
        ok = waiter.notify_reply("task-1", {"result": "done"})
        assert ok is True
        assert waiter.pending_replies() == []
        completed = waiter.completed_waits()
        assert len(completed) == 1
        assert completed[0].reply_data == {"result": "done"}

    def test_notify_reply_unknown_task(self, waiter):
        ok = waiter.notify_reply("nonexistent", {})
        assert ok is False

    def test_clear_completed(self, waiter):
        waiter.start_wait("task-1")
        waiter.start_wait("task-2")
        waiter.notify_reply("task-1", {"done": True})
        waiter.clear_completed()
        assert "task-1" not in waiter._active_waits
        assert "task-2" in waiter._active_waits

    def test_clear_completed_keeps_pending(self, waiter):
        waiter.start_wait("task-1")
        waiter.clear_completed()
        assert waiter.is_waiting()

    def test_not_waiting_when_empty(self, waiter):
        assert waiter.is_waiting() is False


# ── Backlog FIFO ──

class TestBacklog:
    def test_fifo_order(self, waiter):
        waiter.add_backlog_item({"id": "a"})
        waiter.add_backlog_item({"id": "b"})
        waiter.add_backlog_item({"id": "c"})
        assert waiter.get_next_work() == {"id": "a"}
        assert waiter.get_next_work() == {"id": "b"}
        assert waiter.get_next_work() == {"id": "c"}

    def test_empty_backlog_returns_none(self, waiter):
        assert waiter.get_next_work() is None

    def test_backlog_depletes(self, waiter):
        waiter.add_backlog_item({"id": "x"})
        waiter.get_next_work()
        assert waiter.get_next_work() is None


# ── Multiple concurrent waits ──

class TestConcurrentWaits:
    def test_multiple_waits(self, waiter):
        waiter.start_wait("t1")
        waiter.start_wait("t2")
        waiter.start_wait("t3")
        assert len(waiter.pending_replies()) == 3
        waiter.notify_reply("t2", {"ok": True})
        assert waiter.pending_replies() == ["t1", "t3"]

    def test_completed_waits_only_replied(self, waiter):
        waiter.start_wait("t1")
        waiter.start_wait("t2")
        waiter.notify_reply("t1", {})
        assert len(waiter.completed_waits()) == 1
        assert waiter.completed_waits()[0].waiting_for == "t1"


# ── Status reporting ──

class TestStatus:
    def test_empty_status(self, waiter):
        s = waiter.status()
        assert s == {
            "active_waits": 0,
            "pending_replies": [],
            "backlog_size": 0,
            "completed": 0,
        }

    def test_status_with_activity(self, waiter):
        waiter.start_wait("t1")
        waiter.start_wait("t2")
        waiter.add_backlog_item({"desc": "test"})
        waiter.notify_reply("t1", {"r": 1})
        s = waiter.status()
        assert s["active_waits"] == 2
        assert s["pending_replies"] == ["t2"]
        assert s["backlog_size"] == 1
        assert s["completed"] == 1


# ── Wait timing ──

class TestTiming:
    def test_wait_records_start_time(self, waiter):
        before = time.time()
        state = waiter.start_wait("timed")
        after = time.time()
        assert before <= state.started_at <= after

    def test_reply_time_delta(self, waiter):
        state = waiter.start_wait("timed")
        # Simulate a tiny wait
        state.started_at = time.time() - 2.5
        waiter.notify_reply("timed", {})
        # The reply_received flag is set; timing is tracked via started_at
        assert state.reply_received is True
        elapsed = time.time() - state.started_at
        assert elapsed >= 2.0
