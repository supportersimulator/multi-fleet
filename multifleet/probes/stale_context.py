"""Stale Context Probe — detects outdated context files and fleet messages.

Comprehensive stale context detection (merged from code_scanner + probe_engine).
Checks:
1. CLAUDE.md last modified vs recent code changes
2. README.md references to files/functions that no longer exist
3. Comments referencing deprecated/legacy patterns
4. .env.example with keys no longer read by any code
5. package.json scripts referencing missing files
6. Fleet messages older than 7 days still in inbox (not archived)
7. Plan docs older than 30 days with status NOT STARTED
"""

import json
import logging
import re
import subprocess
import time
from pathlib import Path

from multifleet.probe_engine import ProbeFinding, ProbeResult

logger = logging.getLogger("fleet-nerve.probes.stale_context")

_SEVEN_DAYS = 7 * 24 * 3600
_THIRTY_DAYS = 30 * 24 * 3600
_NOT_STARTED_RE = re.compile(r"NOT\s*STARTED", re.IGNORECASE)

_DEPRECATED_PATTERNS = re.compile(
    r"(?:TODO.*remov|FIXME.*deprecat|DEPRECATED|HACK.*workaround|"
    r"# *old (?:api|version|approach)|# *legacy)",
    re.IGNORECASE,
)


def _git_mtime(repo: Path, file_path: str) -> float:
    """Get last commit timestamp for a file (epoch seconds). 0 if unknown."""
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%ct", "--", file_path],
            capture_output=True, text=True, timeout=10, cwd=str(repo),
        )
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, ValueError):
        pass
    return 0.0


def _recent_code_mtime(repo: Path, days: int = 7) -> float:
    """Get most recent commit timestamp for any code file in last N days."""
    try:
        result = subprocess.run(
            ["git", "log", "-1", f"--since={days} days ago", "--format=%ct",
             "--diff-filter=M", "--", "*.py", "*.ts", "*.js", "*.go", "*.rs"],
            capture_output=True, text=True, timeout=10, cwd=str(repo),
        )
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, ValueError):
        pass
    return 0.0


def _collect_py_files(repo: Path) -> list[Path]:
    """Collect Python files, skipping venvs and caches."""
    skip = {".venv", "venv", "node_modules", "__pycache__", ".git", ".tox", "dist", "build"}
    py_files = []
    for f in repo.rglob("*.py"):
        if any(p in f.parts for p in skip):
            continue
        py_files.append(f)
        if len(py_files) >= 200:
            break
    return py_files


def _check_stale_claudemd(repo: Path) -> list[ProbeFinding]:
    """Check 1: CLAUDE.md freshness vs code changes."""
    claude_md = repo / "CLAUDE.md"
    if not claude_md.exists():
        return []

    claude_mtime = _git_mtime(repo, "CLAUDE.md")
    code_mtime = _recent_code_mtime(repo)

    if claude_mtime and code_mtime and (code_mtime - claude_mtime) > 7 * 86400:
        days_stale = int((code_mtime - claude_mtime) / 86400)
        return [ProbeFinding(
            title=f"CLAUDE.md may be stale ({days_stale}d behind code)",
            evidence=[
                f"CLAUDE.md last commit: {int(claude_mtime)}",
                f"Latest code commit: {int(code_mtime)}",
            ],
            blast_radius="medium",
            suggested_injections=["Review CLAUDE.md for accuracy", "Update stale sections"],
            file_paths=["CLAUDE.md"],
        )]
    return []


def _check_readme_dead_refs(repo: Path) -> list[ProbeFinding]:
    """Check 2: README.md references to files/functions that no longer exist."""
    readme = repo / "README.md"
    if not readme.exists():
        return []

    try:
        text = readme.read_text(errors="replace")
    except OSError:
        return []

    path_re = re.compile(r"`([\w\-\.]+(?:/[\w\-\.]+)+)`")
    missing = [m.group(1) for m in path_re.finditer(text)
               if not (repo / m.group(1)).exists() and len(m.group(1)) > 3]

    if not missing:
        return []

    return [ProbeFinding(
        title=f"README.md: {len(missing)} dead reference(s)",
        evidence=[f"Missing: {p}" for p in missing[:10]],
        blast_radius="low",
        suggested_injections=["Update or remove dead references in README"],
        file_paths=["README.md"],
    )]


def _check_deprecated_comments(repo: Path, py_files: list[Path]) -> list[ProbeFinding]:
    """Check 3: Comments referencing old APIs or deprecated patterns."""
    hits: list[tuple[str, int, str]] = []

    for py_file in py_files[:50]:
        try:
            lines = py_file.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            if _DEPRECATED_PATTERNS.search(line):
                rel = str(py_file.relative_to(repo))
                hits.append((rel, i, line.strip()[:80]))
                if len(hits) >= 20:
                    break
        if len(hits) >= 20:
            break

    if not hits:
        return []

    return [ProbeFinding(
        title=f"{len(hits)} deprecated/legacy comment(s) found",
        evidence=[f"{f}:{ln}: {txt}" for f, ln, txt in hits[:10]],
        blast_radius="low",
        suggested_injections=["Review and resolve deprecated markers"],
        file_paths=list(set(f for f, _, _ in hits))[:5],
    )]


def _check_env_example_stale(repo: Path, py_files: list[Path]) -> list[ProbeFinding]:
    """Check 4: .env.example keys no longer read by any code."""
    env_example = repo / ".env.example"
    if not env_example.exists():
        return []

    try:
        text = env_example.read_text(errors="replace")
    except OSError:
        return []

    env_keys = re.findall(r"^([A-Z][A-Z0-9_]+)=", text, re.MULTILINE)
    if not env_keys:
        return []

    all_code = ""
    for py_file in py_files[:100]:
        try:
            all_code += py_file.read_text(errors="replace")
        except OSError:
            continue

    unused = [k for k in env_keys if k not in all_code]
    if not unused:
        return []

    return [ProbeFinding(
        title=f"{len(unused)} .env.example key(s) unused in code",
        evidence=[f"Unused: {k}" for k in unused[:10]],
        blast_radius="low",
        suggested_injections=["Remove stale keys or verify they're used in non-Python code"],
        file_paths=[".env.example"],
    )]


def _check_package_json_scripts(repo: Path) -> list[ProbeFinding]:
    """Check 5: package.json scripts referencing missing files."""
    pkg_json = repo / "package.json"
    if not pkg_json.exists():
        return []

    try:
        data = json.loads(pkg_json.read_text(errors="replace"))
    except (OSError, json.JSONDecodeError):
        return []

    scripts = data.get("scripts", {})
    if not scripts:
        return []

    file_re = re.compile(r"(?:node |ts-node |python[3]? |bash |sh |\./)(\S+\.\w+)")
    missing_refs = []
    for name, cmd in scripts.items():
        for m in file_re.finditer(cmd):
            ref = m.group(1)
            if not (repo / ref).exists() and not ref.startswith("-"):
                missing_refs.append(f"{name}: {ref}")

    if not missing_refs:
        return []

    return [ProbeFinding(
        title=f"package.json: {len(missing_refs)} script(s) reference missing files",
        evidence=missing_refs[:10],
        blast_radius="low",
        suggested_injections=["Fix script paths or remove dead scripts"],
        file_paths=["package.json"],
    )]


def _check_stale_messages(repo: Path) -> tuple[list[ProbeFinding], list[str]]:
    """Check 6: Fleet messages older than 7 days still in inbox."""
    findings: list[ProbeFinding] = []
    missing: list[str] = []

    messages_dir = repo / ".fleet-messages"
    if not messages_dir.is_dir():
        missing.append(str(messages_dir))
        return findings, missing

    now = time.time()
    stale_files: list[tuple[str, float]] = []

    for node_dir in messages_dir.iterdir():
        if not node_dir.is_dir() or node_dir.name == "archive":
            continue
        for msg_file in node_dir.iterdir():
            if msg_file.is_file() and msg_file.suffix == ".md":
                age_s = now - msg_file.stat().st_mtime
                if age_s > _SEVEN_DAYS:
                    stale_files.append((str(msg_file.relative_to(repo)), age_s))

    if stale_files:
        stale_files.sort(key=lambda x: -x[1])
        age_strs = [f"{path} ({age / 86400:.0f}d old)" for path, age in stale_files[:10]]
        findings.append(ProbeFinding(
            title=f"{len(stale_files)} fleet message(s) older than 7 days in inbox",
            evidence=age_strs,
            blast_radius="low",
            suggested_injections=["Archive processed messages", "Review stale fleet comms"],
            file_paths=[str(repo / path) for path, _ in stale_files[:5]],
        ))

    return findings, missing


def _check_stale_plans(repo: Path) -> tuple[list[ProbeFinding], list[str]]:
    """Check 7: Plan docs older than 30 days with NOT STARTED status."""
    findings: list[ProbeFinding] = []
    missing: list[str] = []

    plans_dir = repo / "docs" / "plans"
    if not plans_dir.is_dir():
        missing.append(str(plans_dir))
        return findings, missing

    now = time.time()
    stale_plans: list[tuple[str, float]] = []

    for plan_file in plans_dir.iterdir():
        if not plan_file.is_file() or plan_file.suffix != ".md":
            continue
        age_s = now - plan_file.stat().st_mtime
        if age_s < _THIRTY_DAYS:
            continue
        try:
            text = plan_file.read_text(errors="replace")[:2048]
            if _NOT_STARTED_RE.search(text):
                stale_plans.append((str(plan_file.relative_to(repo)), age_s))
        except OSError:
            continue

    if stale_plans:
        stale_plans.sort(key=lambda x: -x[1])
        age_strs = [f"{path} ({age / 86400:.0f}d old)" for path, age in stale_plans[:10]]
        findings.append(ProbeFinding(
            title=f"{len(stale_plans)} plan doc(s) older than 30 days still NOT STARTED",
            evidence=age_strs,
            blast_radius="low",
            suggested_injections=["Review stale plans", "Archive or activate"],
            file_paths=[str(repo / path) for path, _ in stale_plans[:5]],
        ))

    return findings, missing


def probe_stale_context(context: dict) -> ProbeResult:
    """Comprehensive stale context detection.

    Merges code_scanner's deep analysis (CLAUDE.md freshness, deprecated comments,
    env staleness, package.json) with probe_engine's fleet-aware checks (stale
    messages, stale plans).

    Context keys:
        repo_root (str): Path to repository root.
    """
    repo = Path(context.get("repo_root", ".")).resolve()
    all_findings: list[ProbeFinding] = []
    all_missing: list[str] = []

    py_files = _collect_py_files(repo)

    # Code-scanner checks (1-5)
    all_findings.extend(_check_stale_claudemd(repo))
    all_findings.extend(_check_readme_dead_refs(repo))
    all_findings.extend(_check_deprecated_comments(repo, py_files))
    all_findings.extend(_check_env_example_stale(repo, py_files))
    all_findings.extend(_check_package_json_scripts(repo))

    # Fleet-aware checks (6-7)
    msg_findings, msg_missing = _check_stale_messages(repo)
    all_findings.extend(msg_findings)
    all_missing.extend(msg_missing)

    plan_findings, plan_missing = _check_stale_plans(repo)
    all_findings.extend(plan_findings)
    all_missing.extend(plan_missing)

    if not all_findings and not py_files and len(all_missing) >= 2:
        return ProbeResult.skipped("stale_context", "context_quality", all_missing)

    severity = "info"
    if all_findings:
        total = sum(len(f.evidence) for f in all_findings)
        if total > 10:
            severity = "high"
        elif total > 3:
            severity = "medium"
        else:
            severity = "low"

    return ProbeResult(
        probe_id="stale_context",
        category="context_quality",
        status="warning" if all_findings else "ok",
        confidence=0.85,
        severity=severity,
        findings=all_findings,
        missing_inputs=all_missing,
    )
