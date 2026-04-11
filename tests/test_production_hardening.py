"""Production hardening tests for probe engine and IDE bridge.

20 tests covering:
- Probe timeout enforcement
- Probe result caching
- One probe crash doesn't kill engine
- Concurrent probe execution
- Engine health check
- IDE bridge retry on disconnect
- Event queue overflow
- Stale data detection
- Graceful shutdown
"""

import threading
import time

import pytest

from multifleet.probe_engine import (
    ProbeCategory,
    ProbeEngine,
    ProbeFinding,
    ProbeRegistry,
    ProbeResult,
)
from multifleet.ide_bridge import IDEBridge


# ── Helpers ──────────────────────────────────────────────────────────


def _ok_probe(context):
    return ProbeResult(
        probe_id="ok_probe", category="structure", status="ok",
        confidence=0.9, severity="info",
    )


def _slow_probe(context):
    """Probe that takes 30s (will be killed by timeout)."""
    time.sleep(30)
    return ProbeResult(
        probe_id="slow_probe", category="structure", status="ok",
        confidence=0.5, severity="info",
    )


def _crashing_probe(context):
    raise RuntimeError("probe exploded")


def _finding_probe(context):
    return ProbeResult(
        probe_id="finding_probe", category="security", status="warning",
        confidence=0.8, severity="high",
        findings=[ProbeFinding(title="secret found", blast_radius="high")],
    )


class FakeAggregator:
    """Minimal aggregator stub for IDE bridge tests."""
    def __init__(self, stale=False):
        self._stale = stale

    def snapshot(self):
        return {
            "node_id": "test-node",
            "peers": {"reachable": 1, "total": 2},
            "health_dot": "green",
            "daemon": {"status": "running", "peers": {}},
            "rebuttals": {"active": 0, "total": 0},
            "leases": {"active": 0, "held_by_me": 0},
            "liaison": {"pending_reviews": 0, "unread": 0},
        }

    def status_bar_text(self):
        return "Fleet: 2 nodes"


# ═══════════════════════════════════════════════════════════════════
# PROBE ENGINE TESTS
# ═══════════════════════════════════════════════════════════════════


class TestProbeTimeout:

    def test_slow_probe_times_out(self):
        """Probe exceeding timeout returns error result instead of hanging."""
        reg = ProbeRegistry(default_timeout=0.2, cache_ttl=0)
        reg.register("slow", _slow_probe, ProbeCategory.STRUCTURE)
        result = reg.run_one("slow", {})
        assert result.status == "error"
        assert "timed out" in result.findings[0].title.lower()

    def test_fast_probe_completes_within_timeout(self):
        reg = ProbeRegistry(default_timeout=5.0, cache_ttl=0)
        reg.register("ok", _ok_probe, ProbeCategory.STRUCTURE)
        result = reg.run_one("ok", {})
        assert result.status == "ok"

    def test_custom_timeout_per_engine_instance(self):
        """Different registries can have different default timeouts."""
        fast_reg = ProbeRegistry(default_timeout=0.1, cache_ttl=0)
        slow_reg = ProbeRegistry(default_timeout=10.0, cache_ttl=0)
        fast_reg.register("slow", _slow_probe, ProbeCategory.STRUCTURE)
        slow_reg.register("ok", _ok_probe, ProbeCategory.STRUCTURE)

        r1 = fast_reg.run_one("slow", {})
        r2 = slow_reg.run_one("ok", {})
        assert r1.status == "error"
        assert r2.status == "ok"


class TestProbeResultCaching:

    def test_cached_result_returned_within_ttl(self):
        """Same probe within TTL returns cached result without re-execution."""
        call_count = 0

        def counting_probe(ctx):
            nonlocal call_count
            call_count += 1
            return ProbeResult(
                probe_id="counter", category="structure", status="ok",
                confidence=0.9, severity="info",
            )

        reg = ProbeRegistry(default_timeout=5.0, cache_ttl=60.0)
        reg.register("counter", counting_probe, ProbeCategory.STRUCTURE)

        reg.run_one("counter", {})
        reg.run_one("counter", {})
        assert call_count == 1  # Second call served from cache

    def test_cache_expires_after_ttl(self):
        call_count = 0

        def counting_probe(ctx):
            nonlocal call_count
            call_count += 1
            return ProbeResult(
                probe_id="counter", category="structure", status="ok",
                confidence=0.9, severity="info",
            )

        reg = ProbeRegistry(default_timeout=5.0, cache_ttl=0.1)
        reg.register("counter", counting_probe, ProbeCategory.STRUCTURE)

        reg.run_one("counter", {})
        time.sleep(0.15)
        reg.run_one("counter", {})
        assert call_count == 2

    def test_invalidate_cache_forces_rerun(self):
        call_count = 0

        def counting_probe(ctx):
            nonlocal call_count
            call_count += 1
            return ProbeResult(
                probe_id="counter", category="structure", status="ok",
                confidence=0.9, severity="info",
            )

        reg = ProbeRegistry(default_timeout=5.0, cache_ttl=60.0)
        reg.register("counter", counting_probe, ProbeCategory.STRUCTURE)

        reg.run_one("counter", {})
        reg.invalidate_cache("counter")
        reg.run_one("counter", {})
        assert call_count == 2


class TestProbeErrorIsolation:

    def test_crashing_probe_returns_error_not_exception(self):
        reg = ProbeRegistry(default_timeout=5.0, cache_ttl=0)
        reg.register("crasher", _crashing_probe, ProbeCategory.SECURITY)
        result = reg.run_one("crasher", {})
        assert result.status == "error"
        assert "exploded" in result.findings[0].title

    def test_one_crash_doesnt_kill_run_all(self):
        """Engine continues running remaining probes after one crashes."""
        reg = ProbeRegistry(default_timeout=5.0, cache_ttl=0)
        reg.register("ok1", _ok_probe, ProbeCategory.STRUCTURE)
        reg.register("crasher", _crashing_probe, ProbeCategory.SECURITY)
        reg.register("finder", _finding_probe, ProbeCategory.SECURITY)

        results = reg.run_all({}, concurrent=False)
        # All 3 probes ran (crash didn't stop the engine)
        assert len(results) == 3
        statuses = [r.status for r in results]
        assert "ok" in statuses  # At least one succeeded
        assert "error" in statuses  # The crasher errored gracefully

    def test_crash_updates_health_to_degraded(self):
        reg = ProbeRegistry(default_timeout=5.0, cache_ttl=0)
        reg.register("crasher", _crashing_probe, ProbeCategory.SECURITY)
        reg.run_one("crasher", {})
        health = reg.get_health()
        assert health["crasher"]["status"] == "degraded"
        assert health["crasher"]["consecutive_failures"] == 1


class TestConcurrentExecution:

    def test_concurrent_probes_run_in_parallel(self):
        """Multiple probes run concurrently, total time < sum of individual times."""
        def sleepy_probe(ctx):
            time.sleep(0.2)
            return ProbeResult(
                probe_id="sleepy", category="structure", status="ok",
                confidence=0.9, severity="info",
            )

        reg = ProbeRegistry(default_timeout=5.0, cache_ttl=0)
        for i in range(5):
            reg.register(f"sleepy_{i}", sleepy_probe, ProbeCategory.STRUCTURE)

        t0 = time.monotonic()
        results = reg.run_all({}, concurrent=True, max_workers=5)
        elapsed = time.monotonic() - t0

        assert len(results) == 5
        # If sequential, would take ~1.0s. Concurrent should be ~0.2-0.4s.
        assert elapsed < 0.8, f"Took {elapsed:.2f}s — probes not running concurrently"

    def test_concurrent_crash_isolation(self):
        """One crashing probe in concurrent mode doesn't prevent others."""
        reg = ProbeRegistry(default_timeout=5.0, cache_ttl=0)
        reg.register("ok1", _ok_probe, ProbeCategory.STRUCTURE)
        reg.register("crasher", _crashing_probe, ProbeCategory.SECURITY)
        reg.register("finder", _finding_probe, ProbeCategory.SECURITY)

        results = reg.run_all({}, concurrent=True)
        assert len(results) == 3
        error_results = [r for r in results if r.status == "error"]
        assert len(error_results) == 1


class TestEngineHealth:

    def test_health_reports_all_probes(self):
        reg = ProbeRegistry(default_timeout=5.0, cache_ttl=0)
        reg.register("ok1", _ok_probe, ProbeCategory.STRUCTURE)
        reg.register("crasher", _crashing_probe, ProbeCategory.SECURITY)
        engine = ProbeEngine(registry=reg)

        # Before any run, all unknown
        h = engine.health()
        assert h["summary"]["unknown"] == 2

    def test_health_after_scan_shows_status(self):
        reg = ProbeRegistry(default_timeout=5.0, cache_ttl=0)
        reg.register("ok1", _ok_probe, ProbeCategory.STRUCTURE)
        reg.register("crasher", _crashing_probe, ProbeCategory.SECURITY)
        engine = ProbeEngine(registry=reg)
        engine.scan("all", {})

        h = engine.health()
        assert h["summary"]["ok"] >= 1
        assert h["summary"]["degraded"] >= 1 or h["summary"]["broken"] >= 1
        assert h["engine_status"] in ("degraded", "unhealthy")

    def test_health_marks_broken_after_repeated_failures(self):
        reg = ProbeRegistry(default_timeout=5.0, cache_ttl=0)
        reg.register("crasher", _crashing_probe, ProbeCategory.SECURITY)

        for _ in range(3):
            reg.run_one("crasher", {})

        health = reg.get_health()
        assert health["crasher"]["status"] == "broken"
        assert health["crasher"]["consecutive_failures"] == 3


# ═══════════════════════════════════════════════════════════════════
# IDE BRIDGE TESTS
# ═══════════════════════════════════════════════════════════════════


class TestIDEBridgeRetry:

    def test_retry_succeeds_on_second_attempt(self):
        attempts = 0

        def flaky_connect():
            nonlocal attempts
            attempts += 1
            return attempts >= 2  # Fail first, succeed second

        bridge = IDEBridge(max_retries=3)
        result = bridge.connect(flaky_connect)
        assert result is True
        assert attempts == 2
        assert bridge.is_connected

    def test_retry_exhausted_returns_false(self):
        bridge = IDEBridge(max_retries=2)
        result = bridge.connect(lambda: False)
        assert result is False
        assert not bridge.is_connected


class TestEventQueueOverflow:

    def test_overflow_drops_oldest(self):
        bridge = IDEBridge(max_events=5)
        for i in range(10):
            bridge.enqueue_event({"id": i})

        assert bridge.queue_size == 5
        events = bridge.drain_events()
        # Should have events 5-9 (oldest 0-4 dropped)
        assert [e["id"] for e in events] == [5, 6, 7, 8, 9]

    def test_overflow_callback_fired(self):
        overflow_log = []
        bridge = IDEBridge(max_events=3)
        bridge.on_overflow(lambda dropped, total: overflow_log.append((dropped, total)))

        for i in range(5):
            bridge.enqueue_event({"id": i})

        assert len(overflow_log) > 0
        assert bridge.dropped_count > 0


class TestStaleDataDetection:

    def test_stale_when_no_snapshot(self):
        bridge = IDEBridge(stale_threshold=30.0)
        assert bridge.is_stale() is True

    def test_not_stale_after_fresh_snapshot(self):
        bridge = IDEBridge(aggregator=FakeAggregator(), stale_threshold=30.0)
        bridge.get_status_bar_data()  # Updates _last_snapshot_at
        assert bridge.is_stale() is False

    def test_stale_after_threshold(self):
        bridge = IDEBridge(aggregator=FakeAggregator(), stale_threshold=0.1)
        bridge.get_status_bar_data()
        time.sleep(0.15)
        assert bridge.is_stale() is True

    def test_status_bar_marks_stale(self):
        bridge = IDEBridge(aggregator=FakeAggregator(), stale_threshold=0.0)
        # With threshold=0, immediately stale after snapshot
        data = bridge.get_status_bar_data()
        assert data.get("stale") is True


class TestGracefulShutdown:

    def test_shutdown_flushes_notifications(self):
        bridge = IDEBridge()
        bridge.translate_event("fleet.health.degraded", {"node_id": "mac1"})
        bridge.enqueue_event({"type": "test"})

        flushed = bridge.shutdown()
        assert len(flushed) >= 2  # At least 1 notification + 1 event
        assert not bridge.is_connected

    def test_shutdown_clears_queues(self):
        bridge = IDEBridge()
        bridge.enqueue_event({"type": "a"})
        bridge.enqueue_event({"type": "b"})
        bridge.shutdown()
        assert bridge.queue_size == 0
        assert len(bridge.get_pending_notifications()) == 0
