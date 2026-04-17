"""Tests for the unified TheatricalDashboard composer."""

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from multifleet.theatrical.theatrical_dashboard import TheatricalDashboard


def _make_fleet_snapshot():
    """Minimal FleetDashboardData.snapshot() return value."""
    return {
        "timestamp": time.time(),
        "nodes": [
            {"id": "mac1", "status": "online", "last_seen_s": 0, "sessions": 2,
             "channel_score": "self", "broken_channels": [], "is_self": True},
            {"id": "mac2", "status": "online", "last_seen_s": 8, "sessions": 1,
             "channel_score": "A", "broken_channels": [], "is_self": False},
        ],
        "races": [],
        "rebuttals": {"active": 0, "total": 0},
        "leases": [],
        "evidence": [],
        "gains_gate": {"passed": True},
        "eventbus": {},
        "messages": {"sent": 50, "received": 48, "broadcasts": 10, "errors": 1},
        "daemon": {"status": "ok", "uptime_s": 7200},
    }


class TestTheatricalDashboard:
    def test_render_returns_complete_html(self):
        dash = TheatricalDashboard()
        html = dash.render(data={})
        assert html.startswith("<!DOCTYPE html>")
        assert "</html>" in html
        assert "<title>" in html
        assert "Context DNA" in html

    def test_all_9_components_present(self):
        dash = TheatricalDashboard()
        html = dash.render(data={})
        # Each component should have its container div
        assert "tc-vital-signs" in html or "tc-unavailable" in html
        assert "tc-surgeon-feed" in html or "tc-unavailable" in html
        assert "tc-fleet-constellation" in html or "tc-unavailable" in html
        assert "tc-evidence-timeline" in html or "tc-unavailable" in html
        assert "tc-quorum-pulse" in html or "tc-unavailable" in html
        assert "tc-memory-heat-map" in html or "tc-unavailable" in html
        assert "tc-probe-status-grid" in html or "tc-unavailable" in html
        assert "tc-corrigibility-gauge" in html or "tc-unavailable" in html
        assert "tc-session-gold-stream" in html or "tc-unavailable" in html

    def test_auto_refresh_meta(self):
        dash = TheatricalDashboard()
        html = dash.render(data={})
        assert 'http-equiv="refresh" content="10"' in html

    def test_dark_theme_css(self):
        dash = TheatricalDashboard()
        html = dash.render(data={})
        assert "#0f172a" in html  # primary background
        assert "#1e293b" in html  # card background

    def test_render_with_fleet_data(self):
        mock_data = MagicMock()
        mock_data.snapshot.return_value = _make_fleet_snapshot()
        dash = TheatricalDashboard(dashboard_data=mock_data)
        html = dash.render()
        assert "mac1" in html
        assert "mac2" in html

    def test_render_with_evidence_ledger(self):
        mock_ledger = MagicMock()
        mock_ledger.latest.return_value = [
            {"seq": 1, "entry_id": "abc", "timestamp": time.time(),
             "event_type": "merge_decision", "node_id": "mac1",
             "subject": "test merge", "entry_hash": "abcd" * 8,
             "payload": {"decision": "approved"}},
        ]
        mock_ledger.stats = {"total": 1}
        mock_ledger.verify_chain.return_value = {"valid": True, "checked": 1}
        dash = TheatricalDashboard(evidence_ledger=mock_ledger)
        html = dash.render()
        assert "merge_decision" in html

    def test_graceful_degradation_all_sources_down(self):
        """Dashboard renders without crashing even when all data sources fail."""
        mock_data = MagicMock()
        mock_data.snapshot.side_effect = Exception("connection refused")
        mock_ledger = MagicMock()
        mock_ledger.latest.side_effect = Exception("db locked")
        dash = TheatricalDashboard(dashboard_data=mock_data, evidence_ledger=mock_ledger)
        html = dash.render()
        # Should still produce valid HTML
        assert "<!DOCTYPE html>" in html
        assert "</html>" in html

    def test_render_component_by_name(self):
        dash = TheatricalDashboard()
        # Render individual component
        html = dash.render_component("vital_signs", _make_fleet_snapshot())
        assert "Heartbeat" in html
        assert "tc-vital-signs" in html

    def test_render_unknown_component(self):
        dash = TheatricalDashboard()
        html = dash.render_component("nonexistent")
        assert "Unknown component" in html

    def test_render_component_xss(self):
        dash = TheatricalDashboard()
        html = dash.render_component("<script>alert(1)</script>")
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_css_includes_all_component_styles(self):
        dash = TheatricalDashboard()
        html = dash.render(data={})
        # Verify CSS for each component class prefix exists
        assert ".tc-vital-" in html
        assert ".tc-surgeon-" in html
        assert ".tc-constellation-" in html
        assert ".tc-timeline" in html
        assert ".tc-signal-" in html
        assert ".tc-heat-" in html
        assert ".tc-probe-" in html
        assert ".tc-gauge-" in html
        assert ".tc-gold-" in html

    def test_responsive_breakpoints(self):
        dash = TheatricalDashboard()
        html = dash.render(data={})
        assert "@media" in html
        assert "1200px" in html
        assert "768px" in html

    def test_js_auto_refresh_indicator(self):
        dash = TheatricalDashboard()
        html = dash.render(data={})
        assert "<script>" in html
        assert "tc-refresh-dot" in html

    def test_gather_data_with_no_sources(self):
        dash = TheatricalDashboard()
        data = dash.gather_data()
        assert "timestamp" in data
        assert data["fleet"] is None
        assert data["evidence"] is None

    def test_gather_data_with_sources(self):
        mock_data = MagicMock()
        mock_data.snapshot.return_value = _make_fleet_snapshot()
        mock_ledger = MagicMock()
        mock_ledger.latest.return_value = []
        mock_ledger.stats = {"total": 0}
        mock_ledger.verify_chain.return_value = {"valid": True, "checked": 0}
        dash = TheatricalDashboard(dashboard_data=mock_data, evidence_ledger=mock_ledger)
        data = dash.gather_data()
        assert data["fleet"] is not None
        assert data["evidence"] is not None
        assert data["evidence"]["total"] == 0
        assert data["probes"] == mock_data.snapshot.return_value.get("probe_results")

    def test_header_shows_timestamp(self):
        dash = TheatricalDashboard()
        html = dash.render(data={})
        assert "tc-timestamp" in html
        assert "UTC" in html

    def test_extract_surgeon_data_uses_safe_statuses(self):
        dash = TheatricalDashboard()
        fleet = {
            "rebuttals": [
                {
                    "task_id": "task-123",
                    "phase": "review",
                    "verdicts_count": 2,
                    "expected": 3,
                }
            ]
        }

        with patch("multifleet.surgeon_adapter.SurgeonAdapter") as MockAdapter:
            instance = MockAdapter.return_value
            instance.get_surgeon_state.return_value = {
                "atlas": {"status": "online"},
                "cardiologist": {"status": "online"},
                "neurologist": {"status": "offline"},
            }
            data = dash._extract_surgeon_data(fleet)

        assert data["surgeons"]["cardiologist"] == "online"
        assert data["surgeons"]["neurologist"] == "offline"
        assert data["rebuttals"][0]["task_id"] == "task-123"

    def test_safe_text_redacts_secret_like_values(self):
        dash = TheatricalDashboard()
        text = dash._safe_text("OPENAI_API_KEY=sk-secret-123456789 and Bearer abcdefghijklmnop")
        assert "sk-secret" not in text
        assert "REDACTED" in text
        assert "abcdefghijklmnop" not in text
