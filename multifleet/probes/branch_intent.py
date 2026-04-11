"""Branch Intent Probe — detects scope creep and name/change mismatches.

Extracts intent from branch names (feature/X, fix/Y, refactor/Z) and
compares against actual git diff stats. Flags scope creep and mismatches.
"""

import logging
import re
import subprocess
from pathlib import Path

from multifleet.probe_engine import ProbeFinding, ProbeResult

logger = logging.getLogger("fleet-nerve.probes.branch_intent")

_INTENT_PATTERNS = {
    "fix": re.compile(r"^(?:fix|bugfix|hotfix)[/-]", re.IGNORECASE),
    "feature": re.compile(r"^(?:feature|feat)[/-]", re.IGNORECASE),
    "refactor": re.compile(r"^(?:refactor|cleanup|chore)[/-]", re.IGNORECASE),
    "docs": re.compile(r"^(?:docs|documentation)[/-]", re.IGNORECASE),
    "test": re.compile(r"^(?:test|tests|testing)[/-]", re.IGNORECASE),
}


def _current_branch(repo_root: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, cwd=str(repo_root), timeout=10,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def _classify_intent(branch: str) -> str:
    for intent, pattern in _INTENT_PATTERNS.items():
        if pattern.match(branch):
            return intent
    return "unknown"


def _diff_stat(repo_root: Path) -> tuple[int, list[str]]:
    """Return (files_changed, list_of_paths) vs main/origin."""
    for base in ["origin/main", "main", "origin/master", "master"]:
        try:
            out = subprocess.run(
                ["git", "diff", "--stat", "--name-only", f"{base}...HEAD"],
                capture_output=True, text=True, cwd=str(repo_root), timeout=15,
            )
            if out.returncode == 0:
                files = [f.strip() for f in out.stdout.splitlines() if f.strip()]
                return len(files), files
        except (subprocess.TimeoutExpired, FileNotFoundError):
            continue
    return 0, []


def probe_branch_intent(context: dict) -> ProbeResult:
    """Check if branch name matches actual changes."""
    repo_root = Path(context.get("repo_root", ".")).resolve()
    findings: list[ProbeFinding] = []

    branch = context.get("branch") or _current_branch(repo_root)
    if not branch or branch in ("main", "master", "HEAD"):
        return ProbeResult(
            probe_id="branch_intent",
            category="change",
            status="ok",
            confidence=0.9,
            severity="info",
        )

    intent = _classify_intent(branch)
    num_files, changed_files = _diff_stat(repo_root)

    # Scope creep: fix branches touching >10 files
    if intent == "fix" and num_files > 10:
        findings.append(ProbeFinding(
            title=f"Scope creep: fix branch '{branch}' touches {num_files} files",
            evidence=[f"Expected narrow fix, got {num_files} file changes"],
            blast_radius="medium",
            suggested_injections=["Consider splitting into separate branches"],
            file_paths=[str(repo_root / f) for f in changed_files[:5]],
        ))

    # Docs branch touching non-doc files
    if intent == "docs":
        non_doc = [f for f in changed_files if not f.endswith((".md", ".rst", ".txt"))]
        if non_doc:
            findings.append(ProbeFinding(
                title=f"Docs branch '{branch}' modifies {len(non_doc)} non-doc file(s)",
                evidence=[f"Non-doc: {f}" for f in non_doc[:5]],
                blast_radius="low",
                suggested_injections=["Move code changes to separate branch"],
            ))

    # Test branch touching non-test files
    if intent == "test":
        non_test = [f for f in changed_files if "test" not in f.lower()]
        if len(non_test) > len(changed_files) * 0.5:
            findings.append(ProbeFinding(
                title=f"Test branch '{branch}' has {len(non_test)} non-test changes",
                evidence=[f"Non-test: {f}" for f in non_test[:5]],
                blast_radius="low",
            ))

    severity = "medium" if findings else "info"
    return ProbeResult(
        probe_id="branch_intent",
        category="change",
        status="warning" if findings else "ok",
        confidence=0.7,
        severity=severity,
        findings=findings,
    )
