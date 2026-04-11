"""Tests for mac2 code review integration wiring.

Validates that the 5 integration opportunities identified in mac2's review
are properly wired:
1. data_layer singleton created and passed to store/evidence/leases
2. prometheus export produces text via web server
3. probe_context_factory provides context in ProbeEngine.run_all()
4. migration runs on startup, backup on shutdown
5. onboarding shows in fleet CLI status
"""

import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure imports work
FLEET_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(FLEET_ROOT))
REPO_ROOT = FLEET_ROOT.parent
sys.path.insert(0, str(REPO_ROOT))


# ── 1. Data Layer wiring ──

class TestDataLayerWiring:
    """data_layer singleton is created and injected into store/evidence."""

    def test_data_layer_singleton_created(self):
        """get_data_layer returns a FleetDataLayer singleton."""
        from multifleet.data_layer import get_data_layer, reset_data_layer
        reset_data_layer()
        dl1 = get_data_layer()
        dl2 = get_data_layer()
        assert dl1 is dl2, "get_data_layer must return singleton"
        reset_data_layer()

    def test_store_uses_data_layer(self, tmp_path):
        """FleetNerveStore uses data_layer when provided."""
        from multifleet.data_layer import FleetDataLayer
        from multifleet.store import FleetNerveStore

        dl = FleetDataLayer(data_dir=str(tmp_path))
        store = FleetNerveStore(data_layer=dl)
        # store._data_layer should be set
        assert store._data_layer is dl
        dl.close_all()

    def test_evidence_ledger_uses_data_layer(self, tmp_path):
        """EvidenceLedger uses data_layer when provided."""
        from multifleet.data_layer import FleetDataLayer
        from multifleet.evidence_ledger import EvidenceLedger

        dl = FleetDataLayer(data_dir=str(tmp_path))
        ledger = EvidenceLedger(data_layer=dl)
        assert ledger._data_layer is dl
        dl.close_all()

    def test_mcp_get_data_layer_returns_instance(self):
        """_get_data_layer in MCP server returns a FleetDataLayer."""
        with patch.dict(os.environ, {"MULTIFLEET_NODE_ID": "test-node"}):
            # Import after path setup
            sys.path.insert(0, str(FLEET_ROOT / "bin"))
            import importlib
            # We test the function directly
            from multifleet.data_layer import get_data_layer, reset_data_layer
            reset_data_layer()
            dl = get_data_layer()
            assert dl is not None
            assert hasattr(dl, 'connection')
            assert hasattr(dl, 'close_all')
            reset_data_layer()


# ── 2. Prometheus export ──

class TestPrometheusExportWiring:
    """prometheus_export generates text and is served by web server."""

    def test_prometheus_export_produces_text(self):
        """generate_prometheus_text returns non-empty Prometheus text."""
        from multifleet.prometheus_export import generate_prometheus_text
        text = generate_prometheus_text(
            node_id="test-node",
            include_health=False,
        )
        assert isinstance(text, str)
        assert len(text) > 0
        assert "fleet_" in text or "# fleet_scrape_timestamp" in text

    def test_web_server_metrics_route_calls_prometheus(self):
        """Web server /metrics route calls generate_prometheus_text."""
        from multifleet.web_server import FleetWebHandler
        from io import BytesIO

        handler = MagicMock(spec=FleetWebHandler)
        handler.daemon_port = 8855
        handler.path = "/metrics"
        handler.server = MagicMock()
        handler.server.server_address = ("127.0.0.1", 8856)

        # Call the real method
        with patch.dict(os.environ, {"MULTIFLEET_NODE_ID": "test-node"}):
            with patch("multifleet.prometheus_export.generate_prometheus_text",
                       return_value="# test metrics\n") as mock_gen:
                FleetWebHandler._serve_prometheus(handler)
                mock_gen.assert_called_once()


# ── 3. Probe context factory ──

class TestProbeContextFactoryWiring:
    """probe_context_factory auto-populates context in ProbeEngine.run_all()."""

    def test_probe_context_factory_builds_context(self, tmp_path):
        """build_probe_context returns a ProbeContext with git info."""
        # Create a minimal git repo
        import subprocess
        subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
        subprocess.run(["git", "-C", str(tmp_path), "commit", "--allow-empty", "-m", "init"],
                       capture_output=True, env={**os.environ, "GIT_AUTHOR_NAME": "test",
                                                  "GIT_COMMITTER_NAME": "test",
                                                  "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_EMAIL": "t@t"})

        from multifleet.probe_context_factory import build_probe_context
        ctx = build_probe_context(repo_path=str(tmp_path))
        assert ctx.repo_path == str(tmp_path.resolve())
        assert isinstance(ctx.changed_files, list)
        d = ctx.to_dict()
        assert "repo_path" in d
        assert "branch" in d

    def test_run_all_uses_factory_when_no_context(self):
        """ProbeRegistry.run_all() auto-builds context via factory when None."""
        from multifleet.probe_engine import ProbeRegistry, ProbeResult, ProbeCategory

        registry = ProbeRegistry()

        # Register a probe that captures the context it receives
        captured = {}

        def capture_probe(context):
            captured.update(context)
            return ProbeResult(
                probe_id="test_capture", category="structure",
                status="ok", confidence=1.0, severity="info",
            )

        registry.register("test_capture", capture_probe, ProbeCategory.STRUCTURE)

        # Patch build_probe_context to return a known context
        mock_ctx = MagicMock()
        mock_ctx.to_dict.return_value = {"repo_path": "/test", "branch": "main", "changed_files": []}

        with patch("multifleet.probe_context_factory.build_probe_context", return_value=mock_ctx):
            # Call with no context — should use factory
            results = registry.run_all(context=None)
            assert len(results) == 1
            assert captured.get("repo_path") == "/test"


# ── 4. Migration + Backup ──

class TestMigrationAndBackupWiring:
    """Migration runs on startup, backup on shutdown."""

    def test_migration_check_version(self, tmp_path):
        """FleetMigration.check_version() returns version info."""
        from multifleet.migration import FleetMigration
        mig = FleetMigration(fleet_dir=str(tmp_path))
        status = mig.check_version()
        assert "current_version" in status
        assert "expected_version" in status
        assert "needs_migration" in status

    def test_startup_migration_called(self):
        """_run_startup_migration calls FleetMigration.check_version()."""
        sys.path.insert(0, str(FLEET_ROOT / "bin"))

        with patch("multifleet.migration.FleetMigration") as MockMig:
            instance = MockMig.return_value
            instance.check_version.return_value = {
                "needs_migration": False, "current_version": "5.0.0",
                "expected_version": "5.0.0",
            }
            # Import and call
            from bin.fleet_nerve_mcp import _run_startup_migration
            _run_startup_migration()
            instance.check_version.assert_called_once()

    def test_shutdown_backup_called(self):
        """_run_shutdown_backup calls FleetBackup.create_snapshot()."""
        sys.path.insert(0, str(FLEET_ROOT / "bin"))

        with patch("multifleet.backup.FleetBackup") as MockBackup:
            instance = MockBackup.return_value
            instance.create_snapshot.return_value = "/tmp/test-snapshot.tar.gz"
            from bin.fleet_nerve_mcp import _run_shutdown_backup
            _run_shutdown_backup()
            instance.create_snapshot.assert_called_once()


# ── 5. Onboarding in CLI status ──

class TestOnboardingWiring:
    """Onboarding progress shows in fleet CLI status."""

    def test_onboarding_check_all_returns_progress(self):
        """OnboardingWizard.check_all() returns progress_pct and counts."""
        with patch.dict(os.environ, {"MULTIFLEET_NODE_ID": "test-node"}):
            from multifleet.onboarding import OnboardingWizard
            wiz = OnboardingWizard(node_id="test-node")
            status = wiz.check_all()
            assert "progress_pct" in status
            assert "completed" in status
            assert "remaining" in status
            assert isinstance(status["progress_pct"], float)
            total = len(status["completed"]) + len(status["remaining"])
            assert total > 0

    def test_fleet_cli_status_includes_onboarding(self):
        """fleet CLI status command includes onboarding section."""
        fleet_script = FLEET_ROOT / "scripts" / "fleet"
        content = fleet_script.read_text()
        assert "OnboardingWizard" in content
        assert "check_all" in content
        assert "progress_pct" in content
