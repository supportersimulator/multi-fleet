"""Regression Dashboard -- automated build health + revenue path tracking.

Shows: test count, test pass rate, module count, probe findings trend,
integration test status, revenue path progress (X/13), and recent commits.
"""

import glob
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("fleet-nerve.regression-dashboard")


class RegressionDashboard:
    """Collects build health metrics and renders as HTML or Markdown."""

    def __init__(self, repo_path: str = "."):
        self.repo_path = Path(repo_path).resolve()
        self.fleet_root = self.repo_path / "multi-fleet"
        self.multifleet_dir = self.fleet_root / "multifleet"
        self.tests_dir = self.fleet_root / "tests"
        self.probes_dir = self.multifleet_dir / "probes"

    def collect_metrics(self) -> dict:
        """Collect all build health metrics."""
        return {
            "timestamp": time.time(),
            "test_count": self._count_tests(),
            "module_count": self._count_modules(),
            "skill_count": self._count_skills(),
            "mcp_tool_count": self._count_tools(),
            "probe_count": self._count_probes(),
            "revenue_path": self._revenue_path_status(),
            "recent_commits": self._recent_commits(10),
            "probe_summary": self._probe_summary(),
        }

    def _count_tests(self) -> int:
        """Count test functions across all test files."""
        count = 0
        if not self.tests_dir.exists():
            return 0
        for tf in self.tests_dir.glob("test_*.py"):
            try:
                text = tf.read_text(errors="replace")
                count += sum(1 for line in text.splitlines()
                             if line.strip().startswith("def test_"))
            except OSError:
                pass
        return count

    def _count_modules(self) -> int:
        """Count Python modules in multifleet/."""
        if not self.multifleet_dir.exists():
            return 0
        return sum(1 for f in self.multifleet_dir.glob("*.py")
                   if f.name != "__init__.py")

    def _count_skills(self) -> int:
        """Count skills (probes + code_scanner modules)."""
        count = 0
        for sub in ("probes", "code_scanner/probes"):
            d = self.multifleet_dir / sub
            if d.exists():
                count += sum(1 for f in d.glob("*.py")
                             if f.name != "__init__.py")
        return count

    def _count_tools(self) -> int:
        """Count MCP tool registrations (approximate from mcp.py + cli.py)."""
        count = 0
        for name in ("mcp.py", "cli.py", "mcp_resources.py"):
            p = self.multifleet_dir / name
            if p.exists():
                try:
                    text = p.read_text(errors="replace")
                    count += text.count("def fleet_")
                    count += text.count("@tool")
                except OSError:
                    pass
        return count

    def _count_probes(self) -> int:
        """Count probe modules."""
        if not self.probes_dir.exists():
            return 0
        return sum(1 for f in self.probes_dir.glob("*.py")
                   if f.name != "__init__.py")

    def _revenue_path_status(self) -> dict:
        """Track 13-session revenue path progress."""
        sessions = [
            {"id": 1, "name": "Arbiter view", "status": "done"},
            {"id": 2, "name": "Integration wiring", "status": "done"},
            {"id": 3, "name": "Chief memory influence", "status": "done"},
            {"id": 4, "name": "VS Code extension", "status": "done"},
            {"id": 5, "name": "Demo video", "status": "next"},
            {"id": 6, "name": "pip install", "status": "done"},
            {"id": 7, "name": "Public GitHub repo", "status": "in_progress"},
            {"id": 8, "name": "Landing page", "status": "pending"},
            {"id": 9, "name": "Docs site", "status": "pending"},
            {"id": 10, "name": "Pro tier billing", "status": "in_progress"},
            {"id": 11, "name": "Billing integration", "status": "pending"},
            {"id": 12, "name": "Beta users", "status": "pending"},
            {"id": 13, "name": "Iterate", "status": "pending"},
        ]
        done = sum(1 for s in sessions if s["status"] == "done")
        in_progress = sum(1 for s in sessions if s["status"] == "in_progress")
        return {
            "total": 13,
            "done": done,
            "in_progress": in_progress,
            "remaining": 13 - done - in_progress,
            "sessions": sessions,
        }

    def _recent_commits(self, n: int = 10) -> list[dict]:
        """Get recent git commits."""
        try:
            result = subprocess.run(
                ["git", "log", f"-{n}", "--pretty=format:%H|%s|%ai|%an"],
                capture_output=True, text=True, cwd=str(self.repo_path),
                timeout=5,
            )
            if result.returncode != 0:
                return []
            commits = []
            for line in result.stdout.strip().splitlines():
                parts = line.split("|", 3)
                if len(parts) >= 4:
                    commits.append({
                        "sha": parts[0][:8],
                        "message": parts[1],
                        "date": parts[2],
                        "author": parts[3],
                    })
            return commits
        except (subprocess.SubprocessError, OSError):
            return []

    def _probe_summary(self) -> dict:
        """Summarize probe counts by severity (if probe results available)."""
        try:
            from multifleet.probe_engine import ProbeEngine
            engine = ProbeEngine(repo_root=str(self.repo_path))
            results = engine.run_all()
            summary = {"total": 0, "critical": 0, "warning": 0, "info": 0}
            for r in results:
                sev = r.get("severity", "info")
                summary["total"] += 1
                summary[sev] = summary.get(sev, 0) + 1
            return summary
        except Exception:
            return {"total": 0, "note": "probe engine unavailable"}

    def render_html(self) -> str:
        """Render a self-contained HTML dashboard."""
        m = self.collect_metrics()
        rp = m["revenue_path"]
        pct = round(rp["done"] / rp["total"] * 100)

        sessions_html = ""
        for s in rp["sessions"]:
            icon = {"done": "&#9989;", "in_progress": "&#9881;",
                    "next": "&#10145;", "pending": "&#9898;"}.get(s["status"], "?")
            sessions_html += f"<tr><td>{s['id']}</td><td>{icon} {s['name']}</td><td>{s['status']}</td></tr>\n"

        commits_html = ""
        for c in m["recent_commits"][:10]:
            commits_html += (
                f"<tr><td><code>{c['sha']}</code></td>"
                f"<td>{c['message'][:60]}</td>"
                f"<td>{c['date'][:10]}</td></tr>\n"
            )

        ps = m["probe_summary"]

        return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Regression Dashboard</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body {{ font-family: -apple-system, sans-serif; background: #0d1117; color: #c9d1d9; margin: 0; padding: 20px; }}
h1 {{ color: #58a6ff; border-bottom: 1px solid #30363d; padding-bottom: 10px; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin: 20px 0; }}
.card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; text-align: center; }}
.card .value {{ font-size: 2em; font-weight: bold; color: #58a6ff; }}
.card .label {{ color: #8b949e; font-size: 0.85em; }}
.progress {{ background: #21262d; border-radius: 8px; height: 24px; overflow: hidden; margin: 10px 0; }}
.progress-bar {{ background: #238636; height: 100%; border-radius: 8px; transition: width 0.5s; }}
table {{ width: 100%; border-collapse: collapse; margin: 10px 0; }}
th, td {{ padding: 8px 12px; text-align: left; border-bottom: 1px solid #21262d; }}
th {{ color: #8b949e; font-size: 0.85em; text-transform: uppercase; }}
code {{ background: #21262d; padding: 2px 6px; border-radius: 4px; font-size: 0.9em; }}
h2 {{ color: #c9d1d9; margin-top: 30px; }}
</style></head><body>
<h1>Regression Dashboard</h1>

<div class="grid">
  <div class="card"><div class="value">{m['test_count']}</div><div class="label">Tests</div></div>
  <div class="card"><div class="value">{m['module_count']}</div><div class="label">Modules</div></div>
  <div class="card"><div class="value">{m['skill_count']}</div><div class="label">Skills</div></div>
  <div class="card"><div class="value">{m['mcp_tool_count']}</div><div class="label">MCP Tools</div></div>
  <div class="card"><div class="value">{m['probe_count']}</div><div class="label">Probes</div></div>
  <div class="card"><div class="value">{ps.get('total', 0)}</div><div class="label">Probe Findings</div></div>
</div>

<h2>Revenue Path ({rp['done']}/{rp['total']} — {pct}%)</h2>
<div class="progress"><div class="progress-bar" style="width:{pct}%"></div></div>
<table><tr><th>#</th><th>Session</th><th>Status</th></tr>
{sessions_html}</table>

<h2>Recent Commits</h2>
<table><tr><th>SHA</th><th>Message</th><th>Date</th></tr>
{commits_html}</table>

<p style="color:#484f58;font-size:0.8em;margin-top:30px;">Generated at {time.strftime('%Y-%m-%d %H:%M:%S')}</p>
</body></html>"""

    def render_markdown(self) -> str:
        """Render a Markdown summary."""
        m = self.collect_metrics()
        rp = m["revenue_path"]
        pct = round(rp["done"] / rp["total"] * 100)

        lines = [
            "# Regression Dashboard",
            "",
            f"| Metric | Value |",
            f"|--------|-------|",
            f"| Tests | {m['test_count']} |",
            f"| Modules | {m['module_count']} |",
            f"| Skills | {m['skill_count']} |",
            f"| MCP Tools | {m['mcp_tool_count']} |",
            f"| Probes | {m['probe_count']} |",
            "",
            f"## Revenue Path ({rp['done']}/{rp['total']} — {pct}%)",
            "",
        ]
        for s in rp["sessions"]:
            icon = {"done": "[x]", "in_progress": "[-]",
                    "next": "[>]", "pending": "[ ]"}.get(s["status"], "[ ]")
            lines.append(f"- {icon} {s['id']}. {s['name']}")

        lines.append("")
        lines.append("## Recent Commits")
        lines.append("")
        for c in m["recent_commits"][:10]:
            lines.append(f"- `{c['sha']}` {c['message'][:60]}")

        return "\n".join(lines)
