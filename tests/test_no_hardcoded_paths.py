"""Tests verifying no hardcoded superrepo paths remain in multifleet package.

These tests are the revenue-blocker guardrail: if any hardcoded path sneaks
back in, pip install multifleet will break on user machines.
"""

import ast
import os
import re
from pathlib import Path

PACKAGE_DIR = Path(__file__).parent.parent / "multifleet"
BIN_DIR = Path(__file__).parent.parent / "bin"

# Patterns that indicate hardcoded superrepo paths
FORBIDDEN_PATTERNS = [
    r"/Users/aaron",
    r"er-simulator-superrepo",
    r"Documents/er",
    r"~/dev/er",
]

# Pattern for raw Path(__file__).parent.parent (should use _paths.REPO_ROOT)
RAW_PARENT_PARENT = r"Path\(__file__\)\.parent\.parent"

# Files allowed to use Path(__file__).parent.parent (only the central _paths.py)
ALLOWED_RAW_PARENT = {"_paths.py"}


def _python_files(directory: Path):
    """Yield all .py files in directory (non-recursive into sub-packages handled separately)."""
    for f in sorted(directory.rglob("*.py")):
        yield f


def test_no_hardcoded_superrepo_paths():
    """Grep all .py files for hardcoded superrepo references."""
    violations = []
    for py_file in _python_files(PACKAGE_DIR):
        content = py_file.read_text()
        for pattern in FORBIDDEN_PATTERNS:
            for i, line in enumerate(content.splitlines(), 1):
                # Skip comments and docstrings containing the pattern
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if re.search(pattern, line):
                    violations.append(f"{py_file.name}:{i}: {line.strip()}")
    assert not violations, (
        f"Found {len(violations)} hardcoded superrepo path(s):\n"
        + "\n".join(violations)
    )


def test_no_hardcoded_paths_in_bin():
    """Grep bin/ scripts for hardcoded superrepo references."""
    violations = []
    for py_file in _python_files(BIN_DIR):
        content = py_file.read_text()
        for pattern in FORBIDDEN_PATTERNS:
            for i, line in enumerate(content.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if re.search(pattern, line):
                    violations.append(f"{py_file.name}:{i}: {line.strip()}")
    assert not violations, (
        f"Found {len(violations)} hardcoded path(s) in bin/:\n"
        + "\n".join(violations)
    )


def test_no_raw_parent_parent_outside_paths_module():
    """Ensure Path(__file__).parent.parent is only used in _paths.py."""
    violations = []
    for py_file in _python_files(PACKAGE_DIR):
        if py_file.name in ALLOWED_RAW_PARENT:
            continue
        content = py_file.read_text()
        for i, line in enumerate(content.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if re.search(RAW_PARENT_PARENT, line):
                violations.append(f"{py_file.name}:{i}: {line.strip()}")
    assert not violations, (
        f"Found {len(violations)} raw Path(__file__).parent.parent outside _paths.py:\n"
        + "\n".join(violations)
        + "\n\nUse `from ._paths import REPO_ROOT` instead."
    )


def test_paths_module_uses_env_var_fallback():
    """Verify _paths.py respects MULTIFLEET_REPO_ROOT env var."""
    paths_file = PACKAGE_DIR / "_paths.py"
    content = paths_file.read_text()
    assert "MULTIFLEET_REPO_ROOT" in content, (
        "_paths.py must check MULTIFLEET_REPO_ROOT env var for standalone installs"
    )
    assert "Path(__file__)" in content, (
        "_paths.py must fall back to Path(__file__) when env var is not set"
    )


def test_pyproject_no_superrepo_url():
    """Verify pyproject.toml repository URL is multi-fleet specific."""
    pyproject = Path(__file__).parent.parent / "pyproject.toml"
    content = pyproject.read_text()
    assert "er-simulator-superrepo" not in content, (
        "pyproject.toml repository URL still references superrepo"
    )
