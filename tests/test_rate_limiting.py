"""Tests for PeerRateLimiter -- per-peer per-message-type token bucket."""

import sys
import time
from pathlib import Path
from unittest.mock import patch

# Ensure tools/ is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.fleet_nerve_nats import PeerRateLimiter


def test_allow_within_limit():
    """Messages within the configured rate are allowed."""
    rl = PeerRateLimiter(rates={"task": (3, 60)})
    assert rl.allow("mac2", "task") is True
    assert rl.allow("mac2", "task") is True
    assert rl.allow("mac2", "task") is True
    assert rl._stats["allowed"] == 3
    assert rl._stats["denied"] == 0


def test_deny_over_limit():
    """Messages exceeding the rate are denied."""
    rl = PeerRateLimiter(rates={"task": (2, 60)})
    assert rl.allow("mac2", "task") is True
    assert rl.allow("mac2", "task") is True
    # Third should be denied
    assert rl.allow("mac2", "task") is False
    assert rl._stats["denied"] == 1
    assert rl._stats["denied_by_peer"]["mac2"] == 1


def test_independent_peers():
    """Each peer has independent rate buckets."""
    rl = PeerRateLimiter(rates={"task": (1, 60)})
    assert rl.allow("mac1", "task") is True
    assert rl.allow("mac1", "task") is False  # mac1 exhausted
    assert rl.allow("mac2", "task") is True   # mac2 still has capacity
    assert rl.allow("mac3", "task") is True   # mac3 independent


def test_window_expiry():
    """Tokens replenish after the window expires."""
    rl = PeerRateLimiter(rates={"alert": (2, 60)})
    base = 1000.0

    with patch("tools.fleet_nerve_nats.time") as mock_time:
        mock_time.time.return_value = base
        assert rl.allow("mac2", "alert") is True
        assert rl.allow("mac2", "alert") is True
        assert rl.allow("mac2", "alert") is False  # exhausted

        # Advance past window
        mock_time.time.return_value = base + 61
        assert rl.allow("mac2", "alert") is True  # replenished


def test_reset_peer():
    """Resetting a peer clears all its buckets."""
    rl = PeerRateLimiter(rates={"task": (1, 60)})
    assert rl.allow("mac2", "task") is True
    assert rl.allow("mac2", "task") is False
    rl.reset("mac2")
    assert rl.allow("mac2", "task") is True  # bucket cleared


def test_get_state_for_health():
    """get_state() returns per-peer usage for /health endpoint."""
    rl = PeerRateLimiter(rates={"heartbeat": (6, 60)})
    rl.allow("mac1", "heartbeat")
    rl.allow("mac1", "heartbeat")
    rl.allow("mac2", "heartbeat")

    state = rl.get_state()
    assert "mac1" in state["peers"]
    assert state["peers"]["mac1"]["heartbeat"]["used"] == 2
    assert state["peers"]["mac1"]["heartbeat"]["limit"] == 6
    assert state["peers"]["mac2"]["heartbeat"]["used"] == 1
    assert state["stats"]["allowed"] == 3


def test_fallback_rate_for_unknown_type():
    """Unknown message types use the fallback rate (10/60s)."""
    rl = PeerRateLimiter(rates={})  # no configured rates
    for _ in range(10):
        assert rl.allow("mac1", "exotic_type") is True
    assert rl.allow("mac1", "exotic_type") is False  # 11th denied


def test_different_types_independent():
    """Different message types on the same peer are independent."""
    rl = PeerRateLimiter(rates={"task": (1, 60), "context": (1, 60)})
    assert rl.allow("mac1", "task") is True
    assert rl.allow("mac1", "task") is False     # task exhausted
    assert rl.allow("mac1", "context") is True   # context still ok
