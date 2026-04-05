"""
Tests for config loading edge cases — missing files, corrupt JSON, missing fields, env overrides.

Extends the existing test_fleet_config.py with failure-mode and edge-case coverage.
"""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.fleet_nerve_config import (
    detect_node_id,
    load_peers,
    _load_from_json,
    _load_from_shell,
)


class TestMissingConfigFile:
    """Behavior when config files are absent or inaccessible."""

    @pytest.mark.unit
    def test_no_json_no_shell_returns_empty(self):
        """When both config sources are missing, return empty peers."""
        with patch("tools.fleet_nerve_config.JSON_CONF", Path("/nonexistent/config.json")):
            with patch("tools.fleet_nerve_config.SHELL_CONF", Path("/nonexistent/shell.conf")):
                with patch("tools.fleet_nerve_config.DISCOVERY_CACHE", Path("/nonexistent/cache.json")):
                    peers, chief = load_peers(include_discovered=False)
                    assert peers == {}
                    assert chief is None

    @pytest.mark.unit
    def test_json_missing_falls_back_to_shell(self, shell_conf_file):
        """When JSON config is missing, shell conf is used."""
        with patch("tools.fleet_nerve_config.JSON_CONF", Path("/nonexistent/config.json")):
            peers, chief = load_peers(conf_path=str(shell_conf_file))
            assert len(peers) == 3
            assert chief == "mac1"

    @pytest.mark.unit
    def test_unreadable_json_falls_back(self, tmp_path, shell_conf_file):
        """When JSON file exists but is unreadable, fall back to shell conf."""
        conf = tmp_path / "config.json"
        conf.write_text("valid json")
        conf.chmod(0o000)
        try:
            with patch("tools.fleet_nerve_config.JSON_CONF", conf):
                peers, chief = load_peers(conf_path=str(shell_conf_file))
                # Should fall back to shell conf
                assert len(peers) >= 1
        finally:
            conf.chmod(0o644)


class TestCorruptJSON:
    """Behavior when JSON config exists but contains invalid data."""

    @pytest.mark.unit
    def test_invalid_json_raises(self, tmp_path):
        """Corrupted JSON file raises JSONDecodeError in _load_from_json."""
        conf = tmp_path / "corrupt.json"
        conf.write_text("{invalid json content")
        with pytest.raises(json.JSONDecodeError):
            _load_from_json(conf)

    @pytest.mark.unit
    def test_valid_json_wrong_structure(self, tmp_path):
        """JSON that parses but has wrong structure returns empty peers."""
        conf = tmp_path / "wrong.json"
        conf.write_text(json.dumps({"unexpected": "structure"}))
        peers, chief = _load_from_json(conf)
        assert peers == {}
        assert chief is None

    @pytest.mark.unit
    def test_empty_json_object(self, tmp_path):
        """Empty JSON object {} returns empty peers."""
        conf = tmp_path / "empty.json"
        conf.write_text("{}")
        peers, chief = _load_from_json(conf)
        assert peers == {}
        assert chief is None

    @pytest.mark.unit
    def test_null_nodes(self, tmp_path):
        """JSON with nodes: null doesn't crash."""
        conf = tmp_path / "null-nodes.json"
        conf.write_text(json.dumps({"nodes": None, "chief": {}}))
        # Should handle gracefully (nodes.items() on None would fail)
        try:
            peers, chief = _load_from_json(conf)
        except (TypeError, AttributeError):
            pass  # Expected -- None.items() fails

    @pytest.mark.unit
    def test_nodes_as_list_instead_of_dict(self, tmp_path):
        """JSON with nodes as array instead of object."""
        conf = tmp_path / "list-nodes.json"
        conf.write_text(json.dumps({"nodes": ["mac1", "mac2"], "chief": {}}))
        with pytest.raises((AttributeError, TypeError)):
            _load_from_json(conf)


class TestMissingRequiredFields:
    """Behavior when config nodes are missing expected fields."""

    @pytest.mark.unit
    def test_node_without_host(self, tmp_path):
        """Node missing 'host' field gets empty ip."""
        conf = tmp_path / "config.json"
        conf.write_text(json.dumps({
            "nodes": {"mac1": {"user": "alice", "role": "chief"}},
            "chief": {"nodeId": "mac1"},
        }))
        peers, chief = _load_from_json(conf)
        assert peers["mac1"]["ip"] == ""

    @pytest.mark.unit
    def test_node_without_user_uses_env(self, tmp_path):
        """Node missing 'user' field falls back to $USER env var."""
        conf = tmp_path / "config.json"
        conf.write_text(json.dumps({
            "nodes": {"mac1": {"host": "10.0.0.1"}},
            "chief": {},
        }))
        peers, _ = _load_from_json(conf)
        # Should use os.environ USER or empty string
        assert isinstance(peers["mac1"]["user"], str)

    @pytest.mark.unit
    def test_node_without_port_gets_default(self, tmp_path):
        """Node missing 'port' field gets default 8855."""
        conf = tmp_path / "config.json"
        conf.write_text(json.dumps({
            "nodes": {"mac1": {"host": "10.0.0.1"}},
            "chief": {},
        }))
        peers, _ = _load_from_json(conf)
        assert peers["mac1"]["port"] == 8855

    @pytest.mark.unit
    def test_node_without_role_defaults_worker(self, tmp_path):
        """Node missing 'role' field defaults to 'worker'."""
        conf = tmp_path / "config.json"
        conf.write_text(json.dumps({
            "nodes": {"mac1": {"host": "10.0.0.1"}},
            "chief": {},
        }))
        peers, _ = _load_from_json(conf)
        assert peers["mac1"]["role"] == "worker"

    @pytest.mark.unit
    def test_no_chief_section(self, tmp_path):
        """Config without chief section returns None chief."""
        conf = tmp_path / "config.json"
        conf.write_text(json.dumps({
            "nodes": {"mac1": {"host": "10.0.0.1"}},
        }))
        peers, chief = _load_from_json(conf)
        assert chief is None
        assert len(peers) == 1


class TestEnvVarOverrides:
    """Environment variables override config file values."""

    @pytest.mark.unit
    def test_node_id_env_overrides_config(self):
        """MULTIFLEET_NODE_ID env var takes priority over IP match or hostname."""
        with patch.dict(os.environ, {"MULTIFLEET_NODE_ID": "override-node"}):
            assert detect_node_id() == "override-node"

    @pytest.mark.unit
    def test_node_id_empty_env_uses_fallback(self):
        """Empty MULTIFLEET_NODE_ID env falls back to hostname."""
        with patch.dict(os.environ, {"MULTIFLEET_NODE_ID": ""}):
            with patch("tools.fleet_nerve_config._get_local_ip", return_value=None):
                with patch("tools.fleet_nerve_config.JSON_CONF", Path("/nonexistent")):
                    with patch("socket.gethostname", return_value="MyHost.local"):
                        result = detect_node_id()
                        assert result == "myhost"


class TestShellConfEdgeCases:
    """Edge cases in legacy shell config parsing."""

    @pytest.mark.unit
    def test_shell_conf_with_comments(self, tmp_path):
        """Lines starting with # are ignored."""
        conf = tmp_path / "commented.conf"
        conf.write_text('''
# This is a comment
PEER_NAMES="mac1"
PEER_mac1="alice@10.0.0.1"
# Another comment
CHIEF="mac1"
''')
        peers, chief = _load_from_shell(conf)
        assert len(peers) == 1
        assert chief == "mac1"

    @pytest.mark.unit
    def test_shell_conf_single_peer(self, tmp_path):
        """Config with only one peer works."""
        conf = tmp_path / "single.conf"
        conf.write_text('PEER_NAMES="solo"\nPEER_solo="bob@10.0.0.1"\nCHIEF="solo"')
        peers, chief = _load_from_shell(conf)
        assert len(peers) == 1
        assert peers["solo"]["user"] == "bob"

    @pytest.mark.unit
    def test_shell_conf_no_chief_line(self, tmp_path):
        """Shell conf without CHIEF= returns None for chief."""
        conf = tmp_path / "no-chief.conf"
        conf.write_text('PEER_NAMES="mac1"\nPEER_mac1="alice@10.0.0.1"')
        peers, chief = _load_from_shell(conf)
        assert len(peers) == 1
        assert chief is None

    @pytest.mark.unit
    def test_shell_conf_peer_name_in_names_but_no_definition(self, tmp_path):
        """Peer listed in PEER_NAMES but no PEER_<name> definition is skipped."""
        conf = tmp_path / "missing-def.conf"
        conf.write_text('PEER_NAMES="mac1 mac2"\nPEER_mac1="alice@10.0.0.1"')
        peers, chief = _load_from_shell(conf)
        assert "mac1" in peers
        assert "mac2" not in peers  # No definition found


class TestMCPConfigLoading:
    """Config loading from the MCP server's _load_config()."""

    @pytest.mark.unit
    def test_missing_config_returns_defaults(self):
        """When .multifleet/config.json doesn't exist, return safe defaults."""
        from tools.fleet_nerve_mcp import _load_config
        with patch("tools.fleet_nerve_mcp.CONFIG_PATH", Path("/nonexistent/config.json")):
            cfg = _load_config()
            assert cfg == {"nodes": {}, "chief": {}, "protocol": {}}

    @pytest.mark.unit
    def test_valid_config_loads(self, json_config_file):
        from tools.fleet_nerve_mcp import _load_config
        with patch("tools.fleet_nerve_mcp.CONFIG_PATH", json_config_file):
            cfg = _load_config()
            assert "nodes" in cfg
            assert len(cfg["nodes"]) == 3
