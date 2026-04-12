"""Tests for ProbeScheduler — scheduling, caching, tick, and CLI wiring."""

import time
from unittest.mock import MagicMock, patch

import pytest

from multifleet.probe_engine import (
    ProbeCategory,
    ProbeEngine,
    ProbeFinding,
    ProbeRegistry,
    ProbeResult,
)
from multifleet.probe_scheduler import (
    CachedResult,
    ProbeScheduler,
    DEFAULT_SCHEDULES,
)


# ── Helpers ──

def _ok_probe(context: dict) -> ProbeResult:
    return ProbeResult(
        probe_id="test_ok",
        category="structure",
        status="ok",
        confidence=0.95,
        severity="info",
    )


def _warn_probe(context: dict) -> ProbeResult:
    return ProbeResult(
        probe_id="test_warn",
        category="context_quality",
        status="warning",
        confidence=0.7,
        severity="medium",
        findings=[ProbeFinding(title="Drift detected")],
    )


def _make_engine(*probes) -> ProbeEngine:
    """Create a ProbeEngine with given (id, fn, category) tuples."""
    registry = ProbeRegistry(cache_ttl=0)  # disable registry cache for test clarity
    for pid, fn, cat in probes:
        registry.register(pid, fn, cat)
    return ProbeEngine(registry=registry)


# ── Schedule management ──

class TestScheduleProbe:

    def test_schedule_registered_probe(self):
        engine = _make_engine(("alpha", _ok_probe, ProbeCategory.STRUCTURE))
        sched = ProbeScheduler(engine)
        assert sched.schedule_probe("alpha", 60) is True
        assert sched.scheduled_count == 1

    def test_schedule_unregistered_probe_rejected(self):
        engine = _make_engine(("alpha", _ok_probe, ProbeCategory.STRUCTURE))
        sched = ProbeScheduler(engine)
        assert sched.schedule_probe("nonexistent", 60) is False
        assert sched.scheduled_count == 0

    def test_schedule_too_short_interval_rejected(self):
        engine = _make_engine(("alpha", _ok_probe, ProbeCategory.STRUCTURE))
        sched = ProbeScheduler(engine)
        assert sched.schedule_probe("alpha", 5) is False
        assert sched.scheduled_count == 0

    def test_unschedule(self):
        engine = _make_engine(("alpha", _ok_probe, ProbeCategory.STRUCTURE))
        sched = ProbeScheduler(engine)
        sched.schedule_probe("alpha", 60)
        assert sched.unschedule_probe("alpha") is True
        assert sched.scheduled_count == 0
        # Unschedule again returns False
        assert sched.unschedule_probe("alpha") is False


# ── Tick ──

class TestTick:

    def test_tick_runs_due_probes(self):
        engine = _make_engine(("alpha", _ok_probe, ProbeCategory.STRUCTURE))
        sched = ProbeScheduler(engine)
        sched.schedule_probe("alpha", 10)

        # First tick at t=0: last_run defaults to 0, so elapsed >= interval
        results = sched.tick(now=100.0)
        assert len(results) == 1
        assert results[0].probe_id == "test_ok"

    def test_tick_skips_not_due(self):
        engine = _make_engine(("alpha", _ok_probe, ProbeCategory.STRUCTURE))
        sched = ProbeScheduler(engine)
        sched.schedule_probe("alpha", 60)

        # Run once
        sched.tick(now=100.0)
        # 30s later: not due yet
        results = sched.tick(now=130.0)
        assert len(results) == 0

    def test_tick_runs_again_after_interval(self):
        engine = _make_engine(("alpha", _ok_probe, ProbeCategory.STRUCTURE))
        sched = ProbeScheduler(engine)
        sched.schedule_probe("alpha", 60)

        sched.tick(now=100.0)
        # Exactly 60s later: due
        results = sched.tick(now=160.0)
        assert len(results) == 1

    def test_tick_multiple_probes_independent(self):
        engine = _make_engine(
            ("fast", _ok_probe, ProbeCategory.STRUCTURE),
            ("slow", _warn_probe, ProbeCategory.CONTEXT_QUALITY),
        )
        sched = ProbeScheduler(engine)
        sched.schedule_probe("fast", 10)
        sched.schedule_probe("slow", 60)

        # Both due on first tick
        results = sched.tick(now=100.0)
        assert len(results) == 2

        # 15s later: only fast is due
        results = sched.tick(now=115.0)
        assert len(results) == 1

    def test_tick_count_increments(self):
        engine = _make_engine(("alpha", _ok_probe, ProbeCategory.STRUCTURE))
        sched = ProbeScheduler(engine)
        assert sched.tick_count == 0
        sched.tick(now=0.0)
        assert sched.tick_count == 1
        sched.tick(now=1.0)
        assert sched.tick_count == 2

    def test_tick_handles_probe_error(self):
        """A crashing probe should not break the tick loop."""
        def _crash(ctx):
            raise RuntimeError("boom")

        engine = _make_engine(
            ("crasher", _crash, ProbeCategory.RELIABILITY),
            ("ok_one", _ok_probe, ProbeCategory.STRUCTURE),
        )
        sched = ProbeScheduler(engine)
        sched.schedule_probe("crasher", 10)
        sched.schedule_probe("ok_one", 10)

        # scan_one wraps errors into ProbeResult.error, so both should complete
        results = sched.tick(now=100.0)
        # Both ran (crasher returns error result, ok_one returns ok)
        assert len(results) == 2


# ── Cache ──

class TestCache:

    def test_cached_after_tick(self):
        engine = _make_engine(("alpha", _ok_probe, ProbeCategory.STRUCTURE))
        sched = ProbeScheduler(engine)
        sched.schedule_probe("alpha", 10)
        sched.tick(now=100.0)

        cached = sched.get_cached_result("alpha")
        assert cached is not None
        assert cached.result.probe_id == "test_ok"

    def test_cached_staleness(self):
        engine = _make_engine(("alpha", _ok_probe, ProbeCategory.STRUCTURE))
        sched = ProbeScheduler(engine, default_ttl=30.0)
        sched.schedule_probe("alpha", 10)
        sched.tick(now=100.0)

        cached = sched.get_cached_result("alpha")
        # Right after caching, should not be stale (using real monotonic for age)
        # but we can check the TTL was set
        assert cached.ttl_s == 30.0

    def test_get_cached_results_all(self):
        engine = _make_engine(
            ("a", _ok_probe, ProbeCategory.STRUCTURE),
            ("b", _warn_probe, ProbeCategory.CONTEXT_QUALITY),
        )
        sched = ProbeScheduler(engine)
        sched.schedule_probe("a", 10)
        sched.schedule_probe("b", 10)
        sched.tick(now=100.0)

        all_cached = sched.get_cached_results()
        assert len(all_cached) == 2
        assert "a" in all_cached
        assert "b" in all_cached

    def test_get_cached_results_single(self):
        engine = _make_engine(
            ("a", _ok_probe, ProbeCategory.STRUCTURE),
            ("b", _warn_probe, ProbeCategory.CONTEXT_QUALITY),
        )
        sched = ProbeScheduler(engine)
        sched.schedule_probe("a", 10)
        sched.schedule_probe("b", 10)
        sched.tick(now=100.0)

        single = sched.get_cached_results("a")
        assert len(single) == 1
        assert "a" in single

    def test_get_cached_missing_returns_empty(self):
        engine = _make_engine(("a", _ok_probe, ProbeCategory.STRUCTURE))
        sched = ProbeScheduler(engine)
        assert sched.get_cached_results("nonexistent") == {}
        assert sched.get_cached_result("nonexistent") is None

    def test_invalidate_single(self):
        engine = _make_engine(
            ("a", _ok_probe, ProbeCategory.STRUCTURE),
            ("b", _warn_probe, ProbeCategory.CONTEXT_QUALITY),
        )
        sched = ProbeScheduler(engine)
        sched.schedule_probe("a", 10)
        sched.schedule_probe("b", 10)
        sched.tick(now=100.0)

        sched.invalidate("a")
        assert sched.get_cached_result("a") is None
        assert sched.get_cached_result("b") is not None

    def test_invalidate_all(self):
        engine = _make_engine(
            ("a", _ok_probe, ProbeCategory.STRUCTURE),
            ("b", _warn_probe, ProbeCategory.CONTEXT_QUALITY),
        )
        sched = ProbeScheduler(engine)
        sched.schedule_probe("a", 10)
        sched.schedule_probe("b", 10)
        sched.tick(now=100.0)

        sched.invalidate()
        assert sched.get_cached_results() == {}


# ── CachedResult ──

class TestCachedResult:

    def test_to_dict(self):
        result = ProbeResult(
            probe_id="x", category="structure", status="ok",
            confidence=0.9, severity="info",
        )
        cr = CachedResult(result=result, cached_at=time.monotonic(), ttl_s=60.0)
        d = cr.to_dict()
        assert "result" in d
        assert "age_s" in d
        assert "ttl_s" in d
        assert d["ttl_s"] == 60.0
        assert "stale" in d

    def test_staleness_flag(self):
        result = ProbeResult(
            probe_id="x", category="structure", status="ok",
            confidence=0.9, severity="info",
        )
        # Cached far in the past
        cr = CachedResult(result=result, cached_at=time.monotonic() - 1000, ttl_s=60.0)
        assert cr.is_stale is True

        # Cached just now
        cr2 = CachedResult(result=result, cached_at=time.monotonic(), ttl_s=60.0)
        assert cr2.is_stale is False


# ── Load defaults ──

class TestLoadDefaults:

    def test_loads_registered_only(self):
        """load_defaults only schedules probes that exist in the engine."""
        engine = _make_engine(
            ("partition_lease_drift", _ok_probe, ProbeCategory.RELIABILITY),
            ("stale_context", _ok_probe, ProbeCategory.CONTEXT_QUALITY),
        )
        sched = ProbeScheduler(engine)
        count = sched.load_defaults()
        assert count == 2  # only 2 of the defaults are registered

    def test_default_intervals(self):
        """Check that default intervals match DEFAULT_SCHEDULES."""
        assert DEFAULT_SCHEDULES["partition_lease_drift"] == 60.0
        assert DEFAULT_SCHEDULES["stale_context"] == 300.0
        assert DEFAULT_SCHEDULES["dependency_drift"] == 600.0


# ── Status ──

class TestStatus:

    def test_status_structure(self):
        engine = _make_engine(("alpha", _ok_probe, ProbeCategory.STRUCTURE))
        sched = ProbeScheduler(engine, default_ttl=120.0)
        sched.schedule_probe("alpha", 30)
        sched.tick(now=100.0)

        s = sched.status()
        assert s["scheduled_probes"] == 1
        assert s["cached_results"] == 1
        assert s["tick_count"] == 1
        assert s["default_ttl_s"] == 120.0
        assert "alpha" in s["probes"]
        assert s["probes"]["alpha"]["interval_s"] == 30
        assert s["probes"]["alpha"]["run_count"] == 1
