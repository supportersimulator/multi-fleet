"""Tests for FleetMetricsAggregator -- collection, aggregation, trends, prometheus."""

import time
from unittest.mock import patch

from multifleet.fleet_metrics import FleetMetricsAggregator, NodeMetrics


def _mock_health():
    return {
        "node_id": "mac2",
        "uptime_s": 3600,
        "stats": {"sent": 10, "received": 20, "errors": 1, "dedup_dropped": 2},
        "peers": [
            {"node_id": "mac1", "uptime_s": 7200, "stats": {"sent": 5, "received": 8, "errors": 0}},
            {"node_id": "mac3", "uptime_s": 1800, "stats": {"sent": 3, "received": 4, "errors": 2}},
        ],
    }


class TestNodeMetrics:
    def test_to_dict_round_trip(self):
        nm = NodeMetrics(node_id="mac1", messages_sent=42, uptime_s=100)
        d = nm.to_dict()
        assert d["node_id"] == "mac1"
        assert d["messages_sent"] == 42
        assert "last_updated" in d


class TestCollect:
    @patch("multifleet.fleet_metrics._fetch_json")
    def test_collect_parses_health(self, mock_fetch):
        mock_fetch.return_value = _mock_health()
        agg = FleetMetricsAggregator()
        nodes = agg.collect()
        assert "mac2" in nodes
        assert "mac1" in nodes
        assert "mac3" in nodes
        assert nodes["mac2"].messages_sent == 10
        assert nodes["mac1"].messages_received == 8

    @patch("multifleet.fleet_metrics._fetch_json", return_value=None)
    def test_collect_daemon_down(self, mock_fetch):
        agg = FleetMetricsAggregator()
        nodes = agg.collect()
        assert nodes == {}


class TestAggregate:
    @patch("multifleet.fleet_metrics._fetch_json")
    def test_aggregate_totals(self, mock_fetch):
        mock_fetch.return_value = _mock_health()
        agg = FleetMetricsAggregator()
        result = agg.aggregate()
        assert result["node_count"] == 3
        assert result["total_messages_sent"] == 18  # 10 + 5 + 3
        assert result["total_errors"] == 3  # 1 + 0 + 2
        assert "nodes" in result
        assert "mac1" in result["nodes"]


class TestTrends:
    def test_insufficient_data(self):
        agg = FleetMetricsAggregator()
        t = agg.trends()
        assert t["status"] == "insufficient_data"

    @patch("multifleet.fleet_metrics._fetch_json")
    def test_trends_with_history(self, mock_fetch):
        mock_fetch.return_value = _mock_health()
        agg = FleetMetricsAggregator()
        # Inject 2 history points
        now = time.time()
        agg._history = [
            {"timestamp": now - 120, "total_messages_sent": 10, "total_messages_received": 20,
             "total_errors": 0, "total_evidence_entries": 5, "total_probe_findings": 2},
            {"timestamp": now, "total_messages_sent": 30, "total_messages_received": 40,
             "total_errors": 1, "total_evidence_entries": 10, "total_probe_findings": 4},
        ]
        t = agg.trends(window_minutes=5)
        assert t["snapshots"] == 2
        assert t["messages_sent_per_min"] == 10.0  # 20 msgs / 2 min


class TestProductivity:
    @patch("multifleet.fleet_metrics._fetch_json")
    def test_score_range(self, mock_fetch):
        mock_fetch.return_value = _mock_health()
        agg = FleetMetricsAggregator()
        result = agg.productivity_score()
        assert 0 <= result["score"] <= 100
        assert "components" in result
        assert result["raw"]["active_nodes"] == 3


class TestPrometheus:
    @patch("multifleet.fleet_metrics._fetch_json")
    def test_prometheus_output(self, mock_fetch):
        mock_fetch.return_value = _mock_health()
        agg = FleetMetricsAggregator()
        text = agg.to_prometheus()
        assert "fleet_nodes_total 3" in text
        assert 'fleet_node_messages_sent{node="mac2"} 10' in text
        assert "fleet_total_errors" in text
        assert "fleet_aggregator_scrape_timestamp" in text


class TestMessageStats:
    @patch("multifleet.fleet_metrics._fetch_json")
    def test_dashboard_shape(self, mock_fetch):
        mock_fetch.return_value = _mock_health()
        agg = FleetMetricsAggregator()
        stats = agg.message_stats()
        assert "sent" in stats
        assert "received" in stats
        assert "errors" in stats
        assert stats["sent"] == 18
