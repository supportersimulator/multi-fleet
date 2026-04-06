"""Tests for multifleet.packet_registry — PacketRegistry, DeduplicationFilter, create_envelope."""

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

# Ensure multifleet package is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from multifleet.packet_registry import (
    DeduplicationFilter,
    PacketRegistry,
    create_envelope,
)

BUILTIN_DIR = Path(__file__).parent.parent / "packet-types" / "_builtin"


# ── DeduplicationFilter ───────────────────────────────────────────────────


class TestDeduplicationFilter:
    def test_first_message_not_duplicate(self):
        f = DeduplicationFilter()
        assert f.is_duplicate("msg-001") is False

    def test_same_message_is_duplicate(self):
        f = DeduplicationFilter()
        f.is_duplicate("msg-001")
        assert f.is_duplicate("msg-001") is True

    def test_different_messages_not_duplicate(self):
        f = DeduplicationFilter()
        assert f.is_duplicate("msg-001") is False
        assert f.is_duplicate("msg-002") is False

    def test_eviction_at_max_size(self):
        f = DeduplicationFilter(max_size=3)
        f.is_duplicate("a")
        f.is_duplicate("b")
        f.is_duplicate("c")
        f.is_duplicate("d")  # should evict "a"
        assert f.is_duplicate("a") is False  # evicted, no longer seen
        assert f.is_duplicate("d") is True   # still there

    def test_clear_resets(self):
        f = DeduplicationFilter()
        f.is_duplicate("msg-001")
        assert f.is_duplicate("msg-001") is True
        f.clear()
        assert f.is_duplicate("msg-001") is False

    def test_size_tracking(self):
        f = DeduplicationFilter()
        assert f.size() == 0
        f.is_duplicate("a")
        f.is_duplicate("b")
        f.is_duplicate("c")
        assert f.size() == 3
        # Duplicate doesn't increase size
        f.is_duplicate("a")
        assert f.size() == 3


# ── PacketRegistry ─────────────────────────────────────────────────────────


def _register_type(reg, type_name="test_type", owner="test", version=1,
                   required=None, properties=None):
    """Helper to register a type using the actual API."""
    payload_schema = {
        "type": "object",
        "properties": properties or {"field_a": {"type": "string"}},
        "required": required or ["field_a"],
    }
    reg.register(
        type_name=type_name,
        owner=owner,
        version=version,
        payload_schema=payload_schema,
        description=f"Schema for {type_name}",
    )


class TestPacketRegistry:
    def test_register_and_get(self):
        reg = PacketRegistry()
        _register_type(reg, "heartbeat")
        result = reg.get("heartbeat")
        assert result is not None
        assert result["type_name"] == "heartbeat"

    def test_get_unknown_returns_none(self):
        reg = PacketRegistry()
        assert reg.get("nonexistent") is None

    def test_is_registered(self):
        reg = PacketRegistry()
        _register_type(reg, "heartbeat")
        assert reg.is_registered("heartbeat") is True
        assert reg.is_registered("unknown") is False

    def test_list_types_all(self):
        reg = PacketRegistry()
        _register_type(reg, "heartbeat", owner="core")
        _register_type(reg, "ack", owner="core")
        _register_type(reg, "repair", owner="infra")
        types = reg.list_types()
        type_names = {t["type_name"] for t in types}
        assert {"heartbeat", "ack", "repair"}.issubset(type_names)

    def test_list_types_by_owner(self):
        reg = PacketRegistry()
        _register_type(reg, "heartbeat", owner="core")
        _register_type(reg, "ack", owner="core")
        _register_type(reg, "repair", owner="infra")
        core_types = reg.list_types(owner="core")
        core_names = {t["type_name"] for t in core_types}
        assert core_names == {"heartbeat", "ack"}

    def test_validate_valid_payload(self):
        reg = PacketRegistry()
        _register_type(reg, "heartbeat", required=["status"],
                       properties={"status": {"type": "string"}})
        errors = reg.validate("heartbeat", {"status": "online"})
        assert errors == []

    def test_validate_invalid_payload(self):
        reg = PacketRegistry()
        _register_type(reg, "heartbeat", required=["status"],
                       properties={"status": {"type": "string"}})
        errors = reg.validate("heartbeat", {})
        assert len(errors) > 0

    def test_validate_unknown_type_passthrough(self):
        reg = PacketRegistry()
        errors = reg.validate("unknown_type", {"anything": "goes"})
        assert errors == []

    def test_load_file(self, tmp_path):
        schema = {
            "type_name": "custom_type",
            "version": 1,
            "owner": "test",
            "description": "Test type",
            "payload": {
                "type": "object",
                "properties": {"field_a": {"type": "string"}},
                "required": ["field_a"],
            },
        }
        schema_file = tmp_path / "custom_type.json"
        schema_file.write_text(json.dumps(schema))
        reg = PacketRegistry()
        reg.load_file(schema_file)
        assert reg.is_registered("custom_type")
        assert reg.get("custom_type")["owner"] == "test"

    def test_load_directory(self, tmp_path):
        for name in ("type_a", "type_b"):
            schema = {
                "type_name": name,
                "version": 1,
                "owner": "batch",
                "payload": {"type": "object", "properties": {}},
            }
            (tmp_path / f"{name}.json").write_text(json.dumps(schema))
        reg = PacketRegistry()
        reg.load_directory(tmp_path)
        assert reg.is_registered("type_a")
        assert reg.is_registered("type_b")

    def test_load_builtins(self):
        """load_builtins() discovers and loads _builtin/*.json schemas."""
        assert BUILTIN_DIR.is_dir(), f"Missing builtin dir: {BUILTIN_DIR}"
        builtin_files = list(BUILTIN_DIR.glob("*.json"))
        assert len(builtin_files) >= 1, "No builtin schema files found"

        reg = PacketRegistry()
        reg.load_builtins()

        for f in builtin_files:
            schema = json.loads(f.read_text())
            type_name = schema["type_name"]
            assert reg.is_registered(type_name), f"Builtin {type_name} not loaded"

    def test_register_overwrites_version(self):
        reg = PacketRegistry()
        _register_type(reg, "heartbeat", version=1)
        assert reg.get("heartbeat")["version"] == 1
        _register_type(reg, "heartbeat", version=2)
        assert reg.get("heartbeat")["version"] == 2


# ── create_envelope ────────────────────────────────────────────────────────


class TestCreateEnvelope:
    def test_creates_valid_envelope(self):
        env = create_envelope(
            type="heartbeat",
            from_node="mac1",
            to="all",
            payload={"status": "online"},
        )
        assert env["type"] == "heartbeat"
        assert env["from_node"] == "mac1"
        assert env["to_node"] == "all"
        assert env["payload"] == {"status": "online"}
        assert "id" in env
        assert "timestamp" in env

    def test_generates_uuid(self):
        env = create_envelope(
            type="ack",
            from_node="mac2",
            to="mac1",
            payload={},
        )
        assert isinstance(env["id"], str)
        assert len(env["id"]) > 0

    def test_uses_provided_id(self):
        env = create_envelope(
            type="ack",
            from_node="mac2",
            to="mac1",
            payload={},
            msg_id="custom-id-999",
        )
        assert env["id"] == "custom-id-999"

    def test_timestamp_is_iso(self):
        env = create_envelope(
            type="heartbeat",
            from_node="mac1",
            to="all",
            payload={},
        )
        ts = env["timestamp"]
        parsed = datetime.fromisoformat(ts)
        assert parsed.year >= 2026
