"""Invariant Coverage Probe — checks that safety gates are actually firing.

Verifies that gains_gate, evidence_ledger, and invariance gates have
recent activity. If gates exist but haven't fired recently, something
is bypassing them — a silent failure mode.
"""

import logging
import time
from pathlib import Path

from multifleet.probe_engine import ProbeFinding, ProbeResult

logger = logging.getLogger("fleet-nerve.probes.invariant_coverage")

# How old a gate check can be before it's considered stale (seconds)
STALENESS_THRESHOLD = 3600  # 1 hour


def probe_invariant_coverage(context: dict) -> ProbeResult:
    """Check that invariance gates are firing and not stale.

    Context keys:
        repo_root (str): Path to repository root.
        staleness_threshold (int): Max seconds since last gate fire (default 3600).
    """
    repo_root = Path(context.get("repo_root", "."))
    threshold = context.get("staleness_threshold", STALENESS_THRESHOLD)

    findings = []

    # Check 1: gains-gate script exists and has been run recently
    gains_gate = _check_gains_gate(repo_root, threshold)
    if gains_gate:
        findings.append(gains_gate)

    # Check 2: evidence ledger has recent entries
    ledger_check = _check_evidence_ledger(repo_root, threshold)
    if ledger_check:
        findings.append(ledger_check)

    # Check 3: invariance gates module exists and has tests
    gate_check = _check_invariance_gates(repo_root)
    if gate_check:
        findings.append(gate_check)

    # Check 4: pre-commit hooks are installed
    hook_check = _check_hooks(repo_root)
    if hook_check:
        findings.append(hook_check)

    if not findings:
        return ProbeResult(
            probe_id="invariant_coverage",
            category="reliability",
            status="ok",
            confidence=0.85,
            severity="info",
        )

    max_sev = "high" if any(f.blast_radius == "high" for f in findings) else "medium"
    return ProbeResult(
        probe_id="invariant_coverage",
        category="reliability",
        status="warning",
        confidence=0.75,
        severity=max_sev,
        findings=findings,
    )


def _check_gains_gate(repo_root: Path, threshold: int):
    """Check if gains-gate script exists and has recent log."""
    script = repo_root / "scripts" / "gains-gate.sh"
    if not script.exists():
        return ProbeFinding(
            title="gains-gate.sh not found",
            evidence=["Expected at scripts/gains-gate.sh"],
            blast_radius="high",
            suggested_injections=["No gains gate — changes may bypass safety checks"],
        )

    # Check for recent execution log
    log_patterns = [
        repo_root / ".fleet-nerve" / "gains-gate.log",
        repo_root / "multi-fleet" / ".fleet-nerve" / "gains-gate.log",
    ]
    for log_path in log_patterns:
        if log_path.exists():
            age = time.time() - log_path.stat().st_mtime
            if age > threshold:
                return ProbeFinding(
                    title=f"gains-gate last ran {int(age/3600)}h ago (threshold: {threshold//3600}h)",
                    evidence=[f"Log: {log_path}", f"Age: {int(age)}s"],
                    blast_radius="medium",
                    suggested_injections=["Gains gate may be stale"],
                )
            return None  # Recently run, all good
    return None  # No log found, but script exists


def _check_evidence_ledger(repo_root: Path, threshold: int):
    """Check that the evidence ledger has recent entries."""
    ledger_paths = [
        Path.home() / ".fleet-nerve" / "decisions.jsonl",
        Path.home() / ".fleet-nerve" / "evidence.jsonl",
    ]
    for ledger in ledger_paths:
        if ledger.exists():
            age = time.time() - ledger.stat().st_mtime
            if age > threshold * 6:  # 6 hours for ledger
                return ProbeFinding(
                    title=f"Evidence ledger stale ({int(age/3600)}h since last entry)",
                    evidence=[f"Path: {ledger}"],
                    blast_radius="medium",
                    suggested_injections=["Evidence ledger may not be recording"],
                )
            return None
    return None  # No ledger found — may be first run


def _check_invariance_gates(repo_root: Path):
    """Check that invariance_gates.py exists and has tests."""
    gates_file = repo_root / "multi-fleet" / "multifleet" / "invariance_gates.py"
    if not gates_file.exists():
        gates_file = repo_root / "multifleet" / "invariance_gates.py"
    if not gates_file.exists():
        return None  # Not required if not present

    # Check for corresponding tests
    test_patterns = [
        repo_root / "multi-fleet" / "tests" / "test_invariance_gates.py",
        repo_root / "tests" / "test_invariance_gates.py",
    ]
    has_tests = any(t.exists() for t in test_patterns)
    if not has_tests:
        return ProbeFinding(
            title="invariance_gates.py has no dedicated test file",
            evidence=["Gates without tests may silently degrade"],
            blast_radius="medium",
            suggested_injections=["Untested invariance gates"],
        )
    return None


def _check_hooks(repo_root: Path):
    """Check that pre-commit hooks are installed."""
    hook_dir = repo_root / ".git" / "hooks"
    pre_commit = hook_dir / "pre-commit"
    if not pre_commit.exists():
        return ProbeFinding(
            title="No pre-commit hook installed",
            evidence=["Commits bypass secret scanning and banned string checks"],
            blast_radius="high",
            suggested_injections=["Missing pre-commit safety net"],
        )
    return None
