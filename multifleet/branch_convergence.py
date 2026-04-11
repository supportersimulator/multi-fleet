"""Branch Convergence — visualize how multi-machine branches relate.

Tracks: which machines work on which branches, how far they've diverged,
where they overlap (file conflicts), and recommends merge strategy.
"""

import logging
import subprocess
import time
from dataclasses import dataclass, field
from html import escape as html_escape
from pathlib import Path

logger = logging.getLogger("fleet-nerve.branch-convergence")


@dataclass
class BranchInfo:
    branch: str
    machine_id: str
    commits_ahead: int
    commits_behind: int
    files_changed: list[str] = field(default_factory=list)
    last_commit_ts: float = 0.0


class BranchConvergence:
    """Scan fleet branches, detect conflicts, recommend merge order."""

    # Prefixes that indicate fleet branches
    FLEET_PREFIXES = ("fleet/", "mac1/", "mac2/", "mac3/", "race/", "worktree/")

    def __init__(self, repo_path: str = "."):
        self._repo = Path(repo_path).resolve()

    def _git(self, *args: str) -> str:
        """Run git command, return stdout. Raises on failure."""
        result = subprocess.run(
            ["git", "-C", str(self._repo), *args],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)}: {result.stderr.strip()}")
        return result.stdout.strip()

    def _guess_machine(self, branch: str) -> str:
        """Extract machine ID from branch name."""
        for prefix in ("mac1/", "mac2/", "mac3/"):
            if branch.startswith(prefix) or f"/{prefix}" in branch:
                return prefix.rstrip("/")
        # Check last committer
        try:
            author = self._git("log", "-1", "--format=%an", f"origin/{branch}")
            for m in ("mac1", "mac2", "mac3"):
                if m in author.lower():
                    return m
        except (RuntimeError, subprocess.TimeoutExpired):
            pass
        return "unknown"

    def scan_branches(self, base: str = "main") -> list[BranchInfo]:
        """Scan all fleet-related remote branches."""
        try:
            raw = self._git("branch", "-r", "--format=%(refname:short)")
        except RuntimeError:
            return []

        branches: list[BranchInfo] = []
        for ref in raw.splitlines():
            ref = ref.strip()
            if not ref or ref == f"origin/{base}" or ref.endswith("/HEAD"):
                continue
            short = ref.removeprefix("origin/")
            is_fleet = any(short.startswith(p) for p in self.FLEET_PREFIXES)
            if not is_fleet:
                continue

            info = self._branch_info(ref, short, base)
            if info:
                branches.append(info)

        branches.sort(key=lambda b: b.last_commit_ts)
        return branches

    def _branch_info(self, ref: str, short: str, base: str) -> BranchInfo | None:
        """Build BranchInfo for a single branch."""
        try:
            ahead_s = self._git("rev-list", "--count", f"origin/{base}..{ref}")
            behind_s = self._git("rev-list", "--count", f"{ref}..origin/{base}")
        except RuntimeError:
            return None

        ahead = int(ahead_s) if ahead_s.isdigit() else 0
        behind = int(behind_s) if behind_s.isdigit() else 0

        # Skip branches with no divergence
        if ahead == 0 and behind == 0:
            return None

        # Files changed
        files: list[str] = []
        try:
            diff_out = self._git("diff", "--name-only", f"origin/{base}...{ref}")
            files = [f for f in diff_out.splitlines() if f.strip()]
        except RuntimeError:
            pass

        # Last commit timestamp
        ts = 0.0
        try:
            ts_s = self._git("log", "-1", "--format=%ct", ref)
            ts = float(ts_s) if ts_s else 0.0
        except (RuntimeError, ValueError):
            pass

        machine = self._guess_machine(short)
        return BranchInfo(
            branch=short, machine_id=machine,
            commits_ahead=ahead, commits_behind=behind,
            files_changed=files, last_commit_ts=ts,
        )

    def conflict_matrix(self, branches: list[BranchInfo]) -> dict[tuple[str, str], list[str]]:
        """Which branches touch the same files? Returns {(a, b): [overlapping]}."""
        matrix: dict[tuple[str, str], list[str]] = {}
        for i, a in enumerate(branches):
            for b in branches[i + 1:]:
                overlap = sorted(set(a.files_changed) & set(b.files_changed))
                if overlap:
                    matrix[(a.branch, b.branch)] = overlap
        return matrix

    def merge_recommendations(self, branches: list[BranchInfo]) -> list[dict]:
        """Recommend merge order: oldest first, least conflicts, smallest diff."""
        if not branches:
            return []

        conflicts = self.conflict_matrix(branches)
        conflict_count: dict[str, int] = {}
        for (a, b), files in conflicts.items():
            conflict_count[a] = conflict_count.get(a, 0) + len(files)
            conflict_count[b] = conflict_count.get(b, 0) + len(files)

        scored = []
        for b in branches:
            score = (
                b.last_commit_ts,                         # oldest first
                conflict_count.get(b.branch, 0),          # fewest conflicts
                b.commits_ahead,                          # smallest diff
            )
            scored.append((score, b))

        scored.sort(key=lambda x: x[0])
        recs = []
        for rank, (score, b) in enumerate(scored, 1):
            conflicts_for = conflict_count.get(b.branch, 0)
            if conflicts_for == 0:
                strategy = "fast-forward" if b.commits_behind == 0 else "rebase"
            else:
                strategy = "merge --no-ff" if conflicts_for < 5 else "manual review"
            recs.append({
                "rank": rank,
                "branch": b.branch,
                "machine": b.machine_id,
                "ahead": b.commits_ahead,
                "behind": b.commits_behind,
                "conflict_files": conflicts_for,
                "strategy": strategy,
            })
        return recs

    def render_ascii(self, branches: list[BranchInfo]) -> str:
        """ASCII art branch visualization."""
        if not branches:
            return "No fleet branches diverged from main."

        lines = ["Branch Convergence Map", "=" * 50]
        conflicts = self.conflict_matrix(branches)

        for b in branches:
            arrow = "+" * min(b.commits_ahead, 30) or "="
            behind_arrow = "-" * min(b.commits_behind, 10)
            machine_tag = f"[{b.machine_id}]" if b.machine_id != "unknown" else ""
            lines.append(
                f"  main {'<' + behind_arrow if b.commits_behind else ''}──┤ "
                f"{arrow}> {b.branch} {machine_tag} "
                f"(+{b.commits_ahead}/-{b.commits_behind}, {len(b.files_changed)} files)"
            )

        if conflicts:
            lines.append("")
            lines.append("Conflicts:")
            for (a, b_name), files in conflicts.items():
                lines.append(f"  ⚠ {a} ↔ {b_name}: {len(files)} shared files")
                for f in files[:5]:
                    lines.append(f"    - {f}")
                if len(files) > 5:
                    lines.append(f"    ... and {len(files) - 5} more")

        return "\n".join(lines)

    def render_html(self, branches: list[BranchInfo]) -> str:
        """HTML branch convergence map for dashboard."""
        if not branches:
            return "<div class='convergence'>No fleet branches diverged from main.</div>"

        conflicts = self.conflict_matrix(branches)
        rows = []
        for b in branches:
            conflict_pairs = [
                k for k in conflicts if b.branch in k
            ]
            conflict_class = "conflict" if conflict_pairs else "clean"
            bar_width = min(b.commits_ahead * 8, 200)
            rows.append(
                f"<tr class='{conflict_class}'>"
                f"<td>{html_escape(b.branch)}</td>"
                f"<td>{html_escape(b.machine_id)}</td>"
                f"<td>+{b.commits_ahead} / -{b.commits_behind}</td>"
                f"<td>{len(b.files_changed)}</td>"
                f"<td><div class='bar' style='width:{bar_width}px'></div></td>"
                f"</tr>"
            )

        conflict_rows = []
        for (a, b_name), files in conflicts.items():
            conflict_rows.append(
                f"<tr><td>{html_escape(a)}</td><td>{html_escape(b_name)}</td>"
                f"<td>{len(files)}</td><td>{html_escape(', '.join(files[:5]))}</td></tr>"
            )

        html = (
            "<div class='convergence'>"
            "<h3>Branch Convergence</h3>"
            "<table><thead><tr><th>Branch</th><th>Machine</th>"
            "<th>Ahead/Behind</th><th>Files</th><th>Divergence</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>"
        )
        if conflict_rows:
            html += (
                "<h4>Conflict Matrix</h4>"
                "<table class='conflicts'><thead><tr><th>Branch A</th><th>Branch B</th>"
                "<th>Shared Files</th><th>Files</th></tr></thead>"
                f"<tbody>{''.join(conflict_rows)}</tbody></table>"
            )
        html += "</div>"
        return html

    def to_dict(self) -> dict:
        """Full convergence report as dict (for MCP tool response)."""
        branches = self.scan_branches()
        conflicts = self.conflict_matrix(branches)
        recs = self.merge_recommendations(branches)
        return {
            "branches": [
                {
                    "branch": b.branch, "machine": b.machine_id,
                    "ahead": b.commits_ahead, "behind": b.commits_behind,
                    "files": b.files_changed, "last_commit_ts": b.last_commit_ts,
                }
                for b in branches
            ],
            "conflicts": {
                f"{a}|{b}": files for (a, b), files in conflicts.items()
            },
            "recommendations": recs,
            "ascii": self.render_ascii(branches),
            "timestamp": time.time(),
        }
