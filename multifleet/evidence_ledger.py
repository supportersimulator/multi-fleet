"""Evidence Ledger — tamper-evident append-only log of fleet decisions.

Uses hash chain (each entry's hash includes the previous entry's hash)
to detect tampering. Stored in SQLite for queryability.

Event types:
  merge_decision     — when chief decides to merge a branch
  lease_granted      — when a lease is granted/denied
  rebuttal_resolved  — when a rebuttal chain resolves
  gains_gate_result  — each gains gate run
  channel_repair     — successful repair actions
  consensus_reached  — fleet consensus events
"""

import hashlib
import json
import logging
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger("fleet-nerve.evidence")

_WRITE_LOCKS: dict[str, threading.Lock] = {}
_WRITE_LOCKS_GUARD = threading.Lock()


def _get_write_lock(db_path: str) -> threading.Lock:
    """Return a process-local lock for a specific ledger DB path."""
    resolved = str(Path(db_path).resolve())
    with _WRITE_LOCKS_GUARD:
        lock = _WRITE_LOCKS.get(resolved)
        if lock is None:
            lock = threading.Lock()
            _WRITE_LOCKS[resolved] = lock
        return lock


class EvidenceLedger:
    """Append-only evidence ledger with SHA-256 hash chain.

    Each entry's hash includes the previous entry's hash, forming a
    tamper-evident chain. Any modification to a historical entry
    breaks the chain from that point forward.
    """

    def __init__(self, db_path: str = None, data_layer=None):
        self._db_path = db_path or str(Path.home() / ".fleet-nerve" / "evidence.db")
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._data_layer = data_layer
        self._write_lock = _get_write_lock(self._db_path)
        self._init_db()

    def _init_db(self):
        """Create evidence table with hash chain."""
        with self._write_lock:
            conn = self._conn()
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS evidence (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        entry_id TEXT UNIQUE NOT NULL,
                        timestamp REAL NOT NULL,
                        event_type TEXT NOT NULL,
                        node_id TEXT NOT NULL,
                        subject TEXT,
                        payload TEXT NOT NULL,
                        prev_hash TEXT NOT NULL,
                        entry_hash TEXT NOT NULL,
                        verified INTEGER DEFAULT 1
                    )
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_evidence_type
                    ON evidence(event_type)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_evidence_node
                    ON evidence(node_id)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_evidence_ts
                    ON evidence(timestamp)
                """)
                conn.commit()
            finally:
                self._maybe_close(conn)

    def _conn(self) -> sqlite3.Connection:
        """Get a SQLite connection with WAL mode and Row factory.

        When data_layer is set, returns the managed connection (caller must
        NOT close it). Otherwise creates a new connection per call (legacy).
        """
        if self._data_layer is not None:
            return self._data_layer.get_connection("evidence")
        conn = sqlite3.connect(self._db_path, check_same_thread=False, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _maybe_close(self, conn: sqlite3.Connection):
        """Close connection only if NOT managed by data layer."""
        if self._data_layer is None:
            conn.close()

    def _compute_hash(self, entry_id: str, timestamp: float, event_type: str,
                      node_id: str, payload: str, prev_hash: str) -> str:
        """Compute SHA-256 hash of entry fields + previous hash."""
        data = f"{entry_id}:{timestamp}:{event_type}:{node_id}:{payload}:{prev_hash}"
        return hashlib.sha256(data.encode()).hexdigest()

    def _get_last_hash(self, conn: sqlite3.Connection) -> str:
        """Get hash of the most recent entry, or 'genesis'."""
        row = conn.execute(
            "SELECT entry_hash FROM evidence ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        return row["entry_hash"] if row else "genesis"

    def record(self, event_type: str, node_id: str, subject: str,
               payload: dict) -> dict:
        """Append a new evidence entry. Returns the entry dict with hash.

        Uses BEGIN IMMEDIATE for serialized writes — concurrent callers
        will block briefly rather than producing a broken chain.
        """
        entry_id = str(uuid.uuid4())
        ts = time.time()
        payload_json = json.dumps(payload, sort_keys=True)

        with self._write_lock:
            conn = self._conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                prev_hash = self._get_last_hash(conn)
                entry_hash = self._compute_hash(
                    entry_id, ts, event_type, node_id, payload_json, prev_hash
                )
                conn.execute(
                    """INSERT INTO evidence
                       (entry_id, timestamp, event_type, node_id, subject,
                        payload, prev_hash, entry_hash)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (entry_id, ts, event_type, node_id, subject,
                     payload_json, prev_hash, entry_hash),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                self._maybe_close(conn)

        logger.info("Evidence recorded: %s [%s] by %s — %s",
                     entry_id[:8], event_type, node_id, subject or "(no subject)")
        return {
            "entry_id": entry_id,
            "seq": self._get_seq(entry_id),
            "timestamp": ts,
            "event_type": event_type,
            "node_id": node_id,
            "subject": subject,
            "payload": payload,
            "prev_hash": prev_hash,
            "entry_hash": entry_hash,
        }

    def _get_seq(self, entry_id: str) -> Optional[int]:
        """Look up the sequence number for an entry_id."""
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT seq FROM evidence WHERE entry_id = ?", (entry_id,)
            ).fetchone()
            return row["seq"] if row else None
        finally:
            self._maybe_close(conn)

    def verify_chain(self, limit: int = None) -> dict:
        """Verify the hash chain integrity.

        Returns:
            {valid: bool, checked: int, broken_at: int|None, detail: str}
        """
        conn = self._conn()
        try:
            query = "SELECT * FROM evidence ORDER BY seq ASC"
            if limit:
                query += f" LIMIT {int(limit)}"
            rows = conn.execute(query).fetchall()

            if not rows:
                return {"valid": True, "checked": 0, "broken_at": None,
                        "detail": "empty ledger"}

            prev_hash = "genesis"
            checked = 0
            for row in rows:
                expected = self._compute_hash(
                    row["entry_id"], row["timestamp"], row["event_type"],
                    row["node_id"], row["payload"], prev_hash,
                )
                if expected != row["entry_hash"]:
                    logger.error(
                        "Hash chain broken at seq=%d entry=%s: expected=%s got=%s",
                        row["seq"], row["entry_id"][:8], expected[:16], row["entry_hash"][:16],
                    )
                    return {
                        "valid": False,
                        "checked": checked,
                        "broken_at": row["seq"],
                        "detail": f"hash mismatch at seq {row['seq']} ({row['entry_id'][:8]})",
                    }
                if row["prev_hash"] != prev_hash:
                    return {
                        "valid": False,
                        "checked": checked,
                        "broken_at": row["seq"],
                        "detail": f"prev_hash mismatch at seq {row['seq']}",
                    }
                prev_hash = row["entry_hash"]
                checked += 1

            return {"valid": True, "checked": checked, "broken_at": None,
                    "detail": "chain intact"}
        finally:
            self._maybe_close(conn)

    def query(self, event_type: str = None, node_id: str = None,
              since: float = None, limit: int = 50) -> list:
        """Query evidence entries with optional filters."""
        conn = self._conn()
        try:
            conditions = []
            params = []
            if event_type:
                conditions.append("event_type = ?")
                params.append(event_type)
            if node_id:
                conditions.append("node_id = ?")
                params.append(node_id)
            if since:
                conditions.append("timestamp >= ?")
                params.append(since)

            where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
            sql = f"SELECT * FROM evidence{where} ORDER BY seq DESC LIMIT ?"
            params.append(limit)

            rows = conn.execute(sql, params).fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            self._maybe_close(conn)

    def latest(self, count: int = 10) -> list:
        """Get the N most recent entries."""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM evidence ORDER BY seq DESC LIMIT ?", (count,)
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]
        finally:
            self._maybe_close(conn)

    @property
    def stats(self) -> dict:
        """Return ledger stats: total entries, chain valid, last entry time."""
        conn = self._conn()
        try:
            total = conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
            type_rows = conn.execute(
                "SELECT event_type, COUNT(*) as cnt FROM evidence GROUP BY event_type"
            ).fetchall()
            by_type = {r["event_type"]: r["cnt"] for r in type_rows}

            last_row = conn.execute(
                "SELECT timestamp, entry_hash FROM evidence ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            last_ts = last_row["timestamp"] if last_row else None
            last_hash = last_row["entry_hash"][:16] if last_row else None
        finally:
            self._maybe_close(conn)

        # Quick chain check (only last 10 for speed in stats)
        chain_check = self.verify_chain(limit=10)

        return {
            "total": total,
            "by_type": by_type,
            "last_entry_at": last_ts,
            "last_hash_prefix": last_hash,
            "chain_valid": chain_check["valid"],
            "chain_checked": chain_check["checked"],
        }

    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        """Convert a sqlite3.Row to a plain dict with parsed payload."""
        d = dict(row)
        try:
            d["payload"] = json.loads(d["payload"])
        except (json.JSONDecodeError, TypeError):
            pass
        return d
