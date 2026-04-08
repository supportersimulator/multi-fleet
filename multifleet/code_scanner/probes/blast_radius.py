"""Blast Radius Probe -- estimates change impact for modified files.

Checks:
1. How many files import the changed file (direct dependents)
2. How many tests reference the changed file
3. Whether the changed file is in a "hot path" (frequently modified)
4. Whether changes touch public API vs internal implementation
"""

import ast
import logging
import re
import subprocess
from pathlib import Path

from multifleet.code_scanner.probe_contract import Finding, ProbeContext, ProbeResult

logger = logging.getLogger("fleet-nerve.code_scanner.blast_radius")


def _count_importers(repo: Path, target_module: str, py_files: list[Path]) -> list[str]:
    """Count files that import the target module."""
    importers = []
    # Normalize: "foo/bar.py" -> "foo.bar"
    module_parts = target_module.replace("/", ".").replace(".py", "")

    for py_file in py_files:
        try:
            source = py_file.read_text(errors="replace")
        except OSError:
            continue

        # Quick text check before expensive AST parse
        if module_parts not in source and module_parts.split(".")[-1] not in source:
            continue

        try:
            tree = ast.parse(source, filename=str(py_file))
        except (SyntaxError, UnicodeDecodeError):
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if module_parts in alias.name or alias.name.endswith(module_parts.split(".")[-1]):
                        importers.append(str(py_file.relative_to(repo)))
                        break
            elif isinstance(node, ast.ImportFrom) and node.module:
                if module_parts in node.module or node.module.endswith(module_parts.split(".")[-1]):
                    importers.append(str(py_file.relative_to(repo)))

    return importers


def _count_test_refs(repo: Path, target_file: str) -> list[str]:
    """Find tests that reference the changed file."""
    test_refs = []
    target_name = Path(target_file).stem  # e.g., "probe_engine"

    # Search in test directories
    test_dirs = [repo / "tests", repo / "test", repo / "multi-fleet" / "tests"]
    for test_dir in test_dirs:
        if not test_dir.is_dir():
            continue
        for test_file in test_dir.rglob("test_*.py"):
            try:
                source = test_file.read_text(errors="replace")
                if target_name in source:
                    test_refs.append(str(test_file.relative_to(repo)))
            except OSError:
                continue

    return test_refs


def _check_git_churn(repo: Path, file_path: str, days: int = 30) -> int:
    """Count how many times a file was modified in the last N days."""
    try:
        result = subprocess.run(
            ["git", "log", f"--since={days} days ago", "--oneline", "--follow", "--", file_path],
            capture_output=True, text=True, timeout=10,
            cwd=str(repo),
        )
        if result.returncode == 0:
            return len(result.stdout.strip().splitlines())
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return 0


def _is_public_api(repo: Path, file_path: str) -> bool:
    """Check if the file exposes public API (has __all__ or is __init__.py)."""
    full = repo / file_path
    if not full.exists() or not full.suffix == ".py":
        return False

    if full.name == "__init__.py":
        return True

    try:
        source = full.read_text(errors="replace")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "__all__":
                        return True
        # Also check for public classes/functions (no underscore prefix)
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if not node.name.startswith("_"):
                    return True
    except (SyntaxError, UnicodeDecodeError, OSError):
        pass

    return False


def _collect_py_files(repo: Path) -> list[Path]:
    """Collect Python files, skipping venvs."""
    skip = {".venv", "venv", "node_modules", "__pycache__", ".git"}
    py_files = []
    for f in repo.rglob("*.py"):
        if any(p in f.parts for p in skip):
            continue
        py_files.append(f)
        if len(py_files) >= 300:
            break
    return py_files


def probe_blast_radius(context: ProbeContext) -> ProbeResult:
    """Estimate change impact for modified files."""
    repo = Path(context.repo_path).resolve()
    changed = context.changed_files

    if not changed:
        return ProbeResult.skipped("blast_radius", ["no changed_files in context"])

    py_files = _collect_py_files(repo)
    all_findings: list[Finding] = []

    for file_path in changed[:10]:  # Cap at 10 files
        if not file_path.endswith(".py"):
            continue

        full = repo / file_path
        if not full.exists():
            continue

        # 1. Direct dependents
        module_name = file_path.replace("/", ".").replace(".py", "")
        importers = _count_importers(repo, module_name, py_files)

        # 2. Test references
        test_refs = _count_test_refs(repo, file_path)

        # 3. Git churn
        churn = _check_git_churn(repo, file_path)

        # 4. Public API check
        is_public = _is_public_api(repo, file_path)

        # Score blast radius
        importer_count = len(importers)
        test_count = len(test_refs)
        hot = churn > 10

        if importer_count > 10 or (is_public and importer_count > 5):
            radius = "high"
            severity = "high"
        elif importer_count > 3 or (is_public and importer_count > 1):
            radius = "medium"
            severity = "medium"
        else:
            radius = "low"
            severity = "low"

        # Only report medium+ blast radius
        if radius == "low" and not hot:
            continue

        evidence = [
            f"Direct importers: {importer_count}",
            f"Test files referencing: {test_count}",
            f"Git commits (30d): {churn}",
            f"Public API: {is_public}",
        ]
        if importers:
            evidence.extend([f"  Imported by: {f}" for f in importers[:5]])

        actions = []
        if test_count == 0 and importer_count > 2:
            actions.append(f"Add tests -- {importer_count} dependents but 0 tests")
        if hot:
            actions.append(f"Hot file ({churn} commits/30d) -- consider refactoring")
        if is_public and importer_count > 5:
            actions.append("Public API with many dependents -- changes need careful review")

        all_findings.append(Finding(
            title=f"Blast radius for {file_path}: {radius}",
            summary=f"{importer_count} importers, {test_count} tests, churn={churn}, public={is_public}",
            severity=severity,
            confidence=0.8,
            blast_radius=radius,
            evidence_refs=evidence,
            suggested_actions=actions,
            file_path=file_path,
        ))

    if not all_findings:
        return ProbeResult.ok("blast_radius", confidence=0.85)

    return ProbeResult(
        probe_id="blast_radius",
        status="warn",
        confidence=0.8,
        findings=all_findings,
    )


def register(engine) -> None:
    """Auto-discovery hook."""
    engine.register(
        probe_id="blast_radius",
        probe_fn=probe_blast_radius,
        category="change",
        cost="medium",  # AST parsing + git log
    )
