"""Repeated Failure Cluster Probe — detects recurring error patterns across sessions.

Reads audit trail (~/.fleet-nerve/audit-*.jsonl) for error events.
Groups by tool+target pattern. If same error appears 3+ times across
sessions, it's a systemic issue worth blocking on.

Also performs cross-session clustering: when the same failure pattern
appears in 2+ distinct sessions, it's promoted as a systemic issue
and (at 3+ sessions) recorded in the evidence ledger.
"""

import glob
import json
import logging
import os
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


def _cluster_by_session(errors: list[dict]) -> list[dict]:
    """Group failures by session, detect cross-session patterns."""
    pattern_sessions: dict[str, set] = {}
    for e in errors:
        sid = e.get("metadata", {}).get("session_id") if isinstance(e.get("metadata"), dict) else None
        if sid is None:
            sid = e.get("session_id", "unknown")
        pattern = f"{_cluster_key(e)}"
        pattern_sessions.setdefault(pattern, set()).add(sid)

    cross_session = []
    for pattern, sids in pattern_sessions.items():
        if len(sids) >= 2:
            cross_session.append({
                "pattern": pattern,
                "session_count": len(sids),
                "sessions": list(sids)[:5],
                "severity": "high" if len(sids) >= 3 else "medium",
            })

    return cross_session


def _promote_to_evidence(cross_session_clusters: list[dict]) -> None:
    """Promote cross-session failure clusters (3+ sessions) to evidence ledger."""
    try:
        from multifleet.evidence_ledger import EvidenceLedger
        ledger = EvidenceLedger()
        for cluster in cross_session_clusters:
            if cluster["session_count"] >= 3:
                ledger.record(
                    event_type="failure_cluster",
                    node_id=os.environ.get("MULTIFLEET_NODE_ID", "unknown"),
                    subject=f"cross-session failure: {cluster['pattern']}",
                    payload=cluster,
                )
    except Exception:
        pass


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

    # Cross-session clustering
    cross_session = _cluster_by_session(errors)
    for cluster in cross_session:
        findings.append(ProbeFinding(
            title=f"Cross-session failure: {cluster['pattern']} ({cluster['session_count']} sessions)",
            evidence=[
                f"Sessions affected: {cluster['session_count']}",
                f"Severity: {cluster['severity']}",
                f"Sessions: {', '.join(cluster['sessions'][:3])}",
            ],
            blast_radius="high" if cluster["severity"] == "high" else "medium",
            suggested_injections=[
                f"Systemic issue: {cluster['pattern']} persists across sessions",
            ],
        ))

    # Promote high-severity cross-session clusters to evidence ledger
    if cross_session:
        _promote_to_evidence(cross_session)

    severity = "info"
    if findings:
        max_count = max(
            max((len(c[1]) for c in blocking_clusters), default=0),
            max((c["session_count"] for c in cross_session), default=0),
        )
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
