"""Security Surface Probe -- detects potential security issues.

Checks:
1. Hardcoded secrets (API keys, tokens, passwords in code -- regex patterns)
2. os.system() / subprocess.call(shell=True) with unsanitized input
3. Files with overly permissive chmod (777, world-writable)
4. .env files committed to git (check git ls-files)
5. Private keys or certificates in the repo
"""

import logging
import os
import re
import subprocess
from pathlib import Path

from multifleet.code_scanner.probe_contract import Finding, ProbeContext, ProbeResult

logger = logging.getLogger("fleet-nerve.code_scanner.security_surface")

# Patterns that look like hardcoded secrets (high confidence)
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

# Shell injection patterns
_SHELL_INJECTION_RE = re.compile(
    r"""(?:os\.system\s*\(|"""
    r"""subprocess\.(?:call|run|Popen|check_call|check_output)\s*\([^)]*shell\s*=\s*True)""",
)

# Private key markers
_PRIVATE_KEY_MARKERS = [
    "-----BEGIN RSA PRIVATE KEY-----",
    "-----BEGIN OPENSSH PRIVATE KEY-----",
    "-----BEGIN EC PRIVATE KEY-----",
    "-----BEGIN DSA PRIVATE KEY-----",
    "-----BEGIN PRIVATE KEY-----",
]


def _check_hardcoded_secrets(repo: Path, files: list[Path]) -> list[Finding]:
    """Check 1: Hardcoded secrets in source code."""
    findings = []
    hits: list[tuple[str, int, str, str]] = []  # (file, line, match, desc)

    for fpath in files[:100]:
        try:
            lines = fpath.read_text(errors="replace").splitlines()
        except OSError:
            continue

        rel = str(fpath.relative_to(repo))
        # Skip test files and fixture data
        if "test" in rel.lower() and ("fixture" in rel.lower() or "mock" in rel.lower()):
            continue

        for i, line in enumerate(lines, 1):
            # Skip comments
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

    if hits:
        findings.append(Finding(
            title=f"{len(hits)} potential hardcoded secret(s) detected",
            summary=f"Found patterns matching secrets: {', '.join(set(d for _, _, _, d in hits))}",
            severity="high",
            confidence=0.8,
            blast_radius="high",
            evidence_refs=[f"{f}:{ln} ({desc})" for f, ln, _, desc in hits[:10]],
            suggested_actions=[
                "Move secrets to environment variables or secret manager",
                "Rotate any exposed credentials immediately",
            ],
            file_path=hits[0][0] if hits else "",
            line_number=hits[0][1] if hits else 0,
        ))
    return findings


def _check_shell_injection(repo: Path, py_files: list[Path]) -> list[Finding]:
    """Check 2: os.system() / subprocess with shell=True."""
    findings = []
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

    if hits:
        findings.append(Finding(
            title=f"{len(hits)} shell injection risk(s) found",
            summary="Code uses os.system() or subprocess with shell=True",
            severity="medium",
            confidence=0.7,
            blast_radius="medium",
            evidence_refs=[f"{f}:{ln}: {txt}" for f, ln, txt in hits[:10]],
            suggested_actions=[
                "Replace os.system() with subprocess.run()",
                "Use shell=False with explicit argument lists",
                "Validate/sanitize any user input in shell commands",
            ],
            file_path=hits[0][0] if hits else "",
            line_number=hits[0][1] if hits else 0,
        ))
    return findings


def _check_permissive_chmod(repo: Path) -> list[Finding]:
    """Check 3: Files with overly permissive permissions (777, world-writable)."""
    findings = []
    permissive: list[tuple[str, str]] = []

    skip = {".venv", "venv", "node_modules", "__pycache__", ".git"}
    for fpath in repo.rglob("*"):
        if any(p in fpath.parts for p in skip):
            continue
        if not fpath.is_file():
            continue
        try:
            mode = fpath.stat().st_mode & 0o777
            if mode & 0o002:  # world-writable
                rel = str(fpath.relative_to(repo))
                permissive.append((rel, oct(mode)))
                if len(permissive) >= 10:
                    break
        except OSError:
            continue

    if permissive:
        findings.append(Finding(
            title=f"{len(permissive)} world-writable file(s) found",
            summary=f"Files with overly permissive permissions: {', '.join(f for f, _ in permissive[:5])}",
            severity="medium",
            confidence=0.9,
            blast_radius="medium",
            evidence_refs=[f"{f}: {m}" for f, m in permissive[:10]],
            suggested_actions=["Remove world-writable permissions: chmod o-w <file>"],
            file_path=permissive[0][0] if permissive else "",
        ))
    return findings


def _check_env_in_git(repo: Path) -> list[Finding]:
    """Check 4: .env files committed to git."""
    findings = []
    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached"],
            capture_output=True, text=True, timeout=10, cwd=str(repo),
        )
        if result.returncode != 0:
            return findings
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return findings

    tracked = result.stdout.strip().splitlines()
    env_files = [f for f in tracked if re.match(r"(?:.*/)?.env(?:\.\w+)?$", f)
                 and not f.endswith(".example") and not f.endswith(".template")]

    if env_files:
        findings.append(Finding(
            title=f"{len(env_files)} .env file(s) committed to git",
            summary=f"Env files in version control: {', '.join(env_files[:5])}",
            severity="high",
            confidence=0.95,
            blast_radius="high",
            evidence_refs=[f"Tracked: {f}" for f in env_files[:10]],
            suggested_actions=[
                "Remove from git: git rm --cached <file>",
                "Add to .gitignore",
                "Rotate any secrets that were exposed",
            ],
            file_path=env_files[0] if env_files else "",
        ))
    return findings


def _check_private_keys(repo: Path) -> list[Finding]:
    """Check 5: Private keys or certificates in the repo."""
    findings = []
    key_files: list[str] = []

    skip = {".venv", "venv", "node_modules", "__pycache__", ".git"}
    key_exts = {".pem", ".key", ".p12", ".pfx", ".jks"}

    for fpath in repo.rglob("*"):
        if any(p in fpath.parts for p in skip):
            continue
        if not fpath.is_file():
            continue

        rel = str(fpath.relative_to(repo))

        # Check file extension
        if fpath.suffix.lower() in key_exts:
            key_files.append(rel)
            continue

        # Check file content for PEM headers (only small files)
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

    if key_files:
        findings.append(Finding(
            title=f"{len(key_files)} private key/cert file(s) in repo",
            summary=f"Private keys or certs found: {', '.join(key_files[:5])}",
            severity="high",
            confidence=0.85,
            blast_radius="high",
            evidence_refs=[f"Found: {f}" for f in key_files[:10]],
            suggested_actions=[
                "Remove private keys from repo",
                "Use secret manager for key distribution",
                "Add key file patterns to .gitignore",
            ],
            file_path=key_files[0] if key_files else "",
        ))
    return findings


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


def probe_security_surface(context: ProbeContext) -> ProbeResult:
    """Detect potential security issues in the codebase."""
    repo = Path(context.repo_path).resolve()
    if not repo.is_dir():
        return ProbeResult.error("security_surface", f"Repo path not found: {repo}")

    source_files = _collect_source_files(repo)
    py_files = [f for f in source_files if f.suffix == ".py"]
    all_findings: list[Finding] = []

    all_findings.extend(_check_hardcoded_secrets(repo, source_files))
    all_findings.extend(_check_shell_injection(repo, py_files))
    all_findings.extend(_check_permissive_chmod(repo))
    all_findings.extend(_check_env_in_git(repo))
    all_findings.extend(_check_private_keys(repo))

    if not all_findings:
        return ProbeResult.ok("security_surface", confidence=0.8)

    return ProbeResult(
        probe_id="security_surface",
        status="warn",
        confidence=0.75,
        findings=all_findings,
    )


def register(engine) -> None:
    """Auto-discovery hook."""
    engine.register(
        probe_id="security_surface",
        probe_fn=probe_security_surface,
        category="security",
        cost="medium",  # file scanning + git ls-files
    )
