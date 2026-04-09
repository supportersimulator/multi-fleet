"""Tests for multifleet.regression_dashboard — build health + revenue tracking."""

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from multifleet.regression_dashboard import RegressionDashboard
from multifleet.web_server import start_fleet_server, stop_fleet_server


# ═══════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════


@pytest.fixture
def dashboard(tmp_path):
    """Dashboard pointed at a mock repo structure."""
    fleet = tmp_path / "multi-fleet" / "multifleet"
    fleet.mkdir(parents=True)
    probes = fleet / "probes"
    probes.mkdir()
    tests = tmp_path / "multi-fleet" / "tests"
    tests.mkdir()

    # Create some fake modules
    (fleet / "__init__.py").write_text("")
    (fleet / "alpha.py").write_text("class Alpha: pass")
    (fleet / "beta.py").write_text("class Beta: pass")

    # Create fake probes
    (probes / "__init__.py").write_text("")
    (probes / "drift.py").write_text("class Drift: pass")
    (probes / "health.py").write_text("class Health: pass")

    # Create fake test files
    (tests / "test_alpha.py").write_text(
        "def test_one(): pass\ndef test_two(): pass\ndef test_three(): pass\n"
    )
    (tests / "test_beta.py").write_text(
        "def test_b1(): pass\ndef test_b2(): pass\n"
    )

    return RegressionDashboard(repo_path=str(tmp_path))


@pytest.fixture
def server_port():
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def running_server(server_port):
    result = start_fleet_server(port=server_port)
    assert result["status"] == "started"
    time.sleep(0.2)
    yield server_port
    stop_fleet_server()


def _get(port, path, timeout=5):
    url = f"http://127.0.0.1:{port}{path}"
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.status, resp.headers, resp.read()


# ═══════════════════════════════════════════════════════════════════
# Unit: metric collection
# ═══════════════════════════════════════════════════════════════════


class TestMetricCollection:
    def test_count_tests(self, dashboard):
        assert dashboard._count_tests() == 5

    def test_count_modules(self, dashboard):
        assert dashboard._count_modules() == 2

    def test_count_probes(self, dashboard):
        assert dashboard._count_probes() == 2

    def test_collect_metrics_has_all_keys(self, dashboard):
        m = dashboard.collect_metrics()
        expected_keys = {
            "timestamp", "test_count", "module_count", "skill_count",
            "mcp_tool_count", "probe_count", "revenue_path",
            "recent_commits", "probe_summary",
        }
        assert expected_keys <= set(m.keys())

    def test_revenue_path_totals(self, dashboard):
        rp = dashboard._revenue_path_status()
        assert rp["total"] == 13
        assert rp["done"] + rp["in_progress"] + rp["remaining"] == 13
        assert len(rp["sessions"]) == 13


# ═══════════════════════════════════════════════════════════════════
# Unit: rendering
# ═══════════════════════════════════════════════════════════════════


class TestRendering:
    def test_render_html_contains_title(self, dashboard):
        html = dashboard.render_html()
        assert "Regression Dashboard" in html
        assert "Revenue Path" in html

    def test_render_markdown_contains_metrics(self, dashboard):
        md = dashboard.render_markdown()
        assert "# Regression Dashboard" in md
        assert "Tests" in md
        assert "Revenue Path" in md


# ═══════════════════════════════════════════════════════════════════
# Integration: web server endpoints
# ═══════════════════════════════════════════════════════════════════


class TestWebEndpoints:
    def test_regression_html_endpoint(self, running_server):
        status, headers, body = _get(running_server, "/regression")
        assert status == 200
        assert "text/html" in headers.get("Content-Type", "")
        assert b"Regression Dashboard" in body

    def test_regression_api_endpoint(self, running_server):
        status, headers, body = _get(running_server, "/api/regression")
        assert status == 200
        data = json.loads(body)
        assert "revenue_path" in data or "error" in data
