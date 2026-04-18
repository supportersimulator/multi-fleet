"""Tests for multifleet.peer_rate_limit — per-peer inbound/outbound limits."""

import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from multifleet.peer_rate_limit import (
    DEFAULT_MAX_PER_MINUTE,
    DEFAULT_WINDOW_S,
    PeerRateLimiter,
)


# ═══════════════════════════════════════════════════════════════════
# Construction + defaults
# ═══════════════════════════════════════════════════════════════════


class TestConstruction:
    def test_defaults(self):
        limiter = PeerRateLimiter()
        assert limiter.max_inbound == DEFAULT_MAX_PER_MINUTE
        assert limiter.max_outbound == DEFAULT_MAX_PER_MINUTE
        assert limiter.window_s == DEFAULT_WINDOW_S

    def test_custom_limits(self):
        limiter = PeerRateLimiter(
            max_per_minute=100,
            max_per_minute_inbound=50,
            max_per_minute_outbound=200,
            window_s=10.0,
        )
        assert limiter.max_inbound == 50
        assert limiter.max_outbound == 200
        assert limiter.window_s == 10.0

    def test_rejects_bad_args(self):
        with pytest.raises(ValueError):
            PeerRateLimiter(max_per_minute=0)
        with pytest.raises(ValueError):
            PeerRateLimiter(max_per_minute=-5)
        with pytest.raises(ValueError):
            PeerRateLimiter(window_s=0)
        with pytest.raises(ValueError):
            PeerRateLimiter(window_s=-1.0)


# ═══════════════════════════════════════════════════════════════════
# Basic accept/reject
# ═══════════════════════════════════════════════════════════════════


class TestBasicFlow:
    def test_normal_traffic_passes(self):
        limiter = PeerRateLimiter(max_per_minute=60, window_s=60)
        # 10 requests, well under the 60/min limit
        for _ in range(10):
            assert limiter.record_inbound("mac1") is True
            assert limiter.record_outbound("mac1") is True

    def test_burst_exactly_at_limit_accepted(self):
        limiter = PeerRateLimiter(max_per_minute=5, window_s=60)
        for i in range(5):
            assert limiter.record_inbound("mac1") is True, f"req {i} must pass"
        # 6th exceeds
        assert limiter.record_inbound("mac1") is False

    def test_sustained_flood_rejected(self):
        limiter = PeerRateLimiter(max_per_minute=10, window_s=60)
        accepted = sum(1 for _ in range(100) if limiter.record_inbound("flooder"))
        assert accepted == 10
        stats = limiter.get_peer_stats()
        assert stats["flooder"]["inbound"]["accepted"] == 10
        assert stats["flooder"]["inbound"]["limited"] == 90

    def test_inbound_and_outbound_have_separate_budgets(self):
        limiter = PeerRateLimiter(max_per_minute=3, window_s=60)
        # Exhaust inbound
        for _ in range(3):
            assert limiter.record_inbound("mac1") is True
        assert limiter.record_inbound("mac1") is False
        # Outbound still fresh
        for _ in range(3):
            assert limiter.record_outbound("mac1") is True
        assert limiter.record_outbound("mac1") is False


# ═══════════════════════════════════════════════════════════════════
# Multi-peer isolation
# ═══════════════════════════════════════════════════════════════════


class TestPeerIsolation:
    def test_one_flooder_does_not_affect_others(self):
        limiter = PeerRateLimiter(max_per_minute=5, window_s=60)
        # mac1 floods
        for _ in range(20):
            limiter.record_inbound("mac1")
        # mac2 and mac3 still have their full budget
        for _ in range(5):
            assert limiter.record_inbound("mac2") is True
            assert limiter.record_inbound("mac3") is True
        # And mac1 is still blocked
        assert limiter.record_inbound("mac1") is False

    def test_direction_is_per_peer(self):
        limiter = PeerRateLimiter(max_per_minute=2, window_s=60)
        # Exhaust inbound from mac1
        limiter.record_inbound("mac1")
        limiter.record_inbound("mac1")
        assert limiter.record_inbound("mac1") is False
        # Outbound to mac1 unaffected; inbound from mac2 unaffected
        assert limiter.record_outbound("mac1") is True
        assert limiter.record_inbound("mac2") is True


# ═══════════════════════════════════════════════════════════════════
# Window rollover
# ═══════════════════════════════════════════════════════════════════


class TestWindowRollover:
    def test_counts_reset_after_window(self, monkeypatch):
        limiter = PeerRateLimiter(max_per_minute=3, window_s=60)
        # Use a controllable monotonic clock
        current = [1000.0]

        def fake_monotonic():
            return current[0]

        monkeypatch.setattr(time, "monotonic", fake_monotonic)

        for _ in range(3):
            assert limiter.record_inbound("mac1") is True
        assert limiter.record_inbound("mac1") is False

        # Advance past the window
        current[0] += 61.0
        assert limiter.record_inbound("mac1") is True  # reset
        assert limiter.record_inbound("mac1") is True
        assert limiter.record_inbound("mac1") is True
        assert limiter.record_inbound("mac1") is False

    def test_partial_window_does_not_reset(self, monkeypatch):
        limiter = PeerRateLimiter(max_per_minute=3, window_s=60)
        current = [2000.0]
        monkeypatch.setattr(time, "monotonic", lambda: current[0])

        for _ in range(3):
            limiter.record_inbound("mac1")
        current[0] += 30.0  # halfway through window
        assert limiter.record_inbound("mac1") is False


# ═══════════════════════════════════════════════════════════════════
# Runtime reconfiguration
# ═══════════════════════════════════════════════════════════════════


class TestRuntimeReconfig:
    def test_set_limits_inbound(self):
        limiter = PeerRateLimiter(max_per_minute=2, window_s=60)
        limiter.record_inbound("mac1")
        limiter.record_inbound("mac1")
        assert limiter.record_inbound("mac1") is False
        # Raise the bar
        limiter.set_limits(inbound=10)
        # Existing count is 2 under the new limit of 10, so 8 more should pass
        accepted = sum(1 for _ in range(20) if limiter.record_inbound("mac1"))
        assert accepted == 8

    def test_set_limits_outbound_independent(self):
        limiter = PeerRateLimiter(max_per_minute=5, window_s=60)
        limiter.set_limits(outbound=1)
        assert limiter.record_outbound("mac1") is True
        assert limiter.record_outbound("mac1") is False
        # Inbound untouched
        assert limiter.record_inbound("mac1") is True

    def test_set_limits_rejects_bad(self):
        limiter = PeerRateLimiter(max_per_minute=5)
        with pytest.raises(ValueError):
            limiter.set_limits(inbound=0)
        with pytest.raises(ValueError):
            limiter.set_limits(outbound=-1)


# ═══════════════════════════════════════════════════════════════════
# Stats + totals
# ═══════════════════════════════════════════════════════════════════


class TestStats:
    def test_get_peer_stats_shape(self):
        limiter = PeerRateLimiter(max_per_minute=2, window_s=60)
        limiter.record_inbound("mac1")
        limiter.record_outbound("mac2")
        stats = limiter.get_peer_stats()
        assert set(stats.keys()) == {"mac1", "mac2"}
        assert "inbound" in stats["mac1"]
        assert stats["mac1"]["inbound"]["accepted"] == 1
        assert stats["mac1"]["inbound"]["limit"] == 2
        assert stats["mac2"]["outbound"]["accepted"] == 1

    def test_get_totals(self):
        limiter = PeerRateLimiter(max_per_minute=2, window_s=60)
        # mac1 sends 3 inbound → 2 accepted, 1 limited
        for _ in range(3):
            limiter.record_inbound("mac1")
        # mac2 sends 1 outbound
        limiter.record_outbound("mac2")
        totals = limiter.get_totals()
        assert totals["inbound"] == {"accepted": 2, "limited": 1}
        assert totals["outbound"] == {"accepted": 1, "limited": 0}

    def test_reset_single_peer(self):
        limiter = PeerRateLimiter(max_per_minute=2, window_s=60)
        for _ in range(3):
            limiter.record_inbound("mac1")
        limiter.record_inbound("mac2")
        limiter.reset("mac1")
        assert "mac1" not in limiter.get_peer_stats()
        assert "mac2" in limiter.get_peer_stats()

    def test_reset_all(self):
        limiter = PeerRateLimiter(max_per_minute=2, window_s=60)
        limiter.record_inbound("mac1")
        limiter.record_outbound("mac2")
        limiter.reset()
        assert limiter.get_peer_stats() == {}


# ═══════════════════════════════════════════════════════════════════
# Callback + event hooks
# ═══════════════════════════════════════════════════════════════════


class TestOnLimitCallback:
    def test_callback_fires_on_rejection(self):
        calls = []

        def cb(peer_id, direction, count):
            calls.append((peer_id, direction, count))

        limiter = PeerRateLimiter(max_per_minute=1, window_s=60, on_limit=cb)
        limiter.record_inbound("mac1")  # accepted
        limiter.record_inbound("mac1")  # rejected
        limiter.record_outbound("mac1")  # accepted
        limiter.record_outbound("mac1")  # rejected

        assert len(calls) == 2
        assert calls[0][0] == "mac1" and calls[0][1] == "inbound"
        assert calls[1][0] == "mac1" and calls[1][1] == "outbound"

    def test_callback_failure_does_not_break_limiter(self):
        def bad_cb(*a, **kw):
            raise RuntimeError("boom")

        limiter = PeerRateLimiter(max_per_minute=1, window_s=60, on_limit=bad_cb)
        assert limiter.record_inbound("mac1") is True
        # Still returns False even though callback raised
        assert limiter.record_inbound("mac1") is False


# ═══════════════════════════════════════════════════════════════════
# Thread safety
# ═══════════════════════════════════════════════════════════════════


class TestThreadSafety:
    def test_concurrent_record_counts_correctly(self):
        """50 threads × 20 calls each = 1000 calls. Limit 500 → expect exactly 500 accepted."""
        limiter = PeerRateLimiter(max_per_minute=500, window_s=60)
        accepted = [0]
        lock = threading.Lock()

        def worker():
            local = 0
            for _ in range(20):
                if limiter.record_inbound("peer"):
                    local += 1
            with lock:
                accepted[0] += local

        threads = [threading.Thread(target=worker) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert accepted[0] == 500
        stats = limiter.get_peer_stats()
        assert stats["peer"]["inbound"]["accepted"] == 500
        assert stats["peer"]["inbound"]["limited"] == 500


# ═══════════════════════════════════════════════════════════════════
# Edge cases
# ═══════════════════════════════════════════════════════════════════


class TestEdgeCases:
    def test_empty_peer_id_mapped_to_sentinel(self):
        limiter = PeerRateLimiter(max_per_minute=2, window_s=60)
        assert limiter.record_inbound("") is True
        assert limiter.record_inbound("") is True
        assert limiter.record_inbound("") is False
        stats = limiter.get_peer_stats()
        assert "<unknown>" in stats

    def test_none_peer_id_mapped_to_sentinel(self):
        limiter = PeerRateLimiter(max_per_minute=2, window_s=60)
        assert limiter.record_inbound(None) is True  # type: ignore[arg-type]
        assert limiter.record_inbound(None) is True  # type: ignore[arg-type]
        assert limiter.record_inbound(None) is False  # type: ignore[arg-type]


# ═══════════════════════════════════════════════════════════════════
# Persistence
# ═══════════════════════════════════════════════════════════════════


class TestPersistence:
    def test_save_state_writes_json(self, tmp_path):
        limiter = PeerRateLimiter(max_per_minute=10, window_s=60)
        limiter.record_inbound("mac1")
        limiter.record_outbound("mac2")

        target = tmp_path / "rate_limits.json"
        assert limiter.save_state(target) is True
        assert target.exists()
        data = json.loads(target.read_text())
        assert data["version"] == 1
        assert data["max_inbound"] == 10
        assert "mac1" in data["buckets"]["inbound"]
        assert "mac2" in data["buckets"]["outbound"]

    def test_load_state_roundtrip(self, tmp_path):
        limiter = PeerRateLimiter(max_per_minute=10, window_s=60)
        for _ in range(4):
            limiter.record_inbound("mac1")

        target = tmp_path / "rate_limits.json"
        assert limiter.save_state(target) is True

        # Fresh limiter
        restored = PeerRateLimiter(max_per_minute=10, window_s=60)
        assert restored.load_state(target) is True
        stats = restored.get_peer_stats()
        assert stats["mac1"]["inbound"]["accepted"] == 4

    def test_load_state_missing_file(self, tmp_path):
        limiter = PeerRateLimiter(max_per_minute=10, window_s=60)
        assert limiter.load_state(tmp_path / "nope.json") is False

    def test_save_state_failure_is_graceful(self, tmp_path):
        limiter = PeerRateLimiter(max_per_minute=10, window_s=60)
        limiter.record_inbound("mac1")
        # Write to a location we can't create (file where dir should be)
        blocker = tmp_path / "blocked"
        blocker.write_text("i am a file")
        bad_target = blocker / "sub" / "rate_limits.json"
        assert limiter.save_state(bad_target) is False  # no exception

    def test_load_state_stale_resets_counts(self, tmp_path):
        """If the save is older than window_s, counts should reset (keep stats)."""
        limiter = PeerRateLimiter(max_per_minute=10, window_s=60)
        for _ in range(5):
            limiter.record_inbound("mac1")
        target = tmp_path / "rate_limits.json"
        limiter.save_state(target)

        # Forge a very old save
        data = json.loads(target.read_text())
        data["saved_at_wall"] = time.time() - 3600  # 1 hour ago
        target.write_text(json.dumps(data))

        restored = PeerRateLimiter(max_per_minute=10, window_s=60)
        assert restored.load_state(target) is True
        # count was reset but accepted (cumulative stats) persist
        stats = restored.get_peer_stats()
        assert stats["mac1"]["inbound"]["count"] == 0
        assert stats["mac1"]["inbound"]["accepted"] == 5
