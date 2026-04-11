"""Dependency Drift Probe — flags untested dependency changes.

Checks if pyproject.toml/package.json changed in recent commits without
corresponding test modifications. Untested dep changes = risk.
"""

import logging
import subprocess
from pathlib import Path

from multifleet.probe_engine import ProbeFinding, ProbeResult

logger = logging.getLogger("fleet-nerve.probes.dependency_drift")

_DEP_FILES = {"pyproject.toml", "setup.py", "setup.cfg", "requirements.txt",
              "package.json", "package-lock.json", "yarn.lock", "Pipfile", "Pipfile.lock"}
_TEST_PATTERNS = {"test_", "_test.py", "tests/", "spec/", ".test.js", ".spec.js", ".test.ts"}


def _recent_commits_with_files(repo_root: Path, days: int = 14) -> list[tuple[str, list[str]]]:
    """Return [(commit_hash, [files...])] for recent commits."""
    try:
        out = subprocess.run(
            ["git", "log", f"--since={days} days ago", "--format=%H", "--name-only"],
            capture_output=True, text=True, cwd=str(repo_root), timeout=30,
        )
        if out.returncode != 0:
            return []
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []

    commits: list[tuple[str, list[str]]] = []
    current_hash = ""
    current_files: list[str] = []

    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        if len(line) == 40 and all(c in "0123456789abcdef" for c in line):
            if current_hash:
                commits.append((current_hash, current_files))
            current_hash = line
            current_files = []
        else:
            current_files.append(line)

    if current_hash:
        commits.append((current_hash, current_files))

    return commits


def _is_dep_file(path: str) -> bool:
    return Path(path).name in _DEP_FILES


def _is_test_file(path: str) -> bool:
    return any(p in path for p in _TEST_PATTERNS)


def probe_dependency_drift(context: dict) -> ProbeResult:
    """Flag dependency file changes without co-occurring test changes."""
    repo_root = Path(context.get("repo_root", ".")).resolve()
    findings: list[ProbeFinding] = []

    commits = _recent_commits_with_files(repo_root, days=context.get("lookback_days", 14))
    if not commits:
        return ProbeResult(
            probe_id="dependency_drift",
            category="change",
            status="ok",
            confidence=0.5,
            severity="info",
        )

    untested_dep_commits: list[tuple[str, list[str]]] = []
    for commit_hash, files in commits:
        dep_files = [f for f in files if _is_dep_file(f)]
        test_files = [f for f in files if _is_test_file(f)]
        if dep_files and not test_files:
            untested_dep_commits.append((commit_hash, dep_files))

    if untested_dep_commits:
        evidence = [
            f"{h[:8]}: {', '.join(deps)}" for h, deps in untested_dep_commits[:10]
        ]
        findings.append(ProbeFinding(
            title=f"{len(untested_dep_commits)} commit(s) changed deps without tests",
            evidence=evidence,
            blast_radius="medium",
            suggested_injections=["Add tests for dependency changes", "Verify dep compatibility"],
            file_paths=[str(repo_root / d) for _, deps in untested_dep_commits[:3] for d in deps],
        ))

    severity = "medium" if len(untested_dep_commits) > 3 else "low" if untested_dep_commits else "info"
    return ProbeResult(
        probe_id="dependency_drift",
        category="change",
        status="warning" if findings else "ok",
        confidence=0.8,
        severity=severity,
        findings=findings,
    )
