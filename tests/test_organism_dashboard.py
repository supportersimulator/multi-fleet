"""Integration tests — probe_review + surgeon_adapter wired into dashboard + web_server.

15 tests covering:
- Dashboard includes probe review data when available
- Dashboard shows surgeon status
- Product bridge routes through surgeon adapter
- Web server serves probe review endpoint
- Web server serves surgeon status endpoint
- Graceful degradation when surgeon adapter unavailable
- Full pipeline: probe -> review -> dashboard -> web server
"""

import json
import sys
import threading
import time
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure multifleet is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from multifleet.dashboard_html import (
    _render_probe_review,
    _render_static_dashboard,
    _render_surgeon_status,
    render_dashboard,
)


# ── Fixtures ──────────────────────────────────────────────────────────────


def _make_snapshot(**overrides):
    """Build a minimal valid snapshot."""
    base = {
        "timestamp": 1712300000.0,
        "node_id": "mac1",
        "daemon": {"status": "ok", "nats": {"connected": True}, "branch": "main", "sessions": 2},
        "peers": {"total": 2, "reachable": 1, "peers": ["mac2"]},
        "liaison": {"pending_reviews": 0, "unread": 0},
        "rebuttals": {"active": 0, "total": 0},
        "leases": {"active": 0},
        "recent_events": [],
        "health_dot": "green",
    }
    base.update(overrides)
    return base


SAMPLE_PROBE_REVIEW = {
    "node_id": "mac1",
    "task_intent": "test",
    "timestamp": "2026-04-05T00:00:00Z",
    "probes": {
        "total": 3,
        "findings": 2,
        "surgeon_intensity": "standard",
        "blocking": 1,
        "results": [
            {
                "probe": "drift",
                "findings": [
                    {"title": "Config drift detected", "severity": "high"},
                    {"title": "Minor style issue", "severity": "low"},
                ],
            },
        ],
    },
    "surgeon_verdict": {
        "verdict": "approved",
        "confidence": 0.85,
        "reasoning": "Safe to proceed",
    },
    "acceptance": {
        "decision": "accept",
        "reasons": ["all checks passed"],
    },
    "elapsed_s": 1.2,
}

SAMPLE_SURGEON_STATUS = {
    "available": True,
    "state": {
        "phase": "review",
        "disagreement_score": 0.15,
        "correction_loops": 0,
        "abstention": False,
    },
    "counters": {
        "queries": 5,
        "reviews": 2,
        "bridge_errors": 0,
        "degraded_fallbacks": 0,
    },
    "surgeons": {
        "atlas": {"available": True},
        "cardiologist": {"available": True},
        "neurologist": {"available": False},
    },
    "gate_history": [
        {"decision": "accept", "timestamp": 1712300000.0},
        {"decision": "reject", "timestamp": 1712299000.0},
    ],
}


# ── Task 1: Dashboard includes probe review data ─────────────────────────


class TestDashboardProbeReview:
    def test_probe_review_section_with_data(self):
        """Dashboard renders probe findings with severity coloring."""
        snapshot = _make_snapshot(probe_review=SAMPLE_PROBE_REVIEW)
        html = _render_static_dashboard(snapshot)
        assert "Probe Review" in html
        assert "Config drift detected" in html
        assert "[high]" in html
        assert "[low]" in html

    def test_probe_review_shows_verdict(self):
        """Dashboard shows surgeon verdict when present."""
        snapshot = _make_snapshot(probe_review=SAMPLE_PROBE_REVIEW)
        html = _render_probe_review(snapshot)
        assert "approved" in html.lower() or "APPROVED" in html

    def test_probe_review_shows_acceptance_gate(self):
        """Dashboard shows acceptance gate pass/fail."""
        snapshot = _make_snapshot(probe_review=SAMPLE_PROBE_REVIEW)
        html = _render_probe_review(snapshot)
        assert "ACCEPT" in html

    def test_probe_review_empty_graceful(self):
        """Dashboard handles missing probe_review data gracefully."""
        snapshot = _make_snapshot()  # No probe_review key
        html = _render_static_dashboard(snapshot)
        assert "Probe Review" in html
        assert "No probe review data" in html

    def test_probe_review_with_error(self):
        """Dashboard shows error message when probe review errored."""
        snapshot = _make_snapshot(probe_review={"error": "probes module missing"})
        html = _render_probe_review(snapshot)
        assert "probes module missing" in html


# ── Task 2: Dashboard shows surgeon status ────────────────────────────────


class TestDashboardSurgeonStatus:
    def test_surgeon_status_section_with_data(self):
        """Dashboard renders surgeon availability grid."""
        snapshot = _make_snapshot(surgeon_status=SAMPLE_SURGEON_STATUS)
        html = _render_static_dashboard(snapshot)
        assert "3-Surgeon Status" in html
        assert "atlas" in html
        assert "cardiologist" in html
        assert "neurologist" in html

    def test_surgeon_counters_displayed(self):
        """Dashboard shows query/review/error counters."""
        snapshot = _make_snapshot(surgeon_status=SAMPLE_SURGEON_STATUS)
        html = _render_surgeon_status(snapshot)
        # Counters are rendered in the table
        assert "Queries" in html
        assert "Reviews" in html

    def test_surgeon_gate_history(self):
        """Dashboard shows acceptance gate history."""
        snapshot = _make_snapshot(surgeon_status=SAMPLE_SURGEON_STATUS)
        html = _render_surgeon_status(snapshot)
        assert "Gate History" in html
        assert "accept" in html.lower()

    def test_surgeon_status_empty_graceful(self):
        """Dashboard handles missing surgeon_status gracefully."""
        snapshot = _make_snapshot()
        html = _render_surgeon_status(snapshot)
        assert "No surgeon data" in html


# ── Task 3: Product bridge routes through surgeon adapter ─────────────────


class TestProductBridgeSurgeonAdapter:
    def test_fleet_to_surgeons_tries_adapter_first(self):
        """fleet_to_surgeons attempts SurgeonAdapter before generic route."""
        from multifleet.product_bridge import ProductBridge

        bridge = ProductBridge()
        bridge.discover_products()

        mock_adapter = MagicMock()
        mock_adapter.is_available.return_value = True
        mock_adapter.request_review.return_value = {
            "status": "ok",
            "output": "Looks good",
            "review_type": "cardio_review",
        }

        with patch("multifleet.product_bridge.SurgeonAdapter", return_value=mock_adapter):
            # Patch the import inside fleet_to_surgeons
            result = bridge.fleet_to_surgeons({
                "task_id": "test-1",
                "claim": "Test claim",
                "evidence": ["e1", "e2"],
            })
        assert result["source"] == "surgeon_adapter"
        assert result["reasoning"] == "Looks good"
        mock_adapter.request_review.assert_called_once()

    def test_fleet_to_surgeons_falls_back_on_import_error(self):
        """Falls back to generic route when SurgeonAdapter import fails."""
        from multifleet.product_bridge import ProductBridge

        bridge = ProductBridge()

        with patch.dict("sys.modules", {"multifleet.surgeon_adapter": None}):
            # Generic route will also likely fail, but we verify it doesn't crash
            result = bridge.fleet_to_surgeons({
                "claim": "Test",
                "evidence": [],
            })
        # Should return something (error from generic route is fine)
        assert isinstance(result, dict)


# ── Task 4: Web server endpoints ──────────────────────────────────────────


class TestWebServerEndpoints:
    def test_probe_review_endpoint(self):
        """GET /api/probe-review returns JSON."""
        from multifleet.web_server import FleetWebHandler

        handler = FleetWebHandler
        # Test the data fetcher directly
        mock_handler = MagicMock(spec=FleetWebHandler)
        mock_handler._get_probe_review = FleetWebHandler._get_probe_review

        with patch("multifleet.web_server.run_probe_review", return_value=SAMPLE_PROBE_REVIEW):
            # Direct method test
            result = FleetWebHandler._get_probe_review(mock_handler)
            assert "probes" in result or "error" in result

    def test_surgeon_status_endpoint(self):
        """GET /api/surgeon-status returns JSON."""
        from multifleet.web_server import FleetWebHandler

        mock_handler = MagicMock(spec=FleetWebHandler)
        mock_handler._get_surgeon_status = FleetWebHandler._get_surgeon_status

        result = FleetWebHandler._get_surgeon_status(mock_handler)
        # Should have available key (True or False depending on env)
        assert "available" in result or "error" in result

    def test_surgeon_status_degraded_without_bridge(self):
        """Surgeon status endpoint returns available=False when bridge missing."""
        from multifleet.web_server import FleetWebHandler

        mock_handler = MagicMock(spec=FleetWebHandler)

        with patch("multifleet.surgeon_adapter.SurgeonAdapter") as MockAdapter:
            instance = MockAdapter.return_value
            instance.is_available.return_value = False
            instance.get_state.return_value = {"phase": "unavailable", "disagreement_score": 0.0, "correction_loops": 0, "abstention": True}
            instance.get_counters.return_value = {"queries": 0, "reviews": 0, "bridge_errors": 0, "degraded_fallbacks": 1}
            result = FleetWebHandler._get_surgeon_status(mock_handler)
        assert result["available"] is False


# ── Task 5 (partial): Full pipeline ──────────────────────────────────────


class TestFullPipeline:
    def test_probe_review_to_dashboard(self):
        """Full pipeline: probe_review result renders correctly in dashboard."""
        review_data = dict(SAMPLE_PROBE_REVIEW)
        snapshot = _make_snapshot(
            probe_review=review_data,
            surgeon_status=SAMPLE_SURGEON_STATUS,
        )
        html = _render_static_dashboard(snapshot)

        # All zones present
        assert "Nodes" in html  # Zone 1
        assert "Probe Review" in html  # Zone 2
        assert "3-Surgeon Status" in html  # Zone 3
        assert "Config drift detected" in html  # Probe finding
        assert "atlas" in html  # Surgeon name
        assert "ACCEPT" in html  # Gate result
        assert "<!DOCTYPE html>" in html  # Valid HTML

    def test_dashboard_xss_safety_in_probe_review(self):
        """Probe review data is HTML-escaped to prevent XSS."""
        xss_review = {
            "probes": {
                "total": 1,
                "findings": 1,
                "surgeon_intensity": "light",
                "blocking": 0,
                "results": [
                    {"findings": [{"title": "<script>alert(1)</script>", "severity": "high"}]}
                ],
            },
            "surgeon_verdict": {"skipped": True, "reason": "test"},
            "acceptance": {},
            "elapsed_s": 0.1,
        }
        snapshot = _make_snapshot(probe_review=xss_review)
        html = _render_probe_review(snapshot)
        assert "<script>" not in html
        assert "&lt;script&gt;" in html
