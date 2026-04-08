"""Fleet Migration — upgrade between multi-fleet versions.

Tracks schema version, runs migration steps, validates post-migration.
"""

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("fleet-nerve.migration")

VERSION_FILE = "version.json"


class FleetMigration:
    """Manage fleet state schema versions and migrations."""

    CURRENT_VERSION = "5.0.0"

    # Ordered migration steps: (from, to, method_name)
    MIGRATIONS = [
        ("3.0.0", "4.0.0", "_migrate_3_to_4"),
        ("4.0.0", "5.0.0", "_migrate_4_to_5"),
    ]

    def __init__(self, fleet_dir: str = None):
        self._dir = Path(fleet_dir) if fleet_dir else Path.home() / ".fleet-nerve"

    def check_version(self) -> dict:
        """Check current fleet state version vs expected."""
        current = self._read_version()
        needs_migration = current is not None and current != self.CURRENT_VERSION
        return {
            "current_version": current,
            "expected_version": self.CURRENT_VERSION,
            "up_to_date": current == self.CURRENT_VERSION,
            "needs_migration": needs_migration,
            "fleet_dir": str(self._dir),
        }

    def migrate(self, from_version: str = None, to_version: str = None) -> dict:
        """Run migration steps between versions.

        Args:
            from_version: Starting version (default: auto-detect).
            to_version: Target version (default: CURRENT_VERSION).

        Returns:
            Dict with steps executed and any errors.
        """
        if from_version is None:
            from_version = self._read_version() or "3.0.0"
        if to_version is None:
            to_version = self.CURRENT_VERSION

        if from_version == to_version:
            return {"status": "already_current", "version": to_version, "steps": []}

        steps_run = []
        errors = []
        current = from_version

        for mig_from, mig_to, method_name in self.MIGRATIONS:
            if current == to_version:
                break
            if mig_from != current:
                continue

            method = getattr(self, method_name, None)
            if method is None:
                errors.append(f"migration method {method_name} not found")
                break

            try:
                logger.info("migrating %s -> %s", mig_from, mig_to)
                method()
                steps_run.append({"from": mig_from, "to": mig_to, "status": "ok"})
                current = mig_to
            except Exception as e:
                logger.error("migration %s -> %s failed: %s", mig_from, mig_to, e)
                steps_run.append({"from": mig_from, "to": mig_to, "status": "error", "error": str(e)})
                errors.append(str(e))
                break

        if not errors:
            self._write_version(current)

        return {
            "status": "ok" if not errors else "error",
            "from_version": from_version,
            "to_version": current,
            "target_version": to_version,
            "steps": steps_run,
            "errors": errors,
        }

    def _migrate_3_to_4(self):
        """v3->v4: add audit trail directory, config file."""
        audit_dir = self._dir / "audit"
        audit_dir.mkdir(parents=True, exist_ok=True)

        config_file = self._dir / "config.json"
        if not config_file.exists():
            config_file.write_text(json.dumps({"migrated_from": "3.0.0", "migrated_at": time.time()}, indent=2))

    def _migrate_4_to_5(self):
        """v4->v5: add probe tables, evidence ledger, lease schema updates."""
        # Evidence ledger DB
        evidence_db = self._dir / "evidence.db"
        conn = sqlite3.connect(str(evidence_db))
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS evidence (
                    id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    node_id TEXT,
                    payload TEXT,
                    prev_hash TEXT,
                    hash TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_evidence_ts ON evidence(timestamp)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_evidence_type ON evidence(event_type)")
            conn.commit()
        finally:
            conn.close()

        # Leases DB — add probe-related columns if messages.db exists
        messages_db = self._dir / "messages.db"
        if messages_db.exists():
            conn = sqlite3.connect(str(messages_db))
            try:
                # Check and add probe_status column to leases if table exists
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
                if "leases" in tables:
                    cols = {r[1] for r in conn.execute("PRAGMA table_info(leases)").fetchall()}
                    if "probe_status" not in cols:
                        conn.execute("ALTER TABLE leases ADD COLUMN probe_status TEXT DEFAULT 'unknown'")
                        conn.commit()
            finally:
                conn.close()

        logger.info("v4->v5 migration complete: evidence ledger + lease schema updated")

    def _read_version(self) -> Optional[str]:
        """Read current version from version.json."""
        vf = self._dir / VERSION_FILE
        if vf.exists():
            try:
                return json.loads(vf.read_text()).get("version")
            except (json.JSONDecodeError, OSError):
                pass
        return None

    def _write_version(self, version: str):
        """Write version to version.json."""
        self._dir.mkdir(parents=True, exist_ok=True)
        vf = self._dir / VERSION_FILE
        vf.write_text(json.dumps({"version": version, "updated_at": time.time()}, indent=2))
        logger.info("version updated to %s", version)
