"""Lightweight in-memory metrics collector for fleet operations.

Thread-safe counters, gauges, and simple histograms.
Resets on restart (no persistence). Consumed by dashboard and doctor.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Any


class FleetMetrics:
    """Thread-safe metrics store: counters, gauges, histograms."""

    def __init__(self, histogram_size: int = 100) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, int] = defaultdict(int)
        self._gauges: dict[str, float] = {}
        self._histograms: dict[str, list[float]] = defaultdict(list)
        self._hist_max = histogram_size
        self._start = time.monotonic()

    def increment(self, name: str, value: int = 1) -> None:
        with self._lock:
            self._counters[name] += value

    def gauge(self, name: str, value: float) -> None:
        with self._lock:
            self._gauges[name] = value

    def observe(self, name: str, value: float) -> None:
        with self._lock:
            buf = self._histograms[name]
            buf.append(value)
            if len(buf) > self._hist_max:
                del buf[: len(buf) - self._hist_max]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            gauges = dict(self._gauges)
            gauges["uptime_s"] = round(time.monotonic() - self._start, 1)
            hists: dict[str, dict[str, float]] = {}
            for name, buf in self._histograms.items():
                if buf:
                    s = sorted(buf)
                    p95_idx = max(0, int(len(s) * 0.95) - 1)
                    hists[name] = {
                        "min": round(s[0], 3),
                        "max": round(s[-1], 3),
                        "avg": round(sum(s) / len(s), 3),
                        "p95": round(s[p95_idx], 3),
                        "count": len(s),
                    }
            return {
                "counters": dict(self._counters),
                "gauges": gauges,
                "histograms": hists,
            }

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._histograms.clear()
            self._start = time.monotonic()


# Singleton for process-wide use
fleet_metrics = FleetMetrics()
