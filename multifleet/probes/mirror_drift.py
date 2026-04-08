"""Mirror Drift Probe — detects docs referencing non-existent files/paths.

Checks CLAUDE.md and README.md for file path references that don't exist
on disk. High-value early warning: when docs drift from code, agents
operate on stale mental models.
"""

import logging
import re
from pathlib import Path

from multifleet.probe_engine import ProbeFinding, ProbeResult, ProbeSeverity

logger = logging.getLogger("fleet-nerve.probes.mirror_drift")

# Matches paths like `memory/foo.py`, `scripts/bar.sh`, `tools/baz.py`
# but not URLs, anchors, or bare words.
_PATH_RE = re.compile(
    r"(?:^|[\s\(`\"])"           # preceded by whitespace, paren, backtick, or quote
    r"((?:[\w\-\.]+/)+[\w\-\.]+)" # at least one dir separator, ending in filename
    r"(?:[\s\)`\"]|$)",           # followed by whitespace, paren, backtick, quote, or EOL
)

# Skip patterns that look like URLs, version strings, or MIME types
_SKIP_RE = re.compile(r"https?://|^\d+\.\d+|/\*|application/|text/|image/")


def _extract_paths(text: str) -> list[str]:
    """Extract plausible file paths from markdown text."""
    paths = []
    for m in _PATH_RE.finditer(text):
        candidate = m.group(1)
        if _SKIP_RE.search(candidate):
            continue
        # Skip very short or very long matches
        if len(candidate) < 5 or len(candidate) > 200:
            continue
        paths.append(candidate)
    return list(set(paths))


def probe_mirror_drift(context: dict) -> ProbeResult:
    """Check CLAUDE.md and README.md for references to non-existent paths."""
    repo_root = Path(context.get("repo_root", ".")).resolve()
    findings: list[ProbeFinding] = []
    missing_inputs: list[str] = []

    docs_to_check = [
        repo_root / "CLAUDE.md",
        repo_root / "README.md",
    ]

    checked_any = False
    for doc_path in docs_to_check:
        if not doc_path.exists():
            missing_inputs.append(str(doc_path))
            continue

        checked_any = True
        text = doc_path.read_text(errors="replace")
        referenced_paths = _extract_paths(text)
        missing_paths = []

        for ref in referenced_paths:
            full = repo_root / ref
            if not full.exists():
                missing_paths.append(ref)

        if missing_paths:
            findings.append(ProbeFinding(
                title=f"{doc_path.name} references {len(missing_paths)} non-existent path(s)",
                evidence=[f"Missing: {p}" for p in missing_paths[:10]],
                blast_radius="medium" if len(missing_paths) > 3 else "low",
                suggested_injections=["Update doc references", "Verify moved files"],
                file_paths=[str(doc_path)] + [str(repo_root / p) for p in missing_paths[:5]],
            ))

    if not checked_any:
        return ProbeResult.skipped("mirror_drift", "context_quality", missing_inputs)

    severity = "info"
    if findings:
        total_missing = sum(len(f.evidence) for f in findings)
        if total_missing > 5:
            severity = "high"
        elif total_missing > 2:
            severity = "medium"
        else:
            severity = "low"

    return ProbeResult(
        probe_id="mirror_drift",
        category="context_quality",
        status="warning" if findings else "ok",
        confidence=0.7 if findings else 0.9,
        severity=severity,
        findings=findings,
        missing_inputs=missing_inputs,
    )
