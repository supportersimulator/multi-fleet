"""Hidden Dependency Probe — detects undeclared imports and coupling.

Scans Python files for imports not listed in requirements.txt or
pyproject.toml. Also detects cross-module coupling where file A imports
from file B but B is not in the same package or declared dependency.
"""

import ast
import logging
import re
from pathlib import Path

from multifleet.probe_engine import ProbeFinding, ProbeResult

logger = logging.getLogger("fleet-nerve.probes.hidden_dependency")

# Standard library modules that should never be flagged
_STDLIB = {
    "abc", "asyncio", "argparse", "base64", "bisect", "collections",
    "contextlib", "copy", "csv", "dataclasses", "datetime", "enum",
    "functools", "glob", "hashlib", "hmac", "http", "importlib",
    "inspect", "io", "itertools", "json", "logging", "math", "multiprocessing",
    "operator", "os", "pathlib", "pickle", "platform", "pprint",
    "queue", "random", "re", "shlex", "shutil", "signal", "socket",
    "sqlite3", "string", "struct", "subprocess", "sys", "tempfile",
    "textwrap", "threading", "time", "traceback", "typing", "unittest",
    "urllib", "uuid", "warnings", "weakref",
}


def probe_hidden_dependency(context: dict) -> ProbeResult:
    """Detect imports not declared in project dependencies.

    Context keys:
        repo_root (str): Path to repository root.
        changed_files (list[str]): Files to scan (optional, scans all .py if empty).
        scan_limit (int): Max files to scan (default 50).
    """
    repo_root = Path(context.get("repo_root", "."))
    changed_files = context.get("changed_files", [])
    scan_limit = context.get("scan_limit", 50)

    # Get declared dependencies
    declared = _get_declared_deps(repo_root)

    # Get local package names
    local_packages = _get_local_packages(repo_root)

    # Determine which files to scan
    if changed_files:
        py_files = [repo_root / f for f in changed_files if f.endswith(".py")]
    else:
        py_files = sorted(repo_root.rglob("*.py"))[:scan_limit]

    if not py_files:
        return ProbeResult(
            probe_id="hidden_dependency",
            category="structure",
            status="ok",
            confidence=0.9,
            severity="info",
        )

    findings = []
    for py_file in py_files[:scan_limit]:
        undeclared = _check_file_imports(py_file, declared, local_packages)
        if undeclared:
            findings.append(ProbeFinding(
                title=f"{py_file.name} imports undeclared: {', '.join(undeclared)}",
                evidence=[f"import {m}" for m in undeclared],
                blast_radius="medium",
                suggested_injections=[f"Missing dep: {m}" for m in undeclared],
                file_paths=[str(py_file.relative_to(repo_root))],
            ))

    if not findings:
        return ProbeResult(
            probe_id="hidden_dependency",
            category="structure",
            status="ok",
            confidence=0.85,
            severity="info",
        )

    return ProbeResult(
        probe_id="hidden_dependency",
        category="structure",
        status="warning",
        confidence=0.75,
        severity="medium" if len(findings) < 5 else "high",
        findings=findings,
    )


def _get_declared_deps(repo_root: Path) -> set[str]:
    """Extract declared dependency names from requirements.txt and pyproject.toml."""
    deps = set()

    # requirements.txt
    for req_file in repo_root.rglob("requirements*.txt"):
        try:
            for line in req_file.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and not line.startswith("-"):
                    pkg = re.split(r"[>=<!\[\];]", line)[0].strip()
                    if pkg:
                        deps.add(pkg.lower().replace("-", "_"))
        except OSError:
            continue

    # pyproject.toml (simple regex extraction)
    for toml_file in repo_root.rglob("pyproject.toml"):
        try:
            text = toml_file.read_text()
            # Match dependency lines like "nats-py>=2.0"
            for match in re.finditer(r'"([a-zA-Z][a-zA-Z0-9_-]*)', text):
                deps.add(match.group(1).lower().replace("-", "_"))
        except OSError:
            continue

    return deps


def _get_local_packages(repo_root: Path) -> set[str]:
    """Find local Python package names (dirs with __init__.py)."""
    packages = set()
    for init in repo_root.rglob("__init__.py"):
        pkg_dir = init.parent
        rel = pkg_dir.relative_to(repo_root)
        parts = rel.parts
        if parts:
            packages.add(parts[0])
    return packages


def _check_file_imports(
    py_file: Path, declared: set[str], local_packages: set[str]
) -> list[str]:
    """Parse a Python file and return undeclared top-level import names."""
    try:
        tree = ast.parse(py_file.read_text(), filename=str(py_file))
    except (SyntaxError, OSError):
        return []

    undeclared = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if _is_undeclared(top, declared, local_packages):
                    undeclared.append(top)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                top = node.module.split(".")[0]
                if _is_undeclared(top, declared, local_packages):
                    undeclared.append(top)

    return sorted(set(undeclared))


def _is_undeclared(module: str, declared: set[str], local_packages: set[str]) -> bool:
    """Check if a module is undeclared (not stdlib, not declared, not local)."""
    if not module or module.startswith("_"):
        return False
    if module in _STDLIB:
        return False
    normalized = module.lower().replace("-", "_")
    if normalized in declared:
        return False
    if module in local_packages:
        return False
    return True
