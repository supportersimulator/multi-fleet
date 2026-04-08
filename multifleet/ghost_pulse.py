"""Ghost Pulse — the ContextDNA subconscious background heartbeat.

Runs on idle/heartbeat intervals. Its job is NOT to produce output.
Its job is to keep the system correctable over time:
- Expire stale beliefs
- Re-rank active facts
- Cluster contradictions
- Detect repeated pain points
- Surface "what changed while you were gone"
- Flag beliefs no longer well-supported

"The ghost is the part of the organism that refuses to let polished
output silently become truth."
"""

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("fleet-nerve.ghost-pulse")


@dataclass
class PulseResult:
    """Result of a ghost pulse cycle."""

    stale_beliefs_expired: int = 0
    contradictions_found: int = 0
    repeated_failures_detected: int = 0
    facts_re_ranked: int = 0
    changes_since_last: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    duration_ms: float = 0.0
    timestamp: str = ""

    def to_dict(self) -> dict:
        return {
            "stale_beliefs_expired": self.stale_beliefs_expired,
            "contradictions_found": self.contradictions_found,
            "repeated_failures_detected": self.repeated_failures_detected,
            "facts_re_ranked": self.facts_re_ranked,
            "changes_since_last": self.changes_since_last,
            "warnings": self.warnings,
            "duration_ms": round(self.duration_ms, 1),
            "timestamp": self.timestamp,
        }

    def summary(self) -> str:
        parts = []
        if self.stale_beliefs_expired:
            parts.append(f"{self.stale_beliefs_expired} stale expired")
        if self.contradictions_found:
            parts.append(f"{self.contradictions_found} contradictions")
        if self.repeated_failures_detected:
            parts.append(f"{self.repeated_failures_detected} repeated failures")
        if self.warnings:
            parts.append(f"{len(self.warnings)} warnings")
        return " | ".join(parts) if parts else "all clear"


class GhostPulse:
    """The ContextDNA subconscious — runs in background to maintain epistemic health.

    Core philosophy: corrigibility > confidence.
    The pulse keeps the system open to correction by detecting when
    beliefs become stale, evidence contradicts, or failures repeat.
    """

    def __init__(
        self,
        repo_root: str = ".",
        evidence_ledger_path: Optional[str] = None,
        max_staleness_hours: int = 48,
    ):
        self._repo_root = Path(repo_root)
        self._ledger_path = Path(
            evidence_ledger_path
            or str(Path.home() / ".fleet-nerve" / "decisions.jsonl")
        )
        self._max_staleness_hours = max_staleness_hours
        self._last_pulse_at: float = 0.0

    def pulse(self) -> PulseResult:
        """Run one pulse cycle. Should be called periodically (e.g., every 5 minutes)."""
        t0 = time.monotonic()
        result = PulseResult(
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )

        # 1. Expire stale beliefs
        result.stale_beliefs_expired = self._expire_stale_beliefs()

        # 2. Detect contradictions in evidence
        result.contradictions_found = self._detect_contradictions()

        # 3. Detect repeated failure patterns
        result.repeated_failures_detected = self._detect_repeated_failures()

        # 4. Surface changes since last pulse
        result.changes_since_last = self._detect_changes_since_last()

        # 5. Check probe staleness (3-surgeon recommendation)
        probe_warning = self._check_probe_staleness()
        if probe_warning:
            result.warnings.append(probe_warning)

        # 6. Generate warnings
        result.warnings.extend(self._generate_warnings())

        result.duration_ms = (time.monotonic() - t0) * 1000
        self._last_pulse_at = time.time()

        logger.info("ghost_pulse complete: %s (%.0fms)", result.summary(), result.duration_ms)
        return result

    def _expire_stale_beliefs(self) -> int:
        """Find and mark evidence entries older than staleness threshold."""
        if not self._ledger_path.exists():
            return 0

        try:
            lines = self._ledger_path.read_text().strip().splitlines()
            cutoff = time.time() - (self._max_staleness_hours * 3600)
            stale_count = 0

            for line in lines:
                try:
                    entry = json.loads(line)
                    ts = entry.get("timestamp", 0)
                    if ts and ts < cutoff:
                        stale_count += 1
                except json.JSONDecodeError:
                    continue

            return stale_count
        except Exception as e:
            logger.warning("Stale belief check failed: %s", e)
            return 0

    def _detect_contradictions(self) -> int:
        """Find decisions where same task_id has conflicting outcomes."""
        if not self._ledger_path.exists():
            return 0

        try:
            lines = self._ledger_path.read_text().strip().splitlines()[-100:]
            by_task: dict[str, list[dict]] = {}

            for line in lines:
                try:
                    entry = json.loads(line)
                    task_id = entry.get("task_id", "")
                    if task_id:
                        by_task.setdefault(task_id, []).append(entry)
                except json.JSONDecodeError:
                    continue

            # Count tasks with contradicting decisions
            contradictions = 0
            for task_id, entries in by_task.items():
                decisions = set(e.get("decision", "") for e in entries)
                if len(decisions) > 1:
                    contradictions += 1
                    logger.info(
                        "Contradiction: task %s has decisions %s",
                        task_id[:8], decisions,
                    )

            return contradictions
        except Exception as e:
            logger.warning("Contradiction check failed: %s", e)
            return 0

    def _detect_repeated_failures(self) -> int:
        """Check for tasks that repeatedly fail or get rejected."""
        if not self._ledger_path.exists():
            return 0

        try:
            lines = self._ledger_path.read_text().strip().splitlines()[-100:]
            reject_count: dict[str, int] = {}

            for line in lines:
                try:
                    entry = json.loads(line)
                    if entry.get("decision") in ("reject", "needs_rebuttal"):
                        task_id = entry.get("task_id", "unknown")
                        reject_count[task_id] = reject_count.get(task_id, 0) + 1
                except json.JSONDecodeError:
                    continue

            return sum(1 for c in reject_count.values() if c >= 2)
        except Exception as e:
            logger.warning("Repeated failure check failed: %s", e)
            return 0

    def _detect_changes_since_last(self) -> list[str]:
        """Detect what changed in the repo since last pulse."""
        if self._last_pulse_at == 0:
            return ["First pulse — no baseline"]

        try:
            import subprocess
            hours_ago = max(1, int((time.time() - self._last_pulse_at) / 3600))
            result = subprocess.run(
                ["git", "log", f"--since={hours_ago} hours ago",
                 "--oneline", "--no-merges"],
                capture_output=True, text=True,
                cwd=str(self._repo_root), timeout=5,
            )
            if result.returncode == 0:
                commits = result.stdout.strip().splitlines()[:10]
                return commits if commits else ["No new commits"]
            return []
        except Exception:
            return []

    def _generate_warnings(self) -> list[str]:
        """Generate ghost warnings about system health."""
        warnings = []

        # Check if evidence ledger is growing too fast
        if self._ledger_path.exists():
            size_mb = self._ledger_path.stat().st_size / (1024 * 1024)
            if size_mb > 10:
                warnings.append(f"Evidence ledger is {size_mb:.1f}MB — consider compaction")

        # Check for stale fleet messages
        inbox = self._repo_root / ".fleet-messages" / "all"
        if inbox.exists():
            for msg in inbox.iterdir():
                if msg.suffix == ".md":
                    age_hours = (time.time() - msg.stat().st_mtime) / 3600
                    if age_hours > 168:  # 7 days
                        warnings.append(f"Fleet message {msg.name} is {int(age_hours/24)}d old")

        # Check CLAUDE.md freshness
        claude_md = self._repo_root / "CLAUDE.md"
        if claude_md.exists():
            age_days = (time.time() - claude_md.stat().st_mtime) / 86400
            if age_days > 14:
                warnings.append(f"CLAUDE.md is {int(age_days)} days old — may need refresh")

        return warnings[:5]

    def _check_probe_staleness(self) -> Optional[str]:
        """Check if probes haven't run recently (3-surgeon recommendation)."""
        try:
            from multifleet.probes import create_engine
            engine = create_engine()
            results = engine.latest_results()
            if not results:
                return "Probes have never run — system operating without diagnostics"
            # Check if last scan is old
            summary = engine.findings_summary()
            last_scan = summary.get("last_scan_at")
            if not last_scan:
                return "No recent probe scan timestamp — run probes"
            return None
        except (ImportError, Exception):
            return None


def run_pulse(repo_root: str = ".", **kwargs) -> dict:
    """Convenience function to run a single pulse cycle."""
    ghost = GhostPulse(repo_root=repo_root, **kwargs)
    result = ghost.pulse()
    return result.to_dict()
