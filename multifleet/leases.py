"""Lease-based task ownership — prevents two nodes from working on the same resource."""

import json
import logging
import time
from typing import Optional

logger = logging.getLogger("fleet-nerve.leases")

LEASE_TTLS = {
    "heartbeat": 30,
    "short_task": 60,
    "long_task": 300,
    "exclusive": 600,
}


class LeaseManager:
    """Manages resource leases backed by the FleetNerveStore's SQLite DB.

    Each lease has a resource_id, holder node, type (with TTL), and expiry.
    Only the holder can renew or release. Expired leases can be reclaimed by anyone.
    """

    def __init__(self, store, node_id: str):
        """
        Args:
            store: FleetNerveStore instance (has _conn() for SQLite)
            node_id: This node's ID
        """
        self._store = store
        self._node_id = node_id
        self._stats = {"acquired": 0, "released": 0, "renewed": 0, "denied": 0, "expired_cleaned": 0}
        self._init_table()

    def _init_table(self):
        """Create leases table if not exists."""
        with self._store._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS leases (
                    resource_id TEXT PRIMARY KEY,
                    node_id TEXT NOT NULL,
                    lease_type TEXT NOT NULL DEFAULT 'short_task',
                    acquired_at REAL NOT NULL,
                    ttl_seconds INTEGER NOT NULL,
                    renewed_at REAL,
                    released_at REAL,
                    metadata TEXT
                )
            """)

    def _expires_at(self, row) -> float:
        """Calculate expiry time from a lease row."""
        base = row["renewed_at"] if row["renewed_at"] else row["acquired_at"]
        return base + row["ttl_seconds"]

    def _is_active(self, row) -> bool:
        """Check if a lease row is still active (not released, not expired)."""
        if row["released_at"] is not None:
            return False
        return time.time() < self._expires_at(row)

    def _row_to_dict(self, row) -> dict:
        """Convert a sqlite3.Row to a plain dict with computed fields."""
        d = dict(row)
        d["expires_at"] = self._expires_at(row)
        d["active"] = self._is_active(row)
        if d["metadata"]:
            try:
                d["metadata"] = json.loads(d["metadata"])
            except (json.JSONDecodeError, TypeError):
                pass
        return d

    def acquire(self, resource_id: str, lease_type: str = "short_task", metadata: dict = None) -> dict:
        """Try to acquire a lease.

        Returns:
            {"acquired": bool, "holder": str, "expires_at": float, "reason": str (if denied)}
        """
        ttl = LEASE_TTLS.get(lease_type)
        if ttl is None:
            return {"acquired": False, "holder": "", "expires_at": 0, "reason": f"unknown lease_type: {lease_type}"}

        now = time.time()
        conn = self._store._conn()

        # Check existing lease
        row = conn.execute("SELECT * FROM leases WHERE resource_id = ?", (resource_id,)).fetchone()

        if row and self._is_active(row):
            # Already held by someone
            if row["node_id"] == self._node_id:
                # We already hold it — renew instead
                conn.execute(
                    "UPDATE leases SET renewed_at = ?, ttl_seconds = ?, lease_type = ? WHERE resource_id = ?",
                    (now, ttl, lease_type, resource_id),
                )
                conn.commit()
                self._stats["renewed"] += 1
                expires = now + ttl
                logger.debug(f"Lease re-acquired (already holder): {resource_id} by {self._node_id}")
                return {"acquired": True, "holder": self._node_id, "expires_at": expires}
            else:
                # Held by another node
                self._stats["denied"] += 1
                expires = self._expires_at(row)
                logger.debug(f"Lease denied: {resource_id} held by {row['node_id']} until {expires:.0f}")
                return {
                    "acquired": False,
                    "holder": row["node_id"],
                    "expires_at": expires,
                    "reason": f"held by {row['node_id']}",
                }

        # Available (no lease, expired, or released) — acquire it
        meta_json = json.dumps(metadata) if metadata else None
        conn.execute(
            """INSERT OR REPLACE INTO leases
               (resource_id, node_id, lease_type, acquired_at, ttl_seconds, renewed_at, released_at, metadata)
               VALUES (?, ?, ?, ?, ?, NULL, NULL, ?)""",
            (resource_id, self._node_id, lease_type, now, ttl, meta_json),
        )
        conn.commit()
        self._stats["acquired"] += 1
        expires = now + ttl
        logger.info(f"Lease acquired: {resource_id} by {self._node_id} ({lease_type}, ttl={ttl}s)")
        return {"acquired": True, "holder": self._node_id, "expires_at": expires}

    def renew(self, resource_id: str) -> dict:
        """Renew a lease this node holds.

        Returns:
            {"renewed": bool, "expires_at": float, "reason": str (if failed)}
        """
        now = time.time()
        conn = self._store._conn()
        row = conn.execute("SELECT * FROM leases WHERE resource_id = ?", (resource_id,)).fetchone()

        if not row:
            return {"renewed": False, "expires_at": 0, "reason": "no lease found"}

        if not self._is_active(row):
            return {"renewed": False, "expires_at": 0, "reason": "lease expired"}

        if row["node_id"] != self._node_id:
            return {"renewed": False, "expires_at": self._expires_at(row), "reason": f"held by {row['node_id']}"}

        conn.execute("UPDATE leases SET renewed_at = ? WHERE resource_id = ?", (now, resource_id))
        conn.commit()
        self._stats["renewed"] += 1
        expires = now + row["ttl_seconds"]
        logger.debug(f"Lease renewed: {resource_id} by {self._node_id}, expires {expires:.0f}")
        return {"renewed": True, "expires_at": expires}

    def release(self, resource_id: str) -> dict:
        """Release a lease this node holds.

        Returns:
            {"released": bool, "reason": str (if failed)}
        """
        now = time.time()
        conn = self._store._conn()
        row = conn.execute("SELECT * FROM leases WHERE resource_id = ?", (resource_id,)).fetchone()

        if not row:
            return {"released": False, "reason": "no lease found"}

        if row["node_id"] != self._node_id:
            return {"released": False, "reason": f"held by {row['node_id']}, not {self._node_id}"}

        conn.execute("UPDATE leases SET released_at = ? WHERE resource_id = ?", (now, resource_id))
        conn.commit()
        self._stats["released"] += 1
        logger.info(f"Lease released: {resource_id} by {self._node_id}")
        return {"released": True}

    def query(self, resource_id: str) -> Optional[dict]:
        """Check who holds a lease.

        Returns:
            Lease dict with computed fields, or None if no lease exists.
        """
        conn = self._store._conn()
        row = conn.execute("SELECT * FROM leases WHERE resource_id = ?", (resource_id,)).fetchone()
        if not row:
            return None
        return self._row_to_dict(row)

    def list_active(self, node_id: str = None) -> list:
        """List all active (non-expired, non-released) leases.

        Args:
            node_id: If provided, filter to this node's leases only.

        Returns:
            List of lease dicts.
        """
        conn = self._store._conn()
        rows = conn.execute("SELECT * FROM leases WHERE released_at IS NULL").fetchall()
        now = time.time()
        result = []
        for row in rows:
            if row["released_at"] is not None:
                continue
            base = row["renewed_at"] if row["renewed_at"] else row["acquired_at"]
            if now >= base + row["ttl_seconds"]:
                continue  # expired
            if node_id and row["node_id"] != node_id:
                continue
            result.append(self._row_to_dict(row))
        return result

    def cleanup_expired(self) -> int:
        """Remove expired and released leases. Returns count removed."""
        now = time.time()
        conn = self._store._conn()

        # Find expired leases (where current time > acquired/renewed + ttl)
        # and released leases
        rows = conn.execute("SELECT resource_id, acquired_at, renewed_at, ttl_seconds, released_at FROM leases").fetchall()
        to_delete = []
        for row in rows:
            if row["released_at"] is not None:
                to_delete.append(row["resource_id"])
                continue
            base = row["renewed_at"] if row["renewed_at"] else row["acquired_at"]
            if now >= base + row["ttl_seconds"]:
                to_delete.append(row["resource_id"])

        if to_delete:
            placeholders = ",".join("?" for _ in to_delete)
            conn.execute(f"DELETE FROM leases WHERE resource_id IN ({placeholders})", to_delete)
            conn.commit()

        self._stats["expired_cleaned"] += len(to_delete)
        if to_delete:
            logger.info(f"Lease cleanup: removed {len(to_delete)} expired/released leases")
        return len(to_delete)

    @property
    def stats(self) -> dict:
        """Return lease stats for health endpoint."""
        conn = self._store._conn()
        total = conn.execute("SELECT COUNT(*) FROM leases WHERE released_at IS NULL").fetchone()[0]
        now = time.time()
        # Count truly active (not expired)
        rows = conn.execute("SELECT acquired_at, renewed_at, ttl_seconds, node_id FROM leases WHERE released_at IS NULL").fetchall()
        active = 0
        this_node = 0
        for row in rows:
            base = row["renewed_at"] if row["renewed_at"] else row["acquired_at"]
            if now < base + row["ttl_seconds"]:
                active += 1
                if row["node_id"] == self._node_id:
                    this_node += 1
        return {
            "active": active,
            "this_node": this_node,
            "expired_cleaned": self._stats["expired_cleaned"],
            "total_acquired": self._stats["acquired"],
            "total_denied": self._stats["denied"],
        }
