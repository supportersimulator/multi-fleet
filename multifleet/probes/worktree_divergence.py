"""Worktree Divergence Probe — detects branches drifting far from main.

Runs git commands to measure how far the current branch has diverged
from main (commit count + files changed). Predicts merge conflict
likelihood based on overlap with recent main changes.
"""

import logging
import subprocess
from pathlib import Path

from multifleet.probe_engine import ProbeFinding, ProbeResult

logger = logging.getLogger("fleet-nerve.probes.worktree_divergence")

_COMMIT_THRESHOLD = 50
_FILE_THRESHOLD = 20


def _git(args: list[str], cwd: str, timeout: int = 10) -> tuple[int, str]:
    """Run a git command, return (returncode, stdout)."""
    try:
        r = subprocess.run(
            ["git"] + args,
            cwd=cwd, capture_output=True, text=True, timeout=timeout,
        )
        return r.returncode, r.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return -1, str(e)


def probe_worktree_divergence(context: dict) -> ProbeResult:
    """Check how far the current branch has diverged from main."""
    repo_root = context.get("repo_root", ".")
    base_branch = context.get("base_branch", "main")
    findings: list[ProbeFinding] = []

    # Check we're in a git repo
    rc, _ = _git(["rev-parse", "--git-dir"], repo_root)
    if rc != 0:
        return ProbeResult.skipped(
            "worktree_divergence", "change", ["not a git repo"]
        )

    # Get current branch
    rc, current_branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], repo_root)
    if rc != 0 or current_branch == base_branch:
        return ProbeResult(
            probe_id="worktree_divergence",
            category="change",
            status="ok",
            confidence=0.9,
            severity="info",
        )

    # Commits ahead of base
    rc, log_output = _git(
        ["log", "--oneline", f"{base_branch}..HEAD"], repo_root
    )
    commits_ahead = len(log_output.splitlines()) if rc == 0 and log_output else 0

    # Commits behind base
    rc, behind_output = _git(
        ["log", "--oneline", f"HEAD..{base_branch}"], repo_root
    )
    commits_behind = len(behind_output.splitlines()) if rc == 0 and behind_output else 0

    # Files changed vs base
    rc, diff_stat = _git(["diff", "--stat", base_branch], repo_root)
    files_changed = len(diff_stat.splitlines()) - 1 if rc == 0 and diff_stat else 0
    files_changed = max(files_changed, 0)

    # Merge conflict prediction: files changed on both sides
    rc, branch_files_raw = _git(
        ["diff", "--name-only", f"{base_branch}...HEAD"], repo_root
    )
    branch_files = set(branch_files_raw.splitlines()) if rc == 0 else set()

    rc, main_files_raw = _git(
        ["diff", "--name-only", f"HEAD...{base_branch}"], repo_root
    )
    main_files = set(main_files_raw.splitlines()) if rc == 0 else set()

    overlap = branch_files & main_files
    conflict_likelihood = "low"
    if len(overlap) > 5:
        conflict_likelihood = "high"
    elif len(overlap) > 0:
        conflict_likelihood = "medium"

    # Determine severity
    severity = "info"
    is_warning = False

    if commits_behind >= _COMMIT_THRESHOLD or files_changed >= _FILE_THRESHOLD:
        severity = "high"
        is_warning = True
        findings.append(ProbeFinding(
            title=f"Branch '{current_branch}' significantly diverged from {base_branch}",
            evidence=[
                f"Commits ahead: {commits_ahead}",
                f"Commits behind: {commits_behind}",
                f"Files changed: {files_changed}",
                f"Conflict likelihood: {conflict_likelihood}",
                f"Overlapping files: {len(overlap)}",
            ],
            blast_radius="high" if conflict_likelihood == "high" else "medium",
            suggested_injections=[
                f"Rebase or merge {base_branch} soon",
                "Review overlapping files for conflicts",
            ],
            file_paths=list(overlap)[:10],
        ))
    elif commits_behind > 10 or files_changed > 5:
        severity = "medium"
        is_warning = True
        findings.append(ProbeFinding(
            title=f"Branch '{current_branch}' drifting from {base_branch}",
            evidence=[
                f"Commits ahead: {commits_ahead}",
                f"Commits behind: {commits_behind}",
                f"Files changed: {files_changed}",
                f"Conflict likelihood: {conflict_likelihood}",
            ],
            blast_radius="low",
            suggested_injections=[f"Consider merging {base_branch}"],
        ))

    return ProbeResult(
        probe_id="worktree_divergence",
        category="change",
        status="warning" if is_warning else "ok",
        confidence=0.85,
        severity=severity,
        findings=findings,
    )
