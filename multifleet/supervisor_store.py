"""Supervisor State Persistence — SQLite-backed supervisor state.

The supervisor loses all task state when the daemon restarts because
it stores everything in memory. This module adds durable persistence
so long-running tasks survive daemon crashes and restarts.

State is written on every phase transition and restored on startup.
"""

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Optional

from multifleet.supervisor import Supervisor, SupervisorState, SupervisorPhase, VisionContract

logger = logging.getLogger("fleet-nerve.supervisor-store")

from ._paths import REPO_ROOT

DB_PATH = REPO_ROOT / ".contextdna" / "local" / "supervisor.db"


class SupervisorStore:
    """SQLite-backed persistence for Supervisor state.

    Usage:
        store = SupervisorStore()

        # Save state after each transition
        store.save(state)

        # Restore on startup
        states = store.load_active()
        for state in states:
            supervisor.restore(state)

        # Wrap a Supervisor with auto-save
        persistent_sup = store.wrap(supervisor)
    """

    def __init__(self, db_path: Path = DB_PATH):
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with self._conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS supervisor_tasks (
                    task_id TEXT PRIMARY KEY,
                    phase TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    done_criteria TEXT DEFAULT '[]',
                    criteria_met TEXT DEFAULT '[]',
                    history TEXT DEFAULT '[]',
                    blocked_reason TEXT DEFAULT '',
                    iteration_count INTEGER DEFAULT 0,
                    max_iterations INTEGER DEFAULT 20,
                    started_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_supervisor_phase
                ON supervisor_tasks(phase)
            """)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def save(self, state: SupervisorState, node_id: str = "") -> None:
        """Save a supervisor state to the database.

        Called after every phase transition for durability.
        """
        contract = state.vision_contract
        done_criteria = json.dumps(contract.done_criteria) if contract else "[]"
        description = contract.description if contract else ""

        with self._conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO supervisor_tasks
                (task_id, phase, node_id, description, done_criteria,
                 criteria_met, history, blocked_reason, iteration_count,
                 max_iterations, started_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                state.task_id,
                state.phase.value,
                node_id,
                description,
                done_criteria,
                json.dumps(state.criteria_met),
                json.dumps(state.history[-20:]),  # Keep last 20 transitions
                state.blocked_reason,
                state.iteration_count,
                state.max_iterations,
                state.started_at,
                time.time(),
            ))

        logger.debug("supervisor_store saved task=%s phase=%s", state.task_id[:8], state.phase.value)

    def load_active(self) -> list[SupervisorState]:
        """Load all non-terminal tasks from the database.

        Returns SupervisorState objects that can be restored into a Supervisor.
        """
        states = []
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT * FROM supervisor_tasks
                WHERE phase NOT IN ('complete', 'blocked')
                ORDER BY updated_at DESC
            """).fetchall()

        for row in rows:
            try:
                state = self._row_to_state(row)
                states.append(state)
            except Exception as e:
                logger.warning("Failed to load task %s: %s", row["task_id"][:8], e)

        logger.info("supervisor_store loaded %d active task(s)", len(states))
        return states

    def load_all(self, limit: int = 50) -> list[SupervisorState]:
        """Load all tasks (including terminal) from the database."""
        states = []
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT * FROM supervisor_tasks
                ORDER BY updated_at DESC
                LIMIT ?
            """, (limit,)).fetchall()

        for row in rows:
            try:
                states.append(self._row_to_state(row))
            except Exception as e:
                logger.warning("Failed to load task %s: %s", row["task_id"][:8], e)

        return states

    def load_task(self, task_id: str) -> Optional[SupervisorState]:
        """Load a specific task by ID."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM supervisor_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()

        if not row:
            return None
        return self._row_to_state(row)

    def delete_task(self, task_id: str) -> bool:
        """Remove a task from persistence."""
        with self._conn() as conn:
            cursor = conn.execute(
                "DELETE FROM supervisor_tasks WHERE task_id = ?", (task_id,)
            )
        return cursor.rowcount > 0

    def cleanup_old(self, max_age_hours: int = 168) -> int:
        """Remove tasks older than max_age_hours. Default: 7 days."""
        cutoff = time.time() - (max_age_hours * 3600)
        with self._conn() as conn:
            cursor = conn.execute(
                "DELETE FROM supervisor_tasks WHERE updated_at < ? AND phase IN ('complete', 'blocked')",
                (cutoff,),
            )
        removed = cursor.rowcount
        if removed:
            logger.info("supervisor_store cleaned %d old task(s)", removed)
        return removed

    def stats(self) -> dict:
        """Get storage statistics."""
        with self._conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM supervisor_tasks").fetchone()[0]
            active = conn.execute(
                "SELECT COUNT(*) FROM supervisor_tasks WHERE phase NOT IN ('complete', 'blocked')"
            ).fetchone()[0]
            completed = conn.execute(
                "SELECT COUNT(*) FROM supervisor_tasks WHERE phase = 'complete'"
            ).fetchone()[0]
            blocked = conn.execute(
                "SELECT COUNT(*) FROM supervisor_tasks WHERE phase = 'blocked'"
            ).fetchone()[0]

        return {
            "total": total,
            "active": active,
            "completed": completed,
            "blocked": blocked,
            "db_path": str(self._db_path),
        }

    def _row_to_state(self, row) -> SupervisorState:
        """Convert a database row to SupervisorState."""
        done_criteria = json.loads(row["done_criteria"]) if row["done_criteria"] else []
        contract = VisionContract(
            task_id=row["task_id"],
            description=row["description"] or "",
            done_criteria=done_criteria,
        )

        return SupervisorState(
            task_id=row["task_id"],
            phase=SupervisorPhase(row["phase"]),
            vision_contract=contract,
            history=json.loads(row["history"]) if row["history"] else [],
            blocked_reason=row["blocked_reason"] or "",
            iteration_count=row["iteration_count"],
            max_iterations=row["max_iterations"],
            started_at=row["started_at"],
            criteria_met=json.loads(row["criteria_met"]) if row["criteria_met"] else [],
        )


class PersistentSupervisor:
    """Wraps a Supervisor with automatic SQLite persistence.

    All state changes are automatically saved. On init, restores
    any active tasks from the database.

    Usage:
        sup = PersistentSupervisor(node_id="mac1")
        # Automatically restores any in-flight tasks from last run

        state = sup.start_task("fix-auth", "Fix auth", ["tests pass"])
        state = sup.advance("fix-auth", {"criteria_satisfied": ["tests pass"]})
        # Each call auto-saves to SQLite
    """

    def __init__(self, node_id: str, db_path: Path = DB_PATH):
        self._node_id = node_id
        self._store = SupervisorStore(db_path)
        self._supervisor = Supervisor(node_id)
        self._restore_from_db()

    def _restore_from_db(self):
        """Restore active tasks from database into memory."""
        active_states = self._store.load_active()
        restored = 0
        for state in active_states:
            try:
                # Directly inject state into supervisor's internal dict
                self._supervisor._tasks[state.task_id] = state
                restored += 1
            except Exception as e:
                logger.warning("Failed to restore task %s: %s", state.task_id[:8], e)

        if restored:
            logger.info(
                "persistent_supervisor restored %d task(s) from db", restored,
            )

    def start_task(self, task_id: str, description: str,
                   done_criteria: list[str]) -> SupervisorState:
        """Start a new supervised task with persistence."""
        state = self._supervisor.start_task(task_id, description, done_criteria)
        self._store.save(state, self._node_id)
        return state

    def advance(self, task_id: str, evidence: dict = None) -> SupervisorState:
        """Advance task phase with auto-save."""
        state = self._supervisor.advance(task_id, evidence)
        self._store.save(state, self._node_id)
        return state

    def block(self, task_id: str, reason: str) -> SupervisorState:
        """Block task with auto-save."""
        state = self._supervisor.block(task_id, reason)
        self._store.save(state, self._node_id)
        return state

    def get_state(self, task_id: str) -> SupervisorState:
        """Get current state (memory-first, db-fallback)."""
        try:
            return self._supervisor.get_state(task_id)
        except ValueError:
            # Try loading from DB
            state = self._store.load_task(task_id)
            if state:
                self._supervisor._tasks[task_id] = state
                return state
            raise

    def list_active(self) -> list[SupervisorState]:
        return self._supervisor.list_active()

    def list_all(self) -> list[SupervisorState]:
        return self._supervisor.list_all()

    def is_done(self, task_id: str) -> bool:
        return self._supervisor.is_done(task_id)

    @property
    def store(self) -> SupervisorStore:
        return self._store

    @property
    def stats(self) -> dict:
        return self._store.stats()
