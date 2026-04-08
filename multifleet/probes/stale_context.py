"""Stale Context Probe — detects aged fleet messages and stagnant plans.

Checks:
- Fleet messages older than 7 days still in inbox (not archived)
- Plan docs older than 30 days with status NOT STARTED
"""

import logging
import os
import re
import time
from pathlib import Path

from multifleet.probe_engine import ProbeFinding, ProbeResult

logger = logging.getLogger("fleet-nerve.probes.stale_context")

_SEVEN_DAYS = 7 * 24 * 3600
_THIRTY_DAYS = 30 * 24 * 3600
_NOT_STARTED_RE = re.compile(r"NOT\s*STARTED", re.IGNORECASE)


def _check_stale_messages(repo_root: Path) -> tuple[list[ProbeFinding], list[str]]:
    """Find fleet messages older than 7 days still in inbox."""
    findings: list[ProbeFinding] = []
    missing: list[str] = []

    messages_dir = repo_root / ".fleet-messages"
    if not messages_dir.is_dir():
        missing.append(str(messages_dir))
        return findings, missing

    now = time.time()
    stale_files: list[tuple[str, float]] = []

    # Check all node inboxes (skip archive/)
    for node_dir in messages_dir.iterdir():
        if not node_dir.is_dir() or node_dir.name == "archive":
            continue
        for msg_file in node_dir.iterdir():
            if msg_file.is_file() and msg_file.suffix == ".md":
                age_s = now - msg_file.stat().st_mtime
                if age_s > _SEVEN_DAYS:
                    stale_files.append((str(msg_file.relative_to(repo_root)), age_s))

    if stale_files:
        stale_files.sort(key=lambda x: -x[1])  # oldest first
        age_strs = [
            f"{path} ({age / 86400:.0f}d old)" for path, age in stale_files[:10]
        ]
        findings.append(ProbeFinding(
            title=f"{len(stale_files)} fleet message(s) older than 7 days in inbox",
            evidence=age_strs,
            blast_radius="low",
            suggested_injections=["Archive processed messages", "Review stale fleet comms"],
            file_paths=[str(repo_root / path) for path, _ in stale_files[:5]],
        ))

    return findings, missing


def _check_stale_plans(repo_root: Path) -> tuple[list[ProbeFinding], list[str]]:
    """Find plan docs older than 30 days with NOT STARTED status."""
    findings: list[ProbeFinding] = []
    missing: list[str] = []

    plans_dir = repo_root / "docs" / "plans"
    if not plans_dir.is_dir():
        missing.append(str(plans_dir))
        return findings, missing

    now = time.time()
    stale_plans: list[tuple[str, float]] = []

    for plan_file in plans_dir.iterdir():
        if not plan_file.is_file() or plan_file.suffix != ".md":
            continue
        age_s = now - plan_file.stat().st_mtime
        if age_s < _THIRTY_DAYS:
            continue
        try:
            # Read first 2KB to check status
            text = plan_file.read_text(errors="replace")[:2048]
            if _NOT_STARTED_RE.search(text):
                stale_plans.append((str(plan_file.relative_to(repo_root)), age_s))
        except OSError:
            continue

    if stale_plans:
        stale_plans.sort(key=lambda x: -x[1])
        age_strs = [
            f"{path} ({age / 86400:.0f}d old)" for path, age in stale_plans[:10]
        ]
        findings.append(ProbeFinding(
            title=f"{len(stale_plans)} plan doc(s) older than 30 days still NOT STARTED",
            evidence=age_strs,
            blast_radius="low",
            suggested_injections=["Review stale plans", "Archive or activate"],
            file_paths=[str(repo_root / path) for path, _ in stale_plans[:5]],
        ))

    return findings, missing


def probe_stale_context(context: dict) -> ProbeResult:
    """Detect stale fleet messages and stagnant plan docs."""
    repo_root = Path(context.get("repo_root", ".")).resolve()

    all_findings: list[ProbeFinding] = []
    all_missing: list[str] = []

    msg_findings, msg_missing = _check_stale_messages(repo_root)
    all_findings.extend(msg_findings)
    all_missing.extend(msg_missing)

    plan_findings, plan_missing = _check_stale_plans(repo_root)
    all_findings.extend(plan_findings)
    all_missing.extend(plan_missing)

    if not all_findings and len(all_missing) == 2:
        return ProbeResult.skipped("stale_context", "context_quality", all_missing)

    severity = "info"
    if all_findings:
        total = sum(len(f.evidence) for f in all_findings)
        if total > 10:
            severity = "high"
        elif total > 3:
            severity = "medium"
        else:
            severity = "low"

    return ProbeResult(
        probe_id="stale_context",
        category="context_quality",
        status="warning" if all_findings else "ok",
        confidence=0.85,
        severity=severity,
        findings=all_findings,
        missing_inputs=all_missing,
    )
