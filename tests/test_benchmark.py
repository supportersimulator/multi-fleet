"""Tests for multifleet.benchmark — fleet performance micro-benchmarks."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from multifleet.benchmark import BenchmarkResult, FleetBenchmark, _percentile, _run_bench


# ── BenchmarkResult ──

class TestBenchmarkResult:
    def test_fields(self):
        r = BenchmarkResult(
            name="test", iterations=100, mean_ms=1.0,
            p50_ms=0.9, p95_ms=1.5, p99_ms=2.0,
            min_ms=0.5, max_ms=3.0,
        )
        assert r.name == "test"
        assert r.iterations == 100
        assert r.mean_ms == 1.0

    def test_to_dict(self):
        r = BenchmarkResult(
            name="x", iterations=10, mean_ms=1.0,
            p50_ms=1.0, p95_ms=1.0, p99_ms=1.0,
            min_ms=0.5, max_ms=1.5,
        )
        d = r.to_dict()
        assert d["name"] == "x"
        assert d["iterations"] == 10
        assert "mean_ms" in d
        assert "p50_ms" in d
        assert "p99_ms" in d


# ── Percentile helper ──

class TestPercentile:
    def test_empty(self):
        assert _percentile([], 50) == 0.0

    def test_single(self):
        assert _percentile([5.0], 50) == 5.0
        assert _percentile([5.0], 99) == 5.0

    def test_sorted_data(self):
        data = [1.0, 2.0, 3.0, 4.0, 5.0]
        assert _percentile(data, 50) == 3.0
        assert _percentile(data, 0) == 1.0
        assert _percentile(data, 100) == 5.0


# ── _run_bench helper ──

class TestRunBench:
    def test_basic(self):
        counter = {"n": 0}
        def fn():
            counter["n"] += 1
        result = _run_bench("counter", fn, 50)
        assert result.name == "counter"
        assert result.iterations == 50
        assert counter["n"] == 50
        assert result.min_ms <= result.mean_ms <= result.max_ms
        assert result.p50_ms <= result.p95_ms <= result.p99_ms


# ── Individual benchmarks ──

class TestPacketValidation:
    def test_runs(self):
        bench = FleetBenchmark()
        r = bench.bench_packet_validation(iterations=10)
        assert r.name == "packet_validation"
        assert r.iterations == 10
        assert r.mean_ms > 0


class TestDedupFilter:
    def test_runs(self):
        bench = FleetBenchmark()
        r = bench.bench_dedup_filter(iterations=100)
        assert r.name == "dedup_filter"
        assert r.iterations == 100
        assert r.mean_ms >= 0


class TestProbeExecution:
    def test_runs(self):
        bench = FleetBenchmark()
        r = bench.bench_probe_execution(iterations=2)
        assert r.name == "probe_execution"
        assert r.iterations == 2


class TestSynthesis:
    def test_runs(self):
        bench = FleetBenchmark()
        r = bench.bench_synthesis(iterations=5)
        assert r.name == "synthesis"
        assert r.iterations == 5
        assert r.mean_ms > 0


class TestLeaseAcquireRelease:
    def test_runs(self):
        bench = FleetBenchmark()
        r = bench.bench_lease_acquire_release(iterations=20)
        assert r.name == "lease_acquire_release"
        assert r.iterations == 20
        assert r.mean_ms > 0


class TestAuditWrite:
    def test_runs(self):
        bench = FleetBenchmark()
        r = bench.bench_audit_write(iterations=20)
        assert r.name == "audit_write"
        assert r.iterations == 20
        assert r.mean_ms > 0


# ── Full suite + reporting ──

class TestFleetBenchmarkSuite:
    def test_run_all(self):
        bench = FleetBenchmark()
        results = bench.run_all()
        assert len(results) == 6
        names = {r.name for r in results}
        assert "packet_validation" in names
        assert "dedup_filter" in names
        assert "synthesis" in names

    def test_render_report_empty(self):
        bench = FleetBenchmark()
        report = bench.render_report()
        assert "No benchmark" in report

    def test_render_report_with_data(self):
        bench = FleetBenchmark()
        bench._results = [
            BenchmarkResult("test", 10, 1.0, 0.9, 1.5, 2.0, 0.5, 3.0),
        ]
        report = bench.render_report()
        assert "test" in report
        assert "P50" in report or "p50" in report.lower() or "1.000ms" in report

    def test_to_dict(self):
        bench = FleetBenchmark()
        bench._results = [
            BenchmarkResult("a", 10, 1.0, 1.0, 1.0, 1.0, 0.5, 1.5),
        ]
        d = bench.to_dict()
        assert d["count"] == 1
        assert len(d["benchmarks"]) == 1
        assert d["benchmarks"][0]["name"] == "a"
