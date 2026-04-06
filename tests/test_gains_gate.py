"""
Tests for FleetGainsGate — structured health verification.

All network calls are mocked. Tests verify gate logic, not infrastructure.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

FLEET_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(FLEET_ROOT))

from multifleet.gains_gate import FleetGainsGate, CheckResult, GateResult


# ── Helpers ──

def _mock_health(status="ok", transport="nats", nats_url="nats://127.0.0.1:4222",
                 peers=None, channel_health=None, registered_types=5, dedup_size=42):
    """Build a fake daemon /health response."""
    return {
        "status": status,
        "uptime_s": 3600,
        "transport": transport,
        "nats_url": nats_url,
        "peers": peers or {"mac1": {}, "mac3": {}},
        "channel_health": channel_health or {"mac1": {"score": 80}, "mac3": {"score": 60}},
        "registered_types": registered_types,
        "dedup_size": dedup_size,
    }


def _patch_health(gate, health_data):
    """Inject cached health into a gate instance."""
    gate._health_cache = health_data


def _patch_urlopen(health_data=None, eventbus_ok=True, leases=None, proposals=None):
    """Create a mock urlopen that returns appropriate data per URL."""
    def fake_urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        mock_resp = MagicMock()
        if "/health" in url and "8031" in url:
            if eventbus_ok:
                mock_resp.read.return_value = json.dumps({"status": "ok"}).encode()
            else:
                raise ConnectionRefusedError("EventBus down")
        elif "/health" in url:
            mock_resp.read.return_value = json.dumps(health_data or _mock_health()).encode()
        elif "/leases" in url:
            mock_resp.read.return_value = json.dumps({"leases": leases or []}).encode()
        elif "/rebuttals" in url:
            mock_resp.read.return_value = json.dumps({"proposals": proposals or []}).encode()
        else:
            raise Exception(f"Unexpected URL: {url}")
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp
    return fake_urlopen


# ── Tests ──

class TestGainsGateAllPass:
    """When all infrastructure is healthy, gate should pass."""

    def test_all_pass_returns_passed(self):
        gate = FleetGainsGate()
        health = _mock_health()
        with patch("urllib.request.urlopen", side_effect=_patch_urlopen(health)):
            with patch.object(gate, "_check_test_suite",
                              return_value=CheckResult("test_suite", True, "OK", critical=False)):
                with patch.object(gate, "_check_audit_trail",
                                  return_value=CheckResult("audit_trail", True, "OK", critical=False)):
                    result = gate.run(quick=False)
        assert result.passed is True
        assert result.critical_failures == 0
        assert result.passed_count == result.total


class TestCriticalFailure:
    """A critical failure should fail the gate."""

    def test_critical_failure_fails_gate(self):
        gate = FleetGainsGate()
        # Daemon down -> all health checks fail
        def fail_urlopen(req, timeout=None):
            raise ConnectionRefusedError("daemon down")
        with patch("urllib.request.urlopen", side_effect=fail_urlopen):
            result = gate.run(quick=True)
        assert result.passed is False
        assert result.critical_failures > 0
        assert "FAILED" in result.recommendation


class TestNonCriticalFailure:
    """Non-critical failures should not block the gate."""

    def test_non_critical_failure_passes_gate(self):
        gate = FleetGainsGate()
        health = _mock_health()
        # EventBus down (non-critical) but everything else OK
        with patch("urllib.request.urlopen",
                   side_effect=_patch_urlopen(health, eventbus_ok=False)):
            result = gate.run(quick=True)
        assert result.passed is True
        assert result.failed_count >= 1
        # The eventbus check is non-critical
        eb_check = next((c for c in result.checks if c["name"] == "eventbus_running"), None)
        assert eb_check is not None
        assert eb_check["passed"] is False
        assert eb_check["critical"] is False


class TestQuickMode:
    """Quick mode runs fewer checks than full mode."""

    def test_quick_mode_fewer_checks(self):
        gate = FleetGainsGate()
        health = _mock_health()
        with patch("urllib.request.urlopen", side_effect=_patch_urlopen(health)):
            quick_result = gate.run(quick=True)
        gate2 = FleetGainsGate()
        with patch("urllib.request.urlopen", side_effect=_patch_urlopen(health)):
            with patch.object(gate2, "_check_test_suite",
                              return_value=CheckResult("test_suite", True, "OK", critical=False)):
                with patch.object(gate2, "_check_audit_trail",
                                  return_value=CheckResult("audit_trail", True, "OK", critical=False)):
                    full_result = gate2.run(quick=False)
        assert quick_result.total == 6
        assert full_result.total == 10


class TestResultStructure:
    """Gate result must have all expected fields."""

    def test_result_structure(self):
        gate = FleetGainsGate()
        health = _mock_health()
        with patch("urllib.request.urlopen", side_effect=_patch_urlopen(health)):
            result = gate.run(quick=True)
        d = result.to_dict()
        assert "passed" in d
        assert "total" in d
        assert "passed_count" in d
        assert "failed_count" in d
        assert "critical_failures" in d
        assert "duration_ms" in d
        assert "timestamp" in d
        assert "recommendation" in d
        assert "checks" in d
        assert isinstance(d["checks"], list)
        # Each check has required fields
        for check in d["checks"]:
            assert "name" in check
            assert "passed" in check
            assert "message" in check
            assert "latency_ms" in check
            assert "critical" in check


class TestRecommendation:
    """Recommendation should reflect failure state."""

    def test_recommendation_on_failure(self):
        gate = FleetGainsGate()
        def fail_urlopen(req, timeout=None):
            raise ConnectionRefusedError("down")
        with patch("urllib.request.urlopen", side_effect=fail_urlopen):
            result = gate.run(quick=True)
        assert "FAILED" in result.recommendation
        assert "critical" in result.recommendation.lower()

    def test_recommendation_on_success(self):
        gate = FleetGainsGate()
        health = _mock_health()
        with patch("urllib.request.urlopen", side_effect=_patch_urlopen(health)):
            result = gate.run(quick=True)
        assert "PASSED" in result.recommendation
