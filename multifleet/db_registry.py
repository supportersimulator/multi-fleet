"""Central registry of all SQLite databases used by multifleet.

Catalogs every database: path, owning module, WAL mode, connection style.
Provides get_db_path() for standardized path resolution and health_check()
for verifying all databases are accessible and properly configured.

This is the single source of truth for "what databases exist in multifleet?"
"""

import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from ._paths import REPO_ROOT

logger = logging.getLogger("fleet-nerve.db-registry")


class ConnectionStyle(Enum):
    """How the owning module manages its connection."""
    CACHED = "cached"           # Single cached conn, reused across calls
    PER_CALL = "per_call"       # New connection each method call
    DATA_LAYER = "data_layer"   # Managed by FleetDataLayer singleton
    EPHEMERAL = "ephemeral"     # Temp DB, created and destroyed per use


@dataclass
class DatabaseEntry:
    """Metadata for a single SQLite database."""
    name: str
    filename: str
    base_dir: Path
    module: str
    tables: list[str] = field(default_factory=list)
    uses_wal: bool = True
    connection_style: ConnectionStyle = ConnectionStyle.CACHED
    notes: str = ""

    @property
    def path(self) -> Path:
        return self.base_dir / self.filename


# ── Static registry ──
# Every known database in multifleet, with its metadata.
# Ordered by base_dir then filename.

_FLEET_NERVE_DIR = Path.home() / ".fleet-nerve"
_MULTIFLEET_DIR = Path.home() / ".multifleet"
_CONTEXTDNA_DIR = REPO_ROOT / ".contextdna" / "local"

KNOWN_DATABASES: list[DatabaseEntry] = [
    # ~/.fleet-nerve/ databases (managed by FleetDataLayer)
    DatabaseEntry(
        name="messages",
        filename="messages.db",
        base_dir=_FLEET_NERVE_DIR,
        module="store.py (FleetNerveStore), task_state.py, leases.py",
        tables=[
            "messages", "peers", "outbox", "wal", "wal_replicas",
            "work_items", "task_runs", "task_states", "task_audit", "leases",
        ],
        uses_wal=True,
        connection_style=ConnectionStyle.DATA_LAYER,
        notes="Primary fleet DB. 3 modules share via FleetDataLayer.",
    ),
    DatabaseEntry(
        name="evidence",
        filename="evidence.db",
        base_dir=_FLEET_NERVE_DIR,
        module="evidence_ledger.py (EvidenceLedger)",
        tables=["evidence"],
        uses_wal=True,
        connection_style=ConnectionStyle.DATA_LAYER,
        notes="Tamper-evident hash-chain log. SHA-256 chain integrity.",
    ),
    DatabaseEntry(
        name="truth_ladder",
        filename="truth_ladder.db",
        base_dir=_FLEET_NERVE_DIR,
        module="truth_ladder.py (TruthLadder)",
        tables=["truth_entries"],
        uses_wal=True,
        connection_style=ConnectionStyle.CACHED,
        notes="Inbox->Vision->Snapshot->DAO promotion pipeline. No close().",
    ),
    DatabaseEntry(
        name="beta",
        filename="beta.db",
        base_dir=_FLEET_NERVE_DIR,
        module="beta_dashboard.py (BetaDashboard)",
        tables=["beta_users", "beta_feedback", "beta_events"],
        uses_wal=True,
        connection_style=ConnectionStyle.PER_CALL,
        notes="Beta user analytics and feedback collection.",
    ),

    # ~/.multifleet/ databases
    DatabaseEntry(
        name="races",
        filename="races.db",
        base_dir=_MULTIFLEET_DIR,
        module="fleet_race.py (_RaceStore)",
        tables=["races"],
        uses_wal=True,
        connection_style=ConnectionStyle.PER_CALL,
        notes="Fleet race coordination state.",
    ),

    # .contextdna/local/ databases (repo-relative)
    DatabaseEntry(
        name="packets",
        filename="multifleet.db",
        base_dir=_CONTEXTDNA_DIR,
        module="packet_store.py (PacketStore)",
        tables=["packets", "fleet_messages"],
        uses_wal=False,
        connection_style=ConnectionStyle.PER_CALL,
        notes="MISSING WAL MODE. fleet_messages duplicates store.py messages.",
    ),
    DatabaseEntry(
        name="supervisor",
        filename="supervisor.db",
        base_dir=_CONTEXTDNA_DIR,
        module="supervisor_store.py (SupervisorStore)",
        tables=["supervisor_tasks"],
        uses_wal=True,
        connection_style=ConnectionStyle.PER_CALL,
        notes="Supervisor task lifecycle persistence.",
    ),
]

# Index by name for fast lookup
_BY_NAME: dict[str, DatabaseEntry] = {db.name: db for db in KNOWN_DATABASES}

_lock = threading.Lock()
_runtime_dbs: dict[str, DatabaseEntry] = {}


def get_db_path(name: str) -> Path:
    """Return the canonical path for a named database.

    Args:
        name: Database name (e.g. "messages", "evidence", "races").

    Returns:
        Resolved Path to the .db file.

    Raises:
        KeyError: If name is not in the registry.
    """
    entry = _BY_NAME.get(name)
    if entry is None:
        with _lock:
            entry = _runtime_dbs.get(name)
    if entry is None:
        raise KeyError(
            f"Unknown database '{name}'. Known: {list(_BY_NAME.keys())}"
        )
    return entry.path


def register_db(
    name: str,
    path: Path,
    module: str,
    tables: Optional[list[str]] = None,
    uses_wal: bool = True,
    connection_style: ConnectionStyle = ConnectionStyle.CACHED,
    notes: str = "",
) -> DatabaseEntry:
    """Register a runtime database (e.g. for plugins or tests).

    Static databases defined in KNOWN_DATABASES cannot be overridden.

    Args:
        name: Unique database identifier.
        path: Full path to the .db file.
        module: Owning module name.

    Returns:
        The registered DatabaseEntry.

    Raises:
        ValueError: If name collides with a static database.
    """
    if name in _BY_NAME:
        raise ValueError(
            f"Cannot override static database '{name}' (owned by {_BY_NAME[name].module})"
        )
    entry = DatabaseEntry(
        name=name,
        filename=path.name,
        base_dir=path.parent,
        module=module,
        tables=tables or [],
        uses_wal=uses_wal,
        connection_style=connection_style,
        notes=notes,
    )
    with _lock:
        _runtime_dbs[name] = entry
    logger.info("Registered runtime database: %s at %s", name, path)
    return entry


def unregister_db(name: str) -> bool:
    """Remove a runtime database from the registry. Returns True if removed."""
    with _lock:
        return _runtime_dbs.pop(name, None) is not None


def list_databases() -> list[dict]:
    """Return metadata for all registered databases (static + runtime).

    Each dict includes: name, path, module, tables, uses_wal,
    connection_style, notes, exists, size_bytes.
    """
    result = []
    all_dbs = list(KNOWN_DATABASES)
    with _lock:
        all_dbs.extend(_runtime_dbs.values())

    for db in all_dbs:
        path = db.path
        exists = path.exists()
        info = {
            "name": db.name,
            "path": str(path),
            "module": db.module,
            "tables": db.tables,
            "uses_wal": db.uses_wal,
            "connection_style": db.connection_style.value,
            "notes": db.notes,
            "exists": exists,
            "size_bytes": path.stat().st_size if exists else 0,
        }
        result.append(info)
    return result


def health_check() -> dict:
    """Check health of all registered databases.

    For each database:
      - exists: file present on disk
      - accessible: can open a read-only connection
      - wal_mode: journal_mode is WAL (if uses_wal=True, mismatch is a warning)
      - size_bytes: file size

    Returns:
        {
            "healthy": bool,  # True if all DBs pass checks
            "total": int,
            "checked": int,
            "warnings": list[str],
            "databases": {name: {status fields...}}
        }
    """
    all_dbs = list(KNOWN_DATABASES)
    with _lock:
        all_dbs.extend(_runtime_dbs.values())

    db_results = {}
    warnings = []
    all_healthy = True

    for db in all_dbs:
        path = db.path
        exists = path.exists()
        info = {
            "path": str(path),
            "module": db.module,
            "exists": exists,
            "accessible": False,
            "wal_mode": None,
            "wal_expected": db.uses_wal,
            "size_bytes": 0,
        }

        if not exists:
            # Non-existent is not necessarily unhealthy — DB may not have
            # been created yet. Only warn if it's a core DB.
            if db.name in ("messages", "evidence"):
                all_healthy = False
                warnings.append(f"{db.name}: core database missing at {path}")
            db_results[db.name] = info
            continue

        info["size_bytes"] = path.stat().st_size

        try:
            conn = sqlite3.connect(str(path), timeout=3)
            try:
                mode = conn.execute("PRAGMA journal_mode").fetchone()
                actual_wal = mode[0] == "wal" if mode else False
                info["accessible"] = True
                info["wal_mode"] = actual_wal

                if db.uses_wal and not actual_wal:
                    warnings.append(
                        f"{db.name}: expected WAL mode but got '{mode[0] if mode else '?'}'"
                    )
                    all_healthy = False
            finally:
                conn.close()
        except Exception as e:
            info["error"] = str(e)
            all_healthy = False
            warnings.append(f"{db.name}: cannot open — {e}")

        db_results[db.name] = info

    return {
        "healthy": all_healthy,
        "total": len(all_dbs),
        "checked": sum(1 for d in db_results.values() if d["accessible"]),
        "warnings": warnings,
        "databases": db_results,
    }


def reset_runtime() -> None:
    """Clear all runtime-registered databases (for testing)."""
    with _lock:
        _runtime_dbs.clear()
