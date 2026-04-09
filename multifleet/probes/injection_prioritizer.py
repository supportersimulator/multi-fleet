"""Injection Prioritizer Probe — ranks context for next prompt injection.

Comprehensive injection prioritizer (merged from code_scanner + probe_engine).
Checks:
1. Which files were recently modified (git log, last 24h)
2. Which files have the most imports (central to the codebase)
3. Which files have failing tests (from last test run if available)
4. Which areas have active rebuttals or unresolved objections
5. Which files are currently changed in the working tree
6. Produces a ranked list of "inject these files/facts into the next prompt"
"""

import ast
import glob
import json
import logging
import re
import subprocess
from collections import Counter, defaultdict
from pathlib import Path

from multifleet.probe_engine import ProbeFinding, ProbeResult

logger = logging.getLogger("fleet-nerve.probes.injection_prioritizer")

_DEFAULT_AUDIT_DIR = Path.home() / ".fleet-nerve"
_TOP_N = 10


def _git(args: list[str], cwd: str, timeout: int = 10) -> tuple[int, str]:
    """Run a git command, return (returncode, stdout)."""
    try:
        r = subprocess.run(
            ["git"] + args,
            cwd=cwd, capture_output=True, text=True, timeout=timeout,
        )
        return r.returncode, r.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return -1, str(e)


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
                parts = node.module.split(".")
                candidate = repo / "/".join(parts)
                candidate_py = repo / ("/".join(parts) + ".py")
                if candidate.exists() or candidate_py.exists():
                    import_counts[node.module] += 1
                else:
                    import_counts[parts[0]] += 1

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
                try:
                    data = json.loads(text)
                    failing.extend(list(data.keys())[:10])
                except (json.JSONDecodeError, TypeError):
                    pass
            elif loc.suffix == ".xml":
                for m in re.finditer(
                    r'<testcase.*?classname="([^"]+)".*?>.*?<failure',
                    text, re.DOTALL,
                ):
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


def _get_changed_files(repo_root: str) -> list[str]:
    """Check 5: Get files changed in working tree + staged."""
    rc, out = _git(["diff", "--name-only", "HEAD"], repo_root)
    files = out.splitlines() if rc == 0 and out else []
    rc2, out2 = _git(["diff", "--name-only", "--cached"], repo_root)
    if rc2 == 0 and out2:
        files.extend(out2.splitlines())
    return list(set(files))


def _get_recent_failures(audit_dir: Path, limit: int = 50) -> list[dict]:
    """Load recent error events from audit trail."""
    errors = []
    pattern = str(audit_dir / "audit-*.jsonl")
    for filepath in sorted(glob.glob(pattern), reverse=True):
        try:
            with open(filepath) as f:
                lines = f.readlines()
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                status = str(event.get("status", "")).lower()
                level = str(event.get("level", "")).lower()
                if "error" in status or "fail" in status or level == "error":
                    errors.append(event)
                    if len(errors) >= limit:
                        return errors
        except OSError:
            continue
    return errors


def _extract_failure_files(failures: list[dict]) -> dict[str, int]:
    """Extract file paths mentioned in failures with frequency counts."""
    file_counts: dict[str, int] = defaultdict(int)
    for f in failures:
        for key in ("file", "file_path", "path", "target"):
            val = f.get(key)
            if val and isinstance(val, str) and "/" in val:
                file_counts[val] += 1
    return dict(file_counts)


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


def probe_injection_prioritizer(context: dict) -> ProbeResult:
    """Comprehensive injection prioritizer.

    Merges code_scanner's deep analysis (git history, AST import analysis,
    test failures, rebuttals) with probe_engine's audit-trail-aware scoring.

    Context keys:
        repo_root (str): Path to repository root.
        audit_dir (str): Path to audit trail directory (optional).
    """
    repo_root = context.get("repo_root", ".")
    repo = Path(repo_root).resolve()
    audit_dir = Path(context.get("audit_dir", str(_DEFAULT_AUDIT_DIR)))

    # Check git availability
    rc, _ = _git(["rev-parse", "--git-dir"], repo_root)
    if rc != 0:
        return ProbeResult.skipped(
            "injection_prioritizer", "agent_readiness", ["not a git repo"]
        )

    py_files = _collect_py_files(repo)

    # Gather signals from all sources
    recent = _recently_modified(repo)
    central = _most_imported(repo, py_files)
    failing = _failing_tests(repo)
    rebuttals = _active_rebuttals(repo)
    changed_files = _get_changed_files(repo_root)
    failures = _get_recent_failures(audit_dir) if audit_dir.exists() else []
    failure_files = _extract_failure_files(failures)

    # Score and rank: combine all signals
    scores: Counter[str] = Counter()

    for fname, commit_count in recent:
        scores[fname] += commit_count * 3

    for module, import_count in central:
        path_key = module.replace(".", "/")
        scores[path_key] += import_count
        scores[f"{path_key}.py"] += import_count

    for test_ref in failing:
        parts = test_ref.replace("::", "/").split("/")
        scores["/".join(parts[:2]) if len(parts) > 1 else test_ref] += 5

    for rebuttal_file in rebuttals:
        scores[rebuttal_file] += 4

    # Boost actively changed files
    for f in changed_files:
        scores[f] += 50
        if f in failure_files:
            scores[f] += 30 * failure_files[f]
        # Boost architectural hotspots
        hotspot_patterns = ["CLAUDE.md", "__init__.py", "probe_engine.py", "settings.json", "config", "schema"]
        for pat in hotspot_patterns:
            if pat in f:
                scores[f] += 20
                break

    # Score failure-only files
    for f, count in failure_files.items():
        if f not in changed_files:
            scores[f] += 25 * count

    # CLAUDE.md section relevance
    claude_md = repo / "CLAUDE.md"
    if claude_md.exists() and changed_files:
        changed_dirs = set()
        for f in changed_files:
            parts = f.split("/")
            if len(parts) > 1:
                changed_dirs.add(parts[0])
        if changed_dirs:
            scores["CLAUDE.md"] += 60

    if not scores:
        return ProbeResult(
            probe_id="injection_prioritizer",
            category="agent_readiness",
            status="ok",
            confidence=0.7,
            severity="info",
        )

    # Build ranked findings
    ranked = scores.most_common(_TOP_N)
    findings: list[ProbeFinding] = []

    evidence = []
    for fname, score in ranked:
        reasons = []
        if any(fname == f or fname.endswith(f) for f, _ in recent):
            reasons.append("recently modified")
        if any(fname.replace("/", ".").replace(".py", "") == m for m, _ in central):
            reasons.append("highly imported")
        if fname in rebuttals:
            reasons.append("has rebuttals")
        if fname in changed_files:
            reasons.append("currently changed")
        if fname in failure_files:
            reasons.append(f"recent failures: {failure_files[fname]}x")
        reason_str = f" ({', '.join(reasons)})" if reasons else ""
        evidence.append(f"  {score:3d}pts  {fname}{reason_str}")

    findings.append(ProbeFinding(
        title=f"Injection priority: {len(ranked)} files ranked",
        evidence=evidence[:15],
        blast_radius="low",
        suggested_injections=[
            f"Inject context from: {', '.join(f for f, _ in ranked[:5])}",
        ],
        file_paths=[f for f, _ in ranked[:5]],
    ))

    if failing:
        findings.append(ProbeFinding(
            title=f"{len(failing)} failing test(s) detected",
            evidence=[f"Failing: {t}" for t in failing[:10]],
            blast_radius="medium",
            suggested_injections=["Fix failing tests before other work"],
            file_paths=[],
        ))

    if rebuttals:
        findings.append(ProbeFinding(
            title=f"{len(rebuttals)} file(s) with unresolved objections",
            evidence=[f"Rebuttal in: {r}" for r in rebuttals[:10]],
            blast_radius="low",
            suggested_injections=["Review and resolve open objections"],
            file_paths=rebuttals[:5],
        ))

    has_failure_driven = bool(failing) or bool(failure_files)

    return ProbeResult(
        probe_id="injection_prioritizer",
        category="agent_readiness",
        status="warning" if has_failure_driven else "ok",
        confidence=0.75,
        severity="medium" if has_failure_driven else "info",
        findings=findings,
    )
