"""
Tests for multifleet.web_dashboard — HTML generation, HTTP handler, data formatting.

Tests verify: dashboard HTML generation, DashboardHandler routing, _fetch_daemon error handling,
port configuration, and serve() setup. All network calls are mocked.
"""

import io
import json
import time
from http.server import HTTPServer
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

import sys
from pathlib import Path

FLEET_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(FLEET_ROOT))

from multifleet.web_dashboard import (
    build_dashboard_html,
    DashboardHandler,
    _fetch_daemon,
    serve,
    DEFAULT_WEB_PORT,
    DAEMON_PORT,
    EVENTBUS_PORT,
)


# ── HTML Generation ──


class TestBuildDashboardHTML:
    """build_dashboard_html returns a self-contained HTML page."""

    def test_returns_valid_html(self):
        html = build_dashboard_html()
        assert html.startswith("<!DOCTYPE html>")
        assert "</html>" in html

    def test_contains_dashboard_title(self):
        html = build_dashboard_html()
        assert "<title>Fleet Dashboard</title>" in html

    def test_contains_css(self):
        html = build_dashboard_html()
        assert "<style>" in html
        assert "--bg-primary:" in html

    def test_contains_javascript(self):
        html = build_dashboard_html()
        assert "<script>" in html
        assert "fetchJSON" in html
        assert "renderNodes" in html

    def test_custom_ports_embedded(self):
        html = build_dashboard_html(daemon_port=9999, eventbus_port=7777)
        assert "http://127.0.0.1:9999" in html
        assert "ws://127.0.0.1:7777" in html

    def test_default_ports_embedded(self):
        html = build_dashboard_html()
        assert f"http://127.0.0.1:{DAEMON_PORT}" in html
        assert f"ws://127.0.0.1:{EVENTBUS_PORT}" in html

    def test_contains_all_card_sections(self):
        """Dashboard should have all expected card panels."""
        html = build_dashboard_html()
        for section in ["Nodes", "Messages", "Gains Gate", "Races",
                        "Evidence Ledger", "Truth Ladder", "Probe Findings",
                        "Node Overlays", "EventBus Live"]:
            assert section in html, f"Missing card section: {section}"

    def test_contains_connection_indicators(self):
        html = build_dashboard_html()
        assert "conn-daemon" in html
        assert "conn-eventbus" in html

    def test_poll_interval_defined(self):
        html = build_dashboard_html()
        assert "POLL_INTERVAL" in html

    def test_no_external_dependencies(self):
        """Self-contained: no CDN links, no external CSS/JS."""
        html = build_dashboard_html()
        assert "https://" not in html
        assert "http://" not in html or "http://127.0.0.1" in html


# ── _fetch_daemon ──


class TestFetchDaemon:
    """_fetch_daemon retrieves JSON from daemon, returns None on error."""

    def test_returns_none_on_connection_error(self):
        with patch("urllib.request.urlopen", side_effect=ConnectionError("refused")):
            result = _fetch_daemon("/health")
            assert result is None

    def test_returns_none_on_timeout(self):
        with patch("urllib.request.urlopen", side_effect=TimeoutError()):
            result = _fetch_daemon("/health")
            assert result is None

    def test_returns_parsed_json(self):
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({"status": "ok"}).encode()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_response):
            result = _fetch_daemon("/health")
            assert result == {"status": "ok"}

    def test_custom_port(self):
        with patch("urllib.request.urlopen", side_effect=ConnectionError()) as mock_open:
            _fetch_daemon("/health", port=9999)
            call_args = mock_open.call_args
            assert "9999" in call_args[0][0]


# ── DashboardHandler ──


class TestDashboardHandler:
    """HTTP handler serves dashboard HTML and health endpoint."""

    def _make_handler(self, path="/"):
        """Create a DashboardHandler with mocked request/response."""
        handler = DashboardHandler.__new__(DashboardHandler)
        handler.path = path
        handler.requestline = f"GET {path} HTTP/1.1"
        handler.request_version = "HTTP/1.1"
        handler.command = "GET"
        handler.headers = {}
        handler.wfile = io.BytesIO()
        handler._headers_buffer = []
        handler.responses = {200: ("OK",), 404: ("Not Found",)}
        # Mock response methods
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        return handler

    def test_root_serves_html(self):
        handler = self._make_handler("/")
        handler.do_GET()
        handler.send_response.assert_called_with(200)
        # Check Content-Type header was set to text/html
        content_type_calls = [
            c for c in handler.send_header.call_args_list
            if c[0][0] == "Content-Type"
        ]
        assert any("text/html" in str(c) for c in content_type_calls)

    def test_dashboard_path_serves_html(self):
        handler = self._make_handler("/dashboard")
        handler.do_GET()
        handler.send_response.assert_called_with(200)

    def test_health_endpoint(self):
        handler = self._make_handler("/health")
        handler.do_GET()
        handler.send_response.assert_called_with(200)
        written = handler.wfile.getvalue()
        data = json.loads(written)
        assert data["status"] == "ok"
        assert data["type"] == "web-dashboard"

    def test_unknown_path_returns_404(self):
        handler = self._make_handler("/nonexistent")
        handler.do_GET()
        handler.send_response.assert_called_with(404)

    def test_fleet_metrics_endpoint_success(self):
        handler = self._make_handler("/fleet-metrics")
        mock_agg = MagicMock()
        mock_agg.aggregate.return_value = {"total_messages_sent": 42}
        with patch("multifleet.web_dashboard.FleetMetricsAggregator",
                    return_value=mock_agg, create=True):
            # Patch the import inside do_GET
            with patch.dict("sys.modules", {"multifleet.fleet_metrics": MagicMock(
                FleetMetricsAggregator=MagicMock(return_value=mock_agg)
            )}):
                handler.do_GET()
        handler.send_response.assert_called_with(200)

    def test_fleet_metrics_endpoint_error(self):
        handler = self._make_handler("/fleet-metrics")
        with patch.dict("sys.modules", {"multifleet.fleet_metrics": None}):
            handler.do_GET()
        handler.send_response.assert_called_with(200)
        written = handler.wfile.getvalue()
        data = json.loads(written)
        assert "error" in data

    def test_log_message_suppressed(self):
        """log_message should be a no-op (suppress request logs)."""
        handler = self._make_handler("/")
        # Should not raise
        handler.log_message("test %s", "message")

    def test_class_level_port_config(self):
        """daemon_port and eventbus_port are configurable at class level."""
        DashboardHandler.daemon_port = 1234
        DashboardHandler.eventbus_port = 5678
        handler = self._make_handler("/")
        handler.do_GET()
        written = handler.wfile.getvalue().decode("utf-8")
        assert "1234" in written
        assert "5678" in written
        # Reset
        DashboardHandler.daemon_port = DAEMON_PORT
        DashboardHandler.eventbus_port = EVENTBUS_PORT


# ── serve() setup ──


class TestServeSetup:
    """serve() configures handler ports and starts HTTPServer."""

    def test_serve_sets_handler_ports(self):
        with patch.object(HTTPServer, "__init__", return_value=None):
            with patch.object(HTTPServer, "serve_forever", side_effect=KeyboardInterrupt):
                with patch.object(HTTPServer, "server_close"):
                    with patch("builtins.print"):
                        try:
                            serve(port=9090, daemon_port=1111,
                                  eventbus_port=2222, open_browser=False)
                        except (KeyboardInterrupt, SystemExit):
                            pass
        assert DashboardHandler.daemon_port == 1111
        assert DashboardHandler.eventbus_port == 2222
        # Reset
        DashboardHandler.daemon_port = DAEMON_PORT
        DashboardHandler.eventbus_port = EVENTBUS_PORT
