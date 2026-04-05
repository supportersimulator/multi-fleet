"""
Tests for fleet_doctor MCP tool — 6 diagnostic health checks.

All network/subprocess/filesystem calls are mocked. Tests verify logic, not infrastructure.
"""

import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

# Ensure tools/ and repo root are importable
REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "multi-fleet"))
sys.path.insert(0, str(REPO_ROOT / "multi-fleet" / "bin"))

# Patch _get_node_id before importing the module so NODE_ID is deterministic
with patch.dict(os.environ, {"MULTIFLEET_NODE_ID": "mac3"}):
    from bin.fleet_nerve_mcp import (
        fleet_doctor,
        _check_daemon_health,
        _check_nats_connectivity,
        _check_peer_reachability,
        _check_keychain_secrets,
        _check_message_store,
        _check_python_deps,
        _make_recommendation,
    )


# ── Helpers ──

def _mock_daemon_health(status="ok", uptime_s=3600, transport="nats", nats_url="nats://127.0.0.1:4222",
                         channel_health=None, peers=None, error=None):
    """Build a mock daemon /health response."""
    if error:
        return {"error": error}
    data = {"status": status, "uptime_s": uptime_s, "transport": transport}
    if nats_url:
        data["nats_url"] = nats_url
    if channel_health is not None:
        data["channel_health"] = channel_health
    if peers is not None:
        data["peers"] = peers
    return data


# ── FN-0001: daemon_health ──

class TestDaemonHealth:

    @patch("bin.fleet_nerve_mcp._daemon_get")
    def test_healthy_daemon(self, mock_get):
        mock_get.return_value = _mock_daemon_health()
        result = _check_daemon_health()
        assert result["code"] == "FN-0001"
        assert result["name"] == "daemon_health"
        assert result["passed"] is True
        assert result["critical"] is True
        assert "3600" in result["message"]
        assert "nats" in result["message"]
        assert result["latency_ms"] >= 0

    @patch("bin.fleet_nerve_mcp._daemon_get")
    def test_unreachable_daemon(self, mock_get):
        mock_get.return_value = _mock_daemon_health(error="Connection refused")
        result = _check_daemon_health()
        assert result["passed"] is False
        assert "unreachable" in result["message"].lower()

    @patch("bin.fleet_nerve_mcp._daemon_get")
    def test_degraded_status(self, mock_get):
        mock_get.return_value = _mock_daemon_health(status="degraded")
        result = _check_daemon_health()
        assert result["passed"] is False
        assert "degraded" in result["message"]


# ── FN-0002: nats_connectivity ──

class TestNatsConnectivity:

    @patch("bin.fleet_nerve_mcp._daemon_get")
    def test_nats_connected(self, mock_get):
        mock_get.return_value = _mock_daemon_health(transport="nats", nats_url="nats://10.0.0.1:4222")
        result = _check_nats_connectivity()
        assert result["code"] == "FN-0002"
        assert result["passed"] is True
        assert "10.0.0.1" in result["message"]

    @patch("bin.fleet_nerve_mcp._daemon_get")
    def test_http_transport(self, mock_get):
        mock_get.return_value = _mock_daemon_health(transport="http")
        result = _check_nats_connectivity()
        assert result["passed"] is False
        assert "http" in result["message"]

    @patch("bin.fleet_nerve_mcp._daemon_get")
    def test_daemon_unreachable(self, mock_get):
        mock_get.return_value = _mock_daemon_health(error="timeout")
        result = _check_nats_connectivity()
        assert result["passed"] is False
        assert result["critical"] is True


# ── FN-0003: peer_reachability ──

class TestPeerReachability:

    @patch("bin.fleet_nerve_mcp._daemon_get")
    def test_peers_with_channel_health(self, mock_get):
        mock_get.return_value = _mock_daemon_health(
            channel_health={"mac1": {"up": 5, "total": 7}, "mac2": {"up": 7, "total": 7}},
        )
        result = _check_peer_reachability()
        assert result["code"] == "FN-0003"
        assert result["passed"] is True
        assert result["critical"] is False
        assert "mac1: 5/7" in result["message"]
        assert "mac2: 7/7" in result["message"]

    @patch("bin.fleet_nerve_mcp._daemon_get")
    def test_no_peers_reachable(self, mock_get):
        mock_get.return_value = _mock_daemon_health(
            channel_health={"mac1": {"up": 0, "total": 7}, "mac2": {"up": 0, "total": 7}},
        )
        result = _check_peer_reachability()
        assert result["passed"] is False

    @patch("bin.fleet_nerve_mcp._daemon_get")
    def test_fallback_to_peers_list(self, mock_get):
        mock_get.return_value = _mock_daemon_health(peers=["mac1", "mac2"])
        result = _check_peer_reachability()
        assert result["passed"] is True
        assert "2 peer(s)" in result["message"]

    @patch("bin.fleet_nerve_mcp._daemon_get")
    def test_no_peer_data(self, mock_get):
        mock_get.return_value = _mock_daemon_health()
        result = _check_peer_reachability()
        assert result["passed"] is False
        assert "no peer data" in result["message"].lower()


# ── FN-0004: keychain_secrets ──

class TestKeychainSecrets:

    @patch("subprocess.run")
    def test_keys_found(self, mock_run):
        # Both key groups found on first try
        mock_run.return_value = MagicMock(returncode=0, stdout="sk-test-key-value\n")
        result = _check_keychain_secrets()
        assert result["code"] == "FN-0004"
        assert result["passed"] is True
        assert result["critical"] is True
        # Key VALUE must NOT appear in message (security)
        assert "sk-test-key-value" not in result["message"]
        assert "OPENAI_API_KEY" in result["message"]

    @patch("subprocess.run")
    def test_no_keys_found(self, mock_run):
        mock_run.return_value = MagicMock(returncode=44, stdout="")
        result = _check_keychain_secrets()
        assert result["passed"] is False
        assert "No API keys" in result["message"]

    @patch("subprocess.run")
    def test_partial_keys(self, mock_run):
        # First call succeeds (OPENAI), rest fail
        def side_effect(*args, **kwargs):
            key_name = args[0][5]  # -a argument position
            if key_name == "OPENAI_API_KEY":
                return MagicMock(returncode=0, stdout="found\n")
            return MagicMock(returncode=44, stdout="")
        mock_run.side_effect = side_effect
        result = _check_keychain_secrets()
        assert result["passed"] is True
        assert "missing" in result["message"].lower()

    @patch("subprocess.run")
    def test_security_command_timeout(self, mock_run):
        import subprocess as sp
        mock_run.side_effect = sp.TimeoutExpired("security", 5)
        result = _check_keychain_secrets()
        assert result["passed"] is False


# ── FN-0005: message_store ──

class TestMessageStore:

    def test_healthy_store(self, tmp_path):
        db_path = tmp_path / "messages.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, data TEXT)")
        conn.execute("INSERT INTO messages VALUES (1, 'test')")
        conn.execute("INSERT INTO messages VALUES (2, 'test2')")
        conn.commit()
        conn.close()

        with patch("bin.fleet_nerve_mcp.Path.home", return_value=tmp_path / "fake_home"):
            # Create the expected path
            fleet_dir = tmp_path / "fake_home" / ".fleet-nerve"
            fleet_dir.mkdir(parents=True)
            actual_db = fleet_dir / "messages.db"
            # Copy the db
            import shutil
            shutil.copy(str(db_path), str(actual_db))

            result = _check_message_store()
            assert result["code"] == "FN-0005"
            assert result["passed"] is True
            assert "2 messages" in result["message"]

    def test_missing_db(self, tmp_path):
        with patch("bin.fleet_nerve_mcp.Path.home", return_value=tmp_path):
            result = _check_message_store()
            assert result["passed"] is False
            assert "not found" in result["message"].lower()


# ── FN-0006: python_deps ──

class TestPythonDeps:

    def test_python_version_ok(self):
        result = _check_python_deps()
        assert result["code"] == "FN-0006"
        assert result["critical"] is False
        # Running tests requires Python >= 3.10, so this should pass
        assert result["passed"] is True
        assert "Python" in result["message"]

    def test_nats_import_reported(self):
        result = _check_python_deps()
        # nats may or may not be installed; just verify it's reported
        assert "nats:" in result["message"]

    def test_mcp_import_reported(self):
        result = _check_python_deps()
        assert "mcp:" in result["message"]


# ── Recommendation ──

class TestRecommendation:

    def test_all_passed(self):
        checks = [
            {"passed": True, "critical": True, "name": "a"},
            {"passed": True, "critical": False, "name": "b"},
        ]
        rec = _make_recommendation(checks)
        assert "All checks passed" in rec

    def test_critical_failure(self):
        checks = [
            {"passed": False, "critical": True, "name": "daemon_health"},
            {"passed": True, "critical": False, "name": "python_deps"},
        ]
        rec = _make_recommendation(checks)
        assert "CRITICAL" in rec
        assert "daemon_health" in rec

    def test_non_critical_only(self):
        checks = [
            {"passed": True, "critical": True, "name": "daemon_health"},
            {"passed": False, "critical": False, "name": "peer_reachability"},
        ]
        rec = _make_recommendation(checks)
        assert "All critical checks passed" in rec
        assert "peer_reachability" in rec


# ── fleet_doctor (integration of all checks) ──

class TestFleetDoctor:

    @patch("bin.fleet_nerve_mcp._check_python_deps")
    @patch("bin.fleet_nerve_mcp._check_message_store")
    @patch("bin.fleet_nerve_mcp._check_keychain_secrets")
    @patch("bin.fleet_nerve_mcp._check_peer_reachability")
    @patch("bin.fleet_nerve_mcp._check_nats_connectivity")
    @patch("bin.fleet_nerve_mcp._check_daemon_health")
    def test_all_pass(self, mock_daemon, mock_nats, mock_peers, mock_keys, mock_store, mock_deps):
        for mock_fn, code, name, critical in [
            (mock_daemon, "FN-0001", "daemon_health", True),
            (mock_nats, "FN-0002", "nats_connectivity", True),
            (mock_peers, "FN-0003", "peer_reachability", False),
            (mock_keys, "FN-0004", "keychain_secrets", True),
            (mock_store, "FN-0005", "message_store", True),
            (mock_deps, "FN-0006", "python_deps", False),
        ]:
            mock_fn.return_value = {
                "code": code, "name": name, "passed": True,
                "message": "OK", "latency_ms": 10.0, "critical": critical,
            }

        result = fleet_doctor()
        assert result["doctor"] == "fleet-nerve"
        assert result["version"] == "2.0.0"
        assert result["passed"] == 6
        assert result["failed"] == 0
        assert result["total"] == 6
        assert result["critical_failures"] == 0
        assert len(result["checks"]) == 6
        assert "timestamp" in result
        assert "node_id" in result
        assert "recommendation" in result

    @patch("bin.fleet_nerve_mcp._check_python_deps")
    @patch("bin.fleet_nerve_mcp._check_message_store")
    @patch("bin.fleet_nerve_mcp._check_keychain_secrets")
    @patch("bin.fleet_nerve_mcp._check_peer_reachability")
    @patch("bin.fleet_nerve_mcp._check_nats_connectivity")
    @patch("bin.fleet_nerve_mcp._check_daemon_health")
    def test_critical_failure_counted(self, mock_daemon, mock_nats, mock_peers, mock_keys, mock_store, mock_deps):
        # Daemon fails (critical), rest pass
        mock_daemon.return_value = {
            "code": "FN-0001", "name": "daemon_health", "passed": False,
            "message": "down", "latency_ms": 5.0, "critical": True,
        }
        for mock_fn, code, name, critical in [
            (mock_nats, "FN-0002", "nats_connectivity", True),
            (mock_peers, "FN-0003", "peer_reachability", False),
            (mock_keys, "FN-0004", "keychain_secrets", True),
            (mock_store, "FN-0005", "message_store", True),
            (mock_deps, "FN-0006", "python_deps", False),
        ]:
            mock_fn.return_value = {
                "code": code, "name": name, "passed": True,
                "message": "OK", "latency_ms": 10.0, "critical": critical,
            }

        result = fleet_doctor()
        assert result["passed"] == 5
        assert result["failed"] == 1
        assert result["critical_failures"] == 1

    @patch("bin.fleet_nerve_mcp._check_python_deps")
    @patch("bin.fleet_nerve_mcp._check_message_store")
    @patch("bin.fleet_nerve_mcp._check_keychain_secrets")
    @patch("bin.fleet_nerve_mcp._check_peer_reachability")
    @patch("bin.fleet_nerve_mcp._check_nats_connectivity")
    @patch("bin.fleet_nerve_mcp._check_daemon_health")
    def test_output_is_json_serializable(self, mock_daemon, mock_nats, mock_peers, mock_keys, mock_store, mock_deps):
        for mock_fn, code, name, critical in [
            (mock_daemon, "FN-0001", "daemon_health", True),
            (mock_nats, "FN-0002", "nats_connectivity", True),
            (mock_peers, "FN-0003", "peer_reachability", False),
            (mock_keys, "FN-0004", "keychain_secrets", True),
            (mock_store, "FN-0005", "message_store", True),
            (mock_deps, "FN-0006", "python_deps", False),
        ]:
            mock_fn.return_value = {
                "code": code, "name": name, "passed": True,
                "message": "OK", "latency_ms": 10.0, "critical": critical,
            }

        result = fleet_doctor()
        # Must be fully JSON-serializable (MCP returns JSON)
        serialized = json.dumps(result)
        parsed = json.loads(serialized)
        assert parsed["total"] == 6

    @patch("bin.fleet_nerve_mcp._check_python_deps")
    @patch("bin.fleet_nerve_mcp._check_message_store")
    @patch("bin.fleet_nerve_mcp._check_keychain_secrets")
    @patch("bin.fleet_nerve_mcp._check_peer_reachability")
    @patch("bin.fleet_nerve_mcp._check_nats_connectivity")
    @patch("bin.fleet_nerve_mcp._check_daemon_health")
    def test_check_order_preserved(self, mock_daemon, mock_nats, mock_peers, mock_keys, mock_store, mock_deps):
        expected_codes = ["FN-0001", "FN-0002", "FN-0003", "FN-0004", "FN-0005", "FN-0006"]
        for mock_fn, code, name, critical in [
            (mock_daemon, "FN-0001", "daemon_health", True),
            (mock_nats, "FN-0002", "nats_connectivity", True),
            (mock_peers, "FN-0003", "peer_reachability", False),
            (mock_keys, "FN-0004", "keychain_secrets", True),
            (mock_store, "FN-0005", "message_store", True),
            (mock_deps, "FN-0006", "python_deps", False),
        ]:
            mock_fn.return_value = {
                "code": code, "name": name, "passed": True,
                "message": "OK", "latency_ms": 10.0, "critical": critical,
            }

        result = fleet_doctor()
        actual_codes = [c["code"] for c in result["checks"]]
        assert actual_codes == expected_codes
