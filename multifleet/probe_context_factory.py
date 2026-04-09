"""ProbeContext Auto-Assembly — populates ProbeContext from live git state.

Instead of callers manually constructing ProbeContext with empty strings,
this factory reads the real git branch, changed files, and node identity.

Usage:
    from multifleet.probe_context_factory import build_probe_context
    ctx = build_probe_context(repo_path="/path/to/repo", task="implement X")
"""

import logging
import os
import subprocess
from pathlib import Path

try:
    from multifleet.code_scanner.probe_contract import ProbeContext
except ImportError:
    # Fallback: code_scanner removed — use a simple dataclass
    from dataclasses import dataclass, field

    @dataclass
    class ProbeContext:
        repo_path: str
        branch: str = ""
        changed_files: list[str] = field(default_factory=list)
        task_description: str = ""
        node_id: str = ""

        def to_dict(self) -> dict:
            return {
                "repo_path": self.repo_path,
                "branch": self.branch,
                "changed_files": self.changed_files,
                "task_description": self.task_description,
                "node_id": self.node_id,
            }

logger = logging.getLogger("fleet-nerve.probe_context_factory")


def _git_cmd(repo: Path, *args: str, timeout: int = 10) -> str:
    """Run a git command, return stdout or empty string on failure."""
    try:
        r = subprocess.run(
            ["git", *args],
            capture_output=True, text=True, timeout=timeout, cwd=str(repo),
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""


def _current_branch(repo: Path) -> str:
    return _git_cmd(repo, "rev-parse", "--abbrev-ref", "HEAD")


def _changed_files(repo: Path) -> list[str]:
    """Files changed in working tree + staged (untracked excluded)."""
    out = _git_cmd(repo, "diff", "--name-only", "HEAD")
    staged = _git_cmd(repo, "diff", "--name-only", "--cached")
    files = set()
    for line in (out + "\n" + staged).strip().splitlines():
        line = line.strip()
        if line:
            files.add(line)
    return sorted(files)


def _node_id() -> str:
    return os.environ.get("MULTIFLEET_NODE_ID", "")


def build_probe_context(
    repo_path: str = ".",
    task: str = "",
    node_id: str = "",
) -> ProbeContext:
    """Build a fully-populated ProbeContext from live git state.

    Args:
        repo_path: Path to the git repository root.
        task: Optional task description for context-aware probes.
        node_id: Override node ID (defaults to MULTIFLEET_NODE_ID env var).

    Returns:
        ProbeContext with branch, changed_files, node_id populated from git.
    """
    repo = Path(repo_path).resolve()
    branch = _current_branch(repo)
    changed = _changed_files(repo)
    nid = node_id or _node_id()

    logger.debug(
        "probe_context built: branch=%s changed=%d node=%s",
        branch, len(changed), nid,
    )
    return ProbeContext(
        repo_path=str(repo),
        branch=branch,
        changed_files=changed,
        task_description=task,
        node_id=nid,
    )
