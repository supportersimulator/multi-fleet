"""Tests for multifleet.event_middleware — advisory schema validation and rate limiting."""

import sys
import logging
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from multifleet.event_bus import IDEEventBus
from multifleet.event_middleware import (
    schema_validator,
    rate_limiter,
    register_default_middleware,
)


@pytest.fixture(autouse=True)
def reset_singleton():
    IDEEventBus.reset()
    yield
    IDEEventBus.reset()


@pytest.fixture
def bus():
    return IDEEventBus(ring_size=100, namespace_ring_size=50)


# ═══════════════════════════════════════════════════════════════════
# schema_validator
# ═══════════════════════════════════════════════════════════════════


class TestSchemaValidator:
    def test_valid_event_passes(self):
        et, p, m = schema_validator("fleet.peer.online", {"peer_id": "mac1"}, {})
        assert p["_validated"] is True
        assert "_validation_errors" not in p

    def test_missing_dot_in_event_type(self, caplog):
        with caplog.at_level(logging.WARNING, logger="multifleet.event_middleware"):
            et, p, m = schema_validator("nodot", {}, {})
        assert p["_validated"] is False
        assert any("missing dotted namespace" in e for e in p["_validation_errors"])
        assert "schema validation failed" in caplog.text.lower()

    def test_empty_event_type(self):
        _, p, _ = schema_validator("", {}, {})
        assert p["_validated"] is False
        assert any("empty" in e for e in p["_validation_errors"])

    def test_always_returns_event(self):
        """Advisory only — never returns None (never suppresses)."""
        result = schema_validator("", {}, {})
        assert result is not None
        result = schema_validator("bad", {}, {})
        assert result is not None
        result = schema_validator("good.event", {}, {})
        assert result is not None

    def test_preserves_original_payload(self):
        _, p, _ = schema_validator("ns.event", {"key": "val"}, {})
        assert p["key"] == "val"
        assert p["_validated"] is True


# ═══════════════════════════════════════════════════════════════════
# rate_limiter
# ═══════════════════════════════════════════════════════════════════


class TestRateLimiter:
    def test_under_threshold_no_warning(self):
        mw = rate_limiter(max_per_second=10)
        for _ in range(10):
            _, p, _ = mw("fleet.tick", {}, {})
        assert "_rate_warning" not in p

    def test_over_threshold_adds_warning(self, caplog):
        mw = rate_limiter(max_per_second=5)
        with caplog.at_level(logging.WARNING, logger="multifleet.event_middleware"):
            for i in range(10):
                _, p, _ = mw("fleet.tick", {}, {})
        # Last payload should have warning
        assert p.get("_rate_warning") is True
        assert "rate limit advisory" in caplog.text.lower()

    def test_never_suppresses(self):
        mw = rate_limiter(max_per_second=1)
        results = []
        for _ in range(50):
            result = mw("fleet.burst", {}, {})
            results.append(result)
        # All results are tuples, none are None
        assert all(r is not None for r in results)

    def test_tracks_per_namespace(self):
        mw = rate_limiter(max_per_second=5)
        # 5 fleet events — at limit, no warning
        for _ in range(5):
            mw("fleet.tick", {}, {})
        # 5 probe events — at limit, no warning
        for _ in range(5):
            _, p, _ = mw("probe.tick", {}, {})
        assert "_rate_warning" not in p

        # 6th fleet event — exceeds
        _, p, _ = mw("fleet.tick", {}, {})
        assert p.get("_rate_warning") is True

    def test_middleware_has_name(self):
        mw = rate_limiter()
        assert mw.__name__ == "rate_limiter"


# ═══════════════════════════════════════════════════════════════════
# register_default_middleware
# ═══════════════════════════════════════════════════════════════════


class TestRegisterDefaultMiddleware:
    def test_registers_both_middleware(self):
        bus = IDEEventBus(ring_size=50, namespace_ring_size=10)
        # Default middleware is auto-registered in __init__, so check count
        names = [getattr(mw, "__name__", "?") for mw in bus._middleware]
        assert "schema_validator" in names
        assert "rate_limiter" in names

    def test_idempotent(self):
        bus = IDEEventBus(ring_size=50, namespace_ring_size=10)
        count_before = len(bus._middleware)
        register_default_middleware(bus)
        register_default_middleware(bus)
        assert len(bus._middleware) == count_before

    def test_events_get_validated_field(self, bus):
        """Events flowing through the bus should get _validated from middleware."""
        received = []
        bus.on("fleet.test", lambda e: received.append(e))
        bus.emit("fleet.test", {"data": 1})
        assert received[0]["payload"]["_validated"] is True


# ═══════════════════════════════════════════════════════════════════
# Integration: middleware does not suppress events
# ═══════════════════════════════════════════════════════════════════


class TestAdvisoryOnly:
    def test_malformed_event_still_delivered(self, bus):
        """Even events with validation errors must be delivered (advisory)."""
        received = []
        bus.on("nodot", lambda e: received.append(e))
        # "nodot" has no dotted namespace — validator warns but passes through
        result = bus.emit("nodot", {"x": 1})
        assert result is not None
        assert len(received) == 1
        assert received[0]["payload"]["_validated"] is False

    def test_high_rate_events_still_delivered(self, bus):
        """Rate-exceeded events must still be delivered."""
        received = []
        bus.on("fleet.burst", lambda e: received.append(e))
        for _ in range(200):
            bus.emit("fleet.burst", {})
        assert len(received) == 200
