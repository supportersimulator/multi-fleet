"""Tests for FleetMetrics — counters, gauges, histograms, thread safety."""

import threading

from multifleet.metrics import FleetMetrics


class TestCounters:
    def test_increment_default(self):
        m = FleetMetrics()
        m.increment("messages_sent")
        assert m.snapshot()["counters"]["messages_sent"] == 1

    def test_increment_by_value(self):
        m = FleetMetrics()
        m.increment("messages_sent", 5)
        m.increment("messages_sent", 3)
        assert m.snapshot()["counters"]["messages_sent"] == 8


class TestGauges:
    def test_gauge_set_and_overwrite(self):
        m = FleetMetrics()
        m.gauge("active_peers", 3)
        m.gauge("active_peers", 5)
        assert m.snapshot()["gauges"]["active_peers"] == 5

    def test_uptime_always_present(self):
        m = FleetMetrics()
        assert "uptime_s" in m.snapshot()["gauges"]


class TestHistograms:
    def test_observe_stats(self):
        m = FleetMetrics()
        for v in [10, 20, 30, 40, 50]:
            m.observe("latency", v)
        h = m.snapshot()["histograms"]["latency"]
        assert h["min"] == 10
        assert h["max"] == 50
        assert h["avg"] == 30.0
        assert h["count"] == 5

    def test_histogram_capped(self):
        m = FleetMetrics(histogram_size=5)
        for i in range(20):
            m.observe("lat", float(i))
        assert m.snapshot()["histograms"]["lat"]["count"] == 5


class TestReset:
    def test_reset_clears_all(self):
        m = FleetMetrics()
        m.increment("x")
        m.gauge("y", 1)
        m.observe("z", 1)
        m.reset()
        s = m.snapshot()
        assert s["counters"] == {}
        assert s["histograms"] == {}
        assert "y" not in s["gauges"]


class TestThreadSafety:
    def test_concurrent_increments(self):
        m = FleetMetrics()
        def worker():
            for _ in range(1000):
                m.increment("c")
        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert m.snapshot()["counters"]["c"] == 4000
