"""Smoke tests for modules without dedicated test files.

Quick import + basic-function tests to ensure every module loads
and core APIs work without external dependencies (NATS, SSH, etc.).
"""

import os
import tempfile
import time

import pytest


class TestEventTypesSmoke:
    def test_import(self):
        from multifleet.event_types import validate_namespace, create_event, EVENT_VERSION
        assert EVENT_VERSION >= 1

    def test_validate_namespace_valid(self):
        from multifleet.event_types import validate_namespace
        assert validate_namespace("fleet.peer.online") is True
        assert validate_namespace("health.check") is True

    def test_validate_namespace_invalid(self):
        from multifleet.event_types import validate_namespace
        assert validate_namespace("bogus.event") is False
        assert validate_namespace("") is False
        assert validate_namespace("noDot") is False

    def test_create_event_structure(self):
        from multifleet.event_types import create_event
        evt = create_event("fleet.peer.online", {"node": "mac1"}, source="test")
        assert evt["type"] == "fleet.peer.online"
        assert evt["version"] == 1
        assert evt["source"] == "test"
        assert "id" in evt
        assert "timestamp" in evt
        assert evt["payload"]["node"] == "mac1"

    def test_create_event_metadata(self):
        from multifleet.event_types import create_event
        evt = create_event("fleet.test", {}, correlation_id="abc", session_id="s1")
        assert evt["metadata"]["correlation_id"] == "abc"
        assert evt["metadata"]["session_id"] == "s1"


class TestVisualSmoke:
    def test_import(self):
        from multifleet.visual import phase_banner, fleet_header, fleet_complete

    def test_phase_banner(self):
        from multifleet.visual import phase_banner
        out = phase_banner(2, 5, "Testing")
        assert "Phase 2/5" in out
        assert "Testing" in out

    def test_fleet_header(self):
        from multifleet.visual import fleet_header
        out = fleet_header("Status Check", 2, 3)
        assert "Status Check" in out
        assert "2/3" in out

    def test_fleet_complete(self):
        from multifleet.visual import fleet_complete
        out = fleet_complete("Deploy", 42, 3, cost=0.01)
        assert "Deploy" in out
        assert "42" in out
        assert "$0.010" in out

    def test_node_card_offline(self):
        from multifleet.visual import node_card
        out = node_card("mac2", "offline")
        assert "Offline" in out

    def test_cross_exam_header(self):
        from multifleet.visual import cross_exam_header
        out = cross_exam_header("test topic", "standard", iteration=1, max_iter=3)
        assert "test topic" in out
        assert "1/3" in out


class TestConfigSmoke:
    def test_import(self):
        from multifleet.config import detect_node_id, load_peers

    def test_detect_node_id_from_env(self):
        from multifleet.config import detect_node_id
        old = os.environ.get("MULTIFLEET_NODE_ID")
        try:
            os.environ["MULTIFLEET_NODE_ID"] = "test-node-42"
            assert detect_node_id() == "test-node-42"
        finally:
            if old is None:
                os.environ.pop("MULTIFLEET_NODE_ID", None)
            else:
                os.environ["MULTIFLEET_NODE_ID"] = old

    def test_load_peers_returns_tuple(self):
        from multifleet.config import load_peers
        result = load_peers()
        assert isinstance(result, tuple)
        assert len(result) == 2
        peers, chief = result
        assert isinstance(peers, dict)


class TestChannelsSmoke:
    def test_import_constants(self):
        from multifleet.channels import ALL_CHANNELS, CHANNEL_ORDER, HEAL_SCORE_THRESHOLD
        assert len(ALL_CHANNELS) >= 7
        assert len(CHANNEL_ORDER) >= 7

    def test_channel_state_record(self):
        from multifleet.channels import ChannelState
        cs = ChannelState()
        cs.record_success("mac2", "P1_nats", latency_ms=15)
        summary = cs.get_peer_summary("mac2")
        assert isinstance(summary, dict)

    def test_channel_state_failure(self):
        from multifleet.channels import ChannelState
        cs = ChannelState()
        cs.record_failure("mac3", "P2_http", "timeout")
        broken = cs.get_broken_channels("mac3")
        assert "P2_http" in broken

    def test_dynamic_probe_count(self):
        from multifleet.channels import _dynamic_probe_count
        assert _dynamic_probe_count(3) == 10  # min 10
        assert _dynamic_probe_count(200) > 10


class TestDashboardEventsSmoke:
    def test_import(self):
        from multifleet.dashboard_events import DashboardEventConsumer

    def test_construct_no_bus(self):
        from multifleet.dashboard_events import DashboardEventConsumer
        dec = DashboardEventConsumer(bus=None)
        assert dec._live_state["events_processed"] == 0

    def test_state_has_expected_keys(self):
        from multifleet.dashboard_events import DashboardEventConsumer
        dec = DashboardEventConsumer()
        state = dec._live_state
        assert "peers_online" in state
        assert "recent_messages" in state
        assert "health_alerts" in state


class TestSessionsSmoke:
    def test_import(self):
        from multifleet.sessions import normalize_session_id, display_session_id

    def test_normalize_full_uuid(self):
        from multifleet.sessions import normalize_session_id
        full = "12345678-1234-1234-1234-123456789abc"
        assert normalize_session_id(full) == full

    def test_normalize_empty(self):
        from multifleet.sessions import normalize_session_id
        assert normalize_session_id("") == ""

    def test_display_session_id(self):
        from multifleet.sessions import display_session_id
        full = "12345678-1234-1234-1234-123456789abc"
        short = display_session_id(full)
        assert len(short) < len(full)


class TestPacketStoreSmoke:
    def test_import(self):
        from multifleet.packet_store import PacketStore

    def test_create_and_ingest(self, tmp_path):
        from multifleet.packet_store import PacketStore
        db = tmp_path / "test.db"
        store = PacketStore(db_path=db)
        row_id = store.ingest({
            "type": "local_verdict",
            "nodeId": "mac1",
            "fleetId": "test-fleet",
            "timestamp": "2026-01-01T00:00:00Z",
            "payload": {"verdict": "pass"},
        })
        assert row_id >= 1

    def test_recent_empty(self, tmp_path):
        from multifleet.packet_store import PacketStore
        db = tmp_path / "test.db"
        store = PacketStore(db_path=db)
        recent = store.recent(limit=5)
        assert isinstance(recent, list)
        assert len(recent) == 0


class TestSecuritySmoke:
    def test_import(self):
        from multifleet.security import FleetSecurity

    def test_construct_with_env_key(self):
        old = os.environ.get("FLEET_NERVE_HMAC_KEY")
        try:
            os.environ["FLEET_NERVE_HMAC_KEY"] = "test-key-for-smoke"
            from multifleet.security import FleetSecurity
            sec = FleetSecurity(known_peers={"mac1", "mac2"})
            assert sec._hmac_key is not None
            assert sec._stats["signed"] == 0
        finally:
            if old is None:
                os.environ.pop("FLEET_NERVE_HMAC_KEY", None)
            else:
                os.environ["FLEET_NERVE_HMAC_KEY"] = old


class TestDiscoverySmoke:
    def test_import(self):
        from multifleet.discovery import FleetDiscoveryManager


class TestSelfHealSmoke:
    def test_import(self):
        from multifleet.self_heal import SelfHealManager
