"""ContextDNA Probe Engine — continuous code scanner feeding evidence pipeline.

Inspired by GhostScan's modular scanner architecture. Each probe:
- Has a uniform output contract (ProbeResult)
- Runs independently with configurable triggers
- Feeds into the evidence pipeline for corroboration
- Results flow to IDE via EventBus

Probe categories:
- Structure: imports, ownership, dependency edges, hotspots
- Change: recent diffs, churn, branch drift, worktree intent
- Reliability: flaky tests, repeated failures, rollback zones
- Context-quality: stale docs, mirror drift, conflicting decisions
- Security: secrets, destructive scripts, dangerous env usage
- Agent-readiness: missing setup, hidden dependencies
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
import math
import subprocess
import threading
import time
import json
import logging
import uuid

logger = logging.getLogger("fleet-nerve.probes")


class ProbeCategory(Enum):
    STRUCTURE = "structure"
    CHANGE = "change"
    RELIABILITY = "reliability"
    CONTEXT_QUALITY = "context_quality"
    SECURITY = "security"
    AGENT_READINESS = "agent_readiness"


class ProbeSeverity(Enum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class ProbeFinding:
    title: str
    evidence: list[str] = field(default_factory=list)
    blast_radius: str = "low"  # low/medium/high
    suggested_injections: list[str] = field(default_factory=list)
    file_paths: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "evidence": self.evidence,
            "blast_radius": self.blast_radius,
            "suggested_injections": self.suggested_injections,
            "file_paths": self.file_paths,
        }


@dataclass
class ProbeResult:
    probe_id: str
    category: str
    status: str  # ok/warning/critical/error/skipped
    confidence: float  # 0.0-1.0
    severity: str
    findings: list[ProbeFinding] = field(default_factory=list)
    missing_inputs: list[str] = field(default_factory=list)
    duration_ms: float = 0.0
    generated_at: str = field(
        default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    )

    def to_dict(self) -> dict:
        return {
            "probe_id": self.probe_id,
            "category": self.category,
            "status": self.status,
            "confidence": self.confidence,
            "severity": self.severity,
            "findings": [f.to_dict() for f in self.findings],
            "missing_inputs": self.missing_inputs,
            "duration_ms": round(self.duration_ms, 2),
            "generated_at": self.generated_at,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    @staticmethod
    def error(probe_id: str, category: str, message: str) -> "ProbeResult":
        """Create an error result for a failed probe."""
        return ProbeResult(
            probe_id=probe_id,
            category=category,
            status="error",
            confidence=0.0,
            severity="info",
            findings=[ProbeFinding(title=f"Probe error: {message}")],
        )

    @staticmethod
    def skipped(probe_id: str, category: str, missing: list[str]) -> "ProbeResult":
        """Create a skipped result when inputs are unavailable."""
        return ProbeResult(
            probe_id=probe_id,
            category=category,
            status="skipped",
            confidence=0.0,
            severity="info",
            missing_inputs=missing,
        )


class ProbeRegistry:
    """Central registry for all probes. GhostScan's SCANNERS pattern."""

    def __init__(self, default_timeout: float = 10.0, cache_ttl: float = 60.0):
        self._probes: dict[str, dict] = {}  # probe_id -> {fn, category, description, triggers}
        self._default_timeout = default_timeout
        self._cache_ttl = cache_ttl
        self._cache: dict[str, tuple[float, ProbeResult]] = {}  # probe_id -> (timestamp, result)
        self._cache_lock = threading.Lock()
        self._probe_health: dict[str, dict] = {}  # probe_id -> {status, last_error, last_run, consecutive_failures}

    def register(
        self,
        probe_id: str,
        fn: Callable,
        category: ProbeCategory,
        description: str = "",
        triggers: Optional[list[str]] = None,
    ) -> None:
        """Register a probe function.

        Args:
            probe_id: Unique identifier for the probe.
            fn: Callable that accepts (context: dict) and returns ProbeResult.
            category: Which bucket this probe belongs to.
            description: Human-readable description.
            triggers: List of trigger names that should run this probe
                      (e.g., "file_save", "commit", "session_end", "manual").
        """
        if probe_id in self._probes:
            logger.warning("probe_overwrite id=%s", probe_id)
        self._probes[probe_id] = {
            "fn": fn,
            "category": category,
            "description": description,
            "triggers": triggers or ["manual"],
        }
        logger.debug("probe_registered id=%s category=%s", probe_id, category.value)

    def _emit_probe_finding(self, result: ProbeResult) -> None:
        """Emit a fleet.probe.finding event to the EventBus (best-effort).

        Note: Evidence ledger recording is handled by ProbeEngine._record_high_findings()
        which filters to HIGH+ severity only, keeping the ledger signal-rich.
        """
        try:
            from multifleet.eventbus import FleetEventBus
            import os
            node_id = os.environ.get("MULTIFLEET_NODE_ID", "unknown")
            bus = FleetEventBus(node_id=node_id)
            bus.emit("probe.finding", result.to_dict())
        except Exception:
            pass  # Best-effort — never fail probe execution for event emission

    def _get_cached(self, probe_id: str) -> Optional[ProbeResult]:
        """Return cached result if still valid, else None."""
        with self._cache_lock:
            if probe_id in self._cache:
                ts, result = self._cache[probe_id]
                if (time.monotonic() - ts) < self._cache_ttl:
                    return result
                del self._cache[probe_id]
        return None

    def _set_cached(self, probe_id: str, result: ProbeResult) -> None:
        with self._cache_lock:
            self._cache[probe_id] = (time.monotonic(), result)

    def invalidate_cache(self, probe_id: Optional[str] = None) -> None:
        """Clear cache for one probe or all probes."""
        with self._cache_lock:
            if probe_id:
                self._cache.pop(probe_id, None)
            else:
                self._cache.clear()

    def _update_health(self, probe_id: str, success: bool, error_msg: str = "") -> None:
        if probe_id not in self._probe_health:
            self._probe_health[probe_id] = {
                "status": "ok", "last_error": "", "last_run": 0.0, "consecutive_failures": 0
            }
        h = self._probe_health[probe_id]
        h["last_run"] = time.time()
        if success:
            h["status"] = "ok"
            h["consecutive_failures"] = 0
            h["last_error"] = ""
        else:
            h["consecutive_failures"] += 1
            h["last_error"] = error_msg
            h["status"] = "broken" if h["consecutive_failures"] >= 3 else "degraded"

    def get_health(self) -> dict[str, dict]:
        """Return health status for all registered probes."""
        result = {}
        for pid in self._probes:
            if pid in self._probe_health:
                result[pid] = dict(self._probe_health[pid])
            else:
                result[pid] = {"status": "unknown", "last_error": "", "last_run": 0.0, "consecutive_failures": 0}
        return result

    def _run_probe(self, probe_id: str, entry: dict, context: dict,
                   timeout: Optional[float] = None, use_cache: bool = True) -> ProbeResult:
        """Execute a single probe with timing, timeout, caching, and error isolation."""
        # Check cache first
        if use_cache:
            cached = self._get_cached(probe_id)
            if cached is not None:
                logger.debug("probe_cache_hit id=%s", probe_id)
                return cached

        effective_timeout = timeout if timeout is not None else self._default_timeout
        t0 = time.monotonic()

        try:
            # Run probe in a thread to enforce timeout
            result_holder: list[ProbeResult] = []
            error_holder: list[Exception] = []

            def _execute():
                try:
                    r = entry["fn"](context)
                    result_holder.append(r)
                except Exception as exc:
                    error_holder.append(exc)

            thread = threading.Thread(target=_execute, daemon=True)
            thread.start()
            thread.join(timeout=effective_timeout)

            if thread.is_alive():
                # Probe timed out
                logger.warning("probe_timeout id=%s timeout=%.1fs", probe_id, effective_timeout)
                result = ProbeResult.error(
                    probe_id, entry["category"].value,
                    f"Probe timed out after {effective_timeout:.1f}s"
                )
                result.duration_ms = (time.monotonic() - t0) * 1000
                self._update_health(probe_id, False, f"timeout after {effective_timeout:.1f}s")
                self._emit_probe_finding(result)
                return result

            if error_holder:
                raise error_holder[0]

            if not result_holder:
                raise RuntimeError("Probe returned no result")

            result = result_holder[0]
            if not isinstance(result, ProbeResult):
                result = ProbeResult.error(
                    probe_id, entry["category"].value,
                    f"Probe returned {type(result).__name__}, expected ProbeResult"
                )
                self._update_health(probe_id, False, "bad return type")
            else:
                self._update_health(probe_id, True)

            result.duration_ms = (time.monotonic() - t0) * 1000
            self._set_cached(probe_id, result)
            self._emit_probe_finding(result)
            return result

        except Exception as e:
            logger.error("probe_error id=%s error=%s", probe_id, e)
            result = ProbeResult.error(probe_id, entry["category"].value, str(e))
            result.duration_ms = (time.monotonic() - t0) * 1000
            self._update_health(probe_id, False, str(e))
            self._emit_probe_finding(result)
            return result

    def run_all(self, context: Optional[dict] = None, concurrent: bool = True,
                max_workers: int = 8) -> list[ProbeResult]:
        """Run every registered probe and return results.

        If no context is provided, auto-builds one from live git state
        via probe_context_factory.

        Args:
            concurrent: If True, run independent probes in parallel via ThreadPoolExecutor.
            max_workers: Max concurrent probe threads (default 8).
        """
        if context is None:
            try:
                from multifleet.probe_context_factory import build_probe_context
                ctx = build_probe_context().to_dict()
            except Exception:
                ctx = {}
        else:
            ctx = context

        if not concurrent or len(self._probes) <= 1:
            return [self._run_probe(pid, entry, ctx) for pid, entry in self._probes.items()]

        results: list[ProbeResult] = []
        with ThreadPoolExecutor(max_workers=min(max_workers, len(self._probes))) as pool:
            futures = {
                pool.submit(self._run_probe, pid, entry, ctx): pid
                for pid, entry in self._probes.items()
            }
            for future in as_completed(futures):
                pid = futures[future]
                try:
                    results.append(future.result())
                except Exception as e:
                    logger.error("probe_future_error id=%s error=%s", pid, e)
                    entry = self._probes[pid]
                    results.append(ProbeResult.error(pid, entry["category"].value, str(e)))
        return results

    def run_category(
        self, category: ProbeCategory, context: Optional[dict] = None
    ) -> list[ProbeResult]:
        """Run all probes in a specific category."""
        ctx = context or {}
        results = []
        for probe_id, entry in self._probes.items():
            if entry["category"] == category:
                results.append(self._run_probe(probe_id, entry, ctx))
        return results

    def run_one(self, probe_id: str, context: Optional[dict] = None) -> ProbeResult:
        """Run a single probe by ID."""
        import re
        if not re.match(r'^[a-z][a-z0-9_]{0,63}$', probe_id):
            return ProbeResult.error(probe_id, "unknown", "Invalid probe_id format")
        entry = self._probes.get(probe_id)
        if not entry:
            return ProbeResult.error(probe_id, "unknown", f"Probe '{probe_id}' not registered")
        return self._run_probe(probe_id, entry, context or {})

    def run_by_trigger(self, trigger: str, context: Optional[dict] = None) -> list[ProbeResult]:
        """Run all probes that match a given trigger."""
        ctx = context or {}
        results = []
        for probe_id, entry in self._probes.items():
            if trigger in entry["triggers"]:
                results.append(self._run_probe(probe_id, entry, ctx))
        return results

    def list_probes(self) -> list[dict]:
        """Return metadata for all registered probes."""
        return [
            {
                "probe_id": pid,
                "category": entry["category"].value,
                "description": entry["description"],
                "triggers": entry["triggers"],
            }
            for pid, entry in self._probes.items()
        ]


class SignalScorer:
    """Ranks and filters probe findings by priority.

    Score = confidence * severity_weight * recurrence_bonus * freshness_decay
    """

    SEVERITY_WEIGHTS = {
        "critical": 1.0, "high": 0.8, "medium": 0.5, "low": 0.2, "info": 0.1
    }

    def __init__(self, evidence_ledger=None):
        self._ledger = evidence_ledger

    def score_results(self, results: list[ProbeResult]) -> list[tuple[float, ProbeResult]]:
        """Score and rank probe results by priority. Returns [(score, result)]."""
        scored = []
        for result in results:
            base = result.confidence * self.SEVERITY_WEIGHTS.get(result.severity, 0.3)
            recurrence = self._recurrence_bonus(result.probe_id)
            freshness = self._freshness_decay(result)
            scored.append((base * recurrence * freshness, result))
        return sorted(scored, key=lambda x: -x[0])

    def top_n(self, results: list[ProbeResult], n: int = 5) -> list[ProbeResult]:
        """Return top N highest-priority results."""
        ranked = self.score_results(results)
        return [r for _, r in ranked[:n]]

    def _recurrence_bonus(self, probe_id: str) -> float:
        """Bonus for recurring findings (same probe flagging repeatedly)."""
        if not self._ledger:
            return 1.0
        try:
            count = self._ledger.count_by_probe(probe_id)
            # Logarithmic bonus: 1 occurrence = 1.0, 5 = ~1.6, 20 = ~2.0
            return 1.0 + math.log1p(count) * 0.3
        except Exception:
            return 1.0

    def _freshness_decay(self, result: ProbeResult) -> float:
        """Decay score for older results. Recent = 1.0, >1h old = decays."""
        try:
            generated = time.mktime(time.strptime(result.generated_at, "%Y-%m-%dT%H:%M:%SZ"))
            age_hours = (time.time() - generated) / 3600
            if age_hours <= 0:
                return 1.0
            # Half-life of 6 hours
            return math.exp(-0.1155 * age_hours)
        except Exception:
            return 1.0

    def filter_noise(self, results: list[ProbeResult], threshold: float = 0.3) -> list[ProbeResult]:
        """Remove results below scoring threshold."""
        scored = self.score_results(results)
        return [r for score, r in scored if score >= threshold]


# ── File-pattern to probe mapping for incremental triggers ──

def _match_ext(ext: str) -> Callable[[str], bool]:
    return lambda f: f.endswith(ext)


def _match_name(name: str) -> Callable[[str], bool]:
    return lambda f: f.endswith("/" + name) or f == name


def _match_contains(fragment: str) -> Callable[[str], bool]:
    return lambda f: fragment in f


FILE_PROBE_MAP: list[tuple[Callable[[str], bool], list[str]]] = [
    (_match_ext(".py"), ["structure_imports", "agent_readiness"]),
    (_match_name("pyproject.toml"), ["dependency_drift"]),
    (_match_name("CLAUDE.md"), ["mirror_drift"]),
    (_match_contains("test"), ["reliability_flaky", "reliability_repeated"]),
    (_match_ext(".toml"), ["dependency_drift"]),
    (_match_ext(".yaml"), ["context_quality"]),
    (_match_ext(".yml"), ["context_quality"]),
    (_match_ext(".json"), ["context_quality"]),
    (_match_ext(".md"), ["mirror_drift"]),
    (_match_ext(".sh"), ["security_scripts"]),
]


class IncrementalTrigger:
    """Triggers probes based on git diff — only scan what changed."""

    def __init__(self, repo_path: str = "."):
        self.repo_path = repo_path

    def changed_files(self, since: str = "HEAD~1") -> list[str]:
        """Get files changed since a reference."""
        try:
            result = subprocess.run(
                ["git", "diff", "--name-only", since],
                capture_output=True, text=True, timeout=10,
                cwd=self.repo_path,
            )
            if result.returncode != 0:
                logger.warning("git diff failed: %s", result.stderr.strip())
                return []
            return [f for f in result.stdout.strip().split("\n") if f]
        except Exception as e:
            logger.error("incremental_trigger git_error=%s", e)
            return []

    def map_to_probes(self, files: list[str], registry: ProbeRegistry) -> list[str]:
        """Map changed files to relevant probe IDs.

        Uses FILE_PROBE_MAP patterns, then intersects with registered probes.
        """
        candidate_ids: set[str] = set()
        for filepath in files:
            for matcher, probe_ids in FILE_PROBE_MAP:
                if matcher(filepath):
                    candidate_ids.update(probe_ids)

        # Only return IDs that are actually registered
        registered_ids = {p["probe_id"] for p in registry.list_probes()}
        return sorted(candidate_ids & registered_ids)

    def scan_incremental(self, engine: "ProbeEngine", since: str = "HEAD~1") -> list[ProbeResult]:
        """Run only probes relevant to changed files."""
        files = self.changed_files(since)
        if not files:
            logger.info("incremental_trigger no_changes since=%s", since)
            return []
        probe_ids = self.map_to_probes(files, engine.registry)
        if not probe_ids:
            logger.info("incremental_trigger no_matching_probes files=%d", len(files))
            return []
        context = {"changed_files": files, "since": since}
        results = []
        for pid in probe_ids:
            results.append(engine.registry.run_one(pid, context))
        return results


class ProbeEngine:
    """Orchestrates probe execution and result routing."""

    def __init__(self, registry: Optional[ProbeRegistry] = None, repo_path: str = "."):
        self.registry = registry or ProbeRegistry()
        self._repo_path = repo_path
        self._results_cache: list[ProbeResult] = []
        self._last_scan_trigger: str = ""
        self._last_scan_at: float = 0.0

    def scan(
        self, trigger: str = "manual", context: Optional[dict] = None
    ) -> list[ProbeResult]:
        """Run probes matching the trigger and cache results.

        Args:
            trigger: "manual", "all", "incremental", or a registered trigger name.
                     "incremental" uses git diff to run only relevant probes.
            context: Optional dict passed to probe functions. For incremental,
                     may include "since" key (default "HEAD~1").
        """
        self._last_scan_trigger = trigger
        self._last_scan_at = time.time()

        if trigger == "incremental":
            since = (context or {}).get("since", "HEAD~1")
            inc = IncrementalTrigger(self._repo_path)
            probe_ids = inc.map_to_probes(inc.changed_files(since), self.registry)
            results = [self.registry.run_one(pid, context) for pid in probe_ids]
        elif trigger == "all":
            results = self.registry.run_all(context)
        else:
            results = self.registry.run_by_trigger(trigger, context)

        # Score and rank results
        scorer = SignalScorer()
        results = scorer.top_n(results, n=len(results))

        # Distributed dispatch: merge remote peer results when NATS/daemon is available
        remote_results = self._dispatch_to_peers(trigger, context)
        if remote_results:
            results = results + remote_results
            logger.info(
                "distributed_merge trigger=%s remote_probes=%d",
                trigger,
                len(remote_results),
            )

        self._results_cache = results
        logger.info(
            "scan_complete trigger=%s probes=%d findings=%d",
            trigger,
            len(results),
            sum(len(r.findings) for r in results),
        )

        # Record HIGH+ findings to evidence ledger (probe → evidence pipeline)
        self._record_high_findings(results)

        return results

    def scan_category(
        self, category: str, context: Optional[dict] = None
    ) -> list[ProbeResult]:
        """Run all probes in a category and cache results."""
        try:
            cat = ProbeCategory(category)
        except ValueError:
            return [ProbeResult.error("engine", "unknown", f"Unknown category: {category}")]

        results = self.registry.run_category(cat, context)
        self._results_cache = results
        return results

    def scan_one(self, probe_id: str, context: Optional[dict] = None) -> ProbeResult:
        """Run a single probe by ID."""
        result = self.registry.run_one(probe_id, context)
        # Append to cache (don't replace — single probe scan is additive)
        self._results_cache.append(result)
        return result

    def latest_results(self) -> list[ProbeResult]:
        """Return the most recent scan results."""
        return list(self._results_cache)

    def findings_summary(self) -> dict:
        """Aggregate findings across latest results into a summary."""
        by_severity: dict[str, int] = {}
        by_category: dict[str, int] = {}
        by_status: dict[str, int] = {}
        total_findings = 0

        for r in self._results_cache:
            by_status[r.status] = by_status.get(r.status, 0) + 1
            by_category[r.category] = by_category.get(r.category, 0) + len(r.findings)
            for _ in r.findings:
                by_severity[r.severity] = by_severity.get(r.severity, 0) + 1
                total_findings += 1

        return {
            "total_probes": len(self._results_cache),
            "total_findings": total_findings,
            "by_severity": by_severity,
            "by_category": by_category,
            "by_status": by_status,
            "surgeon_intensity": self.determine_surgeon_intensity(),
            "has_blocking": any(
                f.blast_radius == "high" for r in self._results_cache for f in r.findings
            ),
            "last_trigger": self._last_scan_trigger,
            "last_scan_at": (
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._last_scan_at))
                if self._last_scan_at
                else None
            ),
        }

    def determine_surgeon_intensity(self) -> str:
        """Map probe results to 3-Surgeons intensity level.

        Returns:
            "full" — blocking findings or critical status
            "standard" — high severity non-blocking findings
            "light" — all clear or low/medium only
        """
        has_critical = any(r.status == "critical" for r in self._results_cache)
        has_high_blast = any(
            f.blast_radius == "high"
            for r in self._results_cache
            for f in r.findings
        )
        has_high_severity = any(r.severity in ("high", "critical") for r in self._results_cache)

        if has_critical or has_high_blast:
            return "full"
        if has_high_severity:
            return "standard"
        return "light"

    def _dispatch_to_peers(
        self, trigger: str, context: Optional[dict]
    ) -> list[ProbeResult]:
        """Dispatch probe scan to fleet peers via NATS/daemon and return merged results.

        Non-blocking: returns [] if daemon is unreachable or NATS is unavailable.
        Each peer's probe dicts are deserialized back into ProbeResult objects so
        they can be ranked and recorded alongside local results.
        """
        try:
            import os
            from multifleet.probes.distributed import DistributedProbeRunner

            node_id = os.environ.get("MULTIFLEET_NODE_ID", "unknown")
            runner = DistributedProbeRunner(daemon_port=8855, node_id=node_id)

            # Quick daemon reachability check — bail out fast if not running
            health = runner._daemon_get("/health", timeout=2)
            if not health:
                return []

            peers = {
                k: v for k, v in health.get("peers", {}).items()
                if not k.startswith("test")
            }
            if not peers:
                return []

            remote: list[ProbeResult] = []
            for peer in peers:
                peer_data = runner._request_peer_probes(peer, timeout_s=10)
                if not peer_data:
                    continue
                # Deserialize peer probe dicts → ProbeResult objects
                for probe_dict in peer_data.get("probes", []):
                    try:
                        findings = [
                            ProbeFinding(
                                title=f.get("title", ""),
                                evidence=f.get("evidence", []),
                                blast_radius=f.get("blast_radius", "low"),
                                suggested_injections=f.get("suggested_injections", []),
                                file_paths=f.get("file_paths", []),
                            )
                            for f in probe_dict.get("findings", [])
                        ]
                        pr = ProbeResult(
                            probe_id=f"{peer}:{probe_dict.get('probe_id', 'unknown')}",
                            category=probe_dict.get("category", "unknown"),
                            status=probe_dict.get("status", "unknown"),
                            confidence=float(probe_dict.get("confidence", 0.0)),
                            severity=probe_dict.get("severity", "info"),
                            findings=findings,
                            missing_inputs=probe_dict.get("missing_inputs", []),
                            duration_ms=float(probe_dict.get("duration_ms", 0.0)),
                            generated_at=probe_dict.get(
                                "generated_at",
                                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            ),
                        )
                        remote.append(pr)
                    except Exception as e:
                        logger.debug("distributed_probe_deserialize peer=%s error=%s", peer, e)

            return remote

        except ImportError:
            logger.debug("distributed_probes unavailable — running local-only")
            return []
        except Exception as e:
            logger.debug("distributed_dispatch non_blocking error=%s", e)
            return []

    def _record_high_findings(self, results: list[ProbeResult]) -> None:
        """Record HIGH/CRITICAL probe findings to the evidence ledger.

        Only findings at severity HIGH or CRITICAL with actual findings
        are recorded, keeping the ledger signal-rich.
        """
        high_count = 0
        try:
            from multifleet.evidence_ledger import EvidenceLedger
            import os
            ledger = EvidenceLedger()
            for result in results:
                if result.severity in ("high", "critical") and result.findings:
                    high_count += 1
                    ledger.record(
                        event_type="probe_finding",
                        node_id=os.environ.get("MULTIFLEET_NODE_ID", "unknown"),
                        subject=f"probe:{result.probe_id} severity:{result.severity}",
                        payload={
                            "probe_id": result.probe_id,
                            "severity": result.severity,
                            "findings_count": len(result.findings),
                            "confidence": result.confidence,
                            "category": result.category,
                            "finding_titles": [f.title for f in result.findings],
                        },
                    )
            logger.debug("Evidence ledger: recorded %d HIGH+ probe findings", high_count)
        except Exception as e:
            logger.debug("Evidence ledger recording failed: %s", e)

        # Push HIGH+ findings to Context DNA (bidirectional sync — closes learning loop)
        if high_count > 0:
            self._push_high_findings_to_contextdna(results)

    def _push_high_findings_to_contextdna(self, results: list[ProbeResult]) -> None:
        """Push HIGH/CRITICAL probe findings to Context DNA via ProductBridge."""
        try:
            from multifleet.product_bridge import get_bridge
            import os
            bridge = get_bridge()
            high_findings = [
                r for r in results
                if r.severity in ("high", "critical") and r.findings
            ]
            bridge.fleet_to_memory({
                "event_type": "probe_high_findings",
                "node_id": os.environ.get("MULTIFLEET_NODE_ID", "unknown"),
                "evidence": {
                    "findings_count": len(high_findings),
                    "probes": [
                        {
                            "probe_id": r.probe_id,
                            "severity": r.severity,
                            "category": r.category,
                            "confidence": r.confidence,
                            "finding_titles": [f.title for f in r.findings[:5]],
                        }
                        for r in high_findings[:10]
                    ],
                },
                "tags": ["probe", "high_findings", "learning_loop"],
            })
        except Exception as e:
            logger.debug("probe->contextdna bridge (non-blocking): %s", e)

    def pull_contextdna_evidence(self, query: str) -> list[dict]:
        """Pull relevant evidence from Context DNA for probe enrichment.

        Used by probes that want historical context before scanning.
        Returns list of evidence entries or empty list on failure.
        """
        try:
            from multifleet.product_bridge import get_bridge
            bridge = get_bridge()
            result = bridge.memory_to_fleet(query)
            return result.get("results", [])
        except Exception as e:
            logger.debug("contextdna->probe pull (non-blocking): %s", e)
            return []

    def health(self) -> dict:
        """Return engine health: which probes are working vs broken.

        Returns dict with:
            probes: per-probe health status
            summary: {total, ok, degraded, broken, unknown}
            engine_status: "healthy" | "degraded" | "unhealthy"
        """
        probe_health = self.registry.get_health()
        counts = {"total": 0, "ok": 0, "degraded": 0, "broken": 0, "unknown": 0}
        for info in probe_health.values():
            counts["total"] += 1
            status = info.get("status", "unknown")
            if status in counts:
                counts[status] += 1
            else:
                counts["unknown"] += 1

        if counts["broken"] > 0:
            engine_status = "unhealthy"
        elif counts["degraded"] > 0:
            engine_status = "degraded"
        else:
            engine_status = "healthy"

        return {
            "probes": probe_health,
            "summary": counts,
            "engine_status": engine_status,
            "last_scan_at": self._last_scan_at,
            "last_trigger": self._last_scan_trigger,
        }

    def blocking_findings(self) -> list[ProbeFinding]:
        """Return all findings with high blast radius (blocking)."""
        return [
            f
            for r in self._results_cache
            for f in r.findings
            if f.blast_radius == "high"
        ]
