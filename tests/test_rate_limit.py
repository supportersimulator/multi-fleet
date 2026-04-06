"""Tests for MCP tool rate limiter."""

from unittest.mock import patch

from multifleet.rate_limit import RateLimiter


def test_unlimited_tool_always_allowed():
    """Tools without a configured limit are always allowed."""
    rl = RateLimiter(limits={"fleet_send": 5})
    for _ in range(100):
        allowed, reason = rl.check("fleet_status")
        assert allowed is True
        assert reason == ""


def test_limited_tool_allowed_under_limit():
    """Tool calls under the limit are allowed."""
    rl = RateLimiter(limits={"fleet_send": 5})
    for _ in range(5):
        allowed, reason = rl.check("fleet_send")
        assert allowed is True
        assert reason == ""


def test_limited_tool_denied_over_limit():
    """Tool calls over the limit are denied."""
    rl = RateLimiter(limits={"fleet_send": 3})
    for _ in range(3):
        allowed, _ = rl.check("fleet_send")
        assert allowed is True

    allowed, reason = rl.check("fleet_send")
    assert allowed is False
    assert "Rate limit" in reason
    assert "fleet_send" in reason
    assert "max 3/min" in reason


def test_window_expires_after_60s():
    """Old entries expire and free up capacity."""
    rl = RateLimiter(limits={"fleet_send": 2})

    # Use up the limit
    with patch("multifleet.rate_limit.time") as mock_time:
        mock_time.monotonic.return_value = 1000.0
        assert rl.check("fleet_send")[0] is True
        assert rl.check("fleet_send")[0] is True
        assert rl.check("fleet_send")[0] is False

        # Advance past 60s window
        mock_time.monotonic.return_value = 1061.0
        allowed, reason = rl.check("fleet_send")
        assert allowed is True
        assert reason == ""


def test_stats_tracking():
    """Stats count allowed and denied calls."""
    rl = RateLimiter(limits={"fleet_send": 2})
    rl.check("fleet_send")  # allowed
    rl.check("fleet_send")  # allowed
    rl.check("fleet_send")  # denied
    rl.check("fleet_status")  # allowed (unlimited)

    assert rl.stats == {"allowed": 3, "denied": 1}


def test_different_tools_independent():
    """Rate limits are tracked independently per tool."""
    rl = RateLimiter(limits={"fleet_send": 1, "fleet_broadcast": 1})

    assert rl.check("fleet_send")[0] is True
    assert rl.check("fleet_send")[0] is False  # send exhausted

    assert rl.check("fleet_broadcast")[0] is True  # broadcast still OK
    assert rl.check("fleet_broadcast")[0] is False  # now exhausted
