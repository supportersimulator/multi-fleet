"""Repeated Failure Cluster Probe — detects recurring error patterns across sessions.

Reads audit trail (~/.fleet-nerve/audit-*.jsonl) for error events.
Groups by tool+target pattern. If same error appears 3+ times across
sessions, it's a systemic issue worth blocking on.
"""

import glob
import json
import logging
from collections import defaultdict
from pathlib import Path

from multifleet.probe_engine import ProbeFinding, ProbeResult

logger = logging.getLogger("fleet-nerve.probes.repeated_failure_cluster")

_DEFAULT_AUDIT_DIR = Path.home() / ".fleet-nerve"
_MIN_CLUSTER_SIZE = 3


def _load_error_events(audit_dir: Path) -> list[dict]:
    """Load error events from all audit JSONL files."""
    errors = []
    pattern = str(audit_dir / "audit-*.jsonl")
    for filepath in sorted(glob.glob(pattern)):
        try:
            with open(filepath) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    # Accept events with error/fail status or error fields
                    status = str(event.get("status", "")).lower()
                    level = str(event.get("level", "")).lower()
                    if "error" in status or "fail" in status or level == "error":
                        errors.append(event)
        except OSError:
            continue
    return errors


def _cluster_key(event: dict) -> str:
    """Build a grouping key from tool + target (or action + subject)."""
    tool = event.get("tool") or event.get("action") or event.get("type", "unknown")
    target = event.get("target") or event.get("subject") or event.get("node", "unknown")
    return f"{tool}::{target}"


def probe_repeated_failure_cluster(context: dict) -> ProbeResult:
    """Detect recurring error patterns in audit trail."""
    audit_dir = Path(context.get("audit_dir", str(_DEFAULT_AUDIT_DIR)))
    findings: list[ProbeFinding] = []

    if not audit_dir.exists():
        return ProbeResult.skipped(
            "repeated_failure_cluster", "reliability", [str(audit_dir)]
        )

    errors = _load_error_events(audit_dir)
    if not errors:
        return ProbeResult(
            probe_id="repeated_failure_cluster",
            category="reliability",
            status="ok",
            confidence=0.9,
            severity="info",
        )

    # Group by tool::target pattern
    clusters: dict[str, list[dict]] = defaultdict(list)
    for evt in errors:
        key = _cluster_key(evt)
        clusters[key].append(evt)

    blocking_clusters = []
    for key, events in clusters.items():
        if len(events) >= _MIN_CLUSTER_SIZE:
            blocking_clusters.append((key, events))

    for key, events in blocking_clusters:
        timestamps = [e.get("timestamp", e.get("ts", "?")) for e in events]
        first_seen = timestamps[0] if timestamps else "?"
        last_seen = timestamps[-1] if timestamps else "?"
        findings.append(ProbeFinding(
            title=f"Repeated failure: {key} ({len(events)}x)",
            evidence=[
                f"Occurrences: {len(events)}",
                f"First seen: {first_seen}",
                f"Last seen: {last_seen}",
                f"Pattern: {key}",
            ],
            blast_radius="high" if len(events) >= 5 else "medium",
            suggested_injections=[
                f"Investigate root cause of {key}",
                "Check if target service is misconfigured",
            ],
        ))

    severity = "info"
    if findings:
        max_count = max(len(c[1]) for c in blocking_clusters)
        if max_count >= 10:
            severity = "critical"
        elif max_count >= 5:
            severity = "high"
        else:
            severity = "medium"

    return ProbeResult(
        probe_id="repeated_failure_cluster",
        category="reliability",
        status="warning" if findings else "ok",
        confidence=0.8 if findings else 0.9,
        severity=severity,
        findings=findings,
    )
