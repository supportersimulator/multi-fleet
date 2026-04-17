"""Tests for theatrical_launcher singleton manager."""

import socket
import time
from unittest.mock import patch, MagicMock

import pytest

from multifleet.theatrical_launcher import (
    _port_open,
    dashboard_status,
    ensure_theatrical,
    TRIGGER_EVENTS,
    _detect_node_id,
)


class TestPortOpen:
    def test_closed_port(self):
        assert _port_open(59999, timeout=0.5) is False

    def test_open_port(self):
        # Bind a port temporarily
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        try:
            assert _port_open(port, timeout=1.0) is True
        finally:
            s.close()


class TestDashboardStatus:
    def test_not_running(self):
        with patch("multifleet.theatrical_launcher._port_open", return_value=False):
            status = dashboard_status()
            assert status["running"] is False
            assert status["port"] is None

    def test_already_running(self):
        mock_health = {"status": "ok"}
        with patch("multifleet.theatrical_launcher._port_open", return_value=True), \
             patch("multifleet.theatrical_launcher._check_health", return_value=mock_health), \
             patch("multifleet.theatrical_launcher._read_pid", return_value=12345):
            status = dashboard_status()
            assert status["running"] is True
            assert status["port"] == 8856
            assert status["pid"] == 12345


class TestEnsureTheatrical:
    def test_attaches_when_running(self):
        with patch("multifleet.theatrical_launcher.dashboard_status",
                   return_value={"running": True, "port": 8856, "url": "http://127.0.0.1:8856"}), \
             patch("multifleet.theatrical_launcher.open_browser") as mock_browser:
            result = ensure_theatrical(open_tab=False)
            assert result.get("running") is True
            mock_browser.assert_not_called()

    def test_no_browser_flag(self):
        with patch("multifleet.theatrical_launcher.launch_dashboard",
                   return_value={"running": True, "action": "attached", "url": "http://127.0.0.1:8856"}):
            result = ensure_theatrical(open_tab=False)
            assert "browser_opened" not in result


class TestTriggerEvents:
    def test_trigger_patterns_not_empty(self):
        assert len(TRIGGER_EVENTS) >= 5

    def test_patterns_use_wildcards(self):
        wildcard_patterns = [p for p in TRIGGER_EVENTS if "*" in p]
        assert len(wildcard_patterns) >= 3


class TestDetectNodeId:
    def test_env_override(self):
        with patch.dict("os.environ", {"MULTIFLEET_NODE_ID": "mac2"}):
            assert _detect_node_id() == "mac2"

    def test_hostname_fallback(self):
        with patch.dict("os.environ", {}, clear=True), \
             patch("platform.node", return_value="mac3-mini.local"):
            # Clear the env var
            import os
            os.environ.pop("MULTIFLEET_NODE_ID", None)
            assert _detect_node_id() == "mac3"
