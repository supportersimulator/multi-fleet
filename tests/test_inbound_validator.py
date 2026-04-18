"""Tests for multifleet.inbound_validator (blocker #4).

Covers:
- envelope-only validation (valid + missing required fields + wrong types)
- per-type validation for: context, task, alert, ack, heartbeat,
  review_verdict, review_critique
- advisory mode: counter increments, message still dispatches
- strict mode: counter increments, message drops, fleet.schema.rejected emitted
- helper honors missing/invalid config gracefully
"""

from __future__ import annotations

import importlib
from unittest.mock import MagicMock

import pytest

pytest.importorskip("jsonschema")

from multifleet import inbound_validator as iv
from multifleet.inbound_validator import (
    DEFAULT_MODE,
    MODE_ADVISORY,
    MODE_STRICT,
    SUPPORTED_TYPES,
    handle_inbound_validation,
    validate_inbound,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _env(msg_type: str, **extra) -> dict:
    """Build a minimal envelope with the given type + payload extras."""
    base = {
        "id": "abc-123",
        "type": msg_type,
        "from": "mac2",
        "to": "mac1",
        "timestamp": "2026-04-17T12:00:00Z",
        "payload": {},
    }
    base.update(extra)
    return base


class _FakeBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, name: str, payload: dict) -> None:
        self.events.append((name, payload))


class _FakeDaemon:
    def __init__(self, with_bus: bool = True) -> None:
        self._schema_rejected = 0
        self._eventbus = _FakeBus() if with_bus else None


# ── Constants / exports ──────────────────────────────────────────────────────


def test_supported_types_match_spec():
    expected = {
        "context", "task", "alert", "ack",
        "heartbeat", "review_verdict", "review_critique",
    }
    assert set(SUPPORTED_TYPES) == expected


def test_default_mode_is_advisory():
    assert DEFAULT_MODE == MODE_ADVISORY


# ── Envelope-only (unknown types pass with valid envelope) ───────────────────


def test_unknown_type_with_valid_envelope_passes():
    msg = {"type": "weird_custom_type", "from": "mac2", "payload": {"x": 1}}
    ok, err = validate_inbound(msg)
    assert ok, err


def test_missing_type_rejected():
    msg = {"from": "mac2", "payload": {}}
    ok, err = validate_inbound(msg)
    assert not ok
    assert "type" in err


def test_missing_from_rejected():
    msg = {"type": "context", "payload": {"subject": "s", "body": "b"}}
    ok, err = validate_inbound(msg)
    assert not ok
    assert "from" in err


def test_missing_payload_rejected_for_typed_message():
    msg = {"type": "context", "from": "mac2"}
    ok, err = validate_inbound(msg)
    assert not ok
    assert "payload" in err


def test_non_dict_rejected():
    ok, err = validate_inbound("not a dict")
    assert not ok
    assert "dict" in err


def test_payload_wrong_type_rejected():
    msg = {"type": "context", "from": "mac2", "payload": "string not dict"}
    ok, err = validate_inbound(msg)
    assert not ok


# ── context ──────────────────────────────────────────────────────────────────


def test_context_valid():
    ok, err = validate_inbound(_env(
        "context",
        payload={"subject": "Hello", "body": "world"},
    ))
    assert ok, err


def test_context_missing_subject():
    ok, err = validate_inbound(_env(
        "context",
        payload={"body": "world"},
    ))
    assert not ok
    assert "subject" in err


def test_context_empty_subject_rejected():
    ok, err = validate_inbound(_env(
        "context",
        payload={"subject": "", "body": "world"},
    ))
    assert not ok


# ── task ─────────────────────────────────────────────────────────────────────


def test_task_valid_minimal():
    ok, err = validate_inbound(_env(
        "task",
        payload={"subject": "Do thing", "body": "details"},
    ))
    assert ok, err


def test_task_valid_with_optional_fields():
    ok, err = validate_inbound(_env(
        "task",
        payload={
            "subject": "Do", "body": "...",
            "task_id": "t-1", "priority": "high",
        },
    ))
    assert ok, err


def test_task_missing_body_rejected():
    ok, err = validate_inbound(_env(
        "task",
        payload={"subject": "Do thing"},
    ))
    assert not ok
    assert "body" in err


# ── alert ────────────────────────────────────────────────────────────────────


def test_alert_valid():
    ok, err = validate_inbound(_env(
        "alert",
        payload={"subject": "Down", "body": "node dead", "severity": "high"},
    ))
    assert ok, err


def test_alert_missing_subject():
    ok, err = validate_inbound(_env(
        "alert",
        payload={"body": "something"},
    ))
    assert not ok


# ── ack ──────────────────────────────────────────────────────────────────────


def test_ack_valid():
    msg = {
        "type": "ack", "from": "mac2",
        "ref": "msg-id-123", "status": "delivered", "channel": "P1_nats",
    }
    ok, err = validate_inbound(msg)
    assert ok, err


def test_ack_missing_ref_rejected():
    msg = {"type": "ack", "from": "mac2", "status": "delivered"}
    ok, err = validate_inbound(msg)
    assert not ok
    assert "ref" in err


def test_ack_does_not_require_payload():
    # ack is a control message; payload is not required.
    msg = {"type": "ack", "from": "mac2", "ref": "r1"}
    ok, err = validate_inbound(msg)
    assert ok, err


# ── heartbeat ────────────────────────────────────────────────────────────────


def test_heartbeat_valid():
    ok, err = validate_inbound(_env(
        "heartbeat",
        payload={"uptime_s": 100.0, "active_tasks": 0},
    ))
    assert ok, err


def test_heartbeat_missing_uptime_rejected():
    ok, err = validate_inbound(_env(
        "heartbeat",
        payload={"active_tasks": 0},
    ))
    assert not ok
    assert "uptime_s" in err


def test_heartbeat_negative_uptime_rejected():
    ok, err = validate_inbound(_env(
        "heartbeat",
        payload={"uptime_s": -1},
    ))
    assert not ok


# ── review_verdict ───────────────────────────────────────────────────────────


def test_review_verdict_valid():
    ok, err = validate_inbound(_env(
        "review_verdict",
        payload={
            "task_id": "t-1",
            "summary": "looks good",
            "confidence": 0.9,
            "files_touched": ["a.py", "b.py"],
        },
    ))
    assert ok, err


def test_review_verdict_missing_task_id():
    ok, err = validate_inbound(_env(
        "review_verdict",
        payload={"summary": "x", "confidence": 0.8},
    ))
    assert not ok
    assert "task_id" in err


def test_review_verdict_confidence_out_of_range():
    ok, err = validate_inbound(_env(
        "review_verdict",
        payload={"task_id": "t-1", "confidence": 1.5},
    ))
    assert not ok


# ── review_critique ──────────────────────────────────────────────────────────


def test_review_critique_valid():
    ok, err = validate_inbound(_env(
        "review_critique",
        payload={"task_id": "t-1", "concern": "thread safety", "severity": "medium"},
    ))
    assert ok, err


def test_review_critique_missing_task_id():
    ok, err = validate_inbound(_env(
        "review_critique",
        payload={"concern": "something"},
    ))
    assert not ok


# ── Mode resolution ──────────────────────────────────────────────────────────


def test_mode_defaults_to_advisory_when_no_config():
    assert iv._load_mode(None) == MODE_ADVISORY
    assert iv._load_mode({}) == MODE_ADVISORY


def test_mode_strict_from_config():
    cfg = {"inbound_validation": {"mode": "strict"}}
    assert iv._load_mode(cfg) == MODE_STRICT


def test_mode_invalid_falls_back_to_advisory():
    cfg = {"inbound_validation": {"mode": "bananas"}}
    assert iv._load_mode(cfg) == MODE_ADVISORY


# ── handle_inbound_validation behavior ───────────────────────────────────────


def test_advisory_mode_counts_but_passes():
    daemon = _FakeDaemon()
    # missing required field
    bad = {"type": "context", "from": "mac2", "payload": {"body": "x"}}
    proceed = handle_inbound_validation(daemon, bad, config={})
    assert proceed is True
    assert daemon._schema_rejected == 1
    # no event emitted in advisory mode
    assert daemon._eventbus.events == []


def test_strict_mode_counts_and_drops_and_emits():
    daemon = _FakeDaemon()
    bad = {"type": "context", "from": "mac2", "payload": {"body": "x"}}
    proceed = handle_inbound_validation(
        daemon, bad, config={"inbound_validation": {"mode": "strict"}}
    )
    assert proceed is False
    assert daemon._schema_rejected == 1
    assert len(daemon._eventbus.events) == 1
    name, payload = daemon._eventbus.events[0]
    assert name == "fleet.schema.rejected"
    assert payload["type"] == "context"
    assert payload["from"] == "mac2"
    assert "error" in payload


def test_valid_message_does_not_increment_counter():
    daemon = _FakeDaemon()
    good = _env("context", payload={"subject": "s", "body": "b"})
    proceed = handle_inbound_validation(daemon, good, config={})
    assert proceed is True
    assert daemon._schema_rejected == 0
    assert daemon._eventbus.events == []


def test_strict_mode_without_eventbus_still_drops():
    daemon = _FakeDaemon(with_bus=False)
    bad = {"type": "heartbeat", "from": "mac2", "payload": {}}
    proceed = handle_inbound_validation(
        daemon, bad, config={"inbound_validation": {"mode": "strict"}}
    )
    assert proceed is False
    assert daemon._schema_rejected == 1


def test_counter_survives_pre_existing_attr_missing():
    """Daemons from before this feature may not have _schema_rejected set.

    The helper must create/update it regardless.
    """
    class _Bare:
        pass

    daemon = _Bare()
    bad = {"type": "context", "from": "mac2", "payload": {}}
    proceed = handle_inbound_validation(daemon, bad, config=None)
    # advisory mode by default => proceed
    assert proceed is True
    assert getattr(daemon, "_schema_rejected", 0) == 1


# ── jsonschema-absent fallback ───────────────────────────────────────────────


def test_validator_degrades_gracefully_without_jsonschema(monkeypatch):
    """If jsonschema is unavailable, validate_inbound returns (True, '')."""
    monkeypatch.setattr(iv, "_jsonschema", None)
    monkeypatch.setattr(iv, "_jsonschema_warned", False)
    bad = {"type": "context", "from": "mac2", "payload": {}}  # missing required
    ok, err = validate_inbound(bad)
    assert ok is True
    assert err == ""


# Reload the module at the end to restore real jsonschema reference for any
# later collection. (Monkeypatch resets attributes automatically; this is a
# belt-and-suspenders guard.)
def teardown_module(_mod):  # pragma: no cover - safety net
    importlib.reload(iv)
