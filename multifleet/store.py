"""
Fleet Nerve Store — SQLite-backed message persistence for the Fleet Nerve daemon.

Tables: messages (inbox/history), peers (node registry), outbox (retry queue).
Pattern follows multifleet_packet_store.py — proven in production.
"""

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_DB = Path.home() / ".fleet-nerve" / "messages.db"


class FleetNerveStore:
    def __init__(self, db_path=None):
        self.db_path = str(db_path or DEFAULT_DB)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._cached_conn = None
        self._init_db()

    def _conn(self):
        if self._cached_conn is not None:
            return self._cached_conn
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        self._cached_conn = conn
        return conn

    def _migrate_ack_status(self, conn):
        """Migration-safe: add ack_status column if missing."""
        cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
        if "ack_status" not in cols:
            conn.execute("ALTER TABLE messages ADD COLUMN ack_status TEXT DEFAULT 'none'")
        if "retry_count" not in cols:
            conn.execute("ALTER TABLE messages ADD COLUMN retry_count INTEGER DEFAULT 0")
        if "last_retry_at" not in cols:
            conn.execute("ALTER TABLE messages ADD COLUMN last_retry_at REAL")

    def _init_db(self):
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS wal (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    msg_id TEXT NOT NULL,
                    raw_json TEXT NOT NULL,
                    received_at REAL NOT NULL,
                    processed INTEGER DEFAULT 0,
                    processed_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_wal_unprocessed ON wal(processed, received_at);

                CREATE TABLE IF NOT EXISTS wal_replicas (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    msg_id TEXT NOT NULL,
                    raw_json TEXT NOT NULL,
                    source_node TEXT NOT NULL,
                    target_node TEXT,
                    received_at REAL NOT NULL,
                    delivered INTEGER DEFAULT 0,
                    delivered_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_wal_replicas_target ON wal_replicas(target_node, delivered);

                CREATE TABLE IF NOT EXISTS work_items (
                    id TEXT PRIMARY KEY,
                    node_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT DEFAULT 'in_progress',
                    priority INTEGER DEFAULT 2,
                    started_at REAL NOT NULL,
                    updated_at REAL,
                    completed_at REAL,
                    result_summary TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_work_node_status ON work_items(node_id, status);

                CREATE TABLE IF NOT EXISTS task_runs (
                    id TEXT PRIMARY KEY,
                    message_id TEXT,
                    node_id TEXT NOT NULL,
                    sender TEXT,
                    subject TEXT,
                    status TEXT DEFAULT 'pending',
                    pid INTEGER,
                    spawned_at REAL,
                    updated_at REAL,
                    completed_at REAL,
                    exit_code INTEGER,
                    result_summary TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_task_runs_status ON task_runs(node_id, status);

                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    type TEXT NOT NULL,
                    from_node TEXT NOT NULL,
                    to_node TEXT,
                    ref TEXT,
                    seq INTEGER,
                    priority INTEGER DEFAULT 2,
                    subject TEXT,
                    body TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at REAL,
                    delivered_at REAL,
                    ttl_hours INTEGER DEFAULT 24
                );
                CREATE INDEX IF NOT EXISTS idx_msg_to_status ON messages(to_node, status);
                CREATE INDEX IF NOT EXISTS idx_msg_type ON messages(type);

                CREATE TABLE IF NOT EXISTS peers (
                    node_id TEXT PRIMARY KEY,
                    ip TEXT,
                    port INTEGER DEFAULT 8855,
                    mac_address TEXT,
                    last_heartbeat REAL,
                    last_seq INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'unknown'
                );

                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT NOT NULL,
                    target_node TEXT NOT NULL,
                    attempts INTEGER DEFAULT 0,
                    next_retry REAL,
                    FOREIGN KEY (message_id) REFERENCES messages(id)
                );
                CREATE INDEX IF NOT EXISTS idx_outbox_retry ON outbox(next_retry);
            """)
            self._migrate_ack_status(conn)

    def store_message(self, msg: dict):
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO messages
                   (id, type, from_node, to_node, ref, seq, priority, subject, body, status, created_at, ttl_hours)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                (
                    msg["id"],
                    msg["type"],
                    msg.get("from", "unknown"),
                    msg.get("to"),
                    msg.get("ref"),
                    msg.get("seq"),
                    msg.get("priority", 2),
                    msg.get("payload", {}).get("subject", ""),
                    json.dumps(msg.get("payload", {})),
                    time.time(),
                    msg.get("ttl_hours", 24),
                ),
            )

    def get_message(self, msg_id: str) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM messages WHERE id = ?", (msg_id,)).fetchone()
            return dict(row) if row else None

    def pending_for_node(self, node_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM messages WHERE to_node = ? AND status = 'pending' ORDER BY created_at",
                (node_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def mark_delivered(self, msg_id: str):
        with self._conn() as conn:
            conn.execute(
                "UPDATE messages SET status = 'delivered', delivered_at = ? WHERE id = ?",
                (time.time(), msg_id),
            )

    # ── ACK tracking ──

    def update_ack_status(self, msg_id: str, status: str):
        """Update ACK status for a sent message. Status: none|awaiting|delivered|processed|failed."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE messages SET ack_status = ? WHERE id = ?",
                (status, msg_id),
            )

    def get_ack_status(self, msg_id: str) -> Optional[dict]:
        """Get ACK status for a message. Returns {id, type, from_node, to_node, ack_status, retry_count} or None."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id, type, from_node, to_node, ack_status, retry_count, created_at, last_retry_at "
                "FROM messages WHERE id = ?",
                (msg_id,),
            ).fetchone()
            return dict(row) if row else None

    def get_awaiting_ack(self, max_age_s: float = 60.0, max_retries: int = 3) -> list[dict]:
        """Get messages awaiting ACK that have exceeded the timeout and haven't exhausted retries."""
        cutoff = time.time() - max_age_s
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT id, type, from_node, to_node, body, retry_count, created_at, last_retry_at
                   FROM messages
                   WHERE ack_status = 'awaiting'
                     AND retry_count < ?
                     AND (last_retry_at IS NULL AND created_at < ? OR last_retry_at < ?)
                   ORDER BY created_at""",
                (max_retries, cutoff, cutoff),
            ).fetchall()
            return [dict(r) for r in rows]

    def bump_retry(self, msg_id: str):
        """Increment retry count and update last_retry_at."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE messages SET retry_count = retry_count + 1, last_retry_at = ? WHERE id = ?",
                (time.time(), msg_id),
            )

    def mark_ack_failed(self, msg_id: str):
        """Mark message as failed after exhausting retries."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE messages SET ack_status = 'failed', status = 'failed' WHERE id = ?",
                (msg_id,),
            )

    def upsert_peer(self, node_id: str, ip: str, port: int = 8855, mac_address: str = None):
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO peers (node_id, ip, port, mac_address, last_heartbeat, status)
                   VALUES (?, ?, ?, ?, ?, 'online')
                   ON CONFLICT(node_id) DO UPDATE SET ip=?, port=?, mac_address=COALESCE(?, mac_address), status='online'""",
                (node_id, ip, port, mac_address, time.time(), ip, port, mac_address),
            )

    def get_peer(self, node_id: str) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM peers WHERE node_id = ?", (node_id,)).fetchone()
            return dict(row) if row else None

    def get_peers(self) -> list[dict]:
        with self._conn() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM peers ORDER BY node_id").fetchall()]

    def update_heartbeat(self, node_id: str, seq: int = 0):
        with self._conn() as conn:
            conn.execute(
                "UPDATE peers SET last_heartbeat = ?, last_seq = ?, status = 'online' WHERE node_id = ?",
                (time.time(), seq, node_id),
            )

    def queue_outbox(self, message_id: str, target_node: str):
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO outbox (message_id, target_node, attempts, next_retry) VALUES (?, ?, 0, ?)",
                (message_id, target_node, time.time()),
            )

    def pending_outbox(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM outbox WHERE next_retry <= ? ORDER BY next_retry", (time.time(),)
            ).fetchall()
            return [dict(r) for r in rows]

    def remove_outbox(self, outbox_id: int):
        with self._conn() as conn:
            conn.execute("DELETE FROM outbox WHERE id = ?", (outbox_id,))

    def bump_outbox_retry(self, outbox_id: int):
        with self._conn() as conn:
            conn.execute(
                "UPDATE outbox SET attempts = attempts + 1, next_retry = ? + (attempts * 30) WHERE id = ?",
                (time.time(), outbox_id),
            )

    # ── Task run lifecycle tracking ──

    def task_run_start(self, task_id: str, message_id: str, node_id: str,
                       sender: str, subject: str, pid: int = 0):
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO task_runs
                   (id, message_id, node_id, sender, subject, status, pid, spawned_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?)""",
                (task_id, message_id, node_id, sender, subject, pid, time.time(), time.time()),
            )

    def task_run_complete(self, task_id: str, exit_code: int = 0, result_summary: str = None):
        status = "completed" if exit_code == 0 else "failed"
        with self._conn() as conn:
            conn.execute(
                "UPDATE task_runs SET status=?, exit_code=?, completed_at=?, updated_at=?, result_summary=? WHERE id=?",
                (status, exit_code, time.time(), time.time(), result_summary, task_id),
            )

    def task_run_timeout(self, task_id: str):
        with self._conn() as conn:
            conn.execute(
                "UPDATE task_runs SET status='timeout', completed_at=?, updated_at=? WHERE id=?",
                (time.time(), time.time(), task_id),
            )

    def task_runs_active(self, node_id: str = None) -> list[dict]:
        with self._conn() as conn:
            if node_id:
                rows = conn.execute(
                    "SELECT * FROM task_runs WHERE node_id=? AND status='running' ORDER BY spawned_at", (node_id,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM task_runs WHERE status='running' ORDER BY spawned_at"
                ).fetchall()
            return [dict(r) for r in rows]

    def task_runs_recent(self, hours: int = 24) -> list[dict]:
        cutoff = time.time() - (hours * 3600)
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM task_runs WHERE spawned_at > ? ORDER BY spawned_at DESC", (cutoff,)
            ).fetchall()
            return [dict(r) for r in rows]

    def task_runs_check_timeouts(self, timeout_s: int = 600):
        """Mark running tasks older than timeout_s as timed out."""
        cutoff = time.time() - timeout_s
        with self._conn() as conn:
            conn.execute(
                "UPDATE task_runs SET status='timeout', completed_at=?, updated_at=? WHERE status='running' AND spawned_at < ?",
                (time.time(), time.time(), cutoff),
            )

    # ── Stats persistence ──

    def save_stats(self, node_id: str, stats: dict):
        """Persist daemon stats to DB — survives restart."""
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO messages
                   (id, type, from_node, to_node, subject, body, status, created_at, ttl_hours)
                   VALUES (?, 'stats', ?, NULL, 'daemon_stats', ?, 'delivered', ?, 8760)""",
                (f"stats-{node_id}", node_id, json.dumps(stats), time.time()),
            )

    def load_stats(self, node_id: str) -> dict:
        """Load persisted stats from DB."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT body FROM messages WHERE id = ?", (f"stats-{node_id}",)
            ).fetchone()
            if row:
                try:
                    return json.loads(row[0])
                except Exception as e:
                    logger.warning(f"Failed to parse stored stats for {node_id}: {e}")
            return {}

    # ── WAL: Write-Ahead Log — durable before processing ──

    def wal_write(self, msg_id: str, raw_json: str) -> int:
        """Write message to WAL BEFORE processing. Returns WAL row ID."""
        with self._conn() as conn:
            cursor = conn.execute(
                "INSERT INTO wal (msg_id, raw_json, received_at) VALUES (?, ?, ?)",
                (msg_id, raw_json, time.time()),
            )
            return cursor.lastrowid

    def wal_mark_processed(self, wal_id: int):
        """Mark WAL entry as processed after successful routing."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE wal SET processed = 1, processed_at = ? WHERE id = ?",
                (time.time(), wal_id),
            )

    def wal_unprocessed(self) -> list[dict]:
        """Get all unprocessed WAL entries (for replay after crash)."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, msg_id, raw_json, received_at FROM wal WHERE processed = 0 ORDER BY received_at"
            ).fetchall()
            return [{"wal_id": r[0], "msg_id": r[1], "raw_json": r[2], "received_at": r[3]} for r in rows]

    # ── WAL Replication: cross-device durability ──

    def wal_replica_write(self, msg_id: str, raw_json: str, source_node: str, target_node: str) -> int:
        """Store a WAL replica from another node. Used by chief as durable relay."""
        with self._conn() as conn:
            cursor = conn.execute(
                "INSERT INTO wal_replicas (msg_id, raw_json, source_node, target_node, received_at) VALUES (?, ?, ?, ?, ?)",
                (msg_id, raw_json, source_node, target_node, time.time()),
            )
            return cursor.lastrowid

    def wal_replicas_for_node(self, target_node: str) -> list[dict]:
        """Get undelivered replicas destined for a specific node."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, msg_id, raw_json, source_node, received_at FROM wal_replicas WHERE target_node = ? AND delivered = 0 ORDER BY received_at",
                (target_node,),
            ).fetchall()
            return [{"replica_id": r[0], "msg_id": r[1], "raw_json": r[2], "source_node": r[3], "received_at": r[4]} for r in rows]

    def wal_replica_mark_delivered(self, replica_id: int):
        """Mark a WAL replica as delivered to its target."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE wal_replicas SET delivered = 1, delivered_at = ? WHERE id = ?",
                (time.time(), replica_id),
            )

    def wal_replica_prune(self, max_age_hours: int = 48):
        """Remove delivered replicas older than max_age_hours."""
        cutoff = time.time() - (max_age_hours * 3600)
        with self._conn() as conn:
            conn.execute("DELETE FROM wal_replicas WHERE delivered = 1 AND delivered_at < ?", (cutoff,))

    # ── Work coordination: track what each node is working on ──

    def work_start(self, work_id: str, node_id: str, title: str, priority: int = 2):
        """Register that a node started working on something."""
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO work_items (id, node_id, title, status, priority, started_at, updated_at)
                   VALUES (?, ?, ?, 'in_progress', ?, ?, ?)""",
                (work_id, node_id, title, priority, time.time(), time.time()),
            )

    def work_update(self, work_id: str, status: str = "in_progress", result_summary: str = None):
        """Update progress on a work item."""
        with self._conn() as conn:
            if status in ("completed", "failed", "done"):
                conn.execute(
                    "UPDATE work_items SET status=?, updated_at=?, completed_at=?, result_summary=? WHERE id=?",
                    (status, time.time(), time.time(), result_summary, work_id),
                )
            else:
                conn.execute(
                    "UPDATE work_items SET status=?, updated_at=? WHERE id=?",
                    (status, time.time(), work_id),
                )

    def work_active(self, node_id: str = None) -> list[dict]:
        """Get active work items, optionally filtered by node."""
        with self._conn() as conn:
            if node_id:
                rows = conn.execute(
                    "SELECT * FROM work_items WHERE node_id=? AND status='in_progress' ORDER BY priority, started_at",
                    (node_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM work_items WHERE status='in_progress' ORDER BY priority, started_at"
                ).fetchall()
            return [dict(r) for r in rows]

    def work_all_recent(self, hours: int = 24) -> list[dict]:
        """Get all work items from the last N hours."""
        cutoff = time.time() - (hours * 3600)
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM work_items WHERE started_at > ? ORDER BY started_at DESC",
                (cutoff,),
            ).fetchall()
            return [dict(r) for r in rows]

    def work_next_unclaimed(self, node_id: str) -> Optional[dict]:
        """Get the highest-priority work item not currently claimed by any node.

        Skips items already in_progress (on any node) and items this node completed.
        Returns None if nothing available.
        """
        with self._conn() as conn:
            # Get all in-progress titles to exclude
            active_titles = {r[0] for r in conn.execute(
                "SELECT title FROM work_items WHERE status='in_progress' AND updated_at > ?",
                (time.time() - 3600,)
            ).fetchall()}

            # Get all pending items ordered by priority
            rows = conn.execute(
                "SELECT * FROM work_items WHERE status='pending' ORDER BY priority, started_at"
            ).fetchall()
            for row in rows:
                r = dict(row)
                if r["title"] not in active_titles:
                    return r

            return None

    def work_add_pending(self, work_id: str, title: str, priority: int = 2):
        """Add a work item to the backlog (pending, not yet started)."""
        with self._conn() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO work_items (id, node_id, title, status, priority, started_at, updated_at)
                   VALUES (?, '', ?, 'pending', ?, ?, ?)""",
                (work_id, title, priority, time.time(), time.time()),
            )

    def work_is_claimed(self, title: str) -> bool:
        """Check if a work item with this title is already in progress on any node."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM work_items WHERE title=? AND status='in_progress' AND updated_at > ?",
                (title, time.time() - 3600),  # stale after 1 hour
            ).fetchone()
            return row is not None

    def wal_prune(self, max_age_hours: int = 24):
        """Remove processed WAL entries older than max_age_hours."""
        cutoff = time.time() - (max_age_hours * 3600)
        with self._conn() as conn:
            conn.execute("DELETE FROM wal WHERE processed = 1 AND processed_at < ?", (cutoff,))

    def expire_old(self):
        with self._conn() as conn:
            conn.execute(
                """UPDATE messages SET status = 'expired'
                   WHERE status = 'pending' AND created_at + (ttl_hours * 3600) < ?""",
                (time.time(),),
            )

    # ── Chief Relay (P3 channel) ──

    def relay_queue(self, msg: dict) -> str:
        """Queue a message on chief for later delivery to target node.

        Stores in messages table with status='relay_pending'. Returns msg_id.
        """
        msg_id = msg.get("id", "")
        # Dedup: if already queued and still pending, skip
        with self._conn() as conn:
            existing = conn.execute(
                "SELECT status FROM messages WHERE id = ?", (msg_id,)
            ).fetchone()
            if existing and existing[0] in ("relay_pending", "delivered"):
                return msg_id

        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO messages
                   (id, type, from_node, to_node, ref, seq, priority, subject, body, status, created_at, ttl_hours)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'relay_pending', ?, ?)""",
                (
                    msg_id,
                    msg.get("type", "context"),
                    msg.get("from", "unknown"),
                    msg.get("to"),
                    msg.get("ref"),
                    msg.get("seq"),
                    msg.get("priority", 2),
                    msg.get("payload", {}).get("subject", ""),
                    json.dumps(msg.get("payload", {})),
                    time.time(),
                    msg.get("ttl_hours", 24),
                ),
            )
        return msg_id

    def relay_pending(self, target_node: str) -> list[dict]:
        """Get all relay_pending messages for a target node."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT id, type, from_node, to_node, ref, priority, subject, body, created_at
                   FROM messages WHERE to_node = ? AND status = 'relay_pending'
                   ORDER BY created_at""",
                (target_node,),
            ).fetchall()
            results = []
            for r in rows:
                d = dict(r)
                # Reconstruct payload from subject + body (body is JSON)
                try:
                    d["payload"] = json.loads(d.pop("body", "{}"))
                except (json.JSONDecodeError, TypeError):
                    d["payload"] = {"subject": d.pop("subject", ""), "body": d.pop("body", "")}
                else:
                    d.pop("subject", None)
                results.append(d)
            return results

    def relay_ack(self, msg_id: str) -> bool:
        """Mark a relayed message as delivered. Returns True if found and updated."""
        with self._conn() as conn:
            cursor = conn.execute(
                "UPDATE messages SET status = 'delivered', delivered_at = ? WHERE id = ? AND status = 'relay_pending'",
                (time.time(), msg_id),
            )
            return cursor.rowcount > 0
