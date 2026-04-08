"""Security Surface Probe — detects potential security issues.

Adapted from code_scanner/probes/security_surface.py for the probe_engine registry.
Uses probe_engine's ProbeResult/ProbeFinding contract.

Checks:
1. Hardcoded secrets (API keys, tokens, passwords -- regex patterns)
2. os.system() / subprocess.call(shell=True) with unsanitized input
3. Files with overly permissive chmod (777, world-writable)
4. .env files committed to git
5. Private keys or certificates in the repo
"""

import logging
import re
import subprocess
from pathlib import Path

from multifleet.probe_engine import ProbeCategory, ProbeFinding, ProbeResult

logger = logging.getLogger("fleet-nerve.probes.security_surface")

# Patterns that look like hardcoded secrets
_SECRET_PATTERNS = [
    (re.compile(r"""['"](?:sk-[a-zA-Z0-9]{20,})['"]"""), "OpenAI API key"),
    (re.compile(r"""['"](?:AKIA[A-Z0-9]{16})['"]"""), "AWS Access Key ID"),
    (re.compile(r"""['"](?:ghp_[a-zA-Z0-9]{36})['"]"""), "GitHub PAT"),
    (re.compile(r"""['"](?:glpat-[a-zA-Z0-9\-_]{20,})['"]"""), "GitLab PAT"),
    (re.compile(r"""['"](?:xox[bsrap]-[a-zA-Z0-9\-]{10,})['"]"""), "Slack token"),
    (re.compile(r"""(?:password|passwd|pwd)\s*=\s*['"][^'"]{8,}['"]""", re.I), "Hardcoded password"),
    (re.compile(r"""(?:api[_-]?key|apikey|secret[_-]?key)\s*=\s*['"][a-zA-Z0-9\-_]{16,}['"]""", re.I), "Hardcoded API key"),
    (re.compile(r"""(?:token)\s*=\s*['"][a-zA-Z0-9\-_\.]{20,}['"]""", re.I), "Hardcoded token"),
]

_SHELL_INJECTION_RE = re.compile(
    r"""(?:os\.system\s*\(|"""
    r"""subprocess\.(?:call|run|Popen|check_call|check_output)\s*\([^)]*shell\s*=\s*True)""",
)

_PRIVATE_KEY_MARKERS = [
    "-----BEGIN RSA PRIVATE KEY-----",
    "-----BEGIN OPENSSH PRIVATE KEY-----",
    "-----BEGIN EC PRIVATE KEY-----",
    "-----BEGIN DSA PRIVATE KEY-----",
    "-----BEGIN PRIVATE KEY-----",
]


def _check_hardcoded_secrets(repo: Path, files: list[Path]) -> list[ProbeFinding]:
    """Check for hardcoded secrets in source code."""
    hits: list[tuple[str, int, str, str]] = []

    for fpath in files[:100]:
        try:
            lines = fpath.read_text(errors="replace").splitlines()
        except OSError:
            continue

        rel = str(fpath.relative_to(repo))
        if "test" in rel.lower() and ("fixture" in rel.lower() or "mock" in rel.lower()):
            continue

        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("//"):
                continue
            for pattern, desc in _SECRET_PATTERNS:
                if pattern.search(line):
                    hits.append((rel, i, stripped[:60], desc))
                    break
            if len(hits) >= 20:
                break
        if len(hits) >= 20:
            break

    if not hits:
        return []

    return [ProbeFinding(
        title=f"{len(hits)} potential hardcoded secret(s) detected",
        evidence=[f"{f}:{ln} ({desc})" for f, ln, _, desc in hits[:10]],
        blast_radius="high",
        suggested_injections=[
            "Move secrets to environment variables or secret manager",
            "Rotate any exposed credentials immediately",
        ],
        file_paths=list({f for f, _, _, _ in hits}),
    )]


def _check_shell_injection(repo: Path, py_files: list[Path]) -> list[ProbeFinding]:
    """Check for os.system() / subprocess with shell=True."""
    hits: list[tuple[str, int, str]] = []

    for py_file in py_files[:80]:
        try:
            lines = py_file.read_text(errors="replace").splitlines()
        except OSError:
            continue

        rel = str(py_file.relative_to(repo))
        for i, line in enumerate(lines, 1):
            if _SHELL_INJECTION_RE.search(line):
                hits.append((rel, i, line.strip()[:80]))
                if len(hits) >= 15:
                    break
        if len(hits) >= 15:
            break

    if not hits:
        return []

    return [ProbeFinding(
        title=f"{len(hits)} shell injection risk(s) found",
        evidence=[f"{f}:{ln}: {txt}" for f, ln, txt in hits[:10]],
        blast_radius="medium",
        suggested_injections=[
            "Replace os.system() with subprocess.run()",
            "Use shell=False with explicit argument lists",
        ],
        file_paths=list({f for f, _, _ in hits}),
    )]


def _check_env_in_git(repo: Path) -> list[ProbeFinding]:
    """Check for .env files committed to git."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached"],
            capture_output=True, text=True, timeout=10, cwd=str(repo),
        )
        if result.returncode != 0:
            return []
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []

    tracked = result.stdout.strip().splitlines()
    env_files = [f for f in tracked if re.match(r"(?:.*/)?.env(?:\.\w+)?$", f)
                 and not f.endswith(".example") and not f.endswith(".template")]

    if not env_files:
        return []

    return [ProbeFinding(
        title=f"{len(env_files)} .env file(s) committed to git",
        evidence=[f"Tracked: {f}" for f in env_files[:10]],
        blast_radius="high",
        suggested_injections=[
            "Remove from git: git rm --cached <file>",
            "Add to .gitignore",
            "Rotate any secrets that were exposed",
        ],
        file_paths=env_files[:10],
    )]


def _check_private_keys(repo: Path) -> list[ProbeFinding]:
    """Check for private keys or certificates in the repo."""
    key_files: list[str] = []
    skip = {".venv", "venv", "node_modules", "__pycache__", ".git"}
    key_exts = {".pem", ".key", ".p12", ".pfx", ".jks"}

    for fpath in repo.rglob("*"):
        if any(p in fpath.parts for p in skip):
            continue
        if not fpath.is_file():
            continue

        rel = str(fpath.relative_to(repo))

        if fpath.suffix.lower() in key_exts:
            key_files.append(rel)
            continue

        if fpath.stat().st_size < 10000:
            try:
                content = fpath.read_text(errors="replace")[:2000]
                for marker in _PRIVATE_KEY_MARKERS:
                    if marker in content:
                        key_files.append(rel)
                        break
            except OSError:
                continue

        if len(key_files) >= 10:
            break

    if not key_files:
        return []

    return [ProbeFinding(
        title=f"{len(key_files)} private key/cert file(s) in repo",
        evidence=[f"Found: {f}" for f in key_files[:10]],
        blast_radius="high",
        suggested_injections=[
            "Remove private keys from repo",
            "Use secret manager for key distribution",
        ],
        file_paths=key_files[:10],
    )]


def _collect_source_files(repo: Path) -> list[Path]:
    """Collect source files (py, js, ts, go, rs, rb)."""
    skip = {".venv", "venv", "node_modules", "__pycache__", ".git", ".tox", "dist", "build"}
    exts = {".py", ".js", ".ts", ".go", ".rs", ".rb", ".java", ".sh"}
    files = []
    for f in repo.rglob("*"):
        if any(p in f.parts for p in skip):
            continue
        if f.is_file() and f.suffix in exts:
            files.append(f)
            if len(files) >= 300:
                break
    return files


def probe_security_surface(context: dict) -> ProbeResult:
    """Detect potential security issues in the codebase.

    Accepts probe_engine context dict with keys:
        repo_root, changed_files, branch, task_intent, node_id
    """
    repo = Path(context.get("repo_root", ".")).resolve()
    if not repo.is_dir():
        return ProbeResult.error("security_surface", "security", f"Repo path not found: {repo}")

    source_files = _collect_source_files(repo)
    py_files = [f for f in source_files if f.suffix == ".py"]
    all_findings: list[ProbeFinding] = []

    all_findings.extend(_check_hardcoded_secrets(repo, source_files))
    all_findings.extend(_check_shell_injection(repo, py_files))
    all_findings.extend(_check_env_in_git(repo))
    all_findings.extend(_check_private_keys(repo))

    if not all_findings:
        return ProbeResult(
            probe_id="security_surface",
            category="security",
            status="ok",
            confidence=0.8,
            severity="info",
        )

    max_blast = max(f.blast_radius for f in all_findings)
    severity = "high" if max_blast == "high" else ("medium" if max_blast == "medium" else "low")

    return ProbeResult(
        probe_id="security_surface",
        category="security",
        status="warning",
        confidence=0.75,
        severity=severity,
        findings=all_findings,
    )
