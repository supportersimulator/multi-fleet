"""Fleet Backup/Restore — snapshot and restore fleet state.

Backs up: messages.db, leases.db, audit trail, decisions.jsonl,
evidence ledger, config, packet schemas. Restores from snapshot.
"""

import json
import logging
import shutil
import tarfile
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("fleet-nerve.backup")

# Files/dirs to include in backup (relative to fleet_dir)
BACKUP_TARGETS = [
    "messages.db",
    "messages.db-wal",
    "messages.db-shm",
    "leases.db",
    "leases.db-wal",
    "leases.db-shm",
    "evidence.db",
    "evidence.db-wal",
    "evidence.db-shm",
    "audit",           # audit trail directory (JSONL per day)
    "decisions.jsonl",
    "config.json",
    "version.json",
]

SNAPSHOT_PREFIX = "fleet-snapshot"


class FleetBackup:
    """Create and restore fleet state snapshots."""

    def __init__(self, fleet_dir: str = None):
        self._dir = Path(fleet_dir) if fleet_dir else Path.home() / ".fleet-nerve"

    def create_snapshot(self, output_path: str = None) -> str:
        """Create a tarball of all fleet state. Returns path to snapshot."""
        if not self._dir.exists():
            raise FileNotFoundError(f"Fleet directory not found: {self._dir}")

        ts = int(time.time())
        name = f"{SNAPSHOT_PREFIX}-{ts}.tar.gz"
        if output_path:
            out = Path(output_path)
        else:
            snapshots_dir = self._dir / "snapshots"
            snapshots_dir.mkdir(parents=True, exist_ok=True)
            out = snapshots_dir / name

        # Collect metadata
        meta = {
            "timestamp": ts,
            "fleet_dir": str(self._dir),
            "version": self._read_version(),
            "files": [],
        }

        with tarfile.open(str(out), "w:gz") as tar:
            for target in BACKUP_TARGETS:
                p = self._dir / target
                if p.exists():
                    arcname = target
                    tar.add(str(p), arcname=arcname)
                    if p.is_file():
                        meta["files"].append({"name": target, "size": p.stat().st_size})
                    else:
                        # directory — count files
                        count = sum(1 for _ in p.rglob("*") if _.is_file())
                        meta["files"].append({"name": target, "type": "dir", "file_count": count})

            # Write metadata into the tarball
            meta_bytes = json.dumps(meta, indent=2).encode()
            import io
            info = tarfile.TarInfo(name="snapshot-meta.json")
            info.size = len(meta_bytes)
            info.mtime = ts
            tar.addfile(info, io.BytesIO(meta_bytes))

        logger.info("snapshot created: %s (%d files)", out, len(meta["files"]))
        return str(out)

    def restore_snapshot(self, snapshot_path: str, *, force: bool = False) -> dict:
        """Restore fleet state from a snapshot tarball.

        Args:
            snapshot_path: Path to the .tar.gz snapshot.
            force: If True, overwrite existing state without backup.

        Returns:
            Dict with restored file count and any warnings.
        """
        snap = Path(snapshot_path)
        if not snap.exists():
            raise FileNotFoundError(f"Snapshot not found: {snap}")

        self._dir.mkdir(parents=True, exist_ok=True)

        # Safety: back up current state before overwriting
        warnings = []
        if not force and any((self._dir / t).exists() for t in BACKUP_TARGETS):
            pre_restore = self._dir / "pre-restore-backup"
            pre_restore.mkdir(exist_ok=True)
            for t in BACKUP_TARGETS:
                p = self._dir / t
                if p.exists():
                    dest = pre_restore / t
                    if p.is_dir():
                        if dest.exists():
                            shutil.rmtree(dest)
                        shutil.copytree(str(p), str(dest))
                    else:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(str(p), str(dest))
            warnings.append(f"existing state backed up to {pre_restore}")

        restored = 0
        with tarfile.open(str(snap), "r:gz") as tar:
            # Security: validate member paths (no path traversal)
            for member in tar.getmembers():
                if member.name.startswith("/") or ".." in member.name:
                    raise ValueError(f"Unsafe path in snapshot: {member.name}")
            tar.extractall(path=str(self._dir))
            restored = len(tar.getmembers())

        logger.info("snapshot restored: %s (%d entries)", snap, restored)
        return {"restored_entries": restored, "fleet_dir": str(self._dir), "warnings": warnings}

    def list_snapshots(self, directory: str = None) -> list[dict]:
        """List available snapshots with metadata."""
        search_dir = Path(directory) if directory else self._dir / "snapshots"
        if not search_dir.exists():
            return []

        snapshots = []
        for f in sorted(search_dir.glob(f"{SNAPSHOT_PREFIX}-*.tar.gz")):
            try:
                info = self.snapshot_info(str(f))
                snapshots.append(info)
            except Exception as e:
                snapshots.append({"path": str(f), "error": str(e)})

        return snapshots

    def snapshot_info(self, path: str) -> dict:
        """Get metadata about a snapshot without extracting."""
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Snapshot not found: {p}")

        result = {
            "path": str(p),
            "size_bytes": p.stat().st_size,
            "created": p.stat().st_mtime,
        }

        try:
            with tarfile.open(str(p), "r:gz") as tar:
                result["entries"] = len(tar.getmembers())
                # Try to read embedded metadata
                try:
                    meta_member = tar.getmember("snapshot-meta.json")
                    f = tar.extractfile(meta_member)
                    if f:
                        meta = json.loads(f.read().decode())
                        result["metadata"] = meta
                except KeyError:
                    pass  # No metadata in this snapshot
        except tarfile.TarError as e:
            result["error"] = f"corrupt tarball: {e}"

        return result

    def _read_version(self) -> Optional[str]:
        """Read current fleet version from version.json."""
        vf = self._dir / "version.json"
        if vf.exists():
            try:
                return json.loads(vf.read_text()).get("version")
            except (json.JSONDecodeError, OSError):
                pass
        return None
