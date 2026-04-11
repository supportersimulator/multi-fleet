"""Tests for dashboard data layer and event consumer.

All daemon HTTP calls are mocked — tests verify aggregation logic, caching,
graceful degradation, and EventBus consumer state management.
"""

import json
import sys
import time
from collections import deque
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

FLEET_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(FLEET_ROOT))

from multifleet.dashboard_data import FleetDashboardData
from multifleet.dashboard_events import DashboardEventConsumer


# ── Sample daemon responses ──

SAMPLE_HEALTH = {
    "nodeId": "mac1",
    "status": "ok",
    "transport": "nats",
    "uptime_s": 3600,
    "activeSessions": 2,
    "sessions": [{"sessionId": "abc123", "kind": "claude", "entrypoint": "claude-code"}],
    "vscode": "active",
    "git": {"branch": "main", "commit": "deadbeef"},
    "peers": {
        "mac2": {"lastSeen": 5, "sessions": 1, "transport": "nats", "vscode": "active"},
        "mac3": {"lastSeen": 200, "sessions": 0, "transport": "nats", "vscode": "inactive"},
    },
    "stats": {"sent": 42, "received": 38, "broadcasts": 5, "errors": 1},
    "channel_health": {
        "mac2": {"score": "5/6", "broken": []},
        "mac3": {"score": "3/6", "broken": ["ssh", "http", "nats"]},
    },
    "eventbus": {"emitted": 100, "delivered": 95},
}

SAMPLE_RACES = {"races": [{"id": "r1", "topic": "dashboard", "status": "active"}], "count": 1}
SAMPLE_EVIDENCE = {"entries": [{"id": "e1", "event_type": "test", "ts": 1000}], "count": 1}
SAMPLE_GAINS = {"passed": True, "checks": 10, "failures": 0}


def _mock_fetch(responses: dict):
    """Create a side_effect for urlopen that returns canned responses by path."""
    def side_effect(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        for path, data in responses.items():
            if path in url:
                resp = MagicMock()
                resp.read.return_value = json.dumps(data).encode()
                resp.status = 200
                resp.__enter__ = MagicMock(return_value=resp)
                resp.__exit__ = MagicMock(return_value=False)
                return resp
        raise ConnectionRefusedError("mocked: no route")
    return side_effect


# ═══════════════════════════════════════════════════
# FleetDashboardData tests
# ═══════════════════════════════════════════════════

class TestDashboardDataSnapshot:
    """Full snapshot aggregation."""

    def test_snapshot_all_sections(self):
        dd = FleetDashboardData(cache_ttl=0)
        with patch("urllib.request.urlopen", side_effect=_mock_fetch({
            "/health": SAMPLE_HEALTH,
            "/races": SAMPLE_RACES,
            "/evidence/latest": SAMPLE_EVIDENCE,
            "/gains-gate": SAMPLE_GAINS,
        })):
            snap = dd.snapshot()

        assert "timestamp" in snap
        assert len(snap["nodes"]) == 3  # self + 2 peers
        assert snap["nodes"][0]["is_self"] is True
        assert snap["races"][0]["topic"] == "dashboard"
        assert snap["evidence"][0]["id"] == "e1"
        assert snap["gains_gate"]["passed"] is True
        assert snap["messages"]["sent"] == 42

    def test_snapshot_daemon_offline(self):
        dd = FleetDashboardData(cache_ttl=0)
        with patch("urllib.request.urlopen", side_effect=ConnectionRefusedError()):
            snap = dd.snapshot()

        assert snap["nodes"] == []
        assert snap["races"] == []
        assert snap["evidence"] == []
        assert snap["gains_gate"]["error"] == "unreachable"
        assert snap["messages"] == {}


class TestNodeClassification:
    """Peer status classification: online/stale/offline."""

    def test_online_peer(self):
        dd = FleetDashboardData(cache_ttl=0)
        with patch("urllib.request.urlopen", side_effect=_mock_fetch({"/health": SAMPLE_HEALTH})):
            nodes = dd._get_nodes()

        mac2 = next(n for n in nodes if n["id"] == "mac2")
        assert mac2["status"] == "online"
        assert mac2["last_seen_s"] == 5

    def test_offline_peer(self):
        dd = FleetDashboardData(cache_ttl=0)
        with patch("urllib.request.urlopen", side_effect=_mock_fetch({"/health": SAMPLE_HEALTH})):
            nodes = dd._get_nodes()

        mac3 = next(n for n in nodes if n["id"] == "mac3")
        assert mac3["status"] == "offline"
        assert mac3["broken_channels"] == ["ssh", "http", "nats"]


class TestCaching:
    """TTL cache prevents redundant daemon calls."""

    def test_cache_hit(self):
        dd = FleetDashboardData(cache_ttl=60)
        call_count = 0
        original_fetch = _mock_fetch({"/health": SAMPLE_HEALTH})

        def counting_fetch(req, timeout=None):
            nonlocal call_count
            call_count += 1
            return original_fetch(req, timeout)

        with patch("urllib.request.urlopen", side_effect=counting_fetch):
            dd._get_nodes()
            dd._get_nodes()  # should hit cache

        assert call_count == 1

    def test_cache_invalidate(self):
        dd = FleetDashboardData(cache_ttl=60)
        with patch("urllib.request.urlopen", side_effect=_mock_fetch({"/health": SAMPLE_HEALTH})):
            dd._get_nodes()
        dd.invalidate_cache()
        assert dd._cache == {}


class TestNodeDetail:
    """node_detail() returns enriched info for a specific node."""

    def test_self_node(self):
        dd = FleetDashboardData(cache_ttl=0)
        with patch("urllib.request.urlopen", side_effect=_mock_fetch({"/health": SAMPLE_HEALTH})):
            detail = dd.node_detail("mac1")

        assert detail["status"] == "self"
        assert detail["uptime_s"] == 3600

    def test_peer_node(self):
        dd = FleetDashboardData(cache_ttl=0)
        with patch("urllib.request.urlopen", side_effect=_mock_fetch({"/health": SAMPLE_HEALTH})):
            detail = dd.node_detail("mac2")

        assert detail["status"] == "online"
        assert detail["channel_score"] == "5/6"

    def test_unknown_node(self):
        dd = FleetDashboardData(cache_ttl=0)
        with patch("urllib.request.urlopen", side_effect=_mock_fetch({"/health": SAMPLE_HEALTH})):
            detail = dd.node_detail("mac99")

        assert detail["status"] == "unknown"


# ═══════════════════════════════════════════════════
# DashboardEventConsumer tests
# ═══════════════════════════════════════════════════

class TestEventConsumer:
    """EventBus consumer maintains live state from fleet events."""

    def _make_event(self, event_type: str, payload: dict) -> dict:
        return {
            "type": event_type,
            "payload": payload,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

    def test_peer_online(self):
        consumer = DashboardEventConsumer()
        consumer._on_peer_event(self._make_event("fleet.peer.online", {"peer_id": "mac2", "sessions": 3}))

        assert "mac2" in consumer.peers_online
        assert consumer.events_processed == 1

    def test_peer_offline_removes(self):
        consumer = DashboardEventConsumer()
        consumer._on_peer_event(self._make_event("fleet.peer.online", {"peer_id": "mac2"}))
        consumer._on_peer_event(self._make_event("fleet.peer.offline", {"peer_id": "mac2"}))

        assert "mac2" not in consumer.peers_online
        assert consumer.events_processed == 2

    def test_message_tracking(self):
        consumer = DashboardEventConsumer()
        consumer._on_message_event(self._make_event(
            "fleet.message.sent", {"target": "mac2", "channel": "nats", "type": "context"}
        ))

        state = consumer.state
        assert len(state["recent_messages"]) == 1
        assert state["recent_messages"][0]["target"] == "mac2"

    def test_health_alert(self):
        consumer = DashboardEventConsumer()
        consumer._on_health_event(self._make_event(
            "fleet.health.degraded", {"peer_id": "mac3", "score": "2/6", "broken": ["nats", "ssh"]}
        ))

        state = consumer.state
        assert len(state["health_alerts"]) == 1
        assert state["health_alerts"][0]["broken"] == ["nats", "ssh"]

    def test_lease_activity(self):
        consumer = DashboardEventConsumer()
        consumer._on_lease_event(self._make_event(
            "fleet.lease.acquired", {"resource_id": "main-branch", "holder": "mac1"}
        ))

        state = consumer.state
        assert len(state["lease_activity"]) == 1
        assert state["lease_activity"][0]["resource_id"] == "main-branch"

    def test_race_lifecycle(self):
        consumer = DashboardEventConsumer()
        consumer._on_race_event(self._make_event(
            "fleet.race.started", {"race_id": "r1", "participants": ["mac1", "mac2"], "topic": "dashboard"}
        ))
        assert "r1" in consumer.state["active_races"]

        consumer._on_race_event(self._make_event(
            "fleet.race.completed", {"race_id": "r1"}
        ))
        assert "r1" not in consumer.state["active_races"]

    def test_state_serializable(self):
        """state property must be JSON-serializable (no deques)."""
        consumer = DashboardEventConsumer()
        consumer._on_message_event(self._make_event(
            "fleet.message.sent", {"target": "mac2", "channel": "nats", "type": "ctx"}
        ))
        # Should not raise
        json.dumps(consumer.state)

    def test_attach_bus(self):
        """attach() wires up subscriptions to an existing bus."""
        bus = MagicMock()
        consumer = DashboardEventConsumer()
        consumer.attach(bus)

        assert bus.on.call_count == 6  # 6 patterns subscribed
        patterns = [call.args[0] for call in bus.on.call_args_list]
        assert "fleet.peer.*" in patterns
        assert "fleet.race.*" in patterns

    def test_ring_buffer_limits(self):
        """recent_messages deque respects MAX_RECENT_MESSAGES."""
        consumer = DashboardEventConsumer()
        for i in range(60):
            consumer._on_message_event(self._make_event(
                "fleet.message.sent", {"target": f"mac{i}", "channel": "nats", "type": "ctx"}
            ))

        state = consumer.state
        assert len(state["recent_messages"]) == 50  # capped at MAX_RECENT_MESSAGES
