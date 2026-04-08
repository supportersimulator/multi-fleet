"""Injection Prioritizer Probe -- "what should be in the next prompt?"

Analyzes what context is most relevant right now and produces a ranked list
of files/facts to inject into the next prompt.

Checks:
1. Which files were recently modified (git log, last 24h)
2. Which files have the most imports (central to the codebase)
3. Which files have failing tests (from last test run if available)
4. Which areas have active rebuttals or unresolved objections
5. Produces a ranked list of "inject these files/facts into the next prompt"
"""

import ast
import logging
import re
import subprocess
from collections import Counter
from pathlib import Path

from multifleet.code_scanner.probe_contract import Finding, ProbeContext, ProbeResult

logger = logging.getLogger("fleet-nerve.code_scanner.injection_prioritizer")


def _recently_modified(repo: Path, hours: int = 24) -> list[tuple[str, int]]:
    """Check 1: Files modified in last N hours, with commit counts."""
    try:
        result = subprocess.run(
            ["git", "log", f"--since={hours} hours ago", "--name-only",
             "--pretty=format:", "--diff-filter=AMRC"],
            capture_output=True, text=True, timeout=15, cwd=str(repo),
        )
        if result.returncode != 0:
            return []
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []

    counts: Counter[str] = Counter()
    for line in result.stdout.strip().splitlines():
        line = line.strip()
        if line and not line.startswith("commit "):
            counts[line] += 1

    return counts.most_common(20)


def _most_imported(repo: Path, py_files: list[Path]) -> list[tuple[str, int]]:
    """Check 2: Files with the most importers (central modules)."""
    import_counts: Counter[str] = Counter()

    for py_file in py_files[:100]:
        try:
            source = py_file.read_text(errors="replace")
            tree = ast.parse(source, filename=str(py_file))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    import_counts[top] += 1
            elif isinstance(node, ast.ImportFrom) and node.module:
                # Track full module path for local modules
                parts = node.module.split(".")
                # Check if it resolves to a local file
                candidate = repo / "/".join(parts)
                candidate_py = repo / ("/".join(parts) + ".py")
                if candidate.exists() or candidate_py.exists():
                    import_counts[node.module] += 1
                else:
                    import_counts[parts[0]] += 1

    # Filter to only local modules (files that exist in repo)
    local_imports = []
    for module, count in import_counts.most_common(50):
        path_parts = module.replace(".", "/")
        if (repo / path_parts).exists() or (repo / f"{path_parts}.py").exists():
            local_imports.append((module, count))
            if len(local_imports) >= 15:
                break

    return local_imports


def _failing_tests(repo: Path) -> list[str]:
    """Check 3: Files with failing tests (parse pytest output if available)."""
    # Check for common test result files
    result_locations = [
        repo / ".pytest_cache" / "v" / "cache" / "lastfailed",
        repo / "test-results.xml",
        repo / "junit.xml",
    ]

    failing = []
    for loc in result_locations:
        if not loc.exists():
            continue

        try:
            text = loc.read_text(errors="replace")
            if loc.name == "lastfailed":
                # pytest lastfailed is a dict: {"test_path::test_name": True}
                import json
                try:
                    data = json.loads(text)
                    failing.extend(list(data.keys())[:10])
                except (json.JSONDecodeError, TypeError):
                    pass
            elif loc.suffix == ".xml":
                # Basic parse of JUnit XML for failures
                for m in re.finditer(r'<testcase.*?classname="([^"]+)".*?>'
                                     r'.*?<failure', text, re.DOTALL):
                    failing.append(m.group(1))
                    if len(failing) >= 10:
                        break
        except OSError:
            continue

    return failing[:10]


def _active_rebuttals(repo: Path) -> list[str]:
    """Check 4: Areas with active rebuttals or unresolved objections."""
    rebuttal_markers = re.compile(
        r"(?:REBUTTAL|OBJECTION|DISAGREE|COUNTER|UNRESOLVED|OPEN QUESTION|"
        r"TODO.*debate|FIXME.*disagree|REVIEW NEEDED)",
        re.IGNORECASE,
    )

    hits: list[str] = []
    skip = {".venv", "venv", "node_modules", "__pycache__", ".git"}

    # Check markdown docs and code comments
    for ext in ("*.md", "*.py", "*.ts"):
        for fpath in repo.rglob(ext):
            if any(p in fpath.parts for p in skip):
                continue
            try:
                text = fpath.read_text(errors="replace")[:20000]
            except OSError:
                continue
            if rebuttal_markers.search(text):
                hits.append(str(fpath.relative_to(repo)))
                if len(hits) >= 10:
                    break
        if len(hits) >= 10:
            break

    return hits


def _collect_py_files(repo: Path) -> list[Path]:
    skip = {".venv", "venv", "node_modules", "__pycache__", ".git", ".tox", "dist", "build"}
    py_files = []
    for f in repo.rglob("*.py"):
        if any(p in f.parts for p in skip):
            continue
        py_files.append(f)
        if len(py_files) >= 200:
            break
    return py_files


def probe_injection_prioritizer(context: ProbeContext) -> ProbeResult:
    """Analyze what context is most relevant for the next prompt injection."""
    repo = Path(context.repo_path).resolve()
    if not repo.is_dir():
        return ProbeResult.error("injection_prioritizer", f"Repo path not found: {repo}")

    py_files = _collect_py_files(repo)

    # Gather signals
    recent = _recently_modified(repo)
    central = _most_imported(repo, py_files)
    failing = _failing_tests(repo)
    rebuttals = _active_rebuttals(repo)

    # Score and rank: recent files get base score, boosted if also central or failing
    scores: Counter[str] = Counter()

    for fname, commit_count in recent:
        scores[fname] += commit_count * 3  # Recent changes are high signal

    for module, import_count in central:
        path_key = module.replace(".", "/")
        scores[path_key] += import_count  # Central modules are important context
        scores[f"{path_key}.py"] += import_count

    for test_ref in failing:
        # Map test class to source file
        parts = test_ref.replace("::", "/").split("/")
        scores["/".join(parts[:2]) if len(parts) > 1 else test_ref] += 5  # Failing = urgent

    for rebuttal_file in rebuttals:
        scores[rebuttal_file] += 4  # Unresolved debates need attention

    if not scores:
        return ProbeResult.ok("injection_prioritizer", confidence=0.5)

    # Build ranked findings
    ranked = scores.most_common(15)
    all_findings: list[Finding] = []

    # Top recommendation
    evidence = []
    for fname, score in ranked:
        reasons = []
        if any(fname == f or fname.endswith(f) for f, _ in recent):
            reasons.append("recently modified")
        if any(fname.replace("/", ".").replace(".py", "") == m for m, _ in central):
            reasons.append("highly imported")
        if fname in [r for r in rebuttals]:
            reasons.append("has rebuttals")
        reason_str = f" ({', '.join(reasons)})" if reasons else ""
        evidence.append(f"  {score:3d}pts  {fname}{reason_str}")

    all_findings.append(Finding(
        title=f"Injection priority: {len(ranked)} files ranked",
        summary=f"Top: {ranked[0][0]} ({ranked[0][1]}pts)" if ranked else "No files ranked",
        severity="low",
        confidence=0.7,
        blast_radius="low",
        evidence_refs=evidence[:15],
        suggested_actions=[
            f"Inject context from: {', '.join(f for f, _ in ranked[:5])}",
        ],
    ))

    if failing:
        all_findings.append(Finding(
            title=f"{len(failing)} failing test(s) detected",
            summary=f"Tests failing: {', '.join(failing[:5])}",
            severity="medium",
            confidence=0.8,
            blast_radius="medium",
            evidence_refs=[f"Failing: {t}" for t in failing[:10]],
            suggested_actions=["Fix failing tests before other work"],
        ))

    if rebuttals:
        all_findings.append(Finding(
            title=f"{len(rebuttals)} file(s) with unresolved objections",
            summary=f"Active rebuttals in: {', '.join(rebuttals[:5])}",
            severity="low",
            confidence=0.6,
            blast_radius="low",
            evidence_refs=[f"Rebuttal in: {r}" for r in rebuttals[:10]],
            suggested_actions=["Review and resolve open objections"],
        ))

    return ProbeResult(
        probe_id="injection_prioritizer",
        status="warn" if failing else "ok",
        confidence=0.7,
        findings=all_findings,
    )


def register(engine) -> None:
    """Auto-discovery hook."""
    engine.register(
        probe_id="injection_prioritizer",
        probe_fn=probe_injection_prioritizer,
        category="context_quality",
        cost="medium",  # git log + AST analysis
    )
