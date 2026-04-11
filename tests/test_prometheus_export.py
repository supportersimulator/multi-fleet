"""Tests for Prometheus text exposition exporter."""

import re
from unittest.mock import patch

from multifleet.prometheus_export import (
    _collect_daemon_metrics,
    _collect_doctor_metrics,
    _collect_lease_metrics,
    _collect_metrics_singleton,
    _collect_probe_metrics,
    _collect_rebuttal_metrics,
    _format_metric,
    _safe_label,
    generate_prometheus_text,
)


class TestSafeLabel:
    def test_plain_string(self):
        assert _safe_label("mac3") == "mac3"

    def test_escapes_quotes(self):
        assert _safe_label('say "hi"') == 'say \\"hi\\"'

    def test_escapes_backslash(self):
        assert _safe_label("a\\b") == "a\\\\b"

    def test_escapes_newline(self):
        assert _safe_label("line1\nline2") == "line1\\nline2"


class TestFormatMetric:
    def test_simple_counter(self):
        out = _format_metric("fleet_msgs", 42, "Total messages", "counter")
        assert "# HELP fleet_msgs Total messages" in out
        assert "# TYPE fleet_msgs counter" in out
        assert "fleet_msgs 42" in out

    def test_with_labels(self):
        out = _format_metric("fleet_up", 1, "", "gauge", {"node": "mac3"})
        assert 'fleet_up{node="mac3"} 1' in out

    def test_multiple_labels_sorted(self):
        out = _format_metric("m", 1, "", "", {"z": "1", "a": "2"})
        assert 'a="2",z="1"' in out

    def test_no_help_or_type(self):
        out = _format_metric("m", 5, "", "")
        assert "HELP" not in out
        assert "TYPE" not in out
        assert "m 5" in out


class TestDaemonMetrics:
    def test_uptime_and_peers(self):
        health = {"uptime_s": 123.4, "peers": ["mac1", "mac2"], "messages_sent": 50}
        blocks = _collect_daemon_metrics(health, "mac3")
        text = "\n".join(blocks)
        assert "fleet_daemon_uptime_seconds" in text
        assert "123.4" in text
        assert "fleet_peers_connected" in text
        assert "fleet_messages_sent_total" in text

    def test_empty_health(self):
        blocks = _collect_daemon_metrics({}, "mac3")
        text = "\n".join(blocks)
        assert "fleet_peers_connected" in text
        assert "0" in text


class TestMetricsSingleton:
    def test_counters_and_gauges(self):
        snapshot = {
            "counters": {"messages_sent": 10, "messages_received": 5},
            "gauges": {"active_peers": 3, "uptime_s": 99.0},
            "histograms": {},
        }
        blocks = _collect_metrics_singleton(snapshot, "mac3")
        text = "\n".join(blocks)
        assert "fleet_messages_sent" in text
        assert "fleet_active_peers" in text

    def test_histograms_expanded(self):
        snapshot = {
            "counters": {},
            "gauges": {},
            "histograms": {
                "latency": {"min": 1.0, "max": 50.0, "avg": 25.0, "p95": 48.0, "count": 100}
            },
        }
        blocks = _collect_metrics_singleton(snapshot, "mac3")
        text = "\n".join(blocks)
        assert "fleet_latency_min" in text
        assert "fleet_latency_p95" in text
        assert "fleet_latency_count" in text


class TestDoctorMetrics:
    def test_doctor_report(self):
        report = {"passed": 5, "failed": 1, "warned": 2}
        blocks = _collect_doctor_metrics(report, "mac3")
        text = "\n".join(blocks)
        assert "fleet_doctor_checks_passed" in text
        assert "fleet_doctor_checks_failed" in text
        assert "fleet_doctor_checks_warned" in text

    def test_none_report(self):
        assert _collect_doctor_metrics(None, "mac3") == []


class TestProbeMetrics:
    def test_probe_findings(self):
        results = [
            {"probe_id": "mirror_drift", "severity": "medium", "findings": [{"title": "f1"}, {"title": "f2"}]},
            {"probe_id": "stale_docs", "severity": "low", "findings": []},
        ]
        blocks = _collect_probe_metrics(results, "mac3")
        text = "\n".join(blocks)
        assert 'probe="mirror_drift"' in text
        assert "2" in text  # 2 findings

    def test_none_results(self):
        assert _collect_probe_metrics(None, "mac3") == []


class TestLeaseMetrics:
    def test_lease_stats(self):
        stats = {"acquired": 10, "released": 8, "renewed": 3, "denied": 1, "expired_cleaned": 2, "active": 4}
        blocks = _collect_lease_metrics(stats, "mac3")
        text = "\n".join(blocks)
        assert "fleet_lease_active" in text
        assert "fleet_lease_acquired_total" in text

    def test_none_stats(self):
        assert _collect_lease_metrics(None, "mac3") == []


class TestRebuttalMetrics:
    def test_active_sessions(self):
        blocks = _collect_rebuttal_metrics(3, "mac3")
        text = "\n".join(blocks)
        assert "fleet_rebuttal_sessions_active" in text
        assert "3" in text


class TestGeneratePrometheusText:
    def test_full_output_structure(self):
        snapshot = {
            "counters": {"msgs": 10},
            "gauges": {"uptime_s": 5.0},
            "histograms": {},
        }
        doctor = {"passed": 3, "failed": 0, "warned": 1}
        text = generate_prometheus_text(
            node_id="mac3",
            metrics_snapshot=snapshot,
            doctor_report=doctor,
            rebuttal_active=2,
            include_health=False,
        )
        assert "fleet_msgs" in text
        assert "fleet_doctor_checks_passed" in text
        assert "fleet_rebuttal_sessions_active" in text
        assert "fleet_scrape_timestamp" in text
        # Valid Prometheus format: no empty metric names
        for line in text.strip().split("\n"):
            if line.startswith("#") or not line.strip():
                continue
            assert re.match(r"[a-z_]", line), f"Bad metric line: {line}"

    @patch("multifleet.prometheus_export._fetch_health")
    def test_includes_health_when_enabled(self, mock_health):
        mock_health.return_value = {"uptime_s": 60, "peers": ["mac1"]}
        text = generate_prometheus_text(
            node_id="mac3",
            metrics_snapshot={"counters": {}, "gauges": {}, "histograms": {}},
            include_health=True,
        )
        assert "fleet_daemon_uptime_seconds" in text
        mock_health.assert_called_once()
