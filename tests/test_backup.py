"""Tests for fleet backup/restore and migration."""

import json
import sqlite3
import tarfile
import time
from pathlib import Path

import pytest


# ── Backup Tests ──


@pytest.fixture
def fleet_dir(tmp_path):
    """Create a populated fleet directory for testing."""
    d = tmp_path / ".fleet-nerve"
    d.mkdir()

    # messages.db
    conn = sqlite3.connect(str(d / "messages.db"))
    conn.execute("CREATE TABLE messages (id TEXT, body TEXT)")
    conn.execute("INSERT INTO messages VALUES ('m1', 'hello')")
    conn.commit()
    conn.close()

    # leases.db
    conn = sqlite3.connect(str(d / "leases.db"))
    conn.execute("CREATE TABLE leases (id TEXT, holder TEXT)")
    conn.execute("INSERT INTO leases VALUES ('l1', 'mac1')")
    conn.commit()
    conn.close()

    # evidence.db
    conn = sqlite3.connect(str(d / "evidence.db"))
    conn.execute("CREATE TABLE evidence (id TEXT, event_type TEXT)")
    conn.commit()
    conn.close()

    # audit directory
    audit = d / "audit"
    audit.mkdir()
    (audit / "2026-04-05.jsonl").write_text('{"event":"test"}\n')

    # config
    (d / "config.json").write_text('{"fleetId":"test"}')

    # version
    (d / "version.json").write_text('{"version":"5.0.0"}')

    return d


@pytest.fixture
def backup(fleet_dir):
    from multifleet.backup import FleetBackup
    return FleetBackup(fleet_dir=str(fleet_dir))


class TestFleetBackup:
    def test_create_snapshot(self, backup, fleet_dir):
        path = backup.create_snapshot()
        assert Path(path).exists()
        assert path.endswith(".tar.gz")
        # Verify tarball contents
        with tarfile.open(path, "r:gz") as tar:
            names = tar.getnames()
            assert "messages.db" in names
            assert "snapshot-meta.json" in names

    def test_create_snapshot_custom_path(self, backup, tmp_path):
        out = tmp_path / "custom.tar.gz"
        path = backup.create_snapshot(output_path=str(out))
        assert path == str(out)
        assert out.exists()

    def test_snapshot_info(self, backup):
        path = backup.create_snapshot()
        info = backup.snapshot_info(path)
        assert "path" in info
        assert "size_bytes" in info
        assert info["entries"] > 0
        assert "metadata" in info
        assert info["metadata"]["version"] == "5.0.0"

    def test_snapshot_info_not_found(self, backup):
        with pytest.raises(FileNotFoundError):
            backup.snapshot_info("/nonexistent/snapshot.tar.gz")

    def test_list_snapshots_empty(self, tmp_path):
        from multifleet.backup import FleetBackup
        b = FleetBackup(fleet_dir=str(tmp_path / "empty"))
        assert b.list_snapshots() == []

    def test_list_snapshots(self, backup, tmp_path):
        # Use distinct output paths to avoid timestamp collision
        backup.create_snapshot(output_path=str(tmp_path / "fleet-snapshot-1000000001.tar.gz"))
        backup.create_snapshot(output_path=str(tmp_path / "fleet-snapshot-1000000002.tar.gz"))
        snaps = backup.list_snapshots(directory=str(tmp_path))
        assert len(snaps) >= 2

    def test_restore_snapshot(self, backup, fleet_dir, tmp_path):
        # Create snapshot from populated dir
        path = backup.create_snapshot()

        # Restore to a fresh dir
        from multifleet.backup import FleetBackup
        target_dir = tmp_path / "restored"
        target_dir.mkdir()
        b2 = FleetBackup(fleet_dir=str(target_dir))
        result = b2.restore_snapshot(path, force=True)

        assert result["restored_entries"] > 0
        assert (target_dir / "messages.db").exists()
        assert (target_dir / "config.json").exists()

    def test_restore_creates_pre_backup(self, backup, fleet_dir):
        path = backup.create_snapshot()
        # Restore over existing — should create pre-restore-backup
        result = backup.restore_snapshot(path)
        assert any("pre-restore-backup" in w for w in result["warnings"])
        assert (fleet_dir / "pre-restore-backup").exists()

    def test_restore_not_found(self, backup):
        with pytest.raises(FileNotFoundError):
            backup.restore_snapshot("/nonexistent.tar.gz")

    def test_restore_rejects_path_traversal(self, backup, tmp_path):
        # Create a malicious tarball with path traversal
        evil_tar = tmp_path / "evil.tar.gz"
        with tarfile.open(str(evil_tar), "w:gz") as tar:
            import io
            info = tarfile.TarInfo(name="../../../etc/evil")
            info.size = 4
            tar.addfile(info, io.BytesIO(b"evil"))

        with pytest.raises(ValueError, match="Unsafe path"):
            backup.restore_snapshot(str(evil_tar), force=True)

    def test_create_snapshot_no_fleet_dir(self, tmp_path):
        from multifleet.backup import FleetBackup
        b = FleetBackup(fleet_dir=str(tmp_path / "nonexistent"))
        with pytest.raises(FileNotFoundError):
            b.create_snapshot()

    def test_snapshot_metadata_has_files(self, backup):
        path = backup.create_snapshot()
        info = backup.snapshot_info(path)
        meta = info["metadata"]
        assert len(meta["files"]) > 0
        file_names = [f["name"] for f in meta["files"]]
        assert "messages.db" in file_names


# ── Migration Tests ──


@pytest.fixture
def mig_dir(tmp_path):
    d = tmp_path / ".fleet-nerve"
    d.mkdir()
    return d


@pytest.fixture
def migration(mig_dir):
    from multifleet.migration import FleetMigration
    return FleetMigration(fleet_dir=str(mig_dir))


class TestFleetMigration:
    def test_check_version_fresh(self, migration):
        result = migration.check_version()
        assert result["current_version"] is None
        assert result["expected_version"] == "5.0.0"
        assert not result["up_to_date"]

    def test_check_version_current(self, migration, mig_dir):
        (mig_dir / "version.json").write_text('{"version":"5.0.0"}')
        result = migration.check_version()
        assert result["up_to_date"]
        assert not result["needs_migration"]

    def test_check_version_outdated(self, migration, mig_dir):
        (mig_dir / "version.json").write_text('{"version":"4.0.0"}')
        result = migration.check_version()
        assert not result["up_to_date"]
        assert result["needs_migration"]

    def test_migrate_already_current(self, migration, mig_dir):
        (mig_dir / "version.json").write_text('{"version":"5.0.0"}')
        result = migration.migrate()
        assert result["status"] == "already_current"
        assert result["steps"] == []

    def test_migrate_4_to_5(self, migration, mig_dir):
        (mig_dir / "version.json").write_text('{"version":"4.0.0"}')
        result = migration.migrate()
        assert result["status"] == "ok"
        assert result["to_version"] == "5.0.0"
        assert len(result["steps"]) == 1
        assert result["steps"][0]["status"] == "ok"
        # Evidence DB should be created
        assert (mig_dir / "evidence.db").exists()
        # Version file updated
        v = json.loads((mig_dir / "version.json").read_text())
        assert v["version"] == "5.0.0"

    def test_migrate_3_to_5(self, migration, mig_dir):
        (mig_dir / "version.json").write_text('{"version":"3.0.0"}')
        result = migration.migrate()
        assert result["status"] == "ok"
        assert result["to_version"] == "5.0.0"
        assert len(result["steps"]) == 2
        # Audit dir created by 3->4
        assert (mig_dir / "audit").is_dir()
        # Evidence DB created by 4->5
        assert (mig_dir / "evidence.db").exists()

    def test_migrate_from_none(self, migration, mig_dir):
        # No version.json — defaults to 3.0.0
        result = migration.migrate()
        assert result["status"] == "ok"
        assert result["from_version"] == "3.0.0"
        assert result["to_version"] == "5.0.0"

    def test_migrate_4_to_5_with_existing_messages_db(self, migration, mig_dir):
        (mig_dir / "version.json").write_text('{"version":"4.0.0"}')
        # Create messages.db with leases table
        conn = sqlite3.connect(str(mig_dir / "messages.db"))
        conn.execute("CREATE TABLE leases (id TEXT, holder TEXT, expiry REAL)")
        conn.commit()
        conn.close()

        result = migration.migrate()
        assert result["status"] == "ok"

        # probe_status column should be added
        conn = sqlite3.connect(str(mig_dir / "messages.db"))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(leases)").fetchall()}
        conn.close()
        assert "probe_status" in cols
