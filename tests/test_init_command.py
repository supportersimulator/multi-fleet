"""Tests for fleet init command — config generation, env detection, and CLI wiring."""

import json
import socket
from pathlib import Path
from unittest import mock

import pytest

from multifleet.init_command import (
    generate_config,
    detect_environment,
    write_config,
    _detect_node_id,
    _detect_os,
    DEFAULT_NATS_URL,
    DEFAULT_DAEMON_PORT,
)


class TestDetectNodeId:
    def test_strips_local_suffix(self):
        with mock.patch("socket.gethostname", return_value="my-macbook.local"):
            assert _detect_node_id() == "my-macbook"

    def test_sanitizes_special_chars(self):
        with mock.patch("socket.gethostname", return_value="Aaron's Mac Pro!"):
            result = _detect_node_id()
            assert result.isascii()
            assert all(c.isalnum() or c == "-" for c in result)
            assert "--" not in result

    def test_empty_hostname_fallback(self):
        with mock.patch("socket.gethostname", return_value=""):
            assert _detect_node_id() == "node1"

    def test_simple_hostname(self):
        with mock.patch("socket.gethostname", return_value="mac2"):
            assert _detect_node_id() == "mac2"

    def test_lowercase(self):
        with mock.patch("socket.gethostname", return_value="MyMachine.local"):
            result = _detect_node_id()
            assert result == result.lower()


class TestDetectOs:
    def test_darwin_is_macos(self):
        with mock.patch("platform.system", return_value="Darwin"):
            assert _detect_os() == "macos"

    def test_linux(self):
        with mock.patch("platform.system", return_value="Linux"):
            assert _detect_os() == "linux"

    def test_windows(self):
        with mock.patch("platform.system", return_value="Windows"):
            assert _detect_os() == "windows"


class TestGenerateConfig:
    def test_sets_node_id(self):
        config = generate_config("test-node")
        assert config["node_id"] == "test-node"

    def test_sets_nats_server(self):
        config = generate_config("n1", nats_server="nats://10.0.0.5:4222")
        assert config["nats"]["server"] == "nats://10.0.0.5:4222"

    def test_defaults(self):
        config = generate_config("n1")
        assert config["nats"]["server"] == DEFAULT_NATS_URL
        assert config["fleet_nerve_port"] == DEFAULT_DAEMON_PORT
        assert config["daemon"]["auto_start"] is True
        assert config["nats"]["cluster_name"] == "fleet"

    def test_sets_daemon_port(self):
        config = generate_config("n1", daemon_port=9999)
        assert config["fleet_nerve_port"] == 9999

    def test_preserves_template_fields(self):
        """Template fields not explicitly set should survive."""
        config = generate_config("n1")
        # ssh_tunnels and ide come from template
        assert "ssh_tunnels" in config
        assert config.get("ide") == "claude-code"

    def test_log_and_pid_dirs_set(self):
        config = generate_config("n1")
        assert "logs" in config["daemon"]["log_dir"]
        assert "run" in config["daemon"]["pid_dir"]


class TestWriteConfig:
    def test_writes_valid_json(self, tmp_path):
        config = generate_config("test-node")
        path = tmp_path / "subdir" / "config.json"
        result = write_config(config, path)
        assert result == path
        assert path.exists()
        loaded = json.loads(path.read_text())
        assert loaded["node_id"] == "test-node"

    def test_creates_parent_dirs(self, tmp_path):
        path = tmp_path / "deep" / "nested" / "config.json"
        write_config({"node_id": "x"}, path)
        assert path.exists()


class TestDetectEnvironment:
    @mock.patch("multifleet.init_command._daemon_healthy", return_value=False)
    @mock.patch("multifleet.init_command._nats_running", return_value=False)
    @mock.patch("multifleet.init_command._nats_installed", return_value=None)
    @mock.patch("multifleet.init_command._detect_os", return_value="macos")
    @mock.patch("multifleet.init_command._detect_node_id", return_value="test-mac")
    @mock.patch("multifleet.init_command._brew_available", return_value=True)
    def test_fresh_install_detection(self, _brew, _node, _os, _nats_bin, _nats_run, _daemon):
        env = detect_environment()
        assert env["os"] == "macos"
        assert env["node_id"] == "test-mac"
        assert env["nats_installed"] is False
        assert env["nats_running"] is False
        assert env["daemon_running"] is False
        assert env["brew_available"] is True

    @mock.patch("multifleet.init_command._daemon_healthy", return_value=True)
    @mock.patch("multifleet.init_command._nats_running", return_value=True)
    @mock.patch("multifleet.init_command._nats_installed", return_value="/usr/local/bin/nats-server")
    @mock.patch("multifleet.init_command._detect_os", return_value="macos")
    @mock.patch("multifleet.init_command._detect_node_id", return_value="mac1")
    @mock.patch("multifleet.init_command._brew_available", return_value=True)
    def test_existing_install_detection(self, _brew, _node, _os, _nats_bin, _nats_run, _daemon):
        env = detect_environment()
        assert env["nats_installed"] is True
        assert env["nats_bin"] == "/usr/local/bin/nats-server"
        assert env["nats_running"] is True
        assert env["daemon_running"] is True


class TestCLIWiring:
    """Verify the init subcommand is registered in the CLI parser."""

    def test_init_in_help(self):
        import subprocess
        import sys
        cli_dir = Path(__file__).resolve().parent.parent
        result = subprocess.run(
            [sys.executable, "-m", "multifleet.cli", "--help"],
            capture_output=True, text=True, cwd=str(cli_dir), timeout=10,
        )
        assert result.returncode == 0
        assert "init" in result.stdout

    def test_init_help_flag(self):
        import subprocess
        import sys
        cli_dir = Path(__file__).resolve().parent.parent
        result = subprocess.run(
            [sys.executable, "-m", "multifleet.cli", "init", "--help"],
            capture_output=True, text=True, cwd=str(cli_dir), timeout=10,
        )
        assert result.returncode == 0
        assert "--yes" in result.stdout
        assert "--node-id" in result.stdout
