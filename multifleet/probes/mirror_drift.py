"""Mirror Drift Probe — detects where docs say X but code does Y.

Comprehensive mirror drift detection (merged from code_scanner + probe_engine).
Checks:
1. CLAUDE.md/README.md path references vs filesystem reality
2. README import examples vs actual module exports
3. Docstring @param claims vs actual function signatures
4. .env.example keys vs code os.environ/getenv reads
5. Directory structure claims in docs vs actual ls
"""

import ast
import logging
import re
from pathlib import Path

from multifleet.probe_engine import ProbeFinding, ProbeResult, ProbeSeverity

logger = logging.getLogger("fleet-nerve.probes.mirror_drift")

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
    r"```(?:text|bash|shell)?\s*\n((?:.*?[/\u2500\u2502\u2514\u251c\u252c].*?\n)+)```",
    re.MULTILINE,
)
_DIR_ENTRY_RE = re.compile(r"[\u2500\u2502\u2514\u251c\u252c\s]*(\S+/?)")

# Common third-party/stdlib modules to skip in import checks
_KNOWN_MODULES = frozenset({
    "os", "sys", "json", "time", "re", "pathlib", "typing",
    "dataclasses", "logging", "asyncio", "subprocess", "hashlib",
    "uuid", "socket", "threading", "collections", "functools",
    "itertools", "contextlib", "abc", "enum", "copy",
    "pytest", "unittest", "mock", "requests", "flask",
    "aiohttp", "nats", "redis", "sqlite3", "yaml",
})


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


def _check_path_refs(repo: Path, doc_path: Path, text: str) -> list[ProbeFinding]:
    """Check 1: File path references in docs vs filesystem."""
    referenced = _extract_paths(text)
    missing = [p for p in referenced if not (repo / p).exists()]

    if not missing:
        return []

    return [ProbeFinding(
        title=f"{doc_path.name} references {len(missing)} non-existent path(s)",
        evidence=[f"Missing: {p}" for p in missing[:10]],
        blast_radius="medium" if len(missing) > 3 else "low",
        suggested_injections=["Update doc references", "Verify moved files"],
        file_paths=[str(doc_path)] + [str(repo / p) for p in missing[:5]],
    )]


def _check_import_examples(repo: Path, doc_path: Path, text: str) -> list[ProbeFinding]:
    """Check 2: Import examples in README vs actual module exports."""
    code_blocks = re.findall(r"```python\s*\n(.*?)```", text, re.DOTALL)
    if not code_blocks:
        return []

    broken_imports = []
    for block in code_blocks:
        for m in _IMPORT_RE.finditer(block):
            module_name = m.group(1) or m.group(2)
            if not module_name:
                continue
            parts = module_name.split(".")
            candidates = [
                repo / "/".join(parts) / "__init__.py",
                repo / "/".join(parts) + ".py",
                repo / "src" / "/".join(parts) / "__init__.py",
                repo / "src" / "/".join(parts) + ".py",
            ]
            if not any(c.exists() for c in candidates):
                if len(parts) == 1 and parts[0] in _KNOWN_MODULES:
                    continue
                broken_imports.append(module_name)

    if not broken_imports:
        return []

    return [ProbeFinding(
        title=f"{doc_path.name}: {len(broken_imports)} import example(s) may be broken",
        evidence=[f"Import: {imp}" for imp in broken_imports[:10]],
        blast_radius="low",
        suggested_injections=["Verify import paths", "Update examples"],
        file_paths=[str(doc_path)],
    )]


def _check_param_drift(repo: Path, changed_files: list[str]) -> list[ProbeFinding]:
    """Check 3: @param docstring claims vs actual function signatures."""
    if not changed_files:
        return []

    findings = []
    for fpath in changed_files[:20]:
        full = repo / fpath
        if not full.exists() or full.suffix != ".py":
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

            actual_params = set()
            for arg in node.args.args + node.args.posonlyargs + node.args.kwonlyargs:
                if arg.arg not in ("self", "cls"):
                    actual_params.add(arg.arg)
            if node.args.vararg:
                actual_params.add(node.args.vararg.arg)
            if node.args.kwarg:
                actual_params.add(node.args.kwarg.arg)

            doc_params = set()
            for m in re.finditer(r"(?:@param|:param)\s+(\w+)", docstring):
                doc_params.add(m.group(1))
            args_match = re.search(r"Args:\s*\n((?:\s+\w+.*\n)+)", docstring)
            if args_match:
                for line in args_match.group(1).strip().split("\n"):
                    param_match = re.match(r"\s+(\w+)", line)
                    if param_match:
                        doc_params.add(param_match.group(1))

            if not doc_params:
                continue

            doc_only = doc_params - actual_params
            if doc_only:
                findings.append(ProbeFinding(
                    title=f"Param drift in {node.name}(): docs mention {doc_only}",
                    evidence=[
                        f"Documented: {sorted(doc_params)}",
                        f"Actual: {sorted(actual_params)}",
                    ],
                    blast_radius="low",
                    suggested_injections=["Update docstring params"],
                    file_paths=[str(full)],
                ))

    return findings


def _check_env_drift(repo: Path) -> list[ProbeFinding]:
    """Check 4: .env.example keys vs code os.environ/getenv reads."""
    env_example = repo / ".env.example"
    if not env_example.exists():
        return []

    try:
        env_text = env_example.read_text(errors="replace")
    except OSError:
        return []

    example_keys = set(_ENVVAR_DOTENV_RE.findall(env_text))
    if not example_keys:
        return []

    code_keys: set[str] = set()
    skip = {".venv", "venv", "node_modules", "__pycache__", ".git"}
    for py_file in repo.rglob("*.py"):
        if any(p in py_file.parts for p in skip):
            continue
        try:
            source = py_file.read_text(errors="replace")[:50000]
        except OSError:
            continue
        for m in _ENVVAR_CODE_RE.finditer(source):
            key = m.group(1) or m.group(2) or m.group(3)
            if key:
                code_keys.add(key)

    undocumented = code_keys - example_keys
    undocumented -= {
        "PATH", "HOME", "USER", "SHELL", "TERM", "LANG",
        "PYTHONPATH", "VIRTUAL_ENV", "PWD", "TMPDIR",
        "CI", "DEBUG", "TESTING", "LOG_LEVEL",
    }

    if not undocumented:
        return []

    return [ProbeFinding(
        title=f"{len(undocumented)} env var(s) used in code but missing from .env.example",
        evidence=[f"Undocumented: {k}" for k in sorted(undocumented)[:10]],
        blast_radius="medium",
        suggested_injections=["Add missing vars to .env.example"],
        file_paths=[str(env_example)],
    )]


def _check_dir_structure_claims(repo: Path, text: str, doc_path: Path) -> list[ProbeFinding]:
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

        missing_entries = [e for e in claimed_entries if not (repo / e).exists()]

        if missing_entries and len(missing_entries) < len(claimed_entries):
            findings.append(ProbeFinding(
                title=f"{doc_path.name}: directory structure claim has {len(missing_entries)} missing entries",
                evidence=[f"Missing: {e}" for e in missing_entries[:10]],
                blast_radius="low",
                suggested_injections=["Update directory structure in docs"],
                file_paths=[str(doc_path)],
            ))

    return findings


def probe_mirror_drift(context: dict) -> ProbeResult:
    """Comprehensive mirror drift: detect where docs claim X but code does Y.

    Runs all 5 mirror drift checks and aggregates findings.

    Context keys:
        repo_root (str): Path to repository root.
        changed_files (list[str]): Files changed (for param drift check).
    """
    repo = Path(context.get("repo_root", ".")).resolve()
    all_findings: list[ProbeFinding] = []
    missing_inputs: list[str] = []

    docs_to_check = [
        repo / "CLAUDE.md",
        repo / "README.md",
    ]

    checked_any = False
    for doc_path in docs_to_check:
        if not doc_path.exists():
            missing_inputs.append(str(doc_path))
            continue
        checked_any = True

        try:
            text = doc_path.read_text(errors="replace")
        except OSError:
            continue

        # Check 1: Path references
        all_findings.extend(_check_path_refs(repo, doc_path, text))

        # Check 2: Import examples
        all_findings.extend(_check_import_examples(repo, doc_path, text))

        # Check 5: Directory structure claims
        all_findings.extend(_check_dir_structure_claims(repo, text, doc_path))

    # Check 3: Param drift (only on changed files -- medium cost)
    changed_files = context.get("changed_files", [])
    if changed_files:
        all_findings.extend(_check_param_drift(repo, changed_files))

    # Check 4: Env var drift
    all_findings.extend(_check_env_drift(repo))

    if not checked_any and not all_findings:
        return ProbeResult.skipped("mirror_drift", "context_quality", missing_inputs)

    severity = "info"
    if all_findings:
        total = sum(len(f.evidence) for f in all_findings)
        if total > 5:
            severity = "high"
        elif total > 2:
            severity = "medium"
        else:
            severity = "low"

    return ProbeResult(
        probe_id="mirror_drift",
        category="context_quality",
        status="warning" if all_findings else "ok",
        confidence=0.75 if all_findings else 0.9,
        severity=severity,
        findings=all_findings,
        missing_inputs=missing_inputs,
    )
