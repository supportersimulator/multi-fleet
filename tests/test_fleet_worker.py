"""
Tests for fleet_remote_worker.py — worker launch, task dispatch, result collection.
"""

import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# We need to mock SCRIPTS_DIR before import
with patch("pathlib.Path.home") as mock_home:
    mock_home.return_value = Path("/fake/home")
    # Prevent the glob scan at import time
    import tools.fleet_remote_worker as worker_mod
    from tools.fleet_remote_worker import FleetWorkerManager


class TestWorkerManagerInit:
    """FleetWorkerManager initialization and config loading."""

    def test_node_id_from_env(self):
        with patch.dict(os.environ, {"MULTIFLEET_NODE_ID": "test-node"}):
            assert worker_mod._get_node_id() == "test-node"

    def test_node_id_hostname_fallback(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("MULTIFLEET_NODE_ID", None)
            with patch("socket.gethostname", return_value="WorkStation.local"):
                assert worker_mod._get_node_id() == "workstation"

    def test_config_loading_from_file(self, json_config_file, tmp_path):
        with patch.object(worker_mod, "REPO_ROOT", tmp_path):
            cfg = worker_mod._get_config()
            assert "nodes" in cfg
            assert cfg["nodes"]["mac1"]["host"] == "TEST_IP_1"


class TestPeerSSH:
    """SSH target resolution for remote workers."""

    def test_local_target_returns_none(self, mock_worker_scripts):
        with patch.object(worker_mod, "SCRIPTS_DIR", mock_worker_scripts):
            with patch.dict(os.environ, {"MULTIFLEET_NODE_ID": "mac1"}):
                mgr = FleetWorkerManager()
                mgr.node_id = "mac1"
                assert mgr._peer_ssh("mac1") is None

    def test_remote_target_returns_ssh_string(self, mock_worker_scripts):
        with patch.object(worker_mod, "SCRIPTS_DIR", mock_worker_scripts):
            with patch.dict(os.environ, {"MULTIFLEET_NODE_ID": "mac1"}):
                mgr = FleetWorkerManager()
                mgr.config = {
                    "nodes": {"mac2": {"user": "bob", "host": "TEST_IP_2"}},
                }
                result = mgr._peer_ssh("mac2")
                assert result == "bob@TEST_IP_2"

    def test_unknown_target_returns_none(self, mock_worker_scripts):
        with patch.object(worker_mod, "SCRIPTS_DIR", mock_worker_scripts):
            mgr = FleetWorkerManager()
            mgr.config = {"nodes": {}}
            result = mgr._peer_ssh("unknown-node")
            assert result is None


class TestWorkerLaunch:
    """Worker launch and lifecycle."""

    def test_launch_local_worker(self, mock_worker_scripts):
        with patch.object(worker_mod, "SCRIPTS_DIR", mock_worker_scripts):
            mgr = FleetWorkerManager()
            mgr.node_id = "mac1"

            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(
                    returncode=0,
                    stdout=json.dumps({"session_id": "sess123", "tmux_name": "w1"}),
                    stderr="",
                )
                result = mgr.launch_worker("mac1", "test-worker")

                assert result["session_id"] == "sess123"
                assert result["target"] == "mac1"
                assert "test-worker" in mgr.active_workers

    def test_launch_failure_returns_error(self, mock_worker_scripts):
        with patch.object(worker_mod, "SCRIPTS_DIR", mock_worker_scripts):
            mgr = FleetWorkerManager()

            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(
                    returncode=1,
                    stdout="",
                    stderr="tmux failed",
                )
                result = mgr.launch_worker("mac1", "test-worker")
                assert "error" in result
                assert "tmux failed" in result["error"]

    def test_list_workers_empty(self, mock_worker_scripts):
        with patch.object(worker_mod, "SCRIPTS_DIR", mock_worker_scripts):
            mgr = FleetWorkerManager()
            assert mgr.list_workers() == []


class TestWorkerResults:
    """Result collection and storage."""

    def test_send_task_saves_result(self, mock_worker_scripts, tmp_path):
        with patch.object(worker_mod, "SCRIPTS_DIR", mock_worker_scripts):
            mgr = FleetWorkerManager()
            mgr._results_dir = tmp_path
            mgr.active_workers["w1"] = {
                "session_id": "sess123",
                "tmux_name": "w1",
                "target": "mac1",
                "started_at": time.time(),
            }

            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(
                    returncode=0, stdout="task completed successfully", stderr="",
                )
                result = mgr.send_task("w1", "run tests")

            assert result == "task completed successfully"
            result_files = list(tmp_path.glob("*.json"))
            assert len(result_files) == 1
            saved = json.loads(result_files[0].read_text())
            assert saved["worker"] == "w1"
            assert "task completed" in saved["response"]

    def test_send_task_to_unknown_worker(self, mock_worker_scripts):
        with patch.object(worker_mod, "SCRIPTS_DIR", mock_worker_scripts):
            mgr = FleetWorkerManager()
            result = mgr.send_task("nonexistent", "do something")
            assert "Error" in result

    def test_get_recent_results(self, mock_worker_scripts, tmp_path):
        with patch.object(worker_mod, "SCRIPTS_DIR", mock_worker_scripts):
            mgr = FleetWorkerManager()
            mgr._results_dir = tmp_path

            # Create some result files
            for i in range(3):
                (tmp_path / f"w-{i}.json").write_text(json.dumps({"worker": f"w{i}"}))

            results = mgr.get_recent_results(limit=2)
            assert len(results) == 2
