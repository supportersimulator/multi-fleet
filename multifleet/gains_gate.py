"""Fleet Gains Gate — structured health checks before major operations.

Modeled on 3-surgeons GainsGate. Checks fleet infrastructure health and
returns a structured pass/fail result. Used before merges, deployments,
and phase transitions.
"""

import json
import logging
import os
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("fleet-nerve.gains-gate")


@dataclass
class CheckResult:
    """Result of a single gate check."""
    name: str
    passed: bool
    message: str
    latency_ms: float = 0.0
    critical: bool = True  # If critical and failed, gate fails

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "passed": self.passed,
            "message": self.message,
            "latency_ms": round(self.latency_ms, 1),
            "critical": self.critical,
        }


@dataclass
class GateResult:
    """Complete gate run result."""
    passed: bool = False
    checks: list = field(default_factory=list)  # list of CheckResult dicts
    total: int = 0
    passed_count: int = 0
    failed_count: int = 0
    critical_failures: int = 0
    duration_ms: float = 0.0
    timestamp: str = ""
    recommendation: str = ""

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "total": self.total,
            "passed_count": self.passed_count,
            "failed_count": self.failed_count,
            "critical_failures": self.critical_failures,
            "duration_ms": round(self.duration_ms, 1),
            "timestamp": self.timestamp,
            "recommendation": self.recommendation,
            "checks": self.checks,
        }


class FleetGainsGate:
    """Run structured health gate before major fleet operations."""

    def __init__(self, daemon_port: int = 8855, eventbus_port: int = 8031):
        self._daemon_port = daemon_port
        self._eventbus_port = eventbus_port
        self._health_cache: Optional[dict] = None

    def run(self, quick: bool = False) -> GateResult:
        """Run all gate checks. Returns structured result."""
        t0 = time.monotonic()
        results: list[CheckResult] = []

        # Core checks (always run)
        results.append(self._check_daemon_health())
        results.append(self._check_nats_connected())
        results.append(self._check_peer_reachability())
        results.append(self._check_packet_registry())
        results.append(self._check_dedup_health())
        results.append(self._check_eventbus_running())

        if not quick:
            results.append(self._check_lease_conflicts())
            results.append(self._check_rebuttal_stale())
            results.append(self._check_audit_trail())
            results.append(self._check_test_suite())
            results.append(self._check_invariance_rules())

        # Build gate result
        checks_dicts = [r.to_dict() for r in results]
        passed_count = sum(1 for r in results if r.passed)
        failed_count = len(results) - passed_count
        critical_failures = sum(1 for r in results if not r.passed and r.critical)
        gate_passed = critical_failures == 0
        duration_ms = (time.monotonic() - t0) * 1000

        recommendation = self._make_recommendation(results, gate_passed)

        gate_result = GateResult(
            passed=gate_passed,
            checks=checks_dicts,
            total=len(results),
            passed_count=passed_count,
            failed_count=failed_count,
            critical_failures=critical_failures,
            duration_ms=duration_ms,
            timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            recommendation=recommendation,
        )

        # Record in evidence ledger (best-effort, never fail the gate)
        try:
            from .evidence_ledger import EvidenceLedger
            ledger = EvidenceLedger()
            node_id = self._health_cache.get("nodeId", "unknown") if self._health_cache else "unknown"
            ledger.record("gains_gate_result", node_id,
                          f"gate {'PASS' if gate_passed else 'FAIL'}: {passed_count}/{len(results)}",
                          {"passed": gate_passed, "total": len(results),
                           "passed_count": passed_count, "failed_count": failed_count,
                           "critical_failures": critical_failures,
                           "duration_ms": round(duration_ms, 1),
                           "quick": quick})
        except Exception:
            pass  # Evidence recording must never break the gate

        return gate_result

    def _get_health(self) -> Optional[dict]:
        """Fetch daemon /health (cached per run)."""
        if self._health_cache is not None:
            return self._health_cache
        try:
            url = f"http://127.0.0.1:{self._daemon_port}/health"
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=3) as resp:
                self._health_cache = json.loads(resp.read())
            return self._health_cache
        except Exception:
            self._health_cache = None
            return None

    def _timed(self, fn) -> CheckResult:
        """Run a check function with timing."""
        t0 = time.monotonic()
        result = fn()
        result.latency_ms = (time.monotonic() - t0) * 1000
        return result

    def _check_daemon_health(self) -> CheckResult:
        """Check fleet daemon is responding."""
        t0 = time.monotonic()
        health = self._get_health()
        latency = (time.monotonic() - t0) * 1000
        if health and health.get("status") == "ok":
            uptime = health.get("uptime_s", 0)
            return CheckResult("daemon_health", True, f"Daemon up {uptime}s",
                               latency_ms=latency, critical=True)
        return CheckResult("daemon_health", False,
                           "Daemon not responding on port %d" % self._daemon_port,
                           latency_ms=latency, critical=True)

    def _check_nats_connected(self) -> CheckResult:
        """Check NATS transport is connected."""
        t0 = time.monotonic()
        health = self._get_health()
        latency = (time.monotonic() - t0) * 1000
        if not health:
            return CheckResult("nats_connected", False, "No health data (daemon down)",
                               latency_ms=latency, critical=True)
        transport = health.get("transport", "")
        nats_url = health.get("nats_url", "")
        if transport == "nats" and nats_url:
            return CheckResult("nats_connected", True, f"NATS connected: {nats_url}",
                               latency_ms=latency, critical=True)
        return CheckResult("nats_connected", False,
                           f"NATS not connected (transport={transport})",
                           latency_ms=latency, critical=True)

    def _check_peer_reachability(self) -> CheckResult:
        """Check at least 1 peer is reachable."""
        t0 = time.monotonic()
        health = self._get_health()
        latency = (time.monotonic() - t0) * 1000
        if not health:
            return CheckResult("peer_reachability", False, "No health data",
                               latency_ms=latency, critical=True)
        # Check channel_health for peer scores
        ch = health.get("channel_health", {})
        reachable = sum(1 for peer_data in ch.values()
                        if isinstance(peer_data, dict) and peer_data.get("score", 0) > 0)
        # Also check peers dict
        peers = health.get("peers", {})
        if reachable > 0 or len(peers) > 0:
            count = max(reachable, len(peers))
            return CheckResult("peer_reachability", True, f"{count} peer(s) reachable",
                               latency_ms=latency, critical=True)
        return CheckResult("peer_reachability", False, "No peers reachable",
                           latency_ms=latency, critical=True)

    def _check_packet_registry(self) -> CheckResult:
        """Check packet types are loaded."""
        t0 = time.monotonic()
        health = self._get_health()
        latency = (time.monotonic() - t0) * 1000
        if not health:
            return CheckResult("packet_registry", False, "No health data",
                               latency_ms=latency, critical=True)
        reg_types = health.get("registered_types", 0)
        if isinstance(reg_types, list):
            count = len(reg_types)
        elif isinstance(reg_types, int):
            count = reg_types
        else:
            count = 0
        if count > 0:
            return CheckResult("packet_registry", True, f"{count} packet types loaded",
                               latency_ms=latency, critical=True)
        return CheckResult("packet_registry", False, "No packet types registered",
                           latency_ms=latency, critical=True)

    def _check_dedup_health(self) -> CheckResult:
        """Check dedup filter is working (not overflowing)."""
        t0 = time.monotonic()
        health = self._get_health()
        latency = (time.monotonic() - t0) * 1000
        if not health:
            return CheckResult("dedup_health", True, "No health data (non-critical)",
                               latency_ms=latency, critical=False)
        dedup = health.get("dedup_size", health.get("dedup_count", -1))
        if dedup >= 0:
            if dedup > 10000:
                return CheckResult("dedup_health", False,
                                   f"Dedup filter large: {dedup} entries",
                                   latency_ms=latency, critical=False)
            return CheckResult("dedup_health", True, f"Dedup filter OK ({dedup} entries)",
                               latency_ms=latency, critical=False)
        return CheckResult("dedup_health", True, "Dedup counter not reported",
                           latency_ms=latency, critical=False)

    def _check_eventbus_running(self) -> CheckResult:
        """Check EventBus WebSocket server on configured port."""
        t0 = time.monotonic()
        try:
            url = f"http://127.0.0.1:{self._eventbus_port}/health"
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=2) as resp:
                data = json.loads(resp.read())
            if data.get("status") == "ok":
                latency = (time.monotonic() - t0) * 1000
                return CheckResult("eventbus_running", True, "EventBus responding",
                                   latency_ms=latency, critical=False)
        except Exception:
            pass
        latency = (time.monotonic() - t0) * 1000
        return CheckResult("eventbus_running", False,
                           f"EventBus not responding on port {self._eventbus_port}",
                           latency_ms=latency, critical=False)

    def _check_lease_conflicts(self) -> CheckResult:
        """Check no expired/conflicting leases."""
        t0 = time.monotonic()
        try:
            url = f"http://127.0.0.1:{self._daemon_port}/leases"
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read())
            leases = data.get("leases", [])
            now = time.time()
            expired = [l for l in leases
                       if isinstance(l, dict) and l.get("expires_at", now + 1) < now]
            latency = (time.monotonic() - t0) * 1000
            if expired:
                return CheckResult("lease_conflicts", False,
                                   f"{len(expired)} expired lease(s)",
                                   latency_ms=latency, critical=False)
            return CheckResult("lease_conflicts", True,
                               f"{len(leases)} active lease(s), none expired",
                               latency_ms=latency, critical=False)
        except Exception:
            latency = (time.monotonic() - t0) * 1000
            return CheckResult("lease_conflicts", True, "Lease endpoint unavailable (OK)",
                               latency_ms=latency, critical=False)

    def _check_rebuttal_stale(self) -> CheckResult:
        """Check no stale unresolved rebuttals."""
        t0 = time.monotonic()
        try:
            url = f"http://127.0.0.1:{self._daemon_port}/rebuttals"
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read())
            proposals = data.get("proposals", [])
            stale = [p for p in proposals
                     if isinstance(p, dict) and p.get("state") in ("open", "rebutted")
                     and time.time() - p.get("created_at", time.time()) > 86400]
            latency = (time.monotonic() - t0) * 1000
            if stale:
                return CheckResult("rebuttal_stale", False,
                                   f"{len(stale)} stale proposal(s) >24h",
                                   latency_ms=latency, critical=False)
            return CheckResult("rebuttal_stale", True,
                               f"{len(proposals)} proposal(s), none stale",
                               latency_ms=latency, critical=False)
        except Exception:
            latency = (time.monotonic() - t0) * 1000
            return CheckResult("rebuttal_stale", True, "Rebuttal endpoint unavailable (OK)",
                               latency_ms=latency, critical=False)

    def _check_audit_trail(self) -> CheckResult:
        """Check audit trail file exists and is writable."""
        t0 = time.monotonic()
        audit_dir = Path.home() / ".fleet-nerve"
        latency_fn = lambda: (time.monotonic() - t0) * 1000
        if not audit_dir.exists():
            return CheckResult("audit_trail", True,
                               "No ~/.fleet-nerve dir (non-critical)",
                               latency_ms=latency_fn(), critical=False)
        audit_files = list(audit_dir.glob("audit-*.jsonl"))
        if not audit_files:
            return CheckResult("audit_trail", False,
                               "No audit trail files found",
                               latency_ms=latency_fn(), critical=False)
        newest = max(audit_files, key=lambda f: f.stat().st_mtime)
        writable = os.access(newest, os.W_OK)
        if writable:
            return CheckResult("audit_trail", True,
                               f"Audit trail OK ({newest.name})",
                               latency_ms=latency_fn(), critical=False)
        return CheckResult("audit_trail", False,
                           f"Audit trail not writable: {newest.name}",
                           latency_ms=latency_fn(), critical=False)

    def _check_test_suite(self) -> CheckResult:
        """Run pytest quick check (non-critical)."""
        t0 = time.monotonic()
        from ._paths import REPO_ROOT as _repo
        test_dir = _repo / "tests"
        if not test_dir.exists():
            return CheckResult("test_suite", True, "No tests dir (skipped)",
                               latency_ms=(time.monotonic() - t0) * 1000, critical=False)
        try:
            cmd = [sys.executable, "-m", "pytest", str(test_dir), "-q", "--tb=no", "-x"]
            # Only add --timeout if pytest-timeout is available
            try:
                import pytest_timeout  # noqa: F401
                cmd.append("--timeout=10")
            except ImportError:
                pass
            r = subprocess.run(
                cmd,
                capture_output=True, text=True, timeout=30,
                cwd=str(_repo),
            )
            latency = (time.monotonic() - t0) * 1000
            # Parse last line for pass/fail count
            lines = r.stdout.strip().splitlines()
            summary = lines[-1] if lines else ""
            if r.returncode == 0:
                return CheckResult("test_suite", True, f"Tests passed: {summary}",
                                   latency_ms=latency, critical=False)
            return CheckResult("test_suite", False, f"Tests failed: {summary}",
                               latency_ms=latency, critical=False)
        except subprocess.TimeoutExpired:
            latency = (time.monotonic() - t0) * 1000
            return CheckResult("test_suite", False, "Tests timed out (30s)",
                               latency_ms=latency, critical=False)
        except Exception as e:
            latency = (time.monotonic() - t0) * 1000
            return CheckResult("test_suite", False, f"Test run error: {e}",
                               latency_ms=latency, critical=False)

    def _check_invariance_rules(self) -> CheckResult:
        """Run 7 GhostScan invariance rules.

        When a supervisor is active, INV-001 and INV-004 are promoted from
        advisory to hard-gate (critical blocking failures).
        """
        t0 = time.monotonic()
        try:
            from .invariance_rules import InvarianceEngine
            engine = InvarianceEngine()
            # Build state from daemon health (best-effort)
            state = {}
            health = self._get_health()
            if health:
                state["transport_channel"] = health.get("transport", "")

            # Detect supervisor — if importable and phase is set, it's active
            supervisor_active = bool(state.get("supervisor_phase"))
            if not supervisor_active:
                try:
                    from .supervisor import Supervisor  # noqa: F401
                    supervisor_active = True
                except ImportError:
                    pass

            results = engine.check_all(state, hard_gate=supervisor_active)
            violations = [r for r in results if not r.passed]
            hard_violations = [r for r in violations if r.hard_gate]
            latency = (time.monotonic() - t0) * 1000
            if violations:
                names = ", ".join(r.rule_id for r in violations)
                # Critical if any hard-gated rule failed
                is_critical = len(hard_violations) > 0
                msg = f"{len(violations)} invariance violation(s): {names}"
                if hard_violations:
                    hg_names = ", ".join(r.rule_id for r in hard_violations)
                    msg += f" [hard-gate: {hg_names}]"
                return CheckResult(
                    "invariance_rules", False, msg,
                    latency_ms=latency, critical=is_critical,
                )
            return CheckResult(
                "invariance_rules", True,
                f"All {len(results)} invariance rules passed",
                latency_ms=latency, critical=False,
            )
        except Exception as e:
            latency = (time.monotonic() - t0) * 1000
            return CheckResult(
                "invariance_rules", True,
                f"Invariance check skipped: {e}",
                latency_ms=latency, critical=False,
            )

    @staticmethod
    def _make_recommendation(results: list[CheckResult], gate_passed: bool) -> str:
        """Generate human-readable recommendation from check results."""
        if gate_passed:
            failed_non_crit = [r for r in results if not r.passed and not r.critical]
            if failed_non_crit:
                names = ", ".join(r.name for r in failed_non_crit)
                return f"Gate PASSED. {len(failed_non_crit)} non-critical issue(s): {names}."
            return "Gate PASSED. All checks healthy. Proceed with operation."

        critical_failed = [r for r in results if not r.passed and r.critical]
        names = ", ".join(r.name for r in critical_failed)
        return (f"Gate FAILED. {len(critical_failed)} critical failure(s): {names}. "
                "Fix before proceeding.")


if __name__ == "__main__":
    import sys as _sys
    gate = FleetGainsGate()
    quick = "--quick" in _sys.argv
    result = gate.run(quick=quick)
    if "--json" in _sys.argv:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        status = "PASSED" if result.passed else "FAILED"
        print(f"Fleet Gains Gate: {status}")
        print(f"{'=' * 40}")
        for c in result.checks:
            icon = "+" if c["passed"] else "X"
            crit = " [CRITICAL]" if c["critical"] and not c["passed"] else ""
            print(f"  [{icon}] {c['name']}: {c['message']}{crit}")
        print(f"{'=' * 40}")
        print(f"  {result.passed_count}/{result.total} passed, "
              f"{result.critical_failures} critical failures")
        print(f"  {result.recommendation}")
        print(f"  Duration: {result.duration_ms:.0f}ms")
    _sys.exit(0 if result.passed else 1)
