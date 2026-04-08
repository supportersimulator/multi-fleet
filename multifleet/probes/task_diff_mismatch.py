"""Task-Diff Mismatch Probe — detects when changes don't match the task intent.

Compares the task description/intent against actual git diff to flag
when changes touch unrelated areas. This catches scope creep and
accidental modifications.
"""

import logging
import re
import subprocess
from pathlib import Path

from multifleet.probe_engine import ProbeFinding, ProbeResult

logger = logging.getLogger("fleet-nerve.probes.task_diff_mismatch")

# Keywords that suggest specific domains
_DOMAIN_KEYWORDS = {
    "auth": {"auth", "login", "token", "jwt", "session", "password", "oauth"},
    "api": {"endpoint", "route", "handler", "request", "response", "api"},
    "database": {"sql", "query", "migration", "schema", "model", "db"},
    "ui": {"component", "panel", "view", "render", "css", "html", "template"},
    "test": {"test", "assert", "mock", "fixture", "spec"},
    "fleet": {"fleet", "nats", "daemon", "peer", "transport", "message"},
    "probe": {"probe", "scan", "detect", "finding", "evidence"},
    "build": {"package", "build", "deploy", "ci", "docker", "pip"},
}


def probe_task_diff_mismatch(context: dict) -> ProbeResult:
    """Detect when git diff touches areas unrelated to task intent.

    Context keys:
        repo_root (str): Path to repository root.
        task_intent (str): Description of what the task is about.
        branch (str): Branch to compare against main (optional).
    """
    repo_root = context.get("repo_root", ".")
    task_intent = context.get("task_intent", "")
    branch = context.get("branch")

    if not task_intent:
        return ProbeResult.skipped(
            "task_diff_mismatch", "change", ["no task_intent provided"]
        )

    # Get changed files
    changed_files = _get_changed_files(repo_root, branch)
    if not changed_files:
        return ProbeResult(
            probe_id="task_diff_mismatch",
            category="change",
            status="ok",
            confidence=0.9,
            severity="info",
        )

    # Determine task domain from intent
    task_domains = _extract_domains(task_intent)

    # Check each changed file for domain relevance
    mismatches = []
    for filepath in changed_files:
        file_domains = _extract_domains(filepath)
        if task_domains and file_domains and not (task_domains & file_domains):
            mismatches.append(filepath)

    if not mismatches:
        return ProbeResult(
            probe_id="task_diff_mismatch",
            category="change",
            status="ok",
            confidence=0.7,
            severity="info",
        )

    # Calculate mismatch ratio
    ratio = len(mismatches) / max(1, len(changed_files))
    severity = "high" if ratio > 0.5 else "medium" if ratio > 0.25 else "low"

    findings = [ProbeFinding(
        title=f"{len(mismatches)}/{len(changed_files)} changed files outside task domain",
        evidence=[
            f"Task intent domains: {', '.join(task_domains) or 'general'}",
            f"Off-domain files: {', '.join(mismatches[:10])}",
        ],
        blast_radius="high" if ratio > 0.5 else "medium",
        suggested_injections=[
            f"Scope check: {len(mismatches)} files may be unrelated to task"
        ],
        file_paths=mismatches[:20],
    )]

    return ProbeResult(
        probe_id="task_diff_mismatch",
        category="change",
        status="warning",
        confidence=0.6,  # Domain matching is heuristic
        severity=severity,
        findings=findings,
    )


def _get_changed_files(repo_root: str, branch: str = None) -> list[str]:
    """Get list of changed files from git."""
    try:
        if branch:
            cmd = ["git", "diff", "--name-only", f"main...{branch}"]
        else:
            cmd = ["git", "diff", "--name-only", "HEAD"]

        result = subprocess.run(
            cmd, capture_output=True, text=True, cwd=repo_root, timeout=5,
        )
        if result.returncode != 0:
            # Try unstaged changes
            result = subprocess.run(
                ["git", "diff", "--name-only"],
                capture_output=True, text=True, cwd=repo_root, timeout=5,
            )

        return [f.strip() for f in result.stdout.strip().splitlines() if f.strip()]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []


def _extract_domains(text: str) -> set[str]:
    """Extract domain keywords from text."""
    text_lower = text.lower()
    words = set(re.findall(r"[a-z]+", text_lower))
    matched = set()
    for domain, keywords in _DOMAIN_KEYWORDS.items():
        if words & keywords:
            matched.add(domain)
    return matched
