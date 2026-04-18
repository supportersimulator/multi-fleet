"""Inbound fleet-message schema validator (blocker #4).

All messages entering via NATS (`_handle_message`) are passed through
``validate_inbound()`` before dispatch. The validator operates in two modes:

- ``advisory`` (default) — logs a warning, increments a counter, and lets the
  message through. Protects against single-node crashes while rollout is
  observed in production.
- ``strict`` — rejects malformed messages, emits a ``fleet.schema.rejected``
  event so every failure is observable. The counter is still incremented.

The module owns JSON Schemas for the 7 fleet message types we actually route:
``context``, ``task``, ``alert``, ``ack``, ``heartbeat``, ``review_verdict``,
``review_critique``. Unknown types are treated as "envelope-only" and must
still satisfy the shared envelope (``type``/``from``/``payload`` + optional
``id``/``to``/``timestamp``). This is deliberate — multi-fleet remains a
generic transport; unknown ``type`` values should not crash the handler.

Usage::

    ok, err = validate_inbound(msg)
    if not ok:
        # In advisory mode the caller logs+counts and proceeds.
        # In strict mode the caller drops the message and emits an event.

Reference: Zero Silent Failures invariant — every rejection increments
``_schema_rejected`` and surfaces through ``fleet.schema.rejected`` when the
mode is strict.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("multifleet.inbound_validator")


# ── jsonschema (optional — graceful degrade) ─────────────────────────────────
_jsonschema = None
_jsonschema_warned = False
try:
    import jsonschema as _jsonschema
except ImportError:  # pragma: no cover - jsonschema is a declared dep
    _jsonschema = None


# ── Validation modes ─────────────────────────────────────────────────────────

MODE_ADVISORY = "advisory"
MODE_STRICT = "strict"
_VALID_MODES = {MODE_ADVISORY, MODE_STRICT}
DEFAULT_MODE = MODE_ADVISORY


# ── Envelope schema (shared by every type) ───────────────────────────────────
# Field names follow the on-the-wire shape produced by
# ``FleetNerveNATS.send()`` and inspected by ``_handle_message``:
# {"id", "type", "from", "to", "payload": {...}, "timestamp"}.
#
# ``additionalProperties`` is permissive (True) because ``_handle_message``
# regularly sees extra metadata (``_chain_id``, ``ref``, ``status``, etc.)
# and we must NOT reject those. We only require the envelope fields we
# actually route on.

_ENVELOPE_REQUIRED = ["type", "from", "payload"]
_ENVELOPE_BASE: dict[str, Any] = {
    "type": "object",
    "required": _ENVELOPE_REQUIRED,
    "properties": {
        "id": {"type": "string", "minLength": 1},
        "type": {"type": "string", "minLength": 1},
        "from": {"type": "string", "minLength": 1},
        "to": {"type": "string"},
        "timestamp": {"type": "string"},
        "payload": {"type": "object"},
    },
    "additionalProperties": True,
}


def _payload_schema(required: list[str], properties: dict) -> dict:
    """Build a payload sub-schema for an envelope."""
    return {
        "type": "object",
        "required": required,
        "properties": properties,
        "additionalProperties": True,
    }


def _envelope_with_payload(fleet_type: str, payload_schema: dict) -> dict:
    """Return an envelope schema with a tightened payload + const `type`."""
    props = dict(_ENVELOPE_BASE["properties"])
    props["type"] = {"type": "string", "const": fleet_type}
    props["payload"] = payload_schema
    return {
        "type": "object",
        "required": _ENVELOPE_REQUIRED,
        "properties": props,
        "additionalProperties": True,
    }


# ── Per-type payload contracts ───────────────────────────────────────────────
# These are intentionally minimal: we require the fields the handlers actually
# dereference today so malformed peer messages surface before they crash us.

_SCHEMAS: dict[str, dict] = {
    # context: generic context injection. Handlers read payload.subject + body.
    "context": _envelope_with_payload(
        "context",
        _payload_schema(
            required=["subject", "body"],
            properties={
                "subject": {"type": "string", "minLength": 1},
                "body": {"type": "string"},
            },
        ),
    ),
    # task: work unit to dispatch. payload.subject + body required.
    "task": _envelope_with_payload(
        "task",
        _payload_schema(
            required=["subject", "body"],
            properties={
                "subject": {"type": "string", "minLength": 1},
                "body": {"type": "string"},
                "task_id": {"type": "string"},
                "priority": {"type": "string"},
            },
        ),
    ),
    # alert: high-priority notification. Same shape as context but requires a subject.
    "alert": _envelope_with_payload(
        "alert",
        _payload_schema(
            required=["subject", "body"],
            properties={
                "subject": {"type": "string", "minLength": 1},
                "body": {"type": "string"},
                "severity": {"type": "string"},
            },
        ),
    ),
    # ack: delivery confirmation. Top-level `ref` + `status` used by _handle_message.
    "ack": {
        "type": "object",
        "required": ["type", "from", "ref"],
        "properties": {
            "id": {"type": "string"},
            "type": {"type": "string", "const": "ack"},
            "from": {"type": "string", "minLength": 1},
            "to": {"type": "string"},
            "ref": {"type": "string", "minLength": 1},
            "status": {"type": "string"},
            "channel": {"type": "string"},
            "node": {"type": "string"},
            "payload": {"type": "object"},
            "timestamp": {"type": "string"},
        },
        "additionalProperties": True,
    },
    # heartbeat: peer liveness. Requires `from` + `uptime_s` in payload.
    "heartbeat": _envelope_with_payload(
        "heartbeat",
        _payload_schema(
            required=["uptime_s"],
            properties={
                "uptime_s": {"type": "number", "minimum": 0},
                "active_tasks": {"type": "integer", "minimum": 0},
                "peers": {"type": "array"},
                "version": {"type": "string"},
            },
        ),
    ),
    # review_verdict: review flow — verdict from assigned machine.
    "review_verdict": _envelope_with_payload(
        "review_verdict",
        _payload_schema(
            required=["task_id"],
            properties={
                "task_id": {"type": "string", "minLength": 1},
                "summary": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "files_touched": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
        ),
    ),
    # review_critique: critique of a verdict. task_id required.
    "review_critique": _envelope_with_payload(
        "review_critique",
        _payload_schema(
            required=["task_id"],
            properties={
                "task_id": {"type": "string", "minLength": 1},
                "concern": {"type": "string"},
                "severity": {"type": "string"},
            },
        ),
    ),
}


SUPPORTED_TYPES = tuple(sorted(_SCHEMAS.keys()))


def _load_mode(config: Optional[dict]) -> str:
    """Resolve the validation mode from a config dict.

    config shape: ``{"inbound_validation": {"mode": "advisory"|"strict"}}``.
    Any value outside the supported set falls back to advisory.
    """
    if not isinstance(config, dict):
        return DEFAULT_MODE
    iv = config.get("inbound_validation")
    if not isinstance(iv, dict):
        return DEFAULT_MODE
    mode = iv.get("mode", DEFAULT_MODE)
    if mode not in _VALID_MODES:
        logger.warning(
            "inbound_validation.mode=%r is invalid; using %r", mode, DEFAULT_MODE
        )
        return DEFAULT_MODE
    return mode


def _first_error(errors) -> str:
    """Format the first jsonschema error as a short one-liner."""
    err = next(iter(errors), None)
    if err is None:
        return ""
    path = ".".join(str(p) for p in err.absolute_path) or "<root>"
    return f"{path}: {err.message}"


def validate_inbound(msg: Any) -> tuple[bool, str]:
    """Validate an inbound fleet message.

    Returns ``(valid, error_msg)``. ``error_msg`` is empty on success.

    Rules:
    - ``msg`` MUST be a dict.
    - Envelope MUST carry ``type``, ``from``, ``payload``.
    - If ``type`` matches a known schema in ``SUPPORTED_TYPES``, the full
      typed schema applies.
    - Otherwise the envelope-only schema applies (multi-fleet is generic
      transport; unknown types still pass if the envelope is well-formed).

    If ``jsonschema`` is not installed (should never happen — it is a declared
    dep), we log once and return ``(True, "")`` so we never break ingress.
    """
    global _jsonschema_warned

    if not isinstance(msg, dict):
        return False, f"message must be a dict, got {type(msg).__name__}"

    if _jsonschema is None:
        if not _jsonschema_warned:
            logger.warning(
                "jsonschema not installed — inbound validation disabled"
            )
            _jsonschema_warned = True
        return True, ""

    msg_type = msg.get("type")
    schema = _SCHEMAS.get(msg_type) if isinstance(msg_type, str) else None
    if schema is None:
        schema = _ENVELOPE_BASE

    validator = _jsonschema.Draft7Validator(schema)
    errors = list(validator.iter_errors(msg))
    if errors:
        return False, _first_error(errors)
    return True, ""


# ── Dispatch helper used by _handle_message ──────────────────────────────────


def handle_inbound_validation(
    daemon,
    msg: dict,
    *,
    config: Optional[dict] = None,
) -> bool:
    """Validate ``msg`` and apply advisory/strict policy.

    Returns ``True`` when the caller should proceed with dispatch, ``False``
    when the message must be dropped (strict mode, invalid message).

    Side effects:
    - Increments ``daemon._schema_rejected`` on every invalid message
      regardless of mode (Zero Silent Failures).
    - Logs a warning with the first schema error.
    - In ``strict`` mode, emits a ``fleet.schema.rejected`` event so
      failures are observable on the event bus.
    """
    ok, err = validate_inbound(msg)
    if ok:
        return True

    # Ensure the counter exists even on daemons that predate this feature.
    current = getattr(daemon, "_schema_rejected", 0) or 0
    try:
        daemon._schema_rejected = current + 1
    except Exception:
        # Best effort — do not allow counter bookkeeping to break ingress.
        logger.debug("could not update _schema_rejected on daemon", exc_info=True)

    sender = msg.get("from") if isinstance(msg, dict) else "?"
    msg_type = msg.get("type") if isinstance(msg, dict) else "?"
    msg_id = msg.get("id") if isinstance(msg, dict) else None
    logger.warning(
        "inbound schema rejected: type=%s from=%s id=%s err=%s",
        msg_type, sender, msg_id, err,
    )

    mode = _load_mode(config)
    if mode == MODE_STRICT:
        _emit_rejected_event(daemon, msg, err)
        return False

    # advisory: log + count, but let the message through.
    return True


def _emit_rejected_event(daemon, msg: dict, err: str) -> None:
    """Emit ``fleet.schema.rejected`` on the fleet event bus if available.

    Degrades silently (logs at debug) when no event bus is wired — the
    counter + warning log are already surfaced.
    """
    payload = {
        "type": msg.get("type") if isinstance(msg, dict) else None,
        "from": msg.get("from") if isinstance(msg, dict) else None,
        "id": msg.get("id") if isinstance(msg, dict) else None,
        "error": err,
    }
    bus = getattr(daemon, "_eventbus", None)
    if bus is None:
        logger.debug("no eventbus attached; skipping fleet.schema.rejected emit")
        return
    try:
        emit = getattr(bus, "emit", None) or getattr(bus, "publish", None)
        if emit is None:
            logger.debug(
                "eventbus has no emit/publish; skipping fleet.schema.rejected"
            )
            return
        emit("fleet.schema.rejected", payload)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("fleet.schema.rejected emit failed: %s", exc)


__all__ = [
    "DEFAULT_MODE",
    "MODE_ADVISORY",
    "MODE_STRICT",
    "SUPPORTED_TYPES",
    "handle_inbound_validation",
    "validate_inbound",
]
