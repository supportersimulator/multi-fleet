"""Fleet Benchmark -- measure performance of all fleet operations.

Runs a suite of micro-benchmarks and reports latency percentiles.
Useful for regression detection and optimization.
"""

import json
import statistics
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Callable


@dataclass
class BenchmarkResult:
    name: str
    iterations: int
    mean_ms: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    min_ms: float
    max_ms: float

    def to_dict(self) -> dict:
        return asdict(self)


def _percentile(data: list[float], pct: float) -> float:
    """Calculate percentile from sorted data."""
    if not data:
        return 0.0
    k = (len(data) - 1) * (pct / 100)
    f = int(k)
    c = f + 1
    if c >= len(data):
        return data[f]
    return data[f] + (k - f) * (data[c] - data[f])


def _run_bench(name: str, fn: Callable, iterations: int) -> BenchmarkResult:
    """Execute fn for iterations, collect timings, return result."""
    if iterations <= 0:
        return BenchmarkResult(
            name=name, iterations=0, mean_ms=0.0,
            p50_ms=0.0, p95_ms=0.0, p99_ms=0.0,
            min_ms=0.0, max_ms=0.0,
        )
    timings: list[float] = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        fn()
        elapsed_ms = (time.perf_counter() - t0) * 1000
        timings.append(elapsed_ms)
    timings.sort()
    return BenchmarkResult(
        name=name,
        iterations=iterations,
        mean_ms=round(statistics.mean(timings), 4),
        p50_ms=round(_percentile(timings, 50), 4),
        p95_ms=round(_percentile(timings, 95), 4),
        p99_ms=round(_percentile(timings, 99), 4),
        min_ms=round(timings[0], 4),
        max_ms=round(timings[-1], 4),
    )


class FleetBenchmark:
    def __init__(self):
        self._results: list[BenchmarkResult] = []

    def run_all(self) -> list[BenchmarkResult]:
        """Run all benchmarks."""
        self._results = [
            self.bench_packet_validation(),
            self.bench_dedup_filter(),
            self.bench_probe_execution(),
            self.bench_synthesis(),
            self.bench_lease_acquire_release(),
            self.bench_audit_write(),
        ]
        return self._results

    def bench_packet_validation(self, iterations: int = 1000) -> BenchmarkResult:
        """Benchmark packet schema validation throughput."""
        from multifleet.packets import validate_packet

        sample = {
            "type": "heartbeat",
            "from_node": "mac1",
            "to_node": "mac2",
            "payload": {"status": "ok", "uptime": 12345},
        }

        def _run():
            # Each iteration validates with a fresh ID to avoid dedup concerns
            d = dict(sample)
            d["id"] = uuid.uuid4().hex
            validate_packet(d)

        return _run_bench("packet_validation", _run, iterations)

    def bench_dedup_filter(self, iterations: int = 10000) -> BenchmarkResult:
        """Benchmark dedup filter check+mark speed."""
        from multifleet.packets import DedupTracker

        tracker = DedupTracker(max_size=50000)

        # Pre-generate IDs so uuid isn't in the hot path
        ids = [uuid.uuid4().hex for _ in range(iterations)]
        idx = 0

        def _run():
            nonlocal idx
            tracker.check_and_mark(ids[idx])
            idx += 1

        result = _run_bench("dedup_filter", _run, iterations)
        return result

    def bench_probe_execution(self, iterations: int = 10) -> BenchmarkResult:
        """Benchmark probe engine full scan."""
        from multifleet.probe_engine import ProbeEngine

        engine = ProbeEngine()

        def _run():
            engine.scan("all", {})

        return _run_bench("probe_execution", _run, iterations)

    def bench_synthesis(self, iterations: int = 100) -> BenchmarkResult:
        """Benchmark chief synthesis with 3 verdicts."""
        from multifleet.synthesis import ChiefSynthesis

        synth = ChiefSynthesis(ledger_path="/dev/null")
        verdicts = {
            "mac1": {
                "branch": "feat/a",
                "confidence": 0.9,
                "reasoning": "Tests pass, clean diff",
                "surgeon": "atlas",
            },
            "mac2": {
                "branch": "feat/a",
                "confidence": 0.7,
                "reasoning": "Looks good, minor style issues",
                "surgeon": "cardiologist",
            },
            "mac3": {
                "branch": "feat/b",
                "confidence": 0.5,
                "reasoning": "Alternative approach, less tested",
                "surgeon": "neurologist",
            },
        }

        def _run():
            synth.synthesize(f"task-{uuid.uuid4().hex[:8]}", verdicts)

        return _run_bench("synthesis", _run, iterations)

    def bench_lease_acquire_release(self, iterations: int = 1000) -> BenchmarkResult:
        """Benchmark lease acquire/release cycle."""
        import tempfile

        from multifleet.leases import LeaseManager
        from multifleet.store import FleetNerveStore

        tmp = tempfile.mkdtemp()
        store = FleetNerveStore(db_path=f"{tmp}/bench.db")
        mgr = LeaseManager(store, node_id="bench-node")
        idx = 0

        def _run():
            nonlocal idx
            rid = f"bench-resource-{idx}"
            idx += 1
            mgr.acquire(rid, "short_task")
            mgr.release(rid)

        return _run_bench("lease_acquire_release", _run, iterations)

    def bench_audit_write(self, iterations: int = 1000) -> BenchmarkResult:
        """Benchmark audit trail write throughput."""
        import tempfile

        from multifleet.audit import AuditTrail

        tmp = tempfile.mkdtemp()
        trail = AuditTrail(storage_dir=tmp, node_id="bench-node")

        def _run():
            trail.record("benchmark", "bench_tool", "target", "ok", 1.23)

        result = _run_bench("audit_write", _run, iterations)
        trail.close()
        return result

    def render_report(self) -> str:
        """Render benchmark results as formatted table."""
        if not self._results:
            return "No benchmark results. Run run_all() first."

        header = f"{'Benchmark':<25} {'Iters':>6} {'Mean':>9} {'P50':>9} {'P95':>9} {'P99':>9} {'Min':>9} {'Max':>9}"
        sep = "-" * len(header)
        lines = [sep, header, sep]
        for r in self._results:
            lines.append(
                f"{r.name:<25} {r.iterations:>6} {r.mean_ms:>8.3f}ms {r.p50_ms:>8.3f}ms "
                f"{r.p95_ms:>8.3f}ms {r.p99_ms:>8.3f}ms {r.min_ms:>8.3f}ms {r.max_ms:>8.3f}ms"
            )
        lines.append(sep)
        return "\n".join(lines)

    def to_dict(self) -> dict:
        """Serialize all results to dict."""
        return {
            "benchmarks": [r.to_dict() for r in self._results],
            "count": len(self._results),
        }
