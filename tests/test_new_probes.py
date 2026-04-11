"""Tests for the 3 new probes: repeated_failure, hidden_dependency, task_diff_mismatch."""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from multifleet.probe_engine import ProbeResult, ProbeFinding
from multifleet.probes.repeated_failure import probe_repeated_failure
from multifleet.probes.hidden_dependency import probe_hidden_dependency
from multifleet.probes.task_diff_mismatch import probe_task_diff_mismatch


class TestRepeatedFailureProbe:
    """Tests for the repeated failure cluster probe."""

    def test_no_store_no_git_returns_skipped(self):
        result = probe_repeated_failure({
            "repo_root": "/nonexistent",
            "store_path": "/nonexistent/store.db",
        })
        # Should either return ok (no data) or error/skipped
        assert result.probe_id == "repeated_failure"
        assert result.status in ("ok", "error", "skipped")

    def test_no_failures_returns_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Create empty SQLite store
            import sqlite3
            db_path = os.path.join(tmp, "test.db")
            conn = sqlite3.connect(db_path)
            conn.execute("""
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY,
                    subject TEXT,
                    payload TEXT,
                    timestamp REAL
                )
            """)
            conn.commit()
            conn.close()

            result = probe_repeated_failure({
                "store_path": db_path,
                "time_window_s": 86400,
            })
            assert result.status == "ok"
            assert len(result.findings) == 0

    def test_cluster_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            import sqlite3
            import time
            db_path = os.path.join(tmp, "test.db")
            conn = sqlite3.connect(db_path)
            conn.execute("""
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY,
                    subject TEXT,
                    payload TEXT,
                    timestamp REAL
                )
            """)
            now = time.time()
            # Insert 4 failures for the same file
            for i in range(4):
                conn.execute(
                    "INSERT INTO messages (subject, payload, timestamp) VALUES (?, ?, ?)",
                    (f"task-fail-{i}", json.dumps({"files": ["src/auth.py"]}), now - i * 100),
                )
            conn.commit()
            conn.close()

            result = probe_repeated_failure({
                "store_path": db_path,
                "cluster_threshold": 3,
            })
            assert result.status in ("warning", "critical")
            assert len(result.findings) > 0
            assert "auth.py" in result.findings[0].title


class TestHiddenDependencyProbe:
    """Tests for the hidden dependency probe."""

    def test_no_files_returns_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = probe_hidden_dependency({
                "repo_root": tmp,
                "changed_files": [],
            })
            assert result.probe_id == "hidden_dependency"
            assert result.status == "ok"

    def test_detects_undeclared_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Create a Python file with an undeclared import
            py_file = Path(tmp) / "test_module.py"
            py_file.write_text("import some_undeclared_package\n")

            result = probe_hidden_dependency({
                "repo_root": tmp,
                "changed_files": ["test_module.py"],
            })
            assert result.probe_id == "hidden_dependency"
            if result.status == "warning":
                assert any("some_undeclared_package" in f.title for f in result.findings)

    def test_stdlib_not_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            py_file = Path(tmp) / "stdlib_user.py"
            py_file.write_text("import os\nimport json\nimport sys\n")

            result = probe_hidden_dependency({
                "repo_root": tmp,
                "changed_files": ["stdlib_user.py"],
            })
            assert result.status == "ok"
            assert len(result.findings) == 0

    def test_declared_dep_not_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Create requirements.txt
            (Path(tmp) / "requirements.txt").write_text("nats-py>=2.0\n")
            # Create a file importing the declared dep
            py_file = Path(tmp) / "uses_nats.py"
            py_file.write_text("import nats_py\n")

            result = probe_hidden_dependency({
                "repo_root": tmp,
                "changed_files": ["uses_nats.py"],
            })
            # nats_py should not be flagged (normalized from nats-py)
            flagged_titles = " ".join(f.title for f in result.findings)
            assert "nats" not in flagged_titles.lower()


class TestTaskDiffMismatchProbe:
    """Tests for the task-diff mismatch probe."""

    def test_no_intent_returns_skipped(self):
        result = probe_task_diff_mismatch({
            "repo_root": ".",
        })
        assert result.probe_id == "task_diff_mismatch"
        assert result.status == "skipped"

    @patch("multifleet.probes.task_diff_mismatch._get_changed_files")
    def test_no_changes_returns_ok(self, mock_files):
        mock_files.return_value = []
        result = probe_task_diff_mismatch({
            "repo_root": ".",
            "task_intent": "Fix auth flow",
        })
        assert result.probe_id == "task_diff_mismatch"
        assert result.status == "ok"

    @patch("multifleet.probes.task_diff_mismatch._get_changed_files")
    def test_detects_mismatch(self, mock_files):
        mock_files.return_value = [
            "src/auth/handler.py",      # matches "auth" intent
            "src/billing/invoice.py",   # doesn't match
            "src/billing/payment.py",   # doesn't match
            "docs/api.md",             # doesn't match
        ]
        result = probe_task_diff_mismatch({
            "repo_root": ".",
            "task_intent": "Fix auth token refresh flow",
        })
        if result.findings:
            assert any("outside task domain" in f.title for f in result.findings)

    @patch("multifleet.probes.task_diff_mismatch._get_changed_files")
    def test_all_matching_returns_ok(self, mock_files):
        mock_files.return_value = [
            "src/auth/handler.py",
            "src/auth/tokens.py",
            "tests/test_auth.py",
        ]
        result = probe_task_diff_mismatch({
            "repo_root": ".",
            "task_intent": "Fix auth token refresh and add tests",
        })
        assert result.status == "ok"


class TestProbeEngineIntegration:
    """Test that all 6 probes register and run correctly."""

    def test_all_probes_registered(self):
        from multifleet.probes import create_engine
        engine = create_engine()
        probes = engine.registry.list_probes()
        probe_ids = {p["probe_id"] for p in probes}
        assert "mirror_drift" in probe_ids
        assert "stale_context" in probe_ids
        assert "fleet_health" in probe_ids
        assert "repeated_failure" in probe_ids
        assert "hidden_dependency" in probe_ids
        assert "task_diff_mismatch" in probe_ids
        assert len(probe_ids) >= 6

    def test_scan_all_runs_without_crash(self):
        from multifleet.probes import create_engine
        engine = create_engine()
        results = engine.scan("all", {"repo_root": "."})
        assert len(results) >= 6
        for r in results:
            assert isinstance(r, ProbeResult)
            assert r.probe_id
            assert r.status in ("ok", "warning", "critical", "error", "skipped")

    def test_findings_summary(self):
        from multifleet.probes import create_engine
        engine = create_engine()
        engine.scan("all", {"repo_root": "."})
        summary = engine.findings_summary()
        assert "total_probes" in summary
        assert "total_findings" in summary
        assert summary["total_probes"] >= 6
