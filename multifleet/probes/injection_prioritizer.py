"""Injection Prioritizer Probe — scores context snippets by relevance.

Analyzes current git context (branch, changed files, recent failures)
to recommend which context snippets should be injected with highest
priority. Returns top-5 ranked recommendations.
"""

import glob
import json
import logging
import subprocess
from collections import defaultdict
from pathlib import Path

from multifleet.probe_engine import ProbeFinding, ProbeResult

logger = logging.getLogger("fleet-nerve.probes.injection_prioritizer")

_DEFAULT_AUDIT_DIR = Path.home() / ".fleet-nerve"
_TOP_N = 5


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


def _get_changed_files(repo_root: str) -> list[str]:
    """Get files changed in working tree + staged."""
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


def _score_snippets(
    changed_files: list[str],
    failure_files: dict[str, int],
    repo_root: str,
) -> list[dict]:
    """Score context snippets by relevance. Returns sorted list of recommendations."""
    scored: list[dict] = []

    # Score changed files — they're actively being worked on
    for f in changed_files:
        score = 50  # base score for being changed
        reasons = ["Currently modified"]

        # Boost if file has recent failures
        if f in failure_files:
            score += 30 * failure_files[f]
            reasons.append(f"Recent failures: {failure_files[f]}x")

        # Boost architectural hotspots
        hotspot_patterns = [
            "CLAUDE.md", "__init__.py", "probe_engine.py",
            "settings.json", "config", "schema",
        ]
        for pat in hotspot_patterns:
            if pat in f:
                score += 20
                reasons.append("Architectural hotspot")
                break

        scored.append({
            "snippet": f,
            "score": score,
            "reasons": reasons,
            "type": "changed_file",
        })

    # Score failure-only files (not in changed set)
    for f, count in failure_files.items():
        if f not in changed_files:
            score = 25 * count
            scored.append({
                "snippet": f,
                "score": score,
                "reasons": [f"Recent failures: {count}x (not actively modified)"],
                "type": "failure_file",
            })

    # Check CLAUDE.md sections relevant to changed dirs
    claude_md = Path(repo_root) / "CLAUDE.md"
    if claude_md.exists():
        changed_dirs = set()
        for f in changed_files:
            parts = f.split("/")
            if len(parts) > 1:
                changed_dirs.add(parts[0])

        if changed_dirs:
            scored.append({
                "snippet": "CLAUDE.md (sections matching: " + ", ".join(sorted(changed_dirs)[:5]) + ")",
                "score": 60,
                "reasons": [
                    "CLAUDE.md sections match changed directories",
                    f"Dirs: {', '.join(sorted(changed_dirs)[:5])}",
                ],
                "type": "claude_md",
            })

    # Sort by score descending
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:_TOP_N]


def probe_injection_prioritizer(context: dict) -> ProbeResult:
    """Analyze context and recommend top injection snippets."""
    repo_root = context.get("repo_root", ".")
    audit_dir = Path(context.get("audit_dir", str(_DEFAULT_AUDIT_DIR)))
    findings: list[ProbeFinding] = []

    # Check git availability
    rc, _ = _git(["rev-parse", "--git-dir"], repo_root)
    if rc != 0:
        return ProbeResult.skipped(
            "injection_prioritizer", "agent_readiness", ["not a git repo"]
        )

    changed_files = _get_changed_files(repo_root)
    failures = _get_recent_failures(audit_dir) if audit_dir.exists() else []
    failure_files = _extract_failure_files(failures)

    recommendations = _score_snippets(changed_files, failure_files, repo_root)

    if not recommendations:
        return ProbeResult(
            probe_id="injection_prioritizer",
            category="agent_readiness",
            status="ok",
            confidence=0.7,
            severity="info",
        )

    # Build findings from top recommendations
    for i, rec in enumerate(recommendations, 1):
        findings.append(ProbeFinding(
            title=f"#{i} [score:{rec['score']}] {rec['snippet']}",
            evidence=rec["reasons"],
            blast_radius="low",
            suggested_injections=[f"Inject context for: {rec['snippet']}"],
            file_paths=[rec["snippet"]] if rec["type"] != "claude_md" else [],
        ))

    # Severity based on whether failures are driving priorities
    has_failure_driven = any(
        r["type"] == "failure_file" or
        any("failure" in reason.lower() for reason in r["reasons"])
        for r in recommendations
    )

    return ProbeResult(
        probe_id="injection_prioritizer",
        category="agent_readiness",
        status="warning" if has_failure_driven else "ok",
        confidence=0.75,
        severity="medium" if has_failure_driven else "info",
        findings=findings,
    )
