"""Probe Scheduler — runs probes on independent schedules with cached results.

Each probe can have its own interval. The scheduler's tick() method checks
which probes are due and runs them, caching results with configurable TTL.

Usage:
    from multifleet.probe_scheduler import ProbeScheduler
    from multifleet.probes import create_engine

    engine = create_engine()
    scheduler = ProbeScheduler(engine)
    scheduler.load_defaults()
    scheduler.tick()  # run any due probes
    results = scheduler.get_cached_results()
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from multifleet.probe_engine import ProbeEngine, ProbeResult

logger = logging.getLogger("fleet-nerve.probe-scheduler")

# Default schedules: probe_id -> interval_seconds
DEFAULT_SCHEDULES: dict[str, float] = {
    "partition_lease_drift": 60.0,
    "fleet_health": 60.0,
    "stale_context": 300.0,
    "repeated_failure": 300.0,
    "security_surface": 300.0,
    "dependency_drift": 600.0,
    "mirror_drift": 600.0,
    "architectural_hotspots": 600.0,
}


@dataclass
class _ScheduleEntry:
    """Internal tracking for a scheduled probe."""
    probe_id: str
    interval_s: float
    last_run: float = 0.0  # monotonic timestamp of last execution
    run_count: int = 0
    last_error: str = ""


@dataclass
class CachedResult:
    """A probe result with staleness metadata."""
    result: ProbeResult
    cached_at: float  # monotonic time
    ttl_s: float

    @property
    def age_s(self) -> float:
        return time.monotonic() - self.cached_at

    @property
    def is_stale(self) -> bool:
        return self.age_s > self.ttl_s

    def to_dict(self) -> dict:
        return {
            "result": self.result.to_dict(),
            "age_s": round(self.age_s, 1),
            "ttl_s": self.ttl_s,
            "stale": self.is_stale,
        }


class ProbeScheduler:
    """Schedules probes to run at independent intervals and caches results.

    Args:
        engine: ProbeEngine instance with registered probes.
        default_ttl: Default cache TTL in seconds (results older than this
                     are marked stale but still returned until refreshed).
    """

    def __init__(self, engine: ProbeEngine, default_ttl: float = 300.0):
        self._engine = engine
        self._default_ttl = default_ttl
        self._schedules: dict[str, _ScheduleEntry] = {}
        self._cache: dict[str, CachedResult] = {}
        self._lock = threading.Lock()
        self._tick_count = 0

    # ── Schedule management ──

    def schedule_probe(self, probe_id: str, interval_s: float, ttl_s: Optional[float] = None) -> bool:
        """Register a probe to run on a fixed interval.

        Args:
            probe_id: Must match a registered probe in the engine.
            interval_s: Seconds between runs. Must be >= 10.
            ttl_s: Cache TTL for this probe's results. Defaults to default_ttl.

        Returns:
            True if scheduled, False if probe_id is not registered.
        """
        if interval_s < 10:
            logger.warning("probe_schedule_rejected id=%s interval=%.1f (min 10s)", probe_id, interval_s)
            return False

        registered = {p["probe_id"] for p in self._engine.registry.list_probes()}
        if probe_id not in registered:
            logger.warning("probe_schedule_rejected id=%s (not registered)", probe_id)
            return False

        with self._lock:
            self._schedules[probe_id] = _ScheduleEntry(
                probe_id=probe_id,
                interval_s=interval_s,
            )
        logger.info("probe_scheduled id=%s interval=%.0fs", probe_id, interval_s)
        return True

    def unschedule_probe(self, probe_id: str) -> bool:
        """Remove a probe from the schedule."""
        with self._lock:
            removed = self._schedules.pop(probe_id, None)
        if removed:
            logger.info("probe_unscheduled id=%s", probe_id)
        return removed is not None

    def load_defaults(self) -> int:
        """Load default schedules for common probes.

        Only schedules probes that are actually registered in the engine.
        Returns the number of probes scheduled.
        """
        count = 0
        for probe_id, interval_s in DEFAULT_SCHEDULES.items():
            if self.schedule_probe(probe_id, interval_s):
                count += 1
        logger.info("default_schedules_loaded count=%d", count)
        return count

    # ── Tick (the heartbeat) ──

    def tick(self, now: Optional[float] = None) -> list[ProbeResult]:
        """Check all schedules and run any probes that are due.

        Args:
            now: Override monotonic time (for testing). Defaults to time.monotonic().

        Returns:
            List of ProbeResults from probes that ran this tick.
        """
        now = now if now is not None else time.monotonic()
        self._tick_count += 1
        due: list[_ScheduleEntry] = []

        with self._lock:
            for entry in self._schedules.values():
                elapsed = now - entry.last_run
                if elapsed >= entry.interval_s:
                    due.append(entry)

        if not due:
            return []

        results: list[ProbeResult] = []
        for entry in due:
            try:
                result = self._engine.scan_one(entry.probe_id)
                ttl = self._default_ttl
                with self._lock:
                    entry.last_run = now
                    entry.run_count += 1
                    entry.last_error = ""
                    self._cache[entry.probe_id] = CachedResult(
                        result=result, cached_at=now, ttl_s=ttl,
                    )
                results.append(result)
                logger.debug(
                    "probe_tick_ran id=%s status=%s duration=%.0fms",
                    entry.probe_id, result.status, result.duration_ms,
                )
            except Exception as e:
                with self._lock:
                    entry.last_error = str(e)
                logger.error("probe_tick_error id=%s error=%s", entry.probe_id, e)

        logger.info("tick_complete ran=%d tick_n=%d", len(results), self._tick_count)
        return results

    # ── Cache access ──

    def get_cached_results(self, probe_id: Optional[str] = None) -> dict[str, CachedResult]:
        """Return cached results with staleness indicators.

        Args:
            probe_id: If provided, return only that probe's cached result.
                      If None, return all cached results.

        Returns:
            Dict of probe_id -> CachedResult. Empty dict if nothing cached.
        """
        with self._lock:
            if probe_id is not None:
                entry = self._cache.get(probe_id)
                return {probe_id: entry} if entry else {}
            return dict(self._cache)

    def get_cached_result(self, probe_id: str) -> Optional[CachedResult]:
        """Return a single cached result or None."""
        with self._lock:
            return self._cache.get(probe_id)

    def invalidate(self, probe_id: Optional[str] = None) -> None:
        """Clear cached results. If probe_id given, clear only that one."""
        with self._lock:
            if probe_id:
                self._cache.pop(probe_id, None)
            else:
                self._cache.clear()

    # ── Status / introspection ──

    def status(self) -> dict:
        """Return scheduler status for CLI/dashboard."""
        with self._lock:
            schedules = {}
            for pid, entry in self._schedules.items():
                cached = self._cache.get(pid)
                schedules[pid] = {
                    "interval_s": entry.interval_s,
                    "last_run_ago_s": round(time.monotonic() - entry.last_run, 1) if entry.last_run else None,
                    "run_count": entry.run_count,
                    "last_error": entry.last_error or None,
                    "cached": cached.to_dict() if cached else None,
                    "stale": cached.is_stale if cached else True,
                }
            return {
                "scheduled_probes": len(self._schedules),
                "cached_results": len(self._cache),
                "tick_count": self._tick_count,
                "default_ttl_s": self._default_ttl,
                "probes": schedules,
            }

    @property
    def scheduled_count(self) -> int:
        return len(self._schedules)

    @property
    def tick_count(self) -> int:
        return self._tick_count
