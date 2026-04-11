"""Worktree Contamination Probe — detects uncommitted changes in inactive worktrees.

Scans git worktrees for uncommitted/unstaged changes that don't belong
to the active task. Cross-worktree bleed causes subtle bugs when changes
leak between isolated branches.
"""

import logging
import subprocess
from pathlib import Path

from multifleet.probe_engine import ProbeFinding, ProbeResult

logger = logging.getLogger("fleet-nerve.probes.worktree_contamination")


def probe_worktree_contamination(context: dict) -> ProbeResult:
    """Detect uncommitted changes in git worktrees.

    Context keys:
        repo_root (str): Path to repository root.
        active_branch (str): Currently active branch (optional).
    """
    repo_root = context.get("repo_root", ".")
    active_branch = context.get("active_branch")

    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            capture_output=True, text=True, cwd=repo_root, timeout=5,
        )
        if result.returncode != 0:
            return ProbeResult.skipped(
                "worktree_contamination", "change", ["git worktree list failed"]
            )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ProbeResult.skipped(
            "worktree_contamination", "change", ["git unavailable"]
        )

    worktrees = _parse_worktrees(result.stdout)

    if len(worktrees) <= 1:
        return ProbeResult(
            probe_id="worktree_contamination",
            category="change",
            status="ok",
            confidence=0.95,
            severity="info",
        )

    findings = []
    for wt in worktrees:
        wt_path = wt.get("worktree", "")
        branch = wt.get("branch", "")

        # Skip the main worktree and the active branch
        if not wt_path or branch == active_branch:
            continue

        # Check for uncommitted changes in this worktree
        dirty_files = _check_dirty(wt_path)
        if dirty_files:
            findings.append(ProbeFinding(
                title=f"Worktree '{branch}' has {len(dirty_files)} uncommitted changes",
                evidence=[f"Path: {wt_path}", f"Dirty files: {', '.join(dirty_files[:5])}"],
                blast_radius="medium",
                suggested_injections=[f"Worktree contamination risk: {branch}"],
                file_paths=dirty_files[:10],
            ))

    if not findings:
        return ProbeResult(
            probe_id="worktree_contamination",
            category="change",
            status="ok",
            confidence=0.9,
            severity="info",
        )

    return ProbeResult(
        probe_id="worktree_contamination",
        category="change",
        status="warning",
        confidence=0.8,
        severity="medium" if len(findings) < 3 else "high",
        findings=findings,
    )


def _parse_worktrees(porcelain_output: str) -> list[dict]:
    """Parse `git worktree list --porcelain` output."""
    worktrees = []
    current: dict = {}
    for line in porcelain_output.splitlines():
        if line.startswith("worktree "):
            if current:
                worktrees.append(current)
            current = {"worktree": line[9:]}
        elif line.startswith("HEAD "):
            current["head"] = line[5:]
        elif line.startswith("branch "):
            current["branch"] = line[7:].replace("refs/heads/", "")
        elif line == "bare":
            current["bare"] = True
        elif line == "detached":
            current["detached"] = True
    if current:
        worktrees.append(current)
    return worktrees


def _check_dirty(worktree_path: str) -> list[str]:
    """Check if a worktree has uncommitted changes."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "-uno"],
            capture_output=True, text=True, cwd=worktree_path, timeout=3,
        )
        if result.returncode != 0:
            return []
        return [line[3:].strip() for line in result.stdout.splitlines() if line.strip()]
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []
