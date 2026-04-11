"""Tests for multifleet.ide_bridge — fleet event to IDE translation."""

import json
import sys
import threading
import time
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from multifleet.ide_bridge import IDEBridge
from multifleet import event_stream
from multifleet.event_stream import push_event, start_server


# ═══════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════

def _make_aggregator(
    peers=None,
    health_dot="green",
    rebuttals=None,
    leases=None,
    liaison=None,
    node_id="mac1",
):
    """Create a mock FleetStatusAggregator."""
    agg = MagicMock()
    daemon_peers = peers or {
        "mac2": {"status": "online", "lastSeen": 5, "sessions": 2},
        "mac3": {"status": "online", "lastSeen": 12, "sessions": 1},
    }
    snap = {
        "timestamp": time.time(),
        "node_id": node_id,
        "daemon": {
            "status": "ok",
            "peers": daemon_peers,
        },
        "peers": {
            "total": len(daemon_peers),
            "reachable": sum(
                1 for p in daemon_peers.values()
                if isinstance(p, dict) and p.get("status") in ("online", "reachable")
            ),
            "peers": list(daemon_peers.keys()),
        },
        "liaison": liaison or {"pending_reviews": 0, "active_rebuttals": 0, "unread": 0, "summary": ""},
        "rebuttals": rebuttals or {"active": 0, "total": 0},
        "leases": leases or {"active": 0, "held_by_me": 0},
        "recent_events": [],
        "health_dot": health_dot,
    }
    agg.snapshot.return_value = snap
    agg.status_bar_text.return_value = f"F:{snap['peers']['reachable']}/{snap['peers']['total']} ●"
    return agg


# ═══════════════════════════════════════════════════════════════════
# Event Translation Tests
# ═══════════════════════════════════════════════════════════════════


class TestTranslateEvent:
    def test_peer_online_returns_statusbar_command(self):
        bridge = IDEBridge()
        result = bridge.translate_event("fleet.peer.online", {"nodeId": "mac2"})
        assert result["command"] == "statusBar"
        assert result["event_type"] == "fleet.peer.online"
        assert result["payload"]["action"] == "online"

    def test_peer_offline_returns_statusbar_command(self):
        bridge = IDEBridge()
        result = bridge.translate_event("fleet.peer.offline", {"nodeId": "mac2"})
        assert result["command"] == "statusBar"
        assert result["payload"]["action"] == "offline"

    def test_health_degraded_returns_warning(self):
        bridge = IDEBridge()
        result = bridge.translate_event("fleet.health.degraded", {"nodeId": "mac2"})
        assert result["command"] == "warning"
        # Should also push a notification
        notifications = bridge.get_pending_notifications()
        assert len(notifications) == 1
        assert notifications[0]["severity"] == "warning"
        assert "mac2" in notifications[0]["message"]

    def test_probe_finding_returns_notification(self):
        bridge = IDEBridge()
        result = bridge.translate_event("fleet.probe.finding", {
            "title": "High memory usage",
            "severity": "warning",
        })
        assert result["command"] == "notification"
        notifications = bridge.get_pending_notifications()
        assert len(notifications) == 1
        assert notifications[0]["severity"] == "warning"

    def test_rebuttal_proposed_returns_badge(self):
        bridge = IDEBridge()
        result = bridge.translate_event("fleet.rebuttal.proposed", {
            "subject": "Schema change proposal",
        })
        assert result["command"] == "badge"
        notifications = bridge.get_pending_notifications()
        assert len(notifications) == 1
        assert "Schema change" in notifications[0]["message"]

    def test_unknown_event_defaults_to_notification(self):
        bridge = IDEBridge()
        result = bridge.translate_event("fleet.unknown.event", {"data": "test"})
        assert result["command"] == "notification"

    def test_timestamp_present(self):
        bridge = IDEBridge()
        result = bridge.translate_event("fleet.peer.online", {"nodeId": "mac2"})
        assert "timestamp" in result
        assert isinstance(result["timestamp"], float)


class TestFormatForExtension:
    def test_format_matches_event_v1_shape(self):
        bridge = IDEBridge()
        event = bridge.translate_event("fleet.peer.online", {"nodeId": "mac2"})
        formatted = bridge.format_for_extension(event)
        assert formatted["type"] == "fleet.peer.online"
        assert "payload" in formatted
        assert formatted["source"] == "fleet-bridge"
        assert "T" in formatted["timestamp"]  # ISO format


# ═══════════════════════════════════════════════════════════════════
# Status Bar Data Tests
# ═══════════════════════════════════════════════════════════════════


class TestStatusBarData:
    def test_without_aggregator_returns_offline(self):
        bridge = IDEBridge(aggregator=None)
        data = bridge.get_status_bar_data()
        assert data["health_dot"] == "red"
        assert data["node_count"] == 0
        assert "off" in data["text"]

    def test_with_healthy_aggregator(self):
        agg = _make_aggregator(health_dot="green")
        bridge = IDEBridge(aggregator=agg)
        data = bridge.get_status_bar_data()
        assert data["health_dot"] == "green"
        assert data["health_status"] == "ok"
        assert data["node_count"] == 3  # self + 2 peers
        assert data["reachable_peers"] == 2

    def test_with_degraded_aggregator(self):
        agg = _make_aggregator(health_dot="yellow")
        bridge = IDEBridge(aggregator=agg)
        data = bridge.get_status_bar_data()
        assert data["health_status"] == "degraded"

    def test_includes_rebuttal_count(self):
        agg = _make_aggregator(rebuttals={"active": 3, "total": 5})
        bridge = IDEBridge(aggregator=agg)
        data = bridge.get_status_bar_data()
        assert data["rebuttals"] == 3


# ═══════════════════════════════════════════════════════════════════
# Activity Bar Data Tests
# ═══════════════════════════════════════════════════════════════════


class TestActivityBarData:
    def test_without_aggregator(self):
        bridge = IDEBridge(aggregator=None)
        data = bridge.get_activity_bar_data()
        assert data["connected"] is False
        assert data["sections"] == []

    def test_peer_section(self):
        agg = _make_aggregator()
        bridge = IDEBridge(aggregator=agg)
        data = bridge.get_activity_bar_data()
        peer_section = next(s for s in data["sections"] if s["id"] == "peers")
        assert peer_section["count"] == 2
        assert len(peer_section["items"]) == 2
        mac2 = next(i for i in peer_section["items"] if i["id"] == "mac2")
        assert mac2["status"] == "online"
        assert mac2["icon"] == "circle-filled"

    def test_rebuttal_section(self):
        agg = _make_aggregator(rebuttals={"active": 2, "total": 5})
        bridge = IDEBridge(aggregator=agg)
        data = bridge.get_activity_bar_data()
        rebuttal_section = next(s for s in data["sections"] if s["id"] == "rebuttals")
        assert rebuttal_section["count"] == 2
        assert rebuttal_section["items"][0]["badge"] == 2

    def test_lease_section(self):
        agg = _make_aggregator(leases={"active": 1, "held_by_me": 1})
        bridge = IDEBridge(aggregator=agg)
        data = bridge.get_activity_bar_data()
        lease_section = next(s for s in data["sections"] if s["id"] == "leases")
        assert lease_section["count"] == 1

    def test_unread_messages_section(self):
        agg = _make_aggregator(liaison={"pending_reviews": 0, "active_rebuttals": 0, "unread": 3, "summary": ""})
        bridge = IDEBridge(aggregator=agg)
        data = bridge.get_activity_bar_data()
        msg_section = next((s for s in data["sections"] if s["id"] == "messages"), None)
        assert msg_section is not None
        assert msg_section["count"] == 3

    def test_no_messages_section_when_zero_unread(self):
        agg = _make_aggregator()
        bridge = IDEBridge(aggregator=agg)
        data = bridge.get_activity_bar_data()
        msg_section = next((s for s in data["sections"] if s["id"] == "messages"), None)
        assert msg_section is None


# ═══════════════════════════════════════════════════════════════════
# Notification Queue Tests
# ═══════════════════════════════════════════════════════════════════


class TestNotifications:
    def test_notifications_cleared_after_get(self):
        bridge = IDEBridge()
        bridge.translate_event("fleet.health.degraded", {"nodeId": "mac2"})
        first = bridge.get_pending_notifications()
        assert len(first) == 1
        second = bridge.get_pending_notifications()
        assert len(second) == 0

    def test_notification_max_cap(self):
        bridge = IDEBridge()
        bridge._max_notifications = 5
        for i in range(10):
            bridge.translate_event("fleet.health.degraded", {"nodeId": f"node{i}"})
        assert len(bridge._pending_notifications) == 5


# ═══════════════════════════════════════════════════════════════════
# Extension Endpoint Tests (HTTP integration)
# ═══════════════════════════════════════════════════════════════════


class TestExtensionEndpoints:
    """Integration tests for /extension/* HTTP endpoints."""

    @pytest.fixture(autouse=True)
    def _start_server(self):
        """Start a test server on an ephemeral port."""
        # Reset module state
        event_stream._recent_events = []
        with event_stream._event_queues_lock:
            event_stream._event_queues.clear()

        agg = _make_aggregator()
        from multifleet.ide_bridge import IDEBridge
        bridge = IDEBridge(aggregator=agg)

        self.server = start_server(
            host="127.0.0.1",
            port=0,  # ephemeral
            aggregator=agg,
            ide_bridge=bridge,
        )
        self.port = self.server.server_address[1]
        self.bridge = bridge
        yield
        self.server.shutdown()

    def _get(self, path: str) -> dict:
        url = f"http://127.0.0.1:{self.port}{path}"
        resp = urllib.request.urlopen(url, timeout=3)
        return json.loads(resp.read().decode("utf-8"))

    def test_status_bar_endpoint(self):
        data = self._get("/extension/status-bar")
        assert "health_dot" in data
        assert "node_count" in data
        assert data["health_dot"] == "green"
        assert data["node_count"] == 3

    def test_activity_bar_endpoint(self):
        data = self._get("/extension/activity-bar")
        assert "sections" in data
        assert data["connected"] is True
        peer_section = next(s for s in data["sections"] if s["id"] == "peers")
        assert peer_section["count"] == 2

    def test_notifications_endpoint_empty(self):
        data = self._get("/extension/notifications")
        assert isinstance(data, list)
        assert len(data) == 0

    def test_notifications_endpoint_after_event(self):
        self.bridge.translate_event("fleet.health.degraded", {"nodeId": "mac2"})
        data = self._get("/extension/notifications")
        assert len(data) == 1
        assert data[0]["severity"] == "warning"
        # Second call should be empty (cleared)
        data2 = self._get("/extension/notifications")
        assert len(data2) == 0
