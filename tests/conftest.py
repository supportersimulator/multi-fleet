"""
Shared fixtures for multi-fleet test suite.

All network calls (NATS, SSH, HTTP) are mocked. Tests verify logic, not infrastructure.
"""

import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure tools/ is importable
REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tools"))


# ── Pytest markers ──

def pytest_configure(config):
    config.addinivalue_line("markers", "unit: fast, isolated unit tests (no I/O, no network)")
    config.addinivalue_line("markers", "integration: tests that require running services (daemon, NATS, etc.)")
    config.addinivalue_line("markers", "security: security-focused tests (HMAC, validation, sanitization)")


# ── Sample config data ──

SAMPLE_JSON_CONFIG = {
    "fleetId": "test-fleet",
    "nodes": {
        "mac1": {
            "host": "TEST_IP_1",
            "port": 8855,
            "user": "alice",
            "role": "chief",
            "mac_address": "AA:BB:CC:DD:EE:01",
        },
        "mac2": {
            "host": "TEST_IP_2",
            "port": 8855,
            "user": "bob",
            "role": "worker",
            "mac_address": "AA:BB:CC:DD:EE:02",
        },
        "mac3": {
            "host": "TEST_IP_3",
            "port": 8855,
            "user": "carol",
            "role": "worker",
            "mac_address": "AA:BB:CC:DD:EE:03",
        },
    },
    "chief": {"nodeId": "mac1", "host": "TEST_IP_1"},
    "protocol": {"version": "2.0", "tunnelReversePortBase": 8855},
}

SAMPLE_SHELL_CONF = '''
PEER_NAMES="mac1 mac2 mac3"
PEER_mac1="alice@TEST_IP_1"
PEER_mac2="bob@TEST_IP_2"
PEER_mac3="carol@TEST_IP_3"
MAC_mac1="AA:BB:CC:DD:EE:01"
MAC_mac2="AA:BB:CC:DD:EE:02"
CHIEF="mac1"
'''


@pytest.fixture
def json_config():
    """Return a sample JSON config dict."""
    return SAMPLE_JSON_CONFIG.copy()


@pytest.fixture
def json_config_file(tmp_path, json_config):
    """Write sample config to a temp file and return its path."""
    conf_dir = tmp_path / ".multifleet"
    conf_dir.mkdir()
    conf_file = conf_dir / "config.json"
    conf_file.write_text(json.dumps(json_config, indent=2))
    return conf_file


@pytest.fixture
def shell_conf_file(tmp_path):
    """Write sample shell conf to a temp file and return its path."""
    conf_file = tmp_path / "3s-network.local.conf"
    conf_file.write_text(SAMPLE_SHELL_CONF)
    return conf_file


@pytest.fixture
def mock_nats_client():
    """Mock nats.NATS client with common methods."""
    nc = AsyncMock()
    nc.is_connected = True
    nc.publish = AsyncMock()
    nc.subscribe = AsyncMock()
    nc.close = AsyncMock()
    nc.new_inbox = MagicMock(return_value="_INBOX.test123")
    return nc


@pytest.fixture
def mock_nerve(mock_nats_client):
    """Create a FleetNerveNATS instance with mocked NATS connection."""
    with patch.dict(os.environ, {"MULTIFLEET_NODE_ID": "mac1"}):
        with patch("tools.fleet_nerve_nats.nats") as mock_nats_mod:
            mock_nats_mod.connect = AsyncMock(return_value=mock_nats_client)
            from tools.fleet_nerve_nats import FleetNerveNATS
            nerve = FleetNerveNATS(node_id="mac1", nats_url="nats://localhost:4222")
            nerve.nc = mock_nats_client
            nerve._peers_config = {
                "mac2": {"user": "bob", "ip": "TEST_IP_2", "port": 8855, "mac_address": "AA:BB:CC:DD:EE:02"},
                "mac3": {"user": "carol", "ip": "TEST_IP_3", "port": 8855, "mac_address": "AA:BB:CC:DD:EE:03"},
            }
            nerve._chief = "mac1"
            yield nerve


@pytest.fixture
def mock_subprocess():
    """Patch subprocess.run to prevent real shell commands."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0, stdout="", stderr="",
        )
        yield mock_run


@pytest.fixture
def mock_worker_scripts(tmp_path):
    """Create fake session-driver scripts directory."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ["launch-worker.sh", "converse.sh", "stop-worker.sh"]:
        (scripts / name).write_text("#!/bin/bash\necho '{}'\n")
        (scripts / name).chmod(0o755)
    return scripts
