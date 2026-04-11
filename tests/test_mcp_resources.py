"""Tests for MCP resources — fleet://health, fleet://races, fleet://doctor."""

import json
import time
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from multifleet.mcp_resources import (
    fleet_health, fleet_races, fleet_doctor,
    list_resources, read_resource, _doctor_cache,
)

HEALTH_RESP = {"status": "ok", "nodeId": "mac1", "uptime_s": 3600,
               "transport": "nats", "nats_url": "nats://127.0.0.1:4222",
               "activeSessions": 2, "channel_health": {"nats": "ok"}}
PEERS_RESP = {"peers": [{"id": "mac2", "last_seen": 5}]}


def _mock_fetch(responses: dict):
    """Patch _fetch to return canned responses by path."""
    def _side_effect(path, timeout=3.0):
        return responses.get(path)
    return patch("multifleet.mcp_resources._fetch", side_effect=_side_effect)


class TestFleetHealth:
    def test_healthy(self):
        with _mock_fetch({"/health": HEALTH_RESP, "/peers": PEERS_RESP}):
            r = fleet_health()
        assert r["status"] == "ok"
        assert r["node_id"] == "mac1"
        assert r["nats_connected"] is True
        assert r["peers"] == [{"id": "mac2", "last_seen": 5}]
        assert "timestamp" in r

    def test_unreachable(self):
        with _mock_fetch({}):
            r = fleet_health()
        assert r["status"] == "unreachable"


class TestFleetRaces:
    def test_with_races(self):
        races_data = {"races": [{"race_id": "r1", "status": "racing"}]}
        with _mock_fetch({"/races": races_data}):
            r = fleet_races()
        assert r["count"] == 1
        assert r["races"][0]["race_id"] == "r1"

    def test_unreachable(self):
        with _mock_fetch({}):
            r = fleet_races()
        assert r["status"] == "unreachable"
        assert r["count"] == 0


class TestFleetDoctor:
    def setup_method(self):
        _doctor_cache["report"] = None
        _doctor_cache["ts"] = 0.0

    def test_fetches_report(self):
        report = {"healthy": True, "passed": 14, "failed": 0, "checks": []}
        with _mock_fetch({"/doctor": report}):
            r = fleet_doctor()
        assert r["healthy"] is True

    def test_caches_result(self):
        report = {"healthy": True, "checks": []}
        with _mock_fetch({"/doctor": report}):
            fleet_doctor()
        # Second call should use cache (no fetch needed)
        with _mock_fetch({}):
            r = fleet_doctor()
        assert r["healthy"] is True

    def test_unreachable(self):
        with _mock_fetch({}):
            r = fleet_doctor()
        assert r["status"] == "unreachable"


class TestRegistry:
    def test_list_resources(self):
        resources = list_resources()
        assert len(resources) == 3
        uris = {r["uri"] for r in resources}
        assert uris == {"fleet://health", "fleet://races", "fleet://doctor"}
        for r in resources:
            assert "fn" not in r
            assert "mimeType" in r

    def test_read_resource_delegates(self):
        with _mock_fetch({"/health": HEALTH_RESP, "/peers": PEERS_RESP}):
            r = read_resource("fleet://health")
        assert r["status"] == "ok"

    def test_read_unknown_resource(self):
        r = read_resource("fleet://nonexistent")
        assert "error" in r
