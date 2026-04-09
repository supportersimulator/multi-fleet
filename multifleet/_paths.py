"""
Centralized path resolution for multifleet.

Works in both contexts:
  1. pip install multifleet  (standalone package)
  2. Dev mode inside a monorepo / superrepo

Rules:
  - REPO_ROOT  = the multi-fleet package root (contains multifleet/, scripts/, .multifleet/)
  - GIT_ROOT   = the git repo root (may be superrepo or the package itself)
  - Never hardcode absolute paths — use env vars with Path(__file__) fallbacks
"""

import os
import subprocess
from pathlib import Path

# Package root: multi-fleet/ directory (parent of multifleet/ package dir)
_PACKAGE_DIR = Path(__file__).resolve().parent.parent

REPO_ROOT = Path(os.environ.get("MULTIFLEET_REPO_ROOT", str(_PACKAGE_DIR)))


def get_git_root() -> Path:
    """Find the git repository root, walking up from REPO_ROOT.

    Returns REPO_ROOT itself if not inside a git repo (e.g. pip install).
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=3,
            cwd=str(REPO_ROOT),
        )
        if result.returncode == 0:
            return Path(result.stdout.strip())
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return REPO_ROOT


def get_superrepo_root() -> Path:
    """Return superrepo root if running inside one, else REPO_ROOT.

    Env var MULTIFLEET_SUPERREPO_ROOT takes priority. Otherwise, walks
    up from REPO_ROOT looking for a parent with .git directory that is
    NOT the same as REPO_ROOT (i.e. a superrepo containing multi-fleet).
    """
    env = os.environ.get("MULTIFLEET_SUPERREPO_ROOT")
    if env:
        return Path(env)
    git_root = get_git_root()
    if git_root != REPO_ROOT:
        return git_root
    return REPO_ROOT
