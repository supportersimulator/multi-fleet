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
    _fleet_api_token,
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


def _get(port, path, timeout=5, token=None):
    """Helper: GET request to local server."""
    url = f"http://127.0.0.1:{port}{path}"
    req = urllib.request.Request(url)
    if token is not None:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.headers, resp.read()


def _auth_get(port, path, timeout=5):
    """Helper: GET with valid auth token."""
    return _get(port, path, timeout=timeout, token=_fleet_api_token)


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
        status, headers, body = _auth_get(running_server, "/")
        assert status == 200
        assert "text/html" in headers.get("Content-Type", "")
        assert b"Fleet Dashboard" in body

    def test_dashboard_alias(self, running_server):
        status, headers, body = _auth_get(running_server, "/dashboard")
        assert status == 200
        assert b"Fleet Dashboard" in body


# ═══════════════════════════════════════════════════════════════════
# API endpoints
# ═══════════════════════════════════════════════════════════════════


class TestAPIEndpoints:
    def test_api_status_returns_json(self, running_server):
        status, headers, body = _auth_get(running_server, "/api/status")
        assert status == 200
        assert "application/json" in headers.get("Content-Type", "")
        data = json.loads(body)
        assert isinstance(data, dict)

    def test_api_probes_returns_json(self, running_server):
        status, headers, body = _auth_get(running_server, "/api/probes")
        assert status == 200
        data = json.loads(body)
        assert "probes" in data

    def test_api_decisions_returns_json(self, running_server):
        status, headers, body = _auth_get(running_server, "/api/decisions")
        assert status == 200
        data = json.loads(body)
        assert "decisions" in data

    def test_api_branches_returns_json(self, running_server):
        status, headers, body = _auth_get(running_server, "/api/branches")
        assert status == 200
        data = json.loads(body)
        assert isinstance(data, dict)


# ═══════════════════════════════════════════════════════════════════
# Prometheus metrics
# ═══════════════════════════════════════════════════════════════════


class TestPrometheusEndpoint:
    def test_metrics_returns_text_no_auth(self, running_server):
        """Metrics is auth-exempt — should work without token."""
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
            _auth_get(running_server, "/nonexistent")
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


# ═══════════════════════════════════════════════════════════════════
# Authentication
# ═══════════════════════════════════════════════════════════════════


class TestAuthentication:
    def test_health_no_auth_required(self, running_server):
        """Health endpoint is exempt from auth."""
        status, headers, body = _get(running_server, "/health")
        assert status == 200

    def test_metrics_no_auth_required(self, running_server):
        """Metrics endpoint is exempt from auth."""
        status, headers, body = _get(running_server, "/metrics")
        assert status == 200

    def test_api_requires_auth(self, running_server):
        """API endpoints return 401 without token."""
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _get(running_server, "/api/status")
        assert exc_info.value.code == 401
        body = json.loads(exc_info.value.read())
        assert body["error"] == "Unauthorized"

    def test_api_rejects_wrong_token(self, running_server):
        """API endpoints reject wrong bearer token."""
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _get(running_server, "/api/status", token="wrong-token")
        assert exc_info.value.code == 401

    def test_api_accepts_valid_token(self, running_server):
        """API endpoints accept valid bearer token."""
        status, headers, body = _auth_get(running_server, "/api/status")
        assert status == 200

    def test_dashboard_requires_auth(self, running_server):
        """Dashboard is a protected route."""
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _get(running_server, "/")
        assert exc_info.value.code == 401

    def test_extension_requires_auth(self, running_server):
        """Extension endpoints are protected."""
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _get(running_server, "/extension/status-bar")
        assert exc_info.value.code == 401


# ═══════════════════════════════════════════════════════════════════
# CORS
# ═══════════════════════════════════════════════════════════════════


class TestCORS:
    def test_no_wildcard_cors(self, running_server):
        """CORS header must not be wildcard."""
        status, headers, body = _get(running_server, "/health")
        origin = headers.get("Access-Control-Allow-Origin", "")
        assert origin != "*", "CORS wildcard must not be used"
        assert "127.0.0.1" in origin


# ═══════════════════════════════════════════════════════════════════
# Input validation
# ═══════════════════════════════════════════════════════════════════


class TestInputValidation:
    def test_parse_int_param_valid(self):
        assert FleetWebHandler._parse_int_param("5", 20, 1, 100) == 5

    def test_parse_int_param_below_min(self):
        assert FleetWebHandler._parse_int_param("-10", 20, 1, 100) == 1

    def test_parse_int_param_above_max(self):
        assert FleetWebHandler._parse_int_param("99999", 20, 1, 100) == 100

    def test_parse_int_param_garbage(self):
        assert FleetWebHandler._parse_int_param("abc", 20, 1, 100) == 20

    def test_parse_float_param_valid(self):
        assert FleetWebHandler._parse_float_param("2.5", 24.0, 0.1, 8760.0) == 2.5

    def test_parse_float_param_below_min(self):
        assert FleetWebHandler._parse_float_param("0.001", 24.0, 0.1, 8760.0) == 0.1

    def test_parse_float_param_above_max(self):
        assert FleetWebHandler._parse_float_param("99999", 24.0, 0.1, 8760.0) == 8760.0

    def test_parse_float_param_garbage(self):
        assert FleetWebHandler._parse_float_param("xyz", 24.0, 0.1, 8760.0) == 24.0
