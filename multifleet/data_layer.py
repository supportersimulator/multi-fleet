"""Unified Data Layer — single managed connection pool for all fleet SQLite databases.

Replaces ad-hoc database creation across modules with a centralized,
WAL-enabled, properly-closing connection manager.

Databases managed:
  - messages.db  (store.py, task_state.py, leases.py)
  - evidence.db  (evidence_ledger.py)
"""

import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

logger = logging.getLogger("fleet-nerve.data")

# Known database names and their default filenames
DB_NAMES = {
    "messages": "messages.db",
    "evidence": "evidence.db",
}


class FleetDataLayer:
    """Centralized SQLite management for all fleet databases.

    Provides WAL-enabled, thread-safe connections with proper lifecycle
    management. Each database gets a single cached connection (SQLite
    serialized mode handles thread safety via check_same_thread=False).
    """

    def __init__(self, data_dir: str = None):
        self._dir = Path(data_dir) if data_dir else Path.home() / ".fleet-nerve"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._connections: dict[str, sqlite3.Connection] = {}
        self._lock = threading.Lock()
        self._closed = False

    @property
    def data_dir(self) -> Path:
        """Return the data directory path."""
        return self._dir

    def _db_path(self, db_name: str) -> Path:
        """Resolve database name to file path."""
        filename = DB_NAMES.get(db_name, f"{db_name}.db")
        return self._dir / filename

    @contextmanager
    def connection(self, db_name: str = "messages"):
        """Get a managed connection. Auto-creates with WAL mode.

        Usage:
            with data_layer.connection("messages") as conn:
                conn.execute("SELECT ...")

        The connection is cached and reused across calls. It is NOT
        closed when the context manager exits — only close_all() or
        close_db() releases it.
        """
        if self._closed:
            raise RuntimeError("FleetDataLayer is closed")

        with self._lock:
            conn = self._connections.get(db_name)
            if conn is None:
                path = self._db_path(db_name)
                conn = sqlite3.connect(str(path), check_same_thread=False, timeout=10)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=5000")
                self._connections[db_name] = conn
                logger.debug("Opened database: %s at %s", db_name, path)

        try:
            yield conn
        except Exception:
            raise

    def get_connection(self, db_name: str = "messages") -> sqlite3.Connection:
        """Get a raw connection (no context manager). Caller must NOT close it.

        Prefer connection() context manager. This exists for modules that
        need to pass a connection object around (e.g., store._conn()).
        """
        if self._closed:
            raise RuntimeError("FleetDataLayer is closed")

        with self._lock:
            conn = self._connections.get(db_name)
            if conn is None:
                path = self._db_path(db_name)
                conn = sqlite3.connect(str(path), check_same_thread=False, timeout=10)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=5000")
                self._connections[db_name] = conn
                logger.debug("Opened database: %s at %s", db_name, path)
            return conn

    def close_db(self, db_name: str):
        """Close a specific database connection."""
        with self._lock:
            conn = self._connections.pop(db_name, None)
            if conn:
                try:
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except Exception as e:
                    logger.warning("Checkpoint failed for %s: %s", db_name, e)
                try:
                    conn.close()
                    logger.debug("Closed database: %s", db_name)
                except Exception as e:
                    logger.warning("Close failed for %s: %s", db_name, e)

    def close_all(self):
        """Close all connections gracefully. Checkpoints WAL first."""
        self._closed = True
        with self._lock:
            for db_name in list(self._connections.keys()):
                conn = self._connections.pop(db_name, None)
                if conn:
                    try:
                        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    except Exception as e:
                        logger.warning("Checkpoint failed for %s: %s", db_name, e)
                    try:
                        conn.close()
                    except Exception as e:
                        logger.warning("Close failed for %s: %s", db_name, e)
            logger.info("All database connections closed")

    def run_maintenance(self) -> dict:
        """Run maintenance on ALL databases: WAL checkpoint, integrity check.

        Returns summary dict with per-database results.
        """
        results = {}
        with self._lock:
            db_names = list(self._connections.keys())

        for db_name in db_names:
            try:
                with self.connection(db_name) as conn:
                    # WAL checkpoint
                    ckpt = conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
                    # Quick integrity check (just first page)
                    ok = conn.execute("PRAGMA quick_check(1)").fetchone()
                    results[db_name] = {
                        "checkpoint": {"busy": ckpt[0], "log": ckpt[1], "checkpointed": ckpt[2]},
                        "integrity": ok[0] if ok else "unknown",
                    }
            except Exception as e:
                results[db_name] = {"error": str(e)}
                logger.error("Maintenance failed for %s: %s", db_name, e)

        logger.info("Maintenance complete: %s", {k: v.get("integrity", "error") for k, v in results.items()})
        return results

    def health_check(self) -> dict:
        """Check health of all known databases.

        Returns:
            {
                "healthy": bool,
                "databases": {name: {"exists": bool, "wal_mode": bool, "size_bytes": int, "connected": bool}},
            }
        """
        db_health = {}
        all_healthy = True

        for db_name, filename in DB_NAMES.items():
            path = self._dir / filename
            exists = path.exists()
            info = {
                "exists": exists,
                "path": str(path),
                "wal_mode": False,
                "size_bytes": path.stat().st_size if exists else 0,
                "connected": db_name in self._connections,
            }

            if db_name in self._connections:
                try:
                    conn = self._connections[db_name]
                    mode = conn.execute("PRAGMA journal_mode").fetchone()
                    info["wal_mode"] = mode[0] == "wal" if mode else False
                except Exception as e:
                    info["error"] = str(e)
                    all_healthy = False
            elif not exists:
                all_healthy = False

            db_health[db_name] = info

        return {"healthy": all_healthy, "databases": db_health}

    @property
    def active_connections(self) -> list[str]:
        """List currently open database names."""
        with self._lock:
            return list(self._connections.keys())

    def __del__(self):
        """Best-effort cleanup on garbage collection."""
        if not self._closed and self._connections:
            try:
                self.close_all()
            except Exception:
                pass


# ── Singleton ──

_instance: Optional[FleetDataLayer] = None
_instance_lock = threading.Lock()


def get_data_layer(data_dir: str = None) -> FleetDataLayer:
    """Get or create the singleton FleetDataLayer.

    Args:
        data_dir: Override data directory (only used on first call).
    """
    global _instance
    with _instance_lock:
        if _instance is None or _instance._closed:
            _instance = FleetDataLayer(data_dir=data_dir)
        return _instance


def reset_data_layer():
    """Reset the singleton (for testing only)."""
    global _instance
    with _instance_lock:
        if _instance is not None:
            _instance.close_all()
            _instance = None
