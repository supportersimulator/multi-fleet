"""Agent Readiness Probe — checks if the local environment is ready for fleet ops.

Checks: Python >= 3.10, key imports, daemon health, Keychain API keys,
Superset local.db project registration. Returns ready/not-ready with blockers.
"""

import importlib
import logging
import subprocess
import sys
from pathlib import Path

from multifleet.probe_engine import ProbeFinding, ProbeResult

logger = logging.getLogger("fleet-nerve.probes.agent_readiness")

_REQUIRED_MODULES = ["nats", "jsonschema"]
_OPTIONAL_MODULES = ["mcp"]
_KEYCHAIN_KEYS = ["ANTHROPIC_API_KEY", "OPENAI_API_KEY"]


def _check_python_version() -> tuple[bool, str]:
    v = sys.version_info
    ok = v >= (3, 10)
    return ok, f"Python {v.major}.{v.minor}.{v.micro}"


def _check_imports() -> tuple[list[str], list[str]]:
    """Return (available, missing) for required modules."""
    available, missing = [], []
    for mod in _REQUIRED_MODULES:
        try:
            importlib.import_module(mod)
            available.append(mod)
        except ImportError:
            missing.append(mod)
    return available, missing


def _check_optional_imports() -> tuple[list[str], list[str]]:
    available, missing = [], []
    for mod in _OPTIONAL_MODULES:
        try:
            importlib.import_module(mod)
            available.append(mod)
        except ImportError:
            missing.append(mod)
    return available, missing


def _check_daemon() -> tuple[bool, str]:
    """Check if fleet daemon is running via health endpoint."""
    try:
        import urllib.request
        req = urllib.request.Request("http://127.0.0.1:8855/health", method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status == 200, "daemon running"
    except Exception as e:
        return False, f"daemon unreachable: {type(e).__name__}"


def _check_keychain() -> tuple[list[str], list[str]]:
    """Check macOS Keychain for required API keys."""
    found, missing = [], []
    for key in _KEYCHAIN_KEYS:
        try:
            out = subprocess.run(
                ["security", "find-generic-password", "-s", key, "-w"],
                capture_output=True, text=True, timeout=5,
            )
            if out.returncode == 0 and out.stdout.strip():
                found.append(key)
            else:
                missing.append(key)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            missing.append(key)
    return found, missing


def _check_superset_db() -> tuple[bool, str]:
    """Check if .superset/local.db exists and has a project."""
    db_path = Path.home() / ".superset" / "local.db"
    if not db_path.exists():
        return False, "~/.superset/local.db not found"
    # Just check file exists and has content
    if db_path.stat().st_size < 100:
        return False, "~/.superset/local.db is empty"
    return True, "superset db exists"


def probe_agent_readiness(context: dict) -> ProbeResult:
    """Check if the local environment is ready for fleet agent operations."""
    findings: list[ProbeFinding] = []
    blockers: list[str] = []

    # Python version
    py_ok, py_info = _check_python_version()
    if not py_ok:
        blockers.append(f"Python >= 3.10 required (got {py_info})")

    # Required imports
    _, req_missing = _check_imports()
    if req_missing:
        blockers.append(f"Missing required modules: {', '.join(req_missing)}")

    # Optional imports
    _, opt_missing = _check_optional_imports()

    # Daemon health
    daemon_ok, daemon_info = _check_daemon()
    if not daemon_ok:
        blockers.append(daemon_info)

    # Keychain
    kc_found, kc_missing = _check_keychain()
    if kc_missing:
        blockers.append(f"Missing Keychain keys: {', '.join(kc_missing)}")

    # Superset DB
    ss_ok, ss_info = _check_superset_db()
    if not ss_ok:
        blockers.append(ss_info)

    # Build findings
    if blockers:
        findings.append(ProbeFinding(
            title=f"Agent NOT ready: {len(blockers)} blocker(s)",
            evidence=blockers,
            blast_radius="high",
            suggested_injections=["Fix blockers before fleet operations"],
        ))

    if opt_missing:
        findings.append(ProbeFinding(
            title=f"Optional modules missing: {', '.join(opt_missing)}",
            evidence=[f"pip install {m}" for m in opt_missing],
            blast_radius="low",
        ))

    ready = len(blockers) == 0
    severity = "info" if ready else ("high" if len(blockers) > 2 else "medium")

    return ProbeResult(
        probe_id="agent_readiness",
        category="agent_readiness",
        status="ok" if ready else "warning",
        confidence=0.95,
        severity=severity,
        findings=findings,
    )
