"""
Tests for fleet_nerve_config.py — config loading, node detection, peer resolution.
"""

import json
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from tools.fleet_nerve_config import (
    detect_node_id,
    load_peers,
    _load_from_json,
    _load_from_shell,
)


class TestDetectNodeId:
    """Node ID detection priority: env var > IP match > hostname fallback."""

    def test_env_var_takes_priority(self):
        with patch.dict(os.environ, {"MULTIFLEET_NODE_ID": "test-node"}):
            assert detect_node_id() == "test-node"

    def test_ip_match_finds_node(self, json_config_file, json_config):
        """When local IP matches a node in config.json, return that node ID."""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("MULTIFLEET_NODE_ID", None)
            with patch("tools.fleet_nerve_config._get_local_ip", return_value="TEST_IP_2"):
                with patch("tools.fleet_nerve_config.JSON_CONF", json_config_file):
                    result = detect_node_id()
                    assert result == "mac2"

    def test_hostname_fallback(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("MULTIFLEET_NODE_ID", None)
            with patch("tools.fleet_nerve_config._get_local_ip", return_value=None):
                with patch("tools.fleet_nerve_config.JSON_CONF", Path("/nonexistent/config.json")):
                    with patch("socket.gethostname", return_value="MyMachine.local"):
                        result = detect_node_id()
                        assert result == "mymachine"

    def test_env_var_empty_string_uses_fallback(self):
        """Empty string env var should still trigger hostname fallback."""
        with patch.dict(os.environ, {"MULTIFLEET_NODE_ID": ""}):
            # Empty string is falsy, so should fall through
            with patch("tools.fleet_nerve_config._get_local_ip", return_value=None):
                with patch("tools.fleet_nerve_config.JSON_CONF", Path("/nonexistent")):
                    with patch("socket.gethostname", return_value="TestHost.local"):
                        result = detect_node_id()
                        assert result == "testhost"


class TestLoadFromJson:
    """JSON config loading — the primary, scalable format."""

    def test_loads_all_nodes(self, json_config_file):
        peers, chief = _load_from_json(json_config_file)
        assert len(peers) == 3
        assert "mac1" in peers
        assert "mac2" in peers
        assert "mac3" in peers

    def test_chief_detected(self, json_config_file):
        peers, chief = _load_from_json(json_config_file)
        assert chief == "mac1"

    def test_node_fields_populated(self, json_config_file):
        peers, _ = _load_from_json(json_config_file)
        mac2 = peers["mac2"]
        assert mac2["user"] == "bob"
        assert mac2["ip"] == "TEST_IP_2"
        assert mac2["port"] == 8855
        assert mac2["role"] == "worker"
        assert mac2["mac_address"] == "AA:BB:CC:DD:EE:02"

    def test_default_port_when_missing(self, tmp_path):
        """Nodes without explicit port get default 8855."""
        cfg = {"nodes": {"n1": {"host": "TEST_IP_1"}}, "chief": {}}
        conf_file = tmp_path / "config.json"
        conf_file.write_text(json.dumps(cfg))
        peers, _ = _load_from_json(conf_file)
        assert peers["n1"]["port"] == 8855


class TestLoadFromShell:
    """Legacy shell conf loading — 3s-network.local.conf format."""

    def test_parses_peers(self, shell_conf_file):
        peers, chief = _load_from_shell(shell_conf_file)
        assert len(peers) == 3
        assert peers["mac1"]["user"] == "alice"
        assert peers["mac1"]["ip"] == "TEST_IP_1"

    def test_parses_chief(self, shell_conf_file):
        _, chief = _load_from_shell(shell_conf_file)
        assert chief == "mac1"

    def test_parses_mac_addresses(self, shell_conf_file):
        peers, _ = _load_from_shell(shell_conf_file)
        assert peers["mac1"]["mac_address"] == "AA:BB:CC:DD:EE:01"
        assert peers["mac2"]["mac_address"] == "AA:BB:CC:DD:EE:02"
        # mac3 has no MAC_ line in sample
        assert "mac_address" not in peers["mac3"]

    def test_empty_file_returns_empty(self, tmp_path):
        empty = tmp_path / "empty.conf"
        empty.write_text("")
        peers, chief = _load_from_shell(empty)
        assert peers == {}
        assert chief is None


class TestLoadPeers:
    """load_peers() with JSON priority over shell conf."""

    def test_json_takes_priority(self, json_config_file, shell_conf_file):
        with patch("tools.fleet_nerve_config.JSON_CONF", json_config_file):
            peers, chief = load_peers()
            assert chief == "mac1"
            assert peers["mac2"]["role"] == "worker"

    def test_falls_back_to_shell(self, shell_conf_file):
        with patch("tools.fleet_nerve_config.JSON_CONF", Path("/nonexistent/config.json")):
            peers, chief = load_peers(conf_path=str(shell_conf_file))
            assert chief == "mac1"
            assert "mac1" in peers

    def test_no_config_returns_empty(self):
        with patch("tools.fleet_nerve_config.JSON_CONF", Path("/nonexistent/config.json")):
            with patch("tools.fleet_nerve_config.SHELL_CONF", Path("/nonexistent/shell.conf")):
                peers, chief = load_peers()
                assert peers == {}
                assert chief is None
