"""Stale Context Probe -- detects outdated CLAUDE.md, README, and context files.

Checks:
1. CLAUDE.md last modified vs recent code changes -- flag if code changed but docs didn't
2. README.md references to files/functions that no longer exist
3. Comments referencing old APIs or deprecated patterns
4. .env.example with keys no longer read by any code
5. package.json scripts that reference missing files
"""

import json
import logging
import os
import re
import subprocess
from pathlib import Path

from multifleet.code_scanner.probe_contract import Finding, ProbeContext, ProbeResult

logger = logging.getLogger("fleet-nerve.code_scanner.stale_context")

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


def _check_stale_claudemd(repo: Path) -> list[Finding]:
    """Check 1: CLAUDE.md freshness vs code changes."""
    findings = []
    claude_md = repo / "CLAUDE.md"
    if not claude_md.exists():
        return findings

    claude_mtime = _git_mtime(repo, "CLAUDE.md")
    code_mtime = _recent_code_mtime(repo)

    if claude_mtime and code_mtime and (code_mtime - claude_mtime) > 7 * 86400:
        days_stale = int((code_mtime - claude_mtime) / 86400)
        findings.append(Finding(
            title=f"CLAUDE.md may be stale ({days_stale}d behind code)",
            summary=f"Code changed recently but CLAUDE.md last updated {days_stale} days earlier",
            severity="medium",
            confidence=0.7,
            blast_radius="medium",
            evidence_refs=[
                f"CLAUDE.md last commit: {int(claude_mtime)}",
                f"Latest code commit: {int(code_mtime)}",
            ],
            suggested_actions=["Review CLAUDE.md for accuracy", "Update stale sections"],
            file_path="CLAUDE.md",
        ))
    return findings


def _check_readme_dead_refs(repo: Path) -> list[Finding]:
    """Check 2: README.md references to files/functions that no longer exist."""
    findings = []
    readme = repo / "README.md"
    if not readme.exists():
        return findings

    try:
        text = readme.read_text(errors="replace")
    except OSError:
        return findings

    # Extract backtick-quoted paths
    path_re = re.compile(r"`([\w\-\.]+(?:/[\w\-\.]+)+)`")
    missing = []
    for m in path_re.finditer(text):
        ref = m.group(1)
        if not (repo / ref).exists() and len(ref) > 3:
            missing.append(ref)

    if missing:
        findings.append(Finding(
            title=f"README.md: {len(missing)} dead reference(s)",
            summary=f"References to non-existent files: {', '.join(missing[:5])}",
            severity="medium" if len(missing) > 3 else "low",
            confidence=0.75,
            blast_radius="low",
            evidence_refs=[f"Missing: {p}" for p in missing[:10]],
            suggested_actions=["Update or remove dead references in README"],
            file_path="README.md",
        ))
    return findings


def _check_deprecated_comments(repo: Path, py_files: list[Path]) -> list[Finding]:
    """Check 3: Comments referencing old APIs or deprecated patterns."""
    findings = []
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

    if hits:
        findings.append(Finding(
            title=f"{len(hits)} deprecated/legacy comment(s) found",
            summary="Code contains comments flagging deprecated or legacy patterns",
            severity="low",
            confidence=0.6,
            blast_radius="low",
            evidence_refs=[f"{f}:{ln}: {txt}" for f, ln, txt in hits[:10]],
            suggested_actions=["Review and resolve deprecated markers"],
            file_path=hits[0][0] if hits else "",
            line_number=hits[0][1] if hits else 0,
        ))
    return findings


def _check_env_example_stale(repo: Path, py_files: list[Path]) -> list[Finding]:
    """Check 4: .env.example keys no longer read by any code."""
    findings = []
    env_example = repo / ".env.example"
    if not env_example.exists():
        return findings

    try:
        text = env_example.read_text(errors="replace")
    except OSError:
        return findings

    env_keys = re.findall(r"^([A-Z][A-Z0-9_]+)=", text, re.MULTILINE)
    if not env_keys:
        return findings

    # Scan code for usage of each key
    all_code = ""
    for py_file in py_files[:100]:
        try:
            all_code += py_file.read_text(errors="replace")
        except OSError:
            continue

    unused = [k for k in env_keys if k not in all_code]
    if unused:
        findings.append(Finding(
            title=f"{len(unused)} .env.example key(s) unused in code",
            summary=f"Keys not found in any Python file: {', '.join(unused[:5])}",
            severity="low",
            confidence=0.6,
            blast_radius="low",
            evidence_refs=[f"Unused: {k}" for k in unused[:10]],
            suggested_actions=["Remove stale keys or verify they're used in non-Python code"],
            file_path=".env.example",
        ))
    return findings


def _check_package_json_scripts(repo: Path) -> list[Finding]:
    """Check 5: package.json scripts referencing missing files."""
    findings = []
    pkg_json = repo / "package.json"
    if not pkg_json.exists():
        return findings

    try:
        data = json.loads(pkg_json.read_text(errors="replace"))
    except (OSError, json.JSONDecodeError):
        return findings

    scripts = data.get("scripts", {})
    if not scripts:
        return findings

    missing_refs = []
    file_re = re.compile(r"(?:node |ts-node |python[3]? |bash |sh |\./)(\S+\.\w+)")
    for name, cmd in scripts.items():
        for m in file_re.finditer(cmd):
            ref = m.group(1)
            if not (repo / ref).exists() and not ref.startswith("-"):
                missing_refs.append(f"{name}: {ref}")

    if missing_refs:
        findings.append(Finding(
            title=f"package.json: {len(missing_refs)} script(s) reference missing files",
            summary=f"Scripts point to non-existent files: {', '.join(missing_refs[:5])}",
            severity="medium",
            confidence=0.7,
            blast_radius="low",
            evidence_refs=missing_refs[:10],
            suggested_actions=["Fix script paths or remove dead scripts"],
            file_path="package.json",
        ))
    return findings


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


def probe_stale_context(context: ProbeContext) -> ProbeResult:
    """Detect outdated context files (CLAUDE.md, README, .env.example, package.json)."""
    repo = Path(context.repo_path).resolve()
    if not repo.is_dir():
        return ProbeResult.error("stale_context", f"Repo path not found: {repo}")

    py_files = _collect_py_files(repo)
    all_findings: list[Finding] = []

    all_findings.extend(_check_stale_claudemd(repo))
    all_findings.extend(_check_readme_dead_refs(repo))
    all_findings.extend(_check_deprecated_comments(repo, py_files))
    all_findings.extend(_check_env_example_stale(repo, py_files))
    all_findings.extend(_check_package_json_scripts(repo))

    if not all_findings:
        return ProbeResult.ok("stale_context", confidence=0.85)

    return ProbeResult(
        probe_id="stale_context",
        status="warn",
        confidence=0.7,
        findings=all_findings,
    )


def register(engine) -> None:
    """Auto-discovery hook."""
    engine.register(
        probe_id="stale_context",
        probe_fn=probe_stale_context,
        category="context_quality",
        cost="medium",  # git log + file scanning
    )
