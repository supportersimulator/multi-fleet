"""Tests for supervisor state persistence (SQLite-backed)."""

import os
import sys
import tempfile
import time
import pytest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from multifleet.supervisor import Supervisor, SupervisorPhase
from multifleet.supervisor_store import SupervisorStore, PersistentSupervisor


@pytest.fixture
def tmp_db():
    """Create a temporary database file for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    yield db_path
    # Cleanup
    for ext in ("", "-wal", "-shm"):
        p = Path(str(db_path) + ext)
        if p.exists():
            p.unlink()


class TestSupervisorStore:
    def test_init_creates_db(self, tmp_db):
        store = SupervisorStore(db_path=tmp_db)
        assert tmp_db.exists()

    def test_save_and_load(self, tmp_db):
        store = SupervisorStore(db_path=tmp_db)
        sup = Supervisor(node_id="test")
        state = sup.start_task("task-1", "Fix auth", ["tests_pass", "no_regressions"])
        store.save(state, node_id="test")

        loaded = store.load_active()
        assert len(loaded) == 1
        assert loaded[0].task_id == "task-1"
        assert loaded[0].phase == SupervisorPhase.INBOX_CAPTURE
        assert loaded[0].vision_contract.done_criteria == ["tests_pass", "no_regressions"]

    def test_save_after_advance(self, tmp_db):
        store = SupervisorStore(db_path=tmp_db)
        sup = Supervisor(node_id="test")
        state = sup.start_task("task-2", "Fix auth", ["done"])
        sup.advance("task-2")
        state = sup.get_state("task-2")
        store.save(state, node_id="test")

        loaded = store.load_task("task-2")
        assert loaded is not None
        assert loaded.phase == SupervisorPhase.BRAINSTORMING

    def test_terminal_not_loaded_as_active(self, tmp_db):
        store = SupervisorStore(db_path=tmp_db)
        sup = Supervisor(node_id="test")
        state = sup.start_task("task-3", "Done task", ["done"])
        # Advance to complete by marking criteria met
        for _ in range(11):  # INBOX -> ... -> COMPLETE
            state = sup.advance("task-3", {"criteria_satisfied": ["done"]})
            if state.is_terminal:
                break
        store.save(state, node_id="test")

        active = store.load_active()
        assert not any(s.task_id == "task-3" for s in active)

    def test_load_all(self, tmp_db):
        store = SupervisorStore(db_path=tmp_db)
        sup = Supervisor(node_id="test")

        state1 = sup.start_task("task-4a", "Task A", ["done"])
        store.save(state1, node_id="test")

        state2 = sup.start_task("task-4b", "Task B", ["done"])
        store.save(state2, node_id="test")

        all_tasks = store.load_all()
        assert len(all_tasks) == 2

    def test_delete_task(self, tmp_db):
        store = SupervisorStore(db_path=tmp_db)
        sup = Supervisor(node_id="test")
        state = sup.start_task("task-5", "Delete me", ["done"])
        store.save(state, node_id="test")

        assert store.delete_task("task-5")
        assert store.load_task("task-5") is None

    def test_cleanup_old(self, tmp_db):
        store = SupervisorStore(db_path=tmp_db)
        sup = Supervisor(node_id="test")
        state = sup.start_task("old-task", "Old", ["done"])
        sup.block("old-task", "test")
        state = sup.get_state("old-task")
        store.save(state, node_id="test")

        # Manually set updated_at to 10 days ago
        import sqlite3
        conn = sqlite3.connect(str(tmp_db))
        conn.execute(
            "UPDATE supervisor_tasks SET updated_at = ? WHERE task_id = ?",
            (time.time() - 10 * 86400, "old-task"),
        )
        conn.commit()
        conn.close()

        removed = store.cleanup_old(max_age_hours=24)
        assert removed == 1

    def test_stats(self, tmp_db):
        store = SupervisorStore(db_path=tmp_db)
        s = store.stats()
        assert s["total"] == 0
        assert s["active"] == 0
        assert "db_path" in s

    def test_criteria_met_persisted(self, tmp_db):
        store = SupervisorStore(db_path=tmp_db)
        sup = Supervisor(node_id="test")
        state = sup.start_task("task-6", "With criteria", ["a", "b"])
        sup.advance("task-6", {"criteria_satisfied": ["a"]})
        state = sup.get_state("task-6")
        store.save(state, node_id="test")

        loaded = store.load_task("task-6")
        assert "a" in loaded.criteria_met

    def test_history_persisted(self, tmp_db):
        store = SupervisorStore(db_path=tmp_db)
        sup = Supervisor(node_id="test")
        state = sup.start_task("task-7", "History test", ["done"])
        sup.advance("task-7")
        sup.advance("task-7")
        state = sup.get_state("task-7")
        store.save(state, node_id="test")

        loaded = store.load_task("task-7")
        assert len(loaded.history) >= 2


class TestPersistentSupervisor:
    def test_start_and_advance(self, tmp_db):
        sup = PersistentSupervisor(node_id="test", db_path=tmp_db)
        state = sup.start_task("ps-1", "Persistent task", ["done"])
        assert state.task_id == "ps-1"

        state = sup.advance("ps-1")
        assert state.phase == SupervisorPhase.BRAINSTORMING

        # Verify persisted
        store = SupervisorStore(db_path=tmp_db)
        loaded = store.load_task("ps-1")
        assert loaded is not None
        assert loaded.phase == SupervisorPhase.BRAINSTORMING

    def test_restore_from_db(self, tmp_db):
        # Create and save a task
        sup1 = PersistentSupervisor(node_id="test", db_path=tmp_db)
        sup1.start_task("ps-2", "Restore test", ["done"])
        sup1.advance("ps-2")

        # Create a NEW supervisor (simulating restart)
        sup2 = PersistentSupervisor(node_id="test", db_path=tmp_db)
        state = sup2.get_state("ps-2")
        assert state.phase == SupervisorPhase.BRAINSTORMING
        assert state.task_id == "ps-2"

    def test_block_persists(self, tmp_db):
        sup = PersistentSupervisor(node_id="test", db_path=tmp_db)
        sup.start_task("ps-3", "Block test", ["done"])
        sup.block("ps-3", "test reason")

        store = SupervisorStore(db_path=tmp_db)
        loaded = store.load_task("ps-3")
        assert loaded.phase == SupervisorPhase.BLOCKED
        assert loaded.blocked_reason == "test reason"

    def test_list_active(self, tmp_db):
        sup = PersistentSupervisor(node_id="test", db_path=tmp_db)
        sup.start_task("ps-4a", "Task A", ["done"])
        sup.start_task("ps-4b", "Task B", ["done"])
        active = sup.list_active()
        assert len(active) == 2

    def test_is_done(self, tmp_db):
        sup = PersistentSupervisor(node_id="test", db_path=tmp_db)
        sup.start_task("ps-5", "Done test", ["criterion_a"])
        assert not sup.is_done("ps-5")

    def test_stats(self, tmp_db):
        sup = PersistentSupervisor(node_id="test", db_path=tmp_db)
        s = sup.stats
        assert "total" in s
        assert "active" in s

    def test_duplicate_task_raises(self, tmp_db):
        sup = PersistentSupervisor(node_id="test", db_path=tmp_db)
        sup.start_task("ps-dup", "Dup test", ["done"])
        with pytest.raises(ValueError, match="already exists"):
            sup.start_task("ps-dup", "Dup again", ["done"])

    def test_get_state_from_db_fallback(self, tmp_db):
        # Save a task using store directly
        store = SupervisorStore(db_path=tmp_db)
        raw_sup = Supervisor(node_id="test")
        state = raw_sup.start_task("ps-fallback", "Fallback test", ["done"])
        store.save(state, node_id="test")

        # PersistentSupervisor should find it (restored from DB on init)
        sup = PersistentSupervisor(node_id="test", db_path=tmp_db)
        loaded = sup.get_state("ps-fallback")
        assert loaded.task_id == "ps-fallback"
