"""End-to-end packet validation tests — PacketRegistry with builtin schemas.

Covers: heartbeat validation (valid/invalid enum), unregistered type passthrough,
envelope creation + validation round-trip, dedup filter integration with registry,
schema loading from files, and cross-type validation isolation.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from multifleet.packet_registry import (
    DeduplicationFilter,
    PacketRegistry,
    create_envelope,
)

BUILTIN_DIR = Path(__file__).parent.parent / "packet-types" / "_builtin"


@pytest.fixture
def registry():
    """Fresh PacketRegistry with builtins loaded."""
    reg = PacketRegistry()
    # Builtins auto-load on init; verify they actually loaded
    assert reg.is_registered("heartbeat"), "heartbeat builtin must be loaded"
    return reg


# ── Heartbeat validation (builtin schema) ────────────────────────────────────


class TestHeartbeatValidation:
    """Validate payloads against the real heartbeat.json builtin schema."""

    def test_valid_heartbeat_passes(self, registry):
        errors = registry.validate("heartbeat", {"status": "online"})
        assert errors == []

    def test_valid_heartbeat_all_fields(self, registry):
        payload = {
            "status": "busy",
            "uptime_s": 3600.5,
            "active_branch": "main",
            "sessions": 2,
            "channel_score": "3/7",
        }
        errors = registry.validate("heartbeat", payload)
        assert errors == []

    def test_heartbeat_missing_required_status(self, registry):
        errors = registry.validate("heartbeat", {})
        assert len(errors) > 0
        assert any("status" in e for e in errors)

    def test_heartbeat_wrong_status_enum(self, registry):
        """Status must be one of: online, idle, busy, degraded."""
        errors = registry.validate("heartbeat", {"status": "exploding"})
        assert len(errors) > 0
        assert any("status" in e or "enum" in e.lower() for e in errors)

    def test_heartbeat_wrong_type_for_uptime(self, registry):
        errors = registry.validate("heartbeat", {"status": "online", "uptime_s": "not-a-number"})
        assert len(errors) > 0

    def test_heartbeat_negative_uptime(self, registry):
        errors = registry.validate("heartbeat", {"status": "online", "uptime_s": -1})
        assert len(errors) > 0

    def test_heartbeat_invalid_channel_score_format(self, registry):
        errors = registry.validate("heartbeat", {"status": "online", "channel_score": "bad"})
        assert len(errors) > 0

    def test_heartbeat_valid_channel_score(self, registry):
        errors = registry.validate("heartbeat", {"status": "online", "channel_score": "5/7"})
        assert errors == []


# ── Unregistered type passthrough ────────────────────────────────────────────


class TestUnregisteredPassthrough:
    """Unregistered packet types should produce zero errors (passthrough)."""

    def test_unknown_type_passes(self, registry):
        errors = registry.validate("totally_made_up", {"anything": "goes"})
        assert errors == []

    def test_unknown_type_with_empty_payload(self, registry):
        errors = registry.validate("custom_plugin_v99", {})
        assert errors == []

    def test_unknown_type_not_registered(self, registry):
        assert not registry.is_registered("unknown_thing")
        assert registry.get("unknown_thing") is None


# ── Envelope creation + validation round-trip ────────────────────────────────


class TestEnvelopeValidationRoundTrip:
    """Create an envelope, then validate its payload against the registry."""

    def test_heartbeat_envelope_validates(self, registry):
        env = create_envelope(
            from_node="mac1",
            to="all",
            type="heartbeat",
            payload={"status": "online", "uptime_s": 120},
        )
        # Envelope structure checks
        assert env["type"] == "heartbeat"
        assert env["from_node"] == "mac1"
        assert env["to_node"] == "all"
        assert "id" in env
        assert "timestamp" in env

        # Payload validates against heartbeat schema
        errors = registry.validate(env["type"], env["payload"])
        assert errors == []

    def test_invalid_heartbeat_envelope_caught(self, registry):
        env = create_envelope(
            from_node="mac2",
            to="all",
            type="heartbeat",
            payload={"status": "invalid_status_value"},
        )
        errors = registry.validate(env["type"], env["payload"])
        assert len(errors) > 0

    def test_ack_envelope_validates(self, registry):
        """ACK is a builtin type — create and validate."""
        if not registry.is_registered("ack"):
            pytest.skip("ack builtin not loaded")
        env = create_envelope(
            from_node="mac2",
            to="mac1",
            type="ack",
            payload={"ref_id": "msg-001", "status": "delivered"},
        )
        errors = registry.validate(env["type"], env["payload"])
        assert errors == []

    def test_unregistered_envelope_passthrough(self, registry):
        env = create_envelope(
            from_node="mac1",
            to="mac3",
            type="custom_notification",
            payload={"anything": True, "nested": {"deep": 1}},
        )
        errors = registry.validate(env["type"], env["payload"])
        assert errors == []


# ── Envelope field completeness ──────────────────────────────────────────────


class TestEnvelopeFields:
    """Verify create_envelope produces all required fields."""

    def test_all_required_fields_present(self):
        env = create_envelope(
            from_node="mac1", to="mac2", type="heartbeat", payload={}
        )
        required = {"id", "version", "type", "from_node", "to_node", "timestamp", "payload", "ttl_s"}
        assert required.issubset(set(env.keys()))

    def test_custom_ttl(self):
        env = create_envelope(
            from_node="mac1", to="all", type="heartbeat",
            payload={}, ttl_s=120,
        )
        assert env["ttl_s"] == 120

    def test_custom_msg_id(self):
        env = create_envelope(
            from_node="mac1", to="all", type="heartbeat",
            payload={}, msg_id="custom-123",
        )
        assert env["id"] == "custom-123"

    def test_default_version(self):
        env = create_envelope(
            from_node="mac1", to="all", type="heartbeat", payload={},
        )
        assert env["version"] == 1


# ── Dedup filter + registry integration ──────────────────────────────────────


class TestDedupRegistryIntegration:
    """PacketRegistry contains a DeduplicationFilter — test they work together."""

    def test_registry_has_dedup(self, registry):
        assert isinstance(registry.dedup, DeduplicationFilter)

    def test_dedup_via_registry(self, registry):
        env = create_envelope(
            from_node="mac1", to="all", type="heartbeat",
            payload={"status": "online"}, msg_id="dedup-test-001",
        )
        assert registry.dedup.is_duplicate(env["id"]) is False
        assert registry.dedup.is_duplicate(env["id"]) is True

    def test_validate_still_works_after_dedup(self, registry):
        """Dedup and validation are independent — both should work."""
        msg_id = "msg-validate-dedup"
        payload = {"status": "online"}

        # First time: not a dup, valid
        assert registry.dedup.is_duplicate(msg_id) is False
        assert registry.validate("heartbeat", payload) == []

        # Second time: is a dup, but validation still works
        assert registry.dedup.is_duplicate(msg_id) is True
        assert registry.validate("heartbeat", payload) == []


# ── Cross-type isolation ─────────────────────────────────────────────────────


class TestCrossTypeIsolation:
    """Validating one type should not affect validation of another."""

    def test_heartbeat_error_does_not_leak_to_ack(self, registry):
        # Invalid heartbeat
        errors_hb = registry.validate("heartbeat", {"status": "invalid"})
        assert len(errors_hb) > 0

        # Valid ack (if registered) or passthrough
        if registry.is_registered("ack"):
            ack_schema = registry.get("ack")
            # Even after heartbeat errors, ack validation is independent
            errors_ack = registry.validate("ack", {"ref_id": "x", "status": "received"})
            # Should not contain heartbeat errors
            assert not any("heartbeat" in e.lower() for e in errors_ack)

    def test_multiple_types_loaded(self, registry):
        """Verify multiple builtin types are loaded and isolated."""
        types = registry.list_types()
        type_names = {t["type_name"] for t in types}
        assert "heartbeat" in type_names
        # At least 2 builtin types should exist
        assert len(type_names) >= 2


# ── Schema loading edge cases ────────────────────────────────────────────────


class TestSchemaLoadingEdgeCases:
    def test_load_nonexistent_file_raises(self):
        reg = PacketRegistry()
        with pytest.raises(FileNotFoundError):
            reg.load_file(Path("/nonexistent/schema.json"))

    def test_load_file_missing_type_name_raises(self, tmp_path):
        bad_schema = tmp_path / "bad.json"
        bad_schema.write_text(json.dumps({"owner": "test"}))
        reg = PacketRegistry()
        with pytest.raises(ValueError, match="type_name"):
            reg.load_file(bad_schema)

    def test_load_empty_directory(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        reg = PacketRegistry()
        loaded = reg.load_directory(empty)
        assert loaded == []

    def test_load_nonexistent_directory(self):
        reg = PacketRegistry()
        loaded = reg.load_directory(Path("/nonexistent/dir"))
        assert loaded == []

    def test_malformed_json_skipped_in_directory(self, tmp_path):
        # One valid, one malformed
        good = {"type_name": "good_type", "owner": "test", "payload": {"type": "object"}}
        (tmp_path / "good.json").write_text(json.dumps(good))
        (tmp_path / "bad.json").write_text("{not valid json!!!")
        reg = PacketRegistry()
        loaded = reg.load_directory(tmp_path)
        assert "good_type" in loaded
        assert len(loaded) == 1


# ── Register overwrite behavior ──────────────────────────────────────────────


class TestRegisterOverwrite:
    def test_overwrite_preserves_latest(self):
        reg = PacketRegistry()
        reg.register("mytype", owner="a", version=1)
        reg.register("mytype", owner="b", version=2)
        entry = reg.get("mytype")
        assert entry["owner"] == "b"
        assert entry["version"] == 2

    def test_validate_uses_latest_schema(self):
        reg = PacketRegistry()
        # v1: requires field_a
        reg.register("mytype", owner="test", version=1,
                     payload_schema={"type": "object", "required": ["field_a"],
                                     "properties": {"field_a": {"type": "string"}}})
        errors_v1 = reg.validate("mytype", {})
        assert len(errors_v1) > 0

        # v2: no requirements
        reg.register("mytype", owner="test", version=2,
                     payload_schema={"type": "object", "properties": {}})
        errors_v2 = reg.validate("mytype", {})
        assert errors_v2 == []
