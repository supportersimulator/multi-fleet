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

    def __init__(self):
        self._probes: dict[str, dict] = {}  # probe_id -> {fn, category, description, triggers}

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

    def _run_probe(self, probe_id: str, entry: dict, context: dict) -> ProbeResult:
        """Execute a single probe with timing and error handling."""
        t0 = time.monotonic()
        try:
            result = entry["fn"](context)
            if not isinstance(result, ProbeResult):
                result = ProbeResult.error(
                    probe_id, entry["category"].value,
                    f"Probe returned {type(result).__name__}, expected ProbeResult"
                )
            result.duration_ms = (time.monotonic() - t0) * 1000
            return result
        except Exception as e:
            logger.error("probe_error id=%s error=%s", probe_id, e)
            result = ProbeResult.error(probe_id, entry["category"].value, str(e))
            result.duration_ms = (time.monotonic() - t0) * 1000
            return result

    def run_all(self, context: Optional[dict] = None) -> list[ProbeResult]:
        """Run every registered probe and return results."""
        ctx = context or {}
        results = []
        for probe_id, entry in self._probes.items():
            results.append(self._run_probe(probe_id, entry, ctx))
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


class ProbeEngine:
    """Orchestrates probe execution and result routing."""

    def __init__(self, registry: Optional[ProbeRegistry] = None):
        self.registry = registry or ProbeRegistry()
        self._results_cache: list[ProbeResult] = []
        self._last_scan_trigger: str = ""
        self._last_scan_at: float = 0.0

    def scan(
        self, trigger: str = "manual", context: Optional[dict] = None
    ) -> list[ProbeResult]:
        """Run probes matching the trigger and cache results."""
        self._last_scan_trigger = trigger
        self._last_scan_at = time.time()

        if trigger == "all":
            results = self.registry.run_all(context)
        else:
            results = self.registry.run_by_trigger(trigger, context)

        self._results_cache = results
        logger.info(
            "scan_complete trigger=%s probes=%d findings=%d",
            trigger,
            len(results),
            sum(len(r.findings) for r in results),
        )
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
            "last_trigger": self._last_scan_trigger,
            "last_scan_at": (
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._last_scan_at))
                if self._last_scan_at
                else None
            ),
        }
