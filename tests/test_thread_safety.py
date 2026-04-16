"""Thread-safety tests for SQLite modules with mutex protection.

Verifies concurrent writes don't corrupt data in store.py, truth_ladder.py,
and task_state.py — all of which use check_same_thread=False with a
threading.Lock() around write operations.
"""

import concurrent.futures
import threading

import pytest

from multifleet.store import FleetNerveStore
from multifleet.task_state import TaskStateMachine, TaskState
from multifleet.truth_ladder import TruthLadder


# ── FleetNerveStore ──


@pytest.fixture
def store(tmp_path):
    return FleetNerveStore(db_path=tmp_path / "store.db")


class TestStoreThreadSafety:
    def test_has_lock(self, store):
        assert isinstance(store._lock, threading.Lock)

    def test_concurrent_store_messages(self, store):
        """50 threads writing messages concurrently — all must persist."""
        n = 50

        def write(i):
            store.store_message({
                "id": f"msg-{i}",
                "type": "context",
                "from": "mac1",
                "to": "mac2",
                "payload": {"subject": f"test-{i}", "body": "hello"},
            })

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(write, range(n)))

        # All messages must exist
        for i in range(n):
            assert store.get_message(f"msg-{i}") is not None

    def test_concurrent_wal_writes(self, store):
        """Concurrent WAL writes must each return a unique row ID."""
        n = 40
        ids = []
        lock = threading.Lock()

        def write(i):
            wal_id = store.wal_write(f"wal-{i}", f'{{"seq":{i}}}')
            with lock:
                ids.append(wal_id)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(write, range(n)))

        assert len(ids) == n
        assert len(set(ids)) == n  # all unique

    def test_concurrent_peer_upserts(self, store):
        """Multiple threads upserting the same peer — last write wins, no crash."""
        n = 30

        def upsert(i):
            store.upsert_peer("mac2", f"192.168.1.{i % 256}", 8855 + i)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(upsert, range(n)))

        peer = store.get_peer("mac2")
        assert peer is not None
        assert peer["status"] == "online"


# ── TruthLadder ──


@pytest.fixture
def ladder(tmp_path):
    return TruthLadder(db_path=str(tmp_path / "truth.db"))


class TestTruthLadderThreadSafety:
    def test_has_lock(self, ladder):
        assert isinstance(ladder._lock, threading.Lock)

    def test_concurrent_inbox_adds(self, ladder):
        """50 threads adding inbox entries — all must persist."""
        n = 50

        def add(i):
            ladder.add_inbox(f"Idea {i}", f"Content for idea {i}")

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(add, range(n)))

        entries = ladder.query(layer="inbox", limit=100)
        assert len(entries) == n

    def test_concurrent_promotions_no_double_promote(self, ladder):
        """Two threads trying to promote the same entry — only one should succeed."""
        entry = ladder.add_inbox("Race condition test", "Content")
        results = []
        lock = threading.Lock()

        def try_promote():
            try:
                result = ladder.promote_to_vision(entry.id, promoted_by="racer")
                with lock:
                    results.append(("ok", result.id))
            except ValueError as e:
                with lock:
                    results.append(("error", str(e)))

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(try_promote) for _ in range(2)]
            concurrent.futures.wait(futures)

        # Exactly one should succeed, one should fail (entry already promoted)
        ok_count = sum(1 for r in results if r[0] == "ok")
        err_count = sum(1 for r in results if r[0] == "error")
        assert ok_count == 1, f"Expected 1 success, got {ok_count}: {results}"
        assert err_count == 1, f"Expected 1 error, got {err_count}: {results}"


# ── TaskStateMachine ──


@pytest.fixture
def tsm(tmp_path):
    return TaskStateMachine(db_path=str(tmp_path / "tasks.db"))


class TestTaskStateThreadSafety:
    def test_has_lock(self, tsm):
        assert isinstance(tsm._lock, threading.Lock)

    def test_concurrent_task_creation(self, tsm):
        """30 threads creating tasks — all must persist with unique IDs."""
        n = 30
        task_ids = []
        lock = threading.Lock()

        def create(i):
            tid = tsm.create_task(f"mac{i % 3 + 1}", "mac1", {"seq": i})
            with lock:
                task_ids.append(tid)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(create, range(n)))

        assert len(task_ids) == n
        assert len(set(task_ids)) == n  # all unique
        for tid in task_ids:
            assert tsm.get(tid) is not None

    def test_concurrent_transitions_same_task(self, tsm):
        """Two threads racing to transition the same task — only one should win."""
        tid = tsm.create_task("mac2", "mac1", {})
        results = []
        lock = threading.Lock()

        def try_transition():
            ok = tsm.transition(tid, TaskState.ASSIGNED, "racer")
            with lock:
                results.append(ok)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(try_transition) for _ in range(2)]
            concurrent.futures.wait(futures)

        # Both should succeed because the transition created->assigned is valid,
        # and with locking only one runs at a time (both see 'created', transition to 'assigned').
        # Actually with the lock, the second thread sees 'assigned' and tries assigned->assigned
        # which is invalid. So exactly one True, one False.
        assert results.count(True) == 1, f"Expected exactly 1 success: {results}"
        assert results.count(False) == 1, f"Expected exactly 1 failure: {results}"

    def test_concurrent_creates_and_transitions(self, tsm):
        """Mixed create + transition workload under contention."""
        n = 20
        task_ids = []
        lock = threading.Lock()

        # Create tasks first
        for i in range(n):
            tid = tsm.create_task("mac2", "mac1", {"i": i})
            task_ids.append(tid)

        # Transition all concurrently
        def transition(tid):
            return tsm.transition(tid, TaskState.ASSIGNED, "batch")

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(transition, task_ids))

        assert all(results), "All transitions should succeed (different tasks)"
        for tid in task_ids:
            assert tsm.get(tid)["state"] == "assigned"
