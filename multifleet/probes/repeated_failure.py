"""Repeated Failure Cluster Probe — detects recurring failures across sessions.

Scans the packet store for failed tasks grouped by file path over the last
24 hours. When the same file appears in 3+ failures, it's a cluster that
warrants investigation rather than another blind retry.

This is the "stop circling" probe — it surfaces when Atlas keeps hitting
the same wall.
"""

import logging
import time
from collections import Counter
from pathlib import Path

from multifleet.probe_engine import ProbeFinding, ProbeResult, ProbeSeverity

logger = logging.getLogger("fleet-nerve.probes.repeated_failure")

# Minimum failures in a file to flag a cluster
CLUSTER_THRESHOLD = 3
# Time window in seconds (24 hours)
TIME_WINDOW_S = 86400


def probe_repeated_failure(context: dict) -> ProbeResult:
    """Detect files/modules with repeated failures in recent history.

    Context keys:
        repo_root (str): Path to repository root.
        store_path (str): Path to packet store SQLite (optional).
        time_window_s (int): Override time window (default 24h).
    """
    repo_root = context.get("repo_root", ".")
    window = context.get("time_window_s", TIME_WINDOW_S)
    threshold = context.get("cluster_threshold", CLUSTER_THRESHOLD)

    # Try packet store first (structured data)
    store_path = context.get("store_path")
    if store_path and Path(store_path).exists():
        return _scan_store(store_path, window, threshold)

    # Fallback: scan git log for reverted/fixed commits
    return _scan_git_log(repo_root, window, threshold)


def _scan_store(store_path: str, window: int, threshold: int) -> ProbeResult:
    """Scan SQLite packet store for failed task clusters."""
    try:
        import sqlite3
        conn = sqlite3.connect(store_path)
        cutoff = time.time() - window

        # Query tasks that failed or were retried
        rows = conn.execute("""
            SELECT payload FROM messages
            WHERE timestamp > ? AND (
                subject LIKE '%fail%' OR subject LIKE '%error%'
                OR subject LIKE '%retry%' OR subject LIKE '%revert%'
            )
            ORDER BY timestamp DESC
            LIMIT 500
        """, (cutoff,)).fetchall()
        conn.close()

        if not rows:
            return ProbeResult(
                probe_id="repeated_failure",
                category="reliability",
                status="ok",
                confidence=0.9,
                severity="info",
            )

        # Extract file paths from payloads and count occurrences
        file_counter: Counter = Counter()
        for (payload_str,) in rows:
            try:
                import json
                payload = json.loads(payload_str) if isinstance(payload_str, str) else {}
                files = payload.get("files", [])
                if isinstance(files, list):
                    for f in files:
                        if isinstance(f, str):
                            file_counter[f] += 1
            except (json.JSONDecodeError, TypeError):
                continue

        return _build_result(file_counter, threshold, len(rows))

    except Exception as e:
        logger.warning("Store scan failed: %s", e)
        return ProbeResult.error("repeated_failure", "reliability", str(e))


def _scan_git_log(repo_root: str, window: int, threshold: int) -> ProbeResult:
    """Fallback: scan git log for files that appear in fix/revert commits."""
    import subprocess

    try:
        hours = max(1, window // 3600)
        result = subprocess.run(
            ["git", "log", f"--since={hours} hours ago",
             "--diff-filter=M", "--name-only", "--pretty=format:",
             "--grep=fix", "--grep=revert", "--grep=retry",
             "--all-match"],
            capture_output=True, text=True, cwd=repo_root, timeout=10,
        )

        if result.returncode != 0:
            return ProbeResult.skipped(
                "repeated_failure", "reliability", ["git log failed"]
            )

        file_counter: Counter = Counter()
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if line and not line.startswith("commit "):
                file_counter[line] += 1

        return _build_result(file_counter, threshold, sum(file_counter.values()))

    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return ProbeResult.skipped(
            "repeated_failure", "reliability", [f"git unavailable: {e}"]
        )


def _build_result(file_counter: Counter, threshold: int, total_events: int) -> ProbeResult:
    """Build ProbeResult from file occurrence counts."""
    clusters = {f: c for f, c in file_counter.items() if c >= threshold}

    if not clusters:
        return ProbeResult(
            probe_id="repeated_failure",
            category="reliability",
            status="ok",
            confidence=0.85,
            severity="info",
        )

    findings = []
    max_severity = "low"
    for filepath, count in sorted(clusters.items(), key=lambda x: -x[1]):
        severity = "high" if count >= threshold * 2 else "medium"
        if severity == "high":
            max_severity = "high"
        elif severity == "medium" and max_severity == "low":
            max_severity = "medium"

        findings.append(ProbeFinding(
            title=f"{filepath} failed {count} times in last 24h",
            evidence=[f"Appeared in {count}/{total_events} failure events"],
            blast_radius="high" if count >= threshold * 2 else "medium",
            suggested_injections=[f"Known fragile: {filepath}"],
            file_paths=[filepath],
        ))

    return ProbeResult(
        probe_id="repeated_failure",
        category="reliability",
        status="warning" if max_severity != "high" else "critical",
        confidence=0.8,
        severity=max_severity,
        findings=findings,
    )
