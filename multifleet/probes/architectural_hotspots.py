"""Architectural Hotspots Probe — finds high-churn, high-blast-radius files.

Runs git log to find frequently modified files in the last 30 days, then
estimates blast radius by counting how many other files import each one.
Score = modification_frequency * blast_radius_estimate.
"""

import logging
import subprocess
from collections import Counter
from pathlib import Path

from multifleet.probe_engine import ProbeFinding, ProbeResult

logger = logging.getLogger("fleet-nerve.probes.architectural_hotspots")


def _git_churn(repo_root: Path, days: int = 30) -> Counter:
    """Count per-file commits in the last N days."""
    try:
        out = subprocess.run(
            ["git", "log", f"--since={days} days ago", "--format=", "--name-only"],
            capture_output=True, text=True, cwd=str(repo_root), timeout=30,
        )
        if out.returncode != 0:
            return Counter()
        files = [f.strip() for f in out.stdout.splitlines() if f.strip()]
        return Counter(files)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return Counter()


def _blast_radius(repo_root: Path, target_files: list[str]) -> dict[str, int]:
    """Estimate blast radius: count how many .py files import each target."""
    radius: dict[str, int] = {f: 1 for f in target_files}
    stems = {}
    for f in target_files:
        p = Path(f)
        if p.suffix == ".py":
            # Convert path to module-style import: "a/b/c.py" -> "a.b.c" and also "c"
            module = p.with_suffix("").as_posix().replace("/", ".")
            stems[p.stem] = f
            stems[module] = f

    if not stems:
        return radius

    # Scan .py files for imports mentioning our targets
    try:
        out = subprocess.run(
            ["git", "ls-files", "*.py", "**/*.py"],
            capture_output=True, text=True, cwd=str(repo_root), timeout=15,
        )
        py_files = [l.strip() for l in out.stdout.splitlines() if l.strip()]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return radius

    for pyf in py_files[:500]:  # cap to avoid slowness
        try:
            text = (repo_root / pyf).read_text(errors="replace")
        except OSError:
            continue
        for key, target in stems.items():
            if key in text and pyf != target:
                radius[target] = radius.get(target, 1) + 1

    return radius


def probe_architectural_hotspots(context: dict) -> ProbeResult:
    """Identify high-churn, high-blast-radius files."""
    repo_root = Path(context.get("repo_root", ".")).resolve()
    findings: list[ProbeFinding] = []

    churn = _git_churn(repo_root, days=context.get("churn_days", 30))
    if not churn:
        return ProbeResult(
            probe_id="architectural_hotspots",
            category="structure",
            status="ok",
            confidence=0.5,
            severity="info",
            findings=[ProbeFinding(title="No file churn detected in last 30 days")],
        )

    # Top 20 by churn, then score with blast radius
    top_churn = [f for f, _ in churn.most_common(20)]
    radius = _blast_radius(repo_root, top_churn)

    scored = []
    for f in top_churn:
        freq = churn[f]
        br = radius.get(f, 1)
        scored.append((f, freq, br, freq * br))

    scored.sort(key=lambda x: -x[3])
    top10 = scored[:10]

    if top10:
        max_score = top10[0][3]
        evidence = [
            f"{f} (commits={freq}, importers={br}, score={score})"
            for f, freq, br, score in top10
        ]
        severity = "high" if max_score > 20 else "medium" if max_score > 8 else "low"
        findings.append(ProbeFinding(
            title=f"Top {len(top10)} architectural hotspots (churn x blast radius)",
            evidence=evidence,
            blast_radius="high" if max_score > 20 else "medium",
            suggested_injections=["Review hotspot stability", "Consider refactoring high-score files"],
            file_paths=[str(repo_root / f) for f, *_ in top10[:5]],
        ))

    return ProbeResult(
        probe_id="architectural_hotspots",
        category="structure",
        status="warning" if findings else "ok",
        confidence=0.75,
        severity=severity if findings else "info",
        findings=findings,
    )
