"""Mirror Drift Probe -- detects where docs say X but code does Y.

THE DEMO CLIMAX PROBE. Every developer instantly recognizes "stale docs."
Highest-ROI probe: catches the gap between what documentation claims
and what the filesystem/code actually provides.

Checks:
1. CLAUDE.md/README.md path references vs filesystem reality
2. README import examples vs actual module exports
3. Docstring @param claims vs actual function signatures
4. .env.example keys vs code os.environ/getenv reads
5. Directory structure claims in docs vs actual ls
"""

import ast
import logging
import os
import re
from pathlib import Path
from typing import Optional

from multifleet.code_scanner.probe_contract import Finding, ProbeContext, ProbeResult

logger = logging.getLogger("fleet-nerve.code_scanner.mirror_drift")

# ── Path reference extraction ──

_PATH_RE = re.compile(
    r"(?:^|[\s\(`\"])"
    r"((?:[\w\-\.]+/)+[\w\-\.]+)"
    r"(?:[\s\)`\"]|$)",
)
_SKIP_RE = re.compile(r"https?://|^\d+\.\d+|/\*|application/|text/|image/|node_modules/")

# ── Import extraction from code blocks ──

_IMPORT_RE = re.compile(
    r"(?:from\s+([\w\.]+)\s+import|import\s+([\w\.]+))"
)

# ── Env var extraction ──

_ENVVAR_CODE_RE = re.compile(
    r"""(?:os\.environ(?:\.get)?\s*\(\s*['"](\w+)['"]|"""
    r"""os\.getenv\s*\(\s*['"](\w+)['"]|"""
    r"""os\.environ\s*\[\s*['"](\w+)['"])""",
)
_ENVVAR_DOTENV_RE = re.compile(r"^([A-Z][A-Z0-9_]+)=", re.MULTILINE)

# ── Directory structure blocks in docs ──

_DIR_BLOCK_RE = re.compile(
    r"```(?:text|bash|shell)?\s*\n((?:.*?[/├└│─┬].*?\n)+)```",
    re.MULTILINE,
)
_DIR_ENTRY_RE = re.compile(r"[├└│─┬\s]*(\S+/?)")


def _extract_paths(text: str) -> list[str]:
    """Extract plausible file paths from markdown text."""
    paths = []
    for m in _PATH_RE.finditer(text):
        candidate = m.group(1)
        if _SKIP_RE.search(candidate):
            continue
        if len(candidate) < 5 or len(candidate) > 200:
            continue
        paths.append(candidate)
    return list(set(paths))


def _check_path_refs(repo: Path, doc_path: Path, text: str) -> list[Finding]:
    """Check 1: File path references in docs vs filesystem."""
    findings = []
    referenced = _extract_paths(text)
    missing = [p for p in referenced if not (repo / p).exists()]

    if missing:
        findings.append(Finding(
            title=f"{doc_path.name}: {len(missing)} referenced path(s) missing",
            summary=f"Doc references files/dirs that don't exist on disk: {', '.join(missing[:5])}",
            severity="high" if len(missing) > 5 else "medium",
            confidence=0.8,
            blast_radius="medium" if len(missing) > 3 else "low",
            evidence_refs=[f"Missing: {p}" for p in missing[:10]],
            suggested_actions=["Update doc references", "Restore moved/deleted files"],
            file_path=str(doc_path),
        ))
    return findings


def _check_import_examples(repo: Path, doc_path: Path, text: str) -> list[Finding]:
    """Check 2: Import examples in README vs actual module exports."""
    findings = []
    # Extract code blocks
    code_blocks = re.findall(r"```python\s*\n(.*?)```", text, re.DOTALL)
    if not code_blocks:
        return findings

    broken_imports = []
    for block in code_blocks:
        for m in _IMPORT_RE.finditer(block):
            module_name = m.group(1) or m.group(2)
            if not module_name:
                continue
            # Convert module path to file path
            parts = module_name.split(".")
            # Check common locations
            candidates = [
                repo / "/".join(parts) / "__init__.py",
                repo / "/".join(parts) + ".py",
                repo / "src" / "/".join(parts) / "__init__.py",
                repo / "src" / "/".join(parts) + ".py",
            ]
            if not any(c.exists() for c in candidates):
                # Could be a third-party package -- skip if it looks like one
                if len(parts) == 1 and parts[0] in (
                    "os", "sys", "json", "time", "re", "pathlib", "typing",
                    "dataclasses", "logging", "asyncio", "subprocess", "hashlib",
                    "uuid", "socket", "threading", "collections", "functools",
                    "itertools", "contextlib", "abc", "enum", "copy",
                    "pytest", "unittest", "mock", "requests", "flask",
                    "aiohttp", "nats", "redis", "sqlite3", "yaml",
                ):
                    continue
                broken_imports.append(module_name)

    if broken_imports:
        findings.append(Finding(
            title=f"{doc_path.name}: {len(broken_imports)} import example(s) may be broken",
            summary=f"Documented imports that don't resolve locally: {', '.join(broken_imports[:5])}",
            severity="medium",
            confidence=0.6,  # Lower -- could be installed packages
            blast_radius="low",
            evidence_refs=[f"Import: {imp}" for imp in broken_imports[:10]],
            suggested_actions=["Verify import paths", "Update examples"],
            file_path=str(doc_path),
        ))
    return findings


def _check_param_drift(repo: Path, changed_files: list[str]) -> list[Finding]:
    """Check 3: @param docstring claims vs actual function signatures."""
    findings = []
    py_files = changed_files if changed_files else []
    # If no changed files, skip (expensive to scan entire repo)
    if not py_files:
        return findings

    _PARAM_DOC_RE = re.compile(r"@param\s+(\w+)|:param\s+(\w+)|Args:\s*\n((?:\s+\w+.*\n)+)")

    for fpath in py_files[:20]:  # Cap at 20 files for performance
        full = repo / fpath
        if not full.exists() or not full.suffix == ".py":
            continue
        try:
            source = full.read_text(errors="replace")
            tree = ast.parse(source, filename=str(full))
        except (SyntaxError, UnicodeDecodeError):
            continue

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            docstring = ast.get_docstring(node)
            if not docstring:
                continue

            # Get actual param names
            actual_params = set()
            for arg in node.args.args + node.args.posonlyargs + node.args.kwonlyargs:
                if arg.arg != "self" and arg.arg != "cls":
                    actual_params.add(arg.arg)
            if node.args.vararg:
                actual_params.add(node.args.vararg.arg)
            if node.args.kwarg:
                actual_params.add(node.args.kwarg.arg)

            # Get documented param names
            doc_params = set()
            for m in re.finditer(r"(?:@param|:param)\s+(\w+)", docstring):
                doc_params.add(m.group(1))
            # Google-style Args: section
            args_match = re.search(r"Args:\s*\n((?:\s+\w+.*\n)+)", docstring)
            if args_match:
                for line in args_match.group(1).strip().split("\n"):
                    param_match = re.match(r"\s+(\w+)", line)
                    if param_match:
                        doc_params.add(param_match.group(1))

            if not doc_params:
                continue

            # Find mismatches
            doc_only = doc_params - actual_params
            if doc_only:
                findings.append(Finding(
                    title=f"Param drift in {node.name}(): docs mention {doc_only}",
                    summary=f"Docstring documents params that don't exist in signature",
                    severity="low",
                    confidence=0.85,
                    blast_radius="low",
                    evidence_refs=[
                        f"Documented: {sorted(doc_params)}",
                        f"Actual: {sorted(actual_params)}",
                    ],
                    suggested_actions=["Update docstring params"],
                    file_path=str(full),
                    line_number=node.lineno,
                ))

    return findings


def _check_env_drift(repo: Path) -> list[Finding]:
    """Check 4: .env.example keys vs code os.environ/getenv reads."""
    findings = []
    env_example = repo / ".env.example"
    if not env_example.exists():
        return findings

    # Parse .env.example keys
    env_text = env_example.read_text(errors="replace")
    example_keys = set(_ENVVAR_DOTENV_RE.findall(env_text))
    if not example_keys:
        return findings

    # Scan Python files for env var usage (limit to top-level and key dirs)
    code_keys: set[str] = set()
    scan_dirs = [repo]
    for scan_dir in scan_dirs:
        for py_file in scan_dir.rglob("*.py"):
            # Skip venvs, node_modules, etc.
            parts = py_file.parts
            if any(p in parts for p in (".venv", "venv", "node_modules", "__pycache__", ".git")):
                continue
            try:
                source = py_file.read_text(errors="replace")[:50000]  # Cap read size
            except OSError:
                continue
            for m in _ENVVAR_CODE_RE.finditer(source):
                key = m.group(1) or m.group(2) or m.group(3)
                if key:
                    code_keys.add(key)

    # Keys in code but not in .env.example
    undocumented = code_keys - example_keys
    # Filter out common stdlib env vars
    undocumented -= {"PATH", "HOME", "USER", "SHELL", "TERM", "LANG",
                     "PYTHONPATH", "VIRTUAL_ENV", "PWD", "TMPDIR",
                     "CI", "DEBUG", "TESTING", "LOG_LEVEL"}

    if undocumented:
        findings.append(Finding(
            title=f"{len(undocumented)} env var(s) used in code but missing from .env.example",
            summary=f"Code reads env vars not documented: {', '.join(sorted(undocumented)[:8])}",
            severity="medium" if len(undocumented) > 3 else "low",
            confidence=0.7,
            blast_radius="medium",
            evidence_refs=[f"Undocumented: {k}" for k in sorted(undocumented)[:10]],
            suggested_actions=["Add missing vars to .env.example"],
            file_path=str(env_example),
        ))

    return findings


def _check_dir_structure_claims(repo: Path, text: str, doc_path: Path) -> list[Finding]:
    """Check 5: Directory structure blocks in docs vs reality."""
    findings = []

    for block_match in _DIR_BLOCK_RE.finditer(text):
        block = block_match.group(1)
        claimed_entries = []
        for line in block.split("\n"):
            m = _DIR_ENTRY_RE.search(line)
            if m:
                entry = m.group(1).rstrip("/")
                if entry and len(entry) > 1 and not entry.startswith("#"):
                    claimed_entries.append(entry)

        missing_entries = []
        for entry in claimed_entries:
            # Try as relative path from repo root
            if not (repo / entry).exists():
                missing_entries.append(entry)

        if missing_entries and len(missing_entries) < len(claimed_entries):
            # Only report if SOME entries exist (confirms it's a real dir listing)
            findings.append(Finding(
                title=f"{doc_path.name}: directory structure claim has {len(missing_entries)} missing entries",
                summary=f"Documented dir structure partially stale: {', '.join(missing_entries[:5])}",
                severity="low",
                confidence=0.6,
                blast_radius="low",
                evidence_refs=[f"Missing: {e}" for e in missing_entries[:10]],
                suggested_actions=["Update directory structure in docs"],
                file_path=str(doc_path),
            ))

    return findings


def probe_mirror_drift(context: ProbeContext) -> ProbeResult:
    """THE DEMO CLIMAX: detect where documentation claims don't match code reality.

    Runs all 5 mirror drift checks and aggregates findings.
    """
    repo = Path(context.repo_path).resolve()
    all_findings: list[Finding] = []
    missing_inputs: list[str] = []

    docs_to_check = [
        repo / "CLAUDE.md",
        repo / "README.md",
    ]

    checked_any = False
    for doc_path in docs_to_check:
        if not doc_path.exists():
            missing_inputs.append(str(doc_path.name))
            continue
        checked_any = True

        try:
            text = doc_path.read_text(errors="replace")
        except OSError as e:
            all_findings.append(Finding(
                title=f"Cannot read {doc_path.name}",
                summary=str(e), severity="low", confidence=1.0,
            ))
            continue

        # Check 1: Path references
        all_findings.extend(_check_path_refs(repo, doc_path, text))

        # Check 2: Import examples
        all_findings.extend(_check_import_examples(repo, doc_path, text))

        # Check 5: Directory structure claims
        all_findings.extend(_check_dir_structure_claims(repo, text, doc_path))

    # Check 3: Param drift (only on changed files -- medium cost)
    if context.changed_files:
        all_findings.extend(_check_param_drift(repo, context.changed_files))

    # Check 4: Env var drift
    all_findings.extend(_check_env_drift(repo))

    if not checked_any and not all_findings:
        return ProbeResult.skipped("mirror_drift", missing_inputs)

    if not all_findings:
        return ProbeResult.ok("mirror_drift", confidence=0.9)

    # Aggregate severity: highest finding wins
    max_sev = max(
        (f.severity for f in all_findings),
        key=lambda s: {"low": 0, "medium": 1, "high": 2}.get(s, 0),
    )

    return ProbeResult(
        probe_id="mirror_drift",
        status="warn",
        confidence=0.75,
        findings=all_findings,
        missing_inputs=missing_inputs,
    )


def register(engine) -> None:
    """Auto-discovery hook: register this probe with the engine."""
    engine.register(
        probe_id="mirror_drift",
        probe_fn=probe_mirror_drift,
        category="context_quality",
        cost="cheap",  # Path checks are fast; env scan bumps toward medium
    )
