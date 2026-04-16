"""
Durable task state machine for fleet orchestration.

Transport-layer only — tracks delivery and task lifecycle, not application logic.
Payload is opaque JSON. States: created → assigned → in_progress → completed/failed.
Lease expiry → stale → retry or expire. Full audit trail in task_audit table.

Usage:
    from multifleet.task_state import TaskStateMachine, TaskState
    tsm = TaskStateMachine(db_path)
    task_id = tsm.create_task("mac2", "mac1", {"prompt": "Fix auth"})
    tsm.transition(task_id, TaskState.ASSIGNED, "dispatched")
    tsm.transition(task_id, TaskState.IN_PROGRESS, "agent started")
    tsm.transition(task_id, TaskState.COMPLETED, "done")
"""

import json
import logging
import sqlite3
import threading
import time
import uuid
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger("fleet-nerve.task-state")

DEFAULT_DB = Path.home() / ".fleet-nerve" / "messages.db"


class TaskState(str, Enum):
    CREATED = "created"
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    STALE = "stale"
    EXPIRED = "expired"


TERMINAL_STATES = {TaskState.COMPLETED, TaskState.FAILED, TaskState.EXPIRED}

TRANSITIONS = {
    TaskState.CREATED: {TaskState.ASSIGNED, TaskState.EXPIRED},
    TaskState.ASSIGNED: {TaskState.IN_PROGRESS, TaskState.FAILED, TaskState.EXPIRED},
    TaskState.IN_PROGRESS: {TaskState.COMPLETED, TaskState.FAILED, TaskState.STALE},
    TaskState.STALE: {TaskState.ASSIGNED, TaskState.EXPIRED},
}

BACKOFF_BASE_S = 5  # retry delay = 5 * 2^retries, capped at 120s


class TaskStateMachine:
    """Durable task state machine backed by SQLite."""

    def __init__(self, db_path: str = None):
        self.db_path = str(db_path or DEFAULT_DB)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_tables()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_tables(self):
        conn = self._conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS task_states (
                id          TEXT PRIMARY KEY,
                chain_id    TEXT,
                chain_step  INTEGER,
                node_id     TEXT NOT NULL,
                origin_node TEXT NOT NULL,
                state       TEXT NOT NULL DEFAULT 'created',
                payload     TEXT NOT NULL DEFAULT '{}',
                lease_s     INTEGER NOT NULL DEFAULT 300,
                retries     INTEGER NOT NULL DEFAULT 0,
                max_retries INTEGER NOT NULL DEFAULT 3,
                created_at  REAL NOT NULL,
                updated_at  REAL NOT NULL,
                leased_at   REAL,
                completed_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_ts_state ON task_states(state);
            CREATE INDEX IF NOT EXISTS idx_ts_chain ON task_states(chain_id, chain_step);
            CREATE INDEX IF NOT EXISTS idx_ts_node  ON task_states(node_id, state);

            CREATE TABLE IF NOT EXISTS task_audit (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id     TEXT NOT NULL,
                old_state   TEXT,
                new_state   TEXT NOT NULL,
                reason      TEXT,
                timestamp   REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ta_task ON task_audit(task_id);
        """)
        conn.close()

    def create_task(self, node_id: str, origin_node: str, payload: dict,
                    chain_id: str = None, chain_step: int = None,
                    lease_s: int = 300, max_retries: int = 3) -> str:
        """Create a new task. Returns task_id."""
        task_id = str(uuid.uuid4())
        now = time.time()
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    """INSERT INTO task_states
                       (id, chain_id, chain_step, node_id, origin_node, state, payload,
                        lease_s, max_retries, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, 'created', ?, ?, ?, ?, ?)""",
                    (task_id, chain_id, chain_step, node_id, origin_node,
                     json.dumps(payload), lease_s, max_retries, now, now),
                )
                conn.execute(
                    "INSERT INTO task_audit (task_id, old_state, new_state, reason, timestamp) "
                    "VALUES (?, NULL, 'created', 'new', ?)",
                    (task_id, now),
                )
                conn.commit()
            finally:
                conn.close()
        logger.info("Task %s created: %s → %s", task_id[:8], origin_node, node_id)
        return task_id

    def transition(self, task_id: str, new_state: TaskState, reason: str = "") -> bool:
        """Transition a task to a new state. Returns True on success."""
        now = time.time()
        with self._lock:
            conn = self._conn()
            try:
                row = conn.execute(
                    "SELECT state, retries, max_retries FROM task_states WHERE id = ?",
                    (task_id,)
                ).fetchone()
                if not row:
                    logger.warning("Task %s not found for transition", task_id[:8])
                    return False

                old = TaskState(row["state"])
                allowed = TRANSITIONS.get(old, set())
                if new_state not in allowed:
                    logger.warning("Invalid transition %s: %s → %s", task_id[:8], old.value, new_state.value)
                    return False

                updates = {"state": new_state.value, "updated_at": now}
                if new_state == TaskState.IN_PROGRESS:
                    updates["leased_at"] = now
                elif new_state in TERMINAL_STATES:
                    updates["completed_at"] = now
                elif new_state == TaskState.ASSIGNED and old == TaskState.STALE:
                    updates["retries"] = row["retries"] + 1

                set_clause = ", ".join(f"{k} = ?" for k in updates)
                vals = list(updates.values()) + [task_id]
                conn.execute(f"UPDATE task_states SET {set_clause} WHERE id = ?", vals)
                conn.execute(
                    "INSERT INTO task_audit (task_id, old_state, new_state, reason, timestamp) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (task_id, old.value, new_state.value, reason, now),
                )
                conn.commit()
            finally:
                conn.close()

        logger.info("Task %s: %s → %s (%s)", task_id[:8], old.value, new_state.value, reason)
        return True

    def get(self, task_id: str) -> Optional[dict]:
        """Get task by ID."""
        conn = self._conn()
        try:
            row = conn.execute("SELECT * FROM task_states WHERE id = ?", (task_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list_active(self, node_id: str = None) -> list[dict]:
        """List non-terminal tasks, optionally filtered by node."""
        conn = self._conn()
        try:
            if node_id:
                rows = conn.execute(
                    "SELECT * FROM task_states WHERE state NOT IN ('completed','failed','expired') "
                    "AND node_id = ? ORDER BY created_at",
                    (node_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM task_states WHERE state NOT IN ('completed','failed','expired') "
                    "ORDER BY created_at"
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def audit_log(self, task_id: str) -> list[dict]:
        """Get full audit trail for a task."""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM task_audit WHERE task_id = ? ORDER BY timestamp",
                (task_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def resume_stale(self, timeout_margin_s: float = 0) -> list[str]:
        """Scan for lease-expired tasks, mark stale, reassign retriable ones.

        Returns list of task_ids that were reassigned.
        """
        now = time.time()
        reassigned = []
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT id, retries, max_retries FROM task_states "
                "WHERE state = 'in_progress' AND leased_at IS NOT NULL "
                "AND leased_at + lease_s + ? < ?",
                (timeout_margin_s, now),
            ).fetchall()
        finally:
            conn.close()

        for r in rows:
            task_id = r["id"]
            self.transition(task_id, TaskState.STALE, "lease_expired")
            if r["retries"] < r["max_retries"]:
                self.transition(task_id, TaskState.ASSIGNED, f"retry_{r['retries'] + 1}")
                reassigned.append(task_id)
            else:
                self.transition(task_id, TaskState.EXPIRED, "max_retries_exhausted")

        if reassigned:
            logger.info("Resumed %d stale tasks", len(reassigned))
        return reassigned

    def expire_old_created(self, max_age_s: float = 3600) -> int:
        """Expire created/assigned tasks older than max_age_s. For daemon startup recovery."""
        now = time.time()
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT id FROM task_states WHERE state IN ('created','assigned') "
                "AND created_at < ?",
                (now - max_age_s,),
            ).fetchall()
        finally:
            conn.close()

        count = 0
        for r in rows:
            if self.transition(r["id"], TaskState.EXPIRED, "startup_cleanup"):
                count += 1
        return count

    @staticmethod
    def next_retry_delay(retries: int) -> float:
        """Exponential backoff: 5s, 10s, 20s, 40s, 80s, 120s (cap)."""
        return min(BACKOFF_BASE_S * (2 ** retries), 120.0)

    def stats(self) -> dict:
        """Get task state distribution."""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT state, COUNT(*) as cnt FROM task_states GROUP BY state"
            ).fetchall()
            return {r["state"]: r["cnt"] for r in rows}
        finally:
            conn.close()
