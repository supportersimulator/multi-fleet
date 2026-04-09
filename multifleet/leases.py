"""Lease-based task ownership — prevents two nodes from working on the same resource."""

import json
import logging
import time
import urllib.request
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

    When chief_node_id is set and this node is NOT the chief, acquire requests
    are forwarded to the chief for coordination. If the chief is unreachable,
    falls back to local-only acquire with a warning.
    """

    def __init__(self, store, node_id: str, chief_node_id: str = None, daemon_port: int = 8855,
                 data_layer=None):
        """
        Args:
            store: FleetNerveStore instance (has _conn() for SQLite)
            node_id: This node's ID
            chief_node_id: The chief node's ID (if set, non-chief acquire goes through chief)
            daemon_port: HTTP port for the fleet daemon (default 8855)
            data_layer: Optional FleetDataLayer — if set, used for connection instead of store._conn()
        """
        self._store = store
        self._data_layer = data_layer
        self._node_id = node_id
        self._chief_node_id = chief_node_id
        self._daemon_port = daemon_port
        self._is_chief = (node_id == chief_node_id) if chief_node_id else True
        self._stats = {"acquired": 0, "released": 0, "renewed": 0, "denied": 0, "expired_cleaned": 0}
        self._init_table()

    @property
    def is_chief(self) -> bool:
        """Whether this node is the chief (or no chief configured)."""
        return self._is_chief

    def _get_conn(self):
        """Get connection from data layer or fall back to store."""
        if self._data_layer is not None:
            return self._data_layer.get_connection("messages")
        return self._store._conn()

    def _init_table(self):
        """Create leases table if not exists."""
        with self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS leases (
                    resource_id TEXT PRIMARY KEY,
                    node_id TEXT NOT NULL,
                    lease_type TEXT NOT NULL DEFAULT 'short_task',
                    acquired_at REAL NOT NULL,
                    ttl_seconds INTEGER NOT NULL,
                    renewed_at REAL,
                    released_at REAL,
                    metadata TEXT,
                    fence_token INTEGER NOT NULL DEFAULT 0
                )
            """)
            # Migration: add fence_token if table already existed without it
            try:
                conn.execute("SELECT fence_token FROM leases LIMIT 1")
            except Exception:
                conn.execute("ALTER TABLE leases ADD COLUMN fence_token INTEGER NOT NULL DEFAULT 0")

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
        d["fence_token"] = d.get("fence_token", 0)
        if d["metadata"]:
            try:
                d["metadata"] = json.loads(d["metadata"])
            except (json.JSONDecodeError, TypeError):
                pass
        return d

    def _request_from_chief(self, resource_id: str, lease_type: str, metadata: dict = None) -> Optional[dict]:
        """Request a lease from the chief node via the daemon HTTP API.

        Returns chief's response dict, or None if chief is unreachable (caller
        should fall back to local acquire with _local_only warning).
        """
        payload = {
            "type": "lease_request",
            "to": self._chief_node_id,
            "payload": {
                "subject": "lease_request",
                "body": json.dumps({
                    "resource_id": resource_id,
                    "lease_type": lease_type,
                    "requesting_node": self._node_id,
                    "metadata": metadata,
                }),
            },
        }
        try:
            body = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                f"http://127.0.0.1:{self._daemon_port}/message",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            resp = urllib.request.urlopen(req, timeout=5)
            if 200 <= resp.status < 300:
                result = json.loads(resp.read().decode("utf-8"))
                logger.info(f"Lease request sent to chief {self._chief_node_id} for {resource_id}")
                return result
        except Exception as exc:
            logger.warning(f"Chief unreachable for lease request ({resource_id}): {exc}")
        return None

    def acquire(self, resource_id: str, lease_type: str = "short_task", metadata: dict = None) -> dict:
        """Try to acquire a lease.

        If this node is not the chief and a chief is configured, the request is
        forwarded to the chief first. On chief failure, falls back to local-only
        acquire with a warning flag.

        Returns:
            {"acquired": bool, "holder": str, "expires_at": float,
             "fence_token": int, "reason": str (if denied),
             "_local_only": bool (if chief was unreachable)}
        """
        ttl = LEASE_TTLS.get(lease_type)
        if ttl is None:
            return {"acquired": False, "holder": "", "expires_at": 0, "fence_token": 0,
                    "reason": f"unknown lease_type: {lease_type}"}

        # Non-chief: try chief coordination first
        local_only = False
        if not self._is_chief and self._chief_node_id:
            chief_resp = self._request_from_chief(resource_id, lease_type, metadata)
            if chief_resp is None:
                # Chief unreachable — fall through to local acquire with warning
                local_only = True
                logger.warning(f"Chief unreachable, local-only acquire for {resource_id}")

        now = time.time()
        conn = self._get_conn()

        # Atomic: BEGIN IMMEDIATE prevents concurrent writers from interleaving
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Check existing lease
            row = conn.execute("SELECT * FROM leases WHERE resource_id = ?", (resource_id,)).fetchone()

            if row and self._is_active(row):
                if row["node_id"] == self._node_id:
                    # We already hold it — renew instead
                    conn.execute(
                        "UPDATE leases SET renewed_at = ?, ttl_seconds = ?, lease_type = ? WHERE resource_id = ?",
                        (now, ttl, lease_type, resource_id),
                    )
                    conn.commit()
                    self._stats["renewed"] += 1
                    expires = now + ttl
                    fence = row["fence_token"] if "fence_token" in row.keys() else 0
                    logger.debug(f"Lease re-acquired (already holder): {resource_id} by {self._node_id}")
                    result = {"acquired": True, "holder": self._node_id, "expires_at": expires, "fence_token": fence}
                    if local_only:
                        result["_local_only"] = True
                    return result
                else:
                    conn.rollback()
                    self._stats["denied"] += 1
                    expires = self._expires_at(row)
                    logger.debug(f"Lease denied: {resource_id} held by {row['node_id']} until {expires:.0f}")
                    return {
                        "acquired": False,
                        "holder": row["node_id"],
                        "expires_at": expires,
                        "fence_token": row["fence_token"] if "fence_token" in row.keys() else 0,
                        "reason": f"held by {row['node_id']}",
                    }

            # Compute next fence token (monotonically increasing per resource)
            max_fence_row = conn.execute(
                "SELECT COALESCE(MAX(fence_token), 0) as max_ft FROM leases WHERE resource_id = ?",
                (resource_id,),
            ).fetchone()
            next_fence = (max_fence_row["max_ft"] if max_fence_row else 0) + 1

            # Available (no lease, expired, or released) — acquire it
            meta_json = json.dumps(metadata) if metadata else None
            conn.execute(
                """INSERT OR REPLACE INTO leases
                   (resource_id, node_id, lease_type, acquired_at, ttl_seconds, renewed_at, released_at, metadata, fence_token)
                   VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?)""",
                (resource_id, self._node_id, lease_type, now, ttl, meta_json, next_fence),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        self._stats["acquired"] += 1
        expires = now + ttl
        logger.info(f"Lease acquired: {resource_id} by {self._node_id} ({lease_type}, ttl={ttl}s, fence={next_fence})")
        result = {"acquired": True, "holder": self._node_id, "expires_at": expires, "fence_token": next_fence}
        if local_only:
            result["_local_only"] = True
        return result

    def renew(self, resource_id: str) -> dict:
        """Renew a lease this node holds.

        Returns:
            {"renewed": bool, "expires_at": float, "fence_token": int, "reason": str (if failed)}
        """
        now = time.time()
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM leases WHERE resource_id = ?", (resource_id,)).fetchone()

        if not row:
            return {"renewed": False, "expires_at": 0, "fence_token": 0, "reason": "no lease found"}

        if not self._is_active(row):
            return {"renewed": False, "expires_at": 0, "fence_token": 0, "reason": "lease expired"}

        if row["node_id"] != self._node_id:
            return {"renewed": False, "expires_at": self._expires_at(row), "fence_token": 0, "reason": f"held by {row['node_id']}"}

        conn.execute("UPDATE leases SET renewed_at = ? WHERE resource_id = ?", (now, resource_id))
        conn.commit()
        self._stats["renewed"] += 1
        expires = now + row["ttl_seconds"]
        fence = row["fence_token"] if "fence_token" in row.keys() else 0
        logger.debug(f"Lease renewed: {resource_id} by {self._node_id}, expires {expires:.0f}")
        return {"renewed": True, "expires_at": expires, "fence_token": fence}

    def release(self, resource_id: str) -> dict:
        """Release a lease this node holds.

        Returns:
            {"released": bool, "reason": str (if failed)}
        """
        now = time.time()
        conn = self._get_conn()
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
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM leases WHERE resource_id = ?", (resource_id,)).fetchone()
        if not row:
            return None
        return self._row_to_dict(row)

    def validate_fence_token(self, resource_id: str, fence_token: int) -> dict:
        """Validate a fence token before performing a fenced write operation.

        A fence token becomes stale when another node has acquired (or re-acquired)
        the lease since this node obtained it. Callers should check this before any
        write that must not execute under a stale lease.

        Returns:
            {"valid": bool, "current_fence": int, "holder": str, "reason": str (if invalid)}
        """
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM leases WHERE resource_id = ?", (resource_id,)).fetchone()

        if not row:
            return {"valid": False, "current_fence": 0, "holder": "", "reason": "no lease found"}

        current_fence = row["fence_token"] if "fence_token" in row.keys() else 0
        holder = row["node_id"]

        if not self._is_active(row):
            return {"valid": False, "current_fence": current_fence, "holder": holder,
                    "reason": "lease expired"}

        if fence_token != current_fence:
            return {"valid": False, "current_fence": current_fence, "holder": holder,
                    "reason": f"stale fence_token {fence_token} != current {current_fence}"}

        if holder != self._node_id:
            return {"valid": False, "current_fence": current_fence, "holder": holder,
                    "reason": f"lease held by {holder}, not {self._node_id}"}

        return {"valid": True, "current_fence": current_fence, "holder": holder}

    def list_active(self, node_id: str = None) -> list:
        """List all active (non-expired, non-released) leases.

        Args:
            node_id: If provided, filter to this node's leases only.

        Returns:
            List of lease dicts.
        """
        conn = self._get_conn()
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
        conn = self._get_conn()

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
        conn = self._get_conn()
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
