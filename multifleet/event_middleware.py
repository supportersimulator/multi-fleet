"""Advisory middleware for the IDEEventBus — schema validation and rate limiting.

These middleware are advisory-only: they log warnings for malformed or high-rate
events but NEVER suppress them. This closes the multifleet audit gap where events
flowed through the bus entirely unchecked.

Middleware signature: (event_type, payload, metadata) -> (event_type, payload, metadata) | None
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

logger = logging.getLogger("multifleet.event_middleware")

# Required top-level fields that every well-formed event_type + payload should satisfy.
# event_type is passed as the first positional arg (always present by construction),
# so we validate payload/metadata content here.
_REQUIRED_PAYLOAD_PRESENCE = False  # payload can be empty dict — that's valid


def schema_validator(
    event_type: str, payload: dict, metadata: dict
) -> tuple[str, dict, dict]:
    """Validate that an event has required structure and payload fields.

    Checks:
    - event_type is a non-empty string with dotted namespace
    - payload is a dict
    - metadata is a dict
    - If event is in the event_catalog, required payload fields are present

    Advisory only — always returns the event (never suppresses).
    Adds ``_validated: True`` to payload on success, or ``_validated: False``
    with ``_validation_errors`` list on failure.
    """
    errors: list[str] = []

    # event_type checks
    if not event_type or not isinstance(event_type, str):
        errors.append("event_type is empty or not a string")
    elif "." not in event_type:
        errors.append(f"event_type '{event_type}' missing dotted namespace")

    # payload type check
    if not isinstance(payload, dict):
        errors.append(f"payload is {type(payload).__name__}, expected dict")

    # metadata type check
    if not isinstance(metadata, dict):
        errors.append(f"metadata is {type(metadata).__name__}, expected dict")

    # Catalog-based payload validation (advisory — adds warnings, never blocks)
    if not errors and isinstance(payload, dict):
        try:
            from .event_catalog import validate_event as catalog_validate
            catalog_errors = catalog_validate(event_type, payload)
            # Only add field-level errors, not "unregistered" warnings
            for err in catalog_errors:
                if "not found in catalog" not in err:
                    errors.append(err)
        except Exception:
            pass  # catalog import failure is non-fatal

    if errors:
        logger.warning(
            "Event schema validation failed for '%s': %s",
            event_type,
            "; ".join(errors),
        )
        payload["_validated"] = False
        payload["_validation_errors"] = errors
    else:
        payload["_validated"] = True

    return event_type, payload, metadata


def rate_limiter(
    max_per_second: int = 100,
) -> Callable[[str, dict, dict], tuple[str, dict, dict]]:
    """Return a middleware that tracks event rate per namespace.

    If the rate for any namespace exceeds *max_per_second* in the current
    one-second window, a warning is logged and ``_rate_warning: True`` is
    added to the payload.  Events are NEVER suppressed.
    """
    # Per-namespace sliding window: {namespace: [count, window_start]}
    _windows: dict[str, list] = {}

    def _rate_limit_middleware(
        event_type: str, payload: dict, metadata: dict
    ) -> tuple[str, dict, dict]:
        ns = event_type.split(".")[0] if "." in event_type else event_type
        now = time.monotonic()

        if ns not in _windows:
            _windows[ns] = [0, now]

        bucket = _windows[ns]
        elapsed = now - bucket[1]

        if elapsed >= 1.0:
            # Reset window
            bucket[0] = 1
            bucket[1] = now
        else:
            bucket[0] += 1

        if bucket[0] > max_per_second:
            logger.warning(
                "Rate limit advisory: namespace '%s' at %d events/s (limit %d)",
                ns,
                bucket[0],
                max_per_second,
            )
            payload["_rate_warning"] = True

        return event_type, payload, metadata

    _rate_limit_middleware.__name__ = "rate_limiter"
    return _rate_limit_middleware


def register_default_middleware(bus) -> None:
    """Register schema_validator and rate_limiter(100) on *bus*.

    Idempotent — checks whether schema_validator is already registered
    before adding.
    """
    # Guard against double-registration
    for mw in bus._middleware:
        if getattr(mw, "__name__", None) == "schema_validator":
            return

    bus.use(schema_validator)
    bus.use(rate_limiter(100))
    logger.info("Default middleware registered: schema_validator, rate_limiter(100)")
