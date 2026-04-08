"""Tests for multifleet.web_server — fleet web server endpoints."""

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from multifleet.web_server import (
    FleetWebHandler,
    start_fleet_server,
    stop_fleet_server,
    fleet_server_status,
    fleet_server_tool,
    _push_sse,
)


# ═══════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════


@pytest.fixture
def server_port():
    """Find a free port for testing."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def running_server(server_port):
    """Start and stop a fleet web server for testing."""
    result = start_fleet_server(port=server_port)
    assert result["status"] == "started"
    time.sleep(0.2)  # let server thread bind
    yield server_port
    stop_fleet_server()


def _get(port, path, timeout=5):
    """Helper: GET request to local server."""
    url = f"http://127.0.0.1:{port}{path}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.headers, resp.read()


# ═══════════════════════════════════════════════════════════════════
# Health endpoint
# ═══════════════════════════════════════════════════════════════════


class TestHealthEndpoint:
    def test_health_returns_200(self, running_server):
        status, headers, body = _get(running_server, "/health")
        assert status == 200
        data = json.loads(body)
        assert data["status"] == "ok"
        assert data["type"] == "fleet-web-server"

    def test_health_content_type_json(self, running_server):
        status, headers, body = _get(running_server, "/health")
        assert "application/json" in headers.get("Content-Type", "")


# ═══════════════════════════════════════════════════════════════════
# Dashboard
# ═══════════════════════════════════════════════════════════════════


class TestDashboardEndpoint:
    def test_dashboard_root_returns_html(self, running_server):
        status, headers, body = _get(running_server, "/")
        assert status == 200
        assert "text/html" in headers.get("Content-Type", "")
        assert b"Fleet Dashboard" in body

    def test_dashboard_alias(self, running_server):
        status, headers, body = _get(running_server, "/dashboard")
        assert status == 200
        assert b"Fleet Dashboard" in body


# ═══════════════════════════════════════════════════════════════════
# API endpoints
# ═══════════════════════════════════════════════════════════════════


class TestAPIEndpoints:
    def test_api_status_returns_json(self, running_server):
        status, headers, body = _get(running_server, "/api/status")
        assert status == 200
        assert "application/json" in headers.get("Content-Type", "")
        data = json.loads(body)
        assert isinstance(data, dict)

    def test_api_probes_returns_json(self, running_server):
        status, headers, body = _get(running_server, "/api/probes")
        assert status == 200
        data = json.loads(body)
        assert "probes" in data

    def test_api_decisions_returns_json(self, running_server):
        status, headers, body = _get(running_server, "/api/decisions")
        assert status == 200
        data = json.loads(body)
        assert "decisions" in data

    def test_api_branches_returns_json(self, running_server):
        status, headers, body = _get(running_server, "/api/branches")
        assert status == 200
        data = json.loads(body)
        assert isinstance(data, dict)


# ═══════════════════════════════════════════════════════════════════
# Prometheus metrics
# ═══════════════════════════════════════════════════════════════════


class TestPrometheusEndpoint:
    def test_metrics_returns_text(self, running_server):
        status, headers, body = _get(running_server, "/metrics")
        assert status == 200
        ct = headers.get("Content-Type", "")
        assert "text/plain" in ct


# ═══════════════════════════════════════════════════════════════════
# 404 handling
# ═══════════════════════════════════════════════════════════════════


class TestNotFound:
    def test_unknown_path_returns_404(self, running_server):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _get(running_server, "/nonexistent")
        assert exc_info.value.code == 404


# ═══════════════════════════════════════════════════════════════════
# Server lifecycle
# ═══════════════════════════════════════════════════════════════════


class TestServerLifecycle:
    def test_start_stop_cycle(self, server_port):
        result = start_fleet_server(port=server_port)
        assert result["status"] == "started"
        assert result["port"] == server_port

        status = fleet_server_status()
        assert status["status"] == "running"

        result = stop_fleet_server()
        assert result["status"] == "stopped"

        status = fleet_server_status()
        assert status["status"] == "stopped"

    def test_double_start_returns_already_running(self, server_port):
        start_fleet_server(port=server_port)
        result = start_fleet_server(port=server_port)
        assert result["status"] == "already_running"
        stop_fleet_server()

    def test_stop_when_not_running(self):
        # Ensure stopped first
        stop_fleet_server()
        result = stop_fleet_server()
        assert result["status"] == "not_running"


# ═══════════════════════════════════════════════════════════════════
# MCP tool function
# ═══════════════════════════════════════════════════════════════════


class TestMCPToolFunction:
    def test_tool_status(self):
        stop_fleet_server()  # ensure clean state
        result = fleet_server_tool(action="status")
        assert result["status"] == "stopped"

    def test_tool_unknown_action(self):
        result = fleet_server_tool(action="restart")
        assert "error" in result
