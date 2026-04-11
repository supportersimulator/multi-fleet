"""
Tests for the generic fleet packet envelope (multifleet.packets).

Covers: envelope validation (valid + invalid), serialization round-trips, dedup,
schema version, opaque payload passthrough, and PacketStore integration.
"""

import copy
import json
import uuid

import pytest

from multifleet.packets import (
    PROTOCOL_VERSION,
    DedupTracker,
    FleetPacket,
    PacketError,
    ingest_packet,
    query_packets,
    validate_packet,
)
from multifleet.packet_store import PacketStore


# ── Fixtures: valid packet dicts ──────────────────────────────────────────────


def _ts():
    return "2026-04-05T12:00:00+00:00"


@pytest.fixture
def simple_packet_data():
    """Minimal valid packet — no payload."""
    return {
        "type": "heartbeat",
        "version": PROTOCOL_VERSION,
        "from_node": "mac1",
        "to_node": "all",
        "timestamp": _ts(),
    }


@pytest.fixture
def full_packet_data():
    """Packet with opaque payload and TTL."""
    return {
        "type": "context_sync",
        "version": PROTOCOL_VERSION,
        "from_node": "mac2",
        "to_node": "mac1",
        "timestamp": _ts(),
        "ttl_minutes": 30,
        "payload": {
            "subject": "Architecture review",
            "body": "Proposing new packet format",
            "metadata": {"priority": "high", "tags": ["infra", "fleet"]},
        },
    }


@pytest.fixture
def broadcast_packet_data():
    """Broadcast packet to all nodes."""
    return {
        "type": "sync_hold",
        "version": PROTOCOL_VERSION,
        "from_node": "mac3",
        "to_node": "all",
        "timestamp": _ts(),
        "payload": {"reason": "Schema migration in progress"},
    }


# ── Envelope validation: valid inputs ────────────────────────────────────────


class TestFleetPacketValid:
    def test_minimal_packet(self, simple_packet_data):
        p = FleetPacket.from_dict(simple_packet_data)
        assert p.type == "heartbeat"
        assert p.from_node == "mac1"
        assert p.to_node == "all"
        assert p.payload == {}
        assert p.ttl_minutes == 0

    def test_full_packet(self, full_packet_data):
        p = FleetPacket.from_dict(full_packet_data)
        assert p.type == "context_sync"
        assert p.from_node == "mac2"
        assert p.to_node == "mac1"
        assert p.ttl_minutes == 30
        assert p.payload["subject"] == "Architecture review"

    def test_payload_is_opaque(self, full_packet_data):
        """Multi-fleet passes payload through without validation."""
        full_packet_data["payload"] = {"anything": [1, 2, {"nested": True}]}
        p = FleetPacket.from_dict(full_packet_data)
        assert p.payload["anything"][2]["nested"] is True

    def test_round_trip(self, full_packet_data):
        p = FleetPacket.from_dict(full_packet_data)
        d = p.to_dict()
        p2 = FleetPacket.from_dict(d)
        assert p.type == p2.type
        assert p.from_node == p2.from_node
        assert p.to_node == p2.to_node
        assert p.payload == p2.payload
        assert p.ttl_minutes == p2.ttl_minutes

    def test_broadcast_packet(self, broadcast_packet_data):
        p = FleetPacket.from_dict(broadcast_packet_data)
        assert p.to_node == "all"
        assert p.payload["reason"] == "Schema migration in progress"

    def test_freeform_type(self):
        """Type is freeform — multi-fleet doesn't restrict it."""
        data = {
            "type": "my_custom_app/verdict/v2",
            "from_node": "mac1",
            "to_node": "mac2",
        }
        p = FleetPacket.from_dict(data)
        assert p.type == "my_custom_app/verdict/v2"

    def test_empty_payload(self, simple_packet_data):
        simple_packet_data["payload"] = {}
        p = FleetPacket.from_dict(simple_packet_data)
        assert p.payload == {}


# ── Envelope validation: invalid inputs ──────────────────────────────────────


class TestFleetPacketInvalid:
    def test_not_a_dict(self):
        with pytest.raises(PacketError, match="packet must be a dict"):
            validate_packet("not a dict")

    def test_missing_type(self):
        with pytest.raises(PacketError, match="missing required field 'type'"):
            validate_packet({"from_node": "mac1", "to_node": "mac2"})

    def test_missing_from_node(self):
        with pytest.raises(PacketError, match="missing required field 'from_node'"):
            validate_packet({"type": "test", "to_node": "mac2"})

    def test_missing_to_node(self):
        with pytest.raises(PacketError, match="missing required field 'to_node'"):
            validate_packet({"type": "test", "from_node": "mac1"})

    def test_type_not_string(self):
        with pytest.raises(PacketError, match="'type' must be str"):
            validate_packet({"type": 123, "from_node": "mac1", "to_node": "mac2"})

    def test_from_node_not_string(self):
        with pytest.raises(PacketError, match="'from_node' must be str"):
            validate_packet({"type": "test", "from_node": 123, "to_node": "mac2"})

    def test_payload_not_dict(self):
        with pytest.raises(PacketError, match="'payload' must be dict"):
            validate_packet({
                "type": "test",
                "from_node": "mac1",
                "to_node": "mac2",
                "payload": "not a dict",
            })

    def test_ttl_negative(self):
        with pytest.raises(PacketError, match="ttl_minutes must be a non-negative int"):
            validate_packet({
                "type": "test",
                "from_node": "mac1",
                "to_node": "mac2",
                "ttl_minutes": -5,
            })

    def test_ttl_not_int(self):
        with pytest.raises(PacketError, match="ttl_minutes must be a non-negative int"):
            validate_packet({
                "type": "test",
                "from_node": "mac1",
                "to_node": "mac2",
                "ttl_minutes": "thirty",
            })


# ── Schema version ───────────────────────────────────────────────────────────


class TestSchemaVersion:
    def test_wrong_version_rejected(self, simple_packet_data):
        simple_packet_data["version"] = "old/v99"
        with pytest.raises(PacketError, match="unsupported version"):
            validate_packet(simple_packet_data)

    def test_default_version_applied(self, simple_packet_data):
        del simple_packet_data["version"]
        p = validate_packet(simple_packet_data)
        assert p.version == PROTOCOL_VERSION

    def test_version_in_serialized_output(self, full_packet_data):
        p = validate_packet(full_packet_data)
        d = p.to_dict()
        assert d["version"] == PROTOCOL_VERSION


# ── Dedup ────────────────────────────────────────────────────────────────────


class TestDedupTracker:
    def test_new_id_is_not_duplicate(self):
        dd = DedupTracker()
        assert not dd.is_duplicate("pkt-001")

    def test_seen_id_is_duplicate(self):
        dd = DedupTracker()
        dd.mark_seen("pkt-001")
        assert dd.is_duplicate("pkt-001")

    def test_check_and_mark(self):
        dd = DedupTracker()
        assert dd.check_and_mark("pkt-001") is False
        assert dd.check_and_mark("pkt-001") is True

    def test_eviction_at_max_size(self):
        dd = DedupTracker(max_size=10)
        for i in range(15):
            dd.mark_seen(f"pkt-{i}")
        assert len(dd) <= 11

    def test_len(self):
        dd = DedupTracker()
        dd.mark_seen("a")
        dd.mark_seen("b")
        assert len(dd) == 2


# ── Unique ID generation ────────────────────────────────────────────────────


class TestPacketId:
    def test_auto_generated_id(self, simple_packet_data):
        simple_packet_data.pop("id", None)
        p1 = FleetPacket.from_dict(simple_packet_data)
        p2 = FleetPacket.from_dict(simple_packet_data)
        assert p1.id != p2.id
        assert len(p1.id) == 36  # UUID format

    def test_preserves_provided_id(self, simple_packet_data):
        simple_packet_data["id"] = "custom-id-123"
        p = FleetPacket.from_dict(simple_packet_data)
        assert p.id == "custom-id-123"


# ── PacketStore integration ──────────────────────────────────────────────────


class TestPacketStoreIntegration:
    @pytest.fixture
    def store(self, tmp_path):
        db = tmp_path / "test.db"
        return PacketStore(db_path=db)

    def test_ingest_packet(self, store, full_packet_data):
        packet, row_id = ingest_packet(store, full_packet_data)
        assert isinstance(packet, FleetPacket)
        assert row_id > 0

    def test_ingest_rejects_invalid(self, store):
        with pytest.raises(PacketError):
            ingest_packet(store, {"type": "test"})  # missing from_node, to_node

    def test_query_packets(self, store, full_packet_data, simple_packet_data):
        ingest_packet(store, full_packet_data)
        ingest_packet(store, simple_packet_data)

        syncs = query_packets(store, packet_type="context_sync")
        assert len(syncs) == 1
        assert syncs[0].type == "context_sync"

        heartbeats = query_packets(store, packet_type="heartbeat")
        assert len(heartbeats) == 1
        assert heartbeats[0].type == "heartbeat"

    def test_query_by_node(self, store, full_packet_data):
        ingest_packet(store, full_packet_data)
        results = query_packets(store, node_id="mac2")
        assert len(results) == 1

    def test_query_skips_malformed(self, store):
        # Insert raw data missing required envelope fields
        store.ingest({"type": "test", "nodeId": "mac1", "bad": True})
        results = query_packets(store, packet_type="test")
        assert len(results) == 0  # gracefully skipped


# ── Serialization: JSON round-trip ───────────────────────────────────────────


class TestJsonRoundTrip:
    """Verify packets survive JSON serialization (the actual transport format)."""

    @pytest.mark.parametrize("fixture_name", [
        "simple_packet_data",
        "full_packet_data",
        "broadcast_packet_data",
    ])
    def test_json_round_trip(self, fixture_name, request):
        data = request.getfixturevalue(fixture_name)
        p1 = validate_packet(data)
        json_str = json.dumps(p1.to_dict())
        restored = json.loads(json_str)
        p2 = validate_packet(restored)
        assert p1.to_dict() == p2.to_dict()


# ── Payload opacity ─────────────────────────────────────────────────────────


class TestPayloadOpacity:
    """Multi-fleet treats payload as opaque — any dict structure is valid."""

    def test_surgical_payload_passes_through(self):
        """Example: 3-surgeons consumer puts verdict data in payload."""
        data = {
            "type": "local_verdict",
            "from_node": "mac2",
            "to_node": "chief",
            "payload": {
                "task_id": "task-001",
                "verdict": "approve",
                "confidence": 0.85,
                "evidence": ["all 42 tests pass"],
                "dissent": ["neurologist: coupling risk"],
            },
        }
        p = validate_packet(data)
        assert p.payload["verdict"] == "approve"
        assert p.payload["confidence"] == 0.85

    def test_deeply_nested_payload(self):
        """Arbitrarily nested payloads are fine — multi-fleet doesn't care."""
        data = {
            "type": "complex_sync",
            "from_node": "mac1",
            "to_node": "mac3",
            "payload": {
                "level1": {
                    "level2": {
                        "level3": [1, 2, {"deep": True}],
                    },
                },
            },
        }
        p = validate_packet(data)
        assert p.payload["level1"]["level2"]["level3"][2]["deep"] is True

    def test_empty_payload_default(self):
        """Payload defaults to empty dict if not provided."""
        data = {
            "type": "ping",
            "from_node": "mac1",
            "to_node": "mac2",
        }
        p = validate_packet(data)
        assert p.payload == {}
