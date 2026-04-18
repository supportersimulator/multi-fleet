"""Tests for inbound message schema validation (OSS blocker #4).

Validates the validate_inbound_message() function added to fleet_nerve_nats.py.
"""

import sys
from pathlib import Path

import pytest

# Add tools/ to path so we can import the validation function
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "tools"))

from fleet_nerve_nats import validate_inbound_message, VALID_MESSAGE_TYPES


def _valid_msg(**overrides):
    """Build a minimal valid message, with optional overrides."""
    base = {
        "type": "context",
        "to": "mac2",
        "from": "mac1",
        "payload": {"subject": "test", "body": "hello"},
    }
    base.update(overrides)
    return base


class TestValidMessages:
    """Messages that should pass validation."""

    def test_minimal_valid(self):
        assert validate_inbound_message(_valid_msg()) is None

    def test_all_valid_types(self):
        for t in VALID_MESSAGE_TYPES:
            assert validate_inbound_message(_valid_msg(type=t)) is None, f"type '{t}' should be valid"

    def test_with_optional_fields(self):
        msg = _valid_msg(
            id="550e8400-e29b-41d4-a716-446655440000",
            timestamp="2026-04-17T12:00:00Z",
        )
        assert validate_inbound_message(msg) is None

    def test_no_payload(self):
        msg = _valid_msg()
        del msg["payload"]
        assert validate_inbound_message(msg) is None


class TestRequiredFields:
    """Messages missing or having invalid required fields."""

    def test_missing_type(self):
        msg = _valid_msg()
        del msg["type"]
        err = validate_inbound_message(msg)
        assert err is not None
        assert "type" in err

    def test_unknown_type(self):
        err = validate_inbound_message(_valid_msg(type="bogus"))
        assert err is not None
        assert "unknown message type" in err
        assert "bogus" in err

    def test_missing_to(self):
        msg = _valid_msg()
        del msg["to"]
        err = validate_inbound_message(msg)
        assert err is not None
        assert "'to'" in err

    def test_missing_from(self):
        msg = _valid_msg()
        del msg["from"]
        err = validate_inbound_message(msg)
        assert err is not None
        assert "'from'" in err

    def test_non_string_type(self):
        err = validate_inbound_message(_valid_msg(type=42))
        assert err is not None
        assert "string" in err

    def test_empty_to(self):
        err = validate_inbound_message(_valid_msg(to=""))
        assert err is not None
        assert "non-empty" in err

    def test_not_a_dict(self):
        err = validate_inbound_message("not a dict")
        assert err is not None
        assert "JSON object" in err


class TestPayloadValidation:
    """Payload field constraints."""

    def test_payload_not_dict(self):
        err = validate_inbound_message(_valid_msg(payload="string"))
        assert err is not None
        assert "payload" in err

    def test_subject_too_long(self):
        err = validate_inbound_message(_valid_msg(payload={"subject": "x" * 201}))
        assert err is not None
        assert "subject" in err
        assert "200" in err

    def test_body_too_long(self):
        err = validate_inbound_message(_valid_msg(payload={"body": "x" * 50001}))
        assert err is not None
        assert "body" in err
        assert "50000" in err

    def test_subject_at_limit(self):
        """Exactly 200 chars should pass."""
        assert validate_inbound_message(_valid_msg(payload={"subject": "x" * 200})) is None

    def test_body_at_limit(self):
        """Exactly 50000 chars should pass."""
        assert validate_inbound_message(_valid_msg(payload={"body": "x" * 50000})) is None
