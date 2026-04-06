"""Tests for multifleet.status_aggregator — fleet status snapshot and health dot."""

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from multifleet.status_aggregator import FleetStatusAggregator


# ── Helpers ──

def _make_daemon_health(
    status="ok",
    nats_connected=True,
    peers=None,
    liaison=None,
    rebuttals=None,
    leases=None,
):
    """Build a daemon /health response dict."""
    return {
        "status": status,
        "nats": {"connected": nats_connected},
        "peers": peers or {},
        "liaison": liaison or {},
        "rebuttals": rebuttals or {},
        "leases": leases or {},
        "recent_events": [],
    }


def _mock_urlopen(data_dict, status=200):
    """Create a mock urllib response."""
    resp = MagicMock()
    resp.status = status
    resp.read.return_value = json.dumps(data_dict).encode("utf-8")
    return resp


# ═══════════════════════════════════════════════════════════════════
# Snapshot tests
# ═══════════════════════════════════════════════════════════════════


class TestSnapshotHealthyDaemon:
    def test_snapshot_returns_all_keys(self):
        health = _make_daemon_health(
            peers={"mac2": {"status": "online"}, "mac3": {"status": "online"}},
        )
        with patch("urllib.request.urlopen", return_value=_mock_urlopen(health)):
            agg = FleetStatusAggregator(node_id="mac1")
            snap = agg.snapshot()

        assert "timestamp" in snap
        assert snap["node_id"] == "mac1"
        assert "daemon" in snap
        assert "peers" in snap
        assert "liaison" in snap
        assert "rebuttals" in snap
        assert "leases" in snap
        assert "recent_events" in snap
        assert "health_dot" in snap

    def test_snapshot_healthy_returns_green(self):
        health = _make_daemon_health(
            nats_connected=True,
            peers={"mac2": {"status": "online"}},
        )
        with patch("urllib.request.urlopen", return_value=_mock_urlopen(health)):
            agg = FleetStatusAggregator(node_id="mac1")
            snap = agg.snapshot()

        assert snap["health_dot"] == "green"

    def test_snapshot_peer_count(self):
        health = _make_daemon_health(
            peers={"mac2": {"status": "online"}, "mac3": {"status": "offline"}},
        )
        with patch("urllib.request.urlopen", return_value=_mock_urlopen(health)):
            agg = FleetStatusAggregator(node_id="mac1")
            snap = agg.snapshot()

        assert snap["peers"]["total"] == 2
        assert snap["peers"]["reachable"] == 1


class TestSnapshotDaemonOffline:
    def test_graceful_when_daemon_unreachable(self):
        with patch("urllib.request.urlopen", side_effect=ConnectionRefusedError("refused")):
            agg = FleetStatusAggregator(node_id="mac1")
            snap = agg.snapshot()

        assert snap["daemon"]["status"] == "unreachable"
        assert snap["health_dot"] == "red"
        assert snap["peers"]["total"] == 0

    def test_snapshot_still_has_all_keys_when_offline(self):
        with patch("urllib.request.urlopen", side_effect=OSError("timeout")):
            agg = FleetStatusAggregator(node_id="mac1")
            snap = agg.snapshot()

        for key in ("timestamp", "node_id", "daemon", "peers", "liaison",
                    "rebuttals", "leases", "recent_events", "health_dot"):
            assert key in snap


# ═══════════════════════════════════════════════════════════════════
# Health dot computation
# ═══════════════════════════════════════════════════════════════════


class TestHealthDot:
    def _agg(self):
        return FleetStatusAggregator(node_id="mac1")

    def test_green_requires_nats_and_peers(self):
        agg = self._agg()
        daemon = _make_daemon_health(nats_connected=True, peers={"mac2": {"status": "online"}})
        peers = agg._get_peer_summary(daemon)
        assert agg._compute_health_dot(daemon, peers) == "green"

    def test_yellow_when_no_nats(self):
        agg = self._agg()
        daemon = _make_daemon_health(nats_connected=False, peers={"mac2": {"status": "online"}})
        peers = agg._get_peer_summary(daemon)
        assert agg._compute_health_dot(daemon, peers) == "yellow"

    def test_yellow_when_no_peers(self):
        agg = self._agg()
        daemon = _make_daemon_health(nats_connected=True, peers={})
        peers = agg._get_peer_summary(daemon)
        assert agg._compute_health_dot(daemon, peers) == "yellow"

    def test_yellow_when_all_peers_offline(self):
        agg = self._agg()
        daemon = _make_daemon_health(nats_connected=True, peers={"mac2": {"status": "offline"}})
        peers = agg._get_peer_summary(daemon)
        assert agg._compute_health_dot(daemon, peers) == "yellow"

    def test_red_when_daemon_unreachable(self):
        agg = self._agg()
        daemon = {"status": "unreachable"}
        peers = {"total": 0, "reachable": 0, "peers": []}
        assert agg._compute_health_dot(daemon, peers) == "red"


# ═══════════════════════════════════════════════════════════════════
# Status bar text formatting
# ═══════════════════════════════════════════════════════════════════


class TestStatusBarText:
    def test_healthy_fleet(self):
        health = _make_daemon_health(
            nats_connected=True,
            peers={"mac2": {"status": "online"}, "mac3": {"status": "online"}},
            liaison={"pending_reviews": 0, "active_rebuttals": 0, "unread": 0, "summary": ""},
            rebuttals={"active": 0, "total": 0},
        )
        with patch("urllib.request.urlopen", return_value=_mock_urlopen(health)):
            agg = FleetStatusAggregator(node_id="mac1")
            text = agg.status_bar_text()

        assert "F:2/2" in text
        assert "●" in text

    def test_degraded_fleet_shows_yellow(self):
        health = _make_daemon_health(
            nats_connected=False,
            peers={"mac2": {"status": "online"}},
            liaison={"pending_reviews": 2, "active_rebuttals": 0, "unread": 5, "summary": ""},
            rebuttals={"active": 0, "total": 0},
        )
        with patch("urllib.request.urlopen", return_value=_mock_urlopen(health)):
            agg = FleetStatusAggregator(node_id="mac1")
            text = agg.status_bar_text()

        assert "◐" in text
        assert "2 reviews" in text
        assert "5 unread" in text

    def test_offline_shows_red_dot(self):
        with patch("urllib.request.urlopen", side_effect=ConnectionRefusedError):
            agg = FleetStatusAggregator(node_id="mac1")
            text = agg.status_bar_text()

        assert "○" in text
        assert "F:0/0" in text

    def test_single_review_no_plural(self):
        health = _make_daemon_health(
            nats_connected=True,
            peers={"mac2": {"status": "online"}},
            liaison={"pending_reviews": 1, "active_rebuttals": 0, "unread": 0, "summary": ""},
            rebuttals={"active": 0, "total": 0},
        )
        with patch("urllib.request.urlopen", return_value=_mock_urlopen(health)):
            agg = FleetStatusAggregator(node_id="mac1")
            text = agg.status_bar_text()

        assert "1 review" in text
        assert "1 reviews" not in text


# ═══════════════════════════════════════════════════════════════════
# Peer summary aggregation
# ═══════════════════════════════════════════════════════════════════


class TestPeerSummary:
    def test_dict_peers(self):
        agg = FleetStatusAggregator(node_id="mac1")
        daemon = _make_daemon_health(peers={"mac2": {"status": "online"}, "mac3": {"status": "offline"}})
        summary = agg._get_peer_summary(daemon)
        assert summary["total"] == 2
        assert summary["reachable"] == 1
        assert "mac2" in summary["peers"]
        assert "mac3" in summary["peers"]

    def test_list_peers(self):
        agg = FleetStatusAggregator(node_id="mac1")
        daemon = {"peers": [{"node_id": "mac2", "status": "reachable"}, {"node_id": "mac3", "status": "ok"}]}
        summary = agg._get_peer_summary(daemon)
        assert summary["total"] == 2
        assert summary["reachable"] == 2

    def test_empty_peers(self):
        agg = FleetStatusAggregator(node_id="mac1")
        daemon = _make_daemon_health(peers={})
        summary = agg._get_peer_summary(daemon)
        assert summary["total"] == 0
        assert summary["reachable"] == 0

    def test_non_dict_peers_value(self):
        agg = FleetStatusAggregator(node_id="mac1")
        daemon = {"peers": "invalid"}
        summary = agg._get_peer_summary(daemon)
        assert summary["total"] == 0


# ═══════════════════════════════════════════════════════════════════
# Caching
# ═══════════════════════════════════════════════════════════════════


class TestCaching:
    def test_daemon_health_is_cached(self):
        health = _make_daemon_health()
        mock_resp = _mock_urlopen(health)
        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_url:
            agg = FleetStatusAggregator(node_id="mac1")
            agg._get_daemon_health()
            agg._get_daemon_health()
            # Should only call once due to caching
            assert mock_url.call_count == 1
