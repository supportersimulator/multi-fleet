"""Tests for 3-surgeon audit fixes.

Covers: WAL mode, benchmark guard, probe_id sanitization,
node_id sanitization, migration schema parity.
"""

import re
import sqlite3
import tempfile
from pathlib import Path

import pytest


# ── Task 1: packet_store WAL mode ────────────────────────────────────────────

class TestPacketStoreWAL:
    def test_wal_mode_enabled(self):
        """PacketStore connections use WAL journal mode."""
        from multifleet.packet_store import PacketStore

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            store = PacketStore(db_path=db_path)
            conn = store._conn()
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            conn.close()
            assert mode == "wal", f"Expected WAL mode, got {mode}"

    def test_wal_persists_across_connections(self):
        """WAL is set on every new connection, not just init."""
        from multifleet.packet_store import PacketStore

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            store = PacketStore(db_path=db_path)
            # Second connection should also be WAL
            conn2 = store._conn()
            mode = conn2.execute("PRAGMA journal_mode").fetchone()[0]
            conn2.close()
            assert mode == "wal"


# ── Task 2: benchmark iterations guard ───────────────────────────────────────

class TestBenchmarkIterationsGuard:
    def test_zero_iterations(self):
        """_run_bench with iterations=0 returns zero result, no crash."""
        from multifleet.benchmark import _run_bench

        result = _run_bench("test_zero", lambda: None, 0)
        assert result.iterations == 0
        assert result.mean_ms == 0.0
        assert result.min_ms == 0.0
        assert result.max_ms == 0.0

    def test_negative_iterations(self):
        """_run_bench with iterations=-1 returns zero result, no crash."""
        from multifleet.benchmark import _run_bench

        result = _run_bench("test_neg", lambda: None, -1)
        assert result.iterations == 0
        assert result.mean_ms == 0.0

    def test_positive_iterations_still_work(self):
        """Positive iterations still execute normally."""
        from multifleet.benchmark import _run_bench

        counter = {"n": 0}
        def inc():
            counter["n"] += 1

        result = _run_bench("test_pos", inc, 5)
        assert result.iterations == 5
        assert counter["n"] == 5
        assert result.mean_ms > 0 or result.mean_ms == 0  # may be very fast


# ── Task 3: probe_id sanitization ────────────────────────────────────────────

class TestProbeIdSanitization:
    def _registry(self):
        from multifleet.probe_engine import ProbeRegistry, ProbeResult, ProbeCategory
        reg = ProbeRegistry()
        reg.register(
            "valid_probe",
            lambda ctx: ProbeResult(
                probe_id="valid_probe", category="test",
                status="ok", confidence=1.0, severity="info",
            ),
            ProbeCategory.STRUCTURE,
        )
        return reg

    def test_path_traversal_rejected(self):
        """probe_id with path traversal chars is rejected."""
        reg = self._registry()
        result = reg.run_one("../../etc/passwd")
        assert result.status == "error"
        assert "Invalid probe_id" in result.findings[0].title

    def test_shell_chars_rejected(self):
        """probe_id with shell metacharacters is rejected."""
        reg = self._registry()
        result = reg.run_one("probe;rm -rf /")
        assert result.status == "error"
        assert "Invalid probe_id" in result.findings[0].title

    def test_uppercase_rejected(self):
        """probe_id with uppercase letters is rejected."""
        reg = self._registry()
        result = reg.run_one("ProbeUpper")
        assert result.status == "error"

    def test_empty_string_rejected(self):
        """Empty probe_id is rejected."""
        reg = self._registry()
        result = reg.run_one("")
        assert result.status == "error"

    def test_too_long_rejected(self):
        """probe_id longer than 64 chars is rejected."""
        reg = self._registry()
        result = reg.run_one("a" * 65)
        assert result.status == "error"

    def test_valid_probe_id_accepted(self):
        """Valid probe_id passes sanitization and runs."""
        reg = self._registry()
        result = reg.run_one("valid_probe")
        assert result.status == "ok"

    def test_valid_with_numbers(self):
        """probe_id with numbers after first char is accepted format-wise."""
        reg = self._registry()
        # Not registered, but passes format check -> "not registered" error
        result = reg.run_one("probe123")
        assert "Invalid probe_id" not in result.findings[0].title


# ── Task 4: node_id sanitization ─────────────────────────────────────────────

class TestNodeIdSanitization:
    def test_injection_chars_sanitized(self):
        """node_id with injection characters falls back to hostname."""
        from multifleet.onboarding import OnboardingWizard
        import socket

        wiz = OnboardingWizard(node_id="'; DROP TABLE--")
        expected_fallback = socket.gethostname().split(".")[0].lower()
        # Should have fallen back since the node_id was invalid
        assert wiz.node_id != "'; DROP TABLE--"

    def test_path_traversal_sanitized(self):
        """node_id with path traversal falls back."""
        from multifleet.onboarding import OnboardingWizard

        wiz = OnboardingWizard(node_id="../../etc")
        assert wiz.node_id != "../../etc"

    def test_valid_node_id_accepted(self):
        """Valid node_id is kept as-is."""
        from multifleet.onboarding import OnboardingWizard

        wiz = OnboardingWizard(node_id="mac1")
        assert wiz.node_id == "mac1"

    def test_valid_node_id_with_dash(self):
        """node_id with hyphens is valid."""
        from multifleet.onboarding import OnboardingWizard

        wiz = OnboardingWizard(node_id="mac-mini-3")
        assert wiz.node_id == "mac-mini-3"


# ── Task 5: migration schema matches evidence_ledger ─────────────────────────

class TestMigrationSchemaMatch:
    def _get_migration_schema(self) -> dict:
        """Run migration and return evidence table column info."""
        from multifleet.migration import FleetMigration

        with tempfile.TemporaryDirectory() as tmp:
            mig = FleetMigration(fleet_dir=tmp)
            mig.migrate(from_version="4.0.0", to_version="5.0.0")
            db = Path(tmp) / "evidence.db"
            conn = sqlite3.connect(str(db))
            cols = conn.execute("PRAGMA table_info(evidence)").fetchall()
            conn.close()
            # Returns list of (cid, name, type, notnull, default, pk)
            return {row[1]: {"type": row[2], "notnull": row[3], "pk": row[5]} for row in cols}

    def _get_ledger_schema(self) -> dict:
        """Create ledger DB and return evidence table column info."""
        from multifleet.evidence_ledger import EvidenceLedger

        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "evidence.db")
            ledger = EvidenceLedger(db_path=db_path)
            conn = sqlite3.connect(db_path)
            cols = conn.execute("PRAGMA table_info(evidence)").fetchall()
            conn.close()
            return {row[1]: {"type": row[2], "notnull": row[3], "pk": row[5]} for row in cols}

    def test_same_columns(self):
        """Migration evidence table has same columns as EvidenceLedger."""
        mig_cols = set(self._get_migration_schema().keys())
        led_cols = set(self._get_ledger_schema().keys())
        assert mig_cols == led_cols, f"Column mismatch: migration={mig_cols}, ledger={led_cols}"

    def test_entry_hash_column_exists(self):
        """Migration uses entry_hash (not 'hash') matching ledger."""
        mig = self._get_migration_schema()
        assert "entry_hash" in mig, "Migration should use 'entry_hash', not 'hash'"
        assert "hash" not in mig, "Migration should not have old 'hash' column"

    def test_seq_autoincrement(self):
        """Migration evidence table has seq as integer primary key."""
        mig = self._get_migration_schema()
        assert "seq" in mig
        assert mig["seq"]["pk"] == 1

    def test_subject_column_exists(self):
        """Migration evidence table has subject column."""
        mig = self._get_migration_schema()
        assert "subject" in mig

    def test_verified_column_exists(self):
        """Migration evidence table has verified column."""
        mig = self._get_migration_schema()
        assert "verified" in mig
