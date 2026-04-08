"""Theatrical Fleet Presentations — dramatic terminal output for fleet operations.

Makes fleet_race, gains_gate, evidence verification, and probe results
visually compelling. Uses Rich for colors, tables, panels, and live updates.
Gracefully degrades to plain text when Rich is unavailable.
"""

import io
import time
from typing import Optional

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.text import Text
    from rich.columns import Columns
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
    from rich import box
    HAS_RICH = True
except ImportError:
    HAS_RICH = False

# Module-level console — used by FleetTheater and importable directly
console = Console(stderr=False) if HAS_RICH else None

# ── Constants ───────────────────────────────────────────────────────

NODE_STYLES = {
    "mac1": ("bold cyan", "cyan", "Chief"),
    "mac2": ("bold magenta", "magenta", "Worker"),
    "mac3": ("bold green", "green", "Worker"),
}

STATUS_STYLE = {
    "racing": "bold yellow",
    "dispatched": "yellow",
    "completed": "bold green",
    "done": "bold green",
    "failed": "bold red",
    "timed_out": "bold red",
    "cross_review": "bold blue",
    "synthesizing": "bold cyan",
    "starting": "dim",
    "pending": "dim",
    "dispatch_failed": "red",
    "lease_denied": "red",
}

SEVERITY_STYLE = {
    "critical": "bold red",
    "high": "red",
    "medium": "yellow",
    "low": "green",
    "info": "dim",
}

BANNER_ART = r"""
   _____ _     _____ _____ _____    ____      _     ____ _____
  |  ___| |   | ____| ____|_   _|  |  _ \    / \   / ___| ____|
  | |_  | |   |  _| |  _|   | |    | |_) |  / _ \ | |   |  _|
  |  _| | |___| |___| |___  | |    |  _ <  / ___ \| |___| |___
  |_|   |_____|_____|_____| |_|    |_| \_\/_/   \_\\____|_____|
"""

FLEET_BANNER_ART = r"""
    __  __       _ _   _       _____ _           _
   |  \/  |_   _| | |_(_)     |  ___| | ___  ___| |_
   | |\/| | | | | | __| |_____| |_  | |/ _ \/ _ \ __|
   | |  | | |_| | | |_| |_____|  _| | |  __/  __/ |_
   |_|  |_|\__,_|_|\__|_|     |_|   |_|\___|\___|\__|
"""


# ── Plain-text fallback helpers ─────────────────────────────────────

def _plain_print(text: str):
    """Print without Rich formatting."""
    print(text)


def _plain_box(title: str, body: str, width: int = 60) -> str:
    """ASCII box for plain text mode."""
    border = "=" * width
    lines = [border, f"  {title}", border]
    for line in body.strip().splitlines():
        lines.append(f"  {line}")
    lines.append(border)
    return "\n".join(lines)


# ── FleetTheater ────────────────────────────────────────────────────

class FleetTheater:
    """Theatrical output for fleet operations.

    Every method gracefully degrades to plain text when Rich is not installed.
    """

    def __init__(self, console_instance: Optional[object] = None):
        self._console = console_instance or console
        self._rich = HAS_RICH and self._console is not None

    # ── Race Presentation ───────────────────────────────────────────

    def race_banner(self, task: str, participants: list[str]):
        """Display dramatic race start banner with ASCII art."""
        if not self._rich:
            _plain_print(_plain_box("FLEET RACE", f"Task: {task}\nNodes: {', '.join(participants)}"))
            return

        c = self._console
        # ASCII art banner
        art_text = Text(BANNER_ART, style="bold bright_blue")
        c.print(art_text)

        # Task panel
        task_text = Text()
        task_text.append("TASK: ", style="bold white")
        task_text.append(task, style="bold bright_yellow")
        c.print(Panel(task_text, border_style="bright_blue", padding=(0, 2)))

        # Participant cards
        cards = []
        for i, node in enumerate(participants):
            style, color, role = NODE_STYLES.get(node, ("bold white", "white", "Node"))
            lane = f"Lane {i + 1}"
            node_text = Text()
            node_text.append(f" {lane} ", style="bold white on dark_blue")
            node_text.append(f"\n {node.upper()} ", style=style)
            node_text.append(f"\n {role}", style="dim")
            cards.append(Panel(node_text, border_style=color, width=20, padding=(0, 1)))

        c.print(Columns(cards, padding=(0, 2)))
        c.print()
        c.print(Text(" ON YOUR MARKS ... ", style="bold white on red"), justify="center")
        c.print()

    def race_scoreboard(self, race_state: dict):
        """Display race scoreboard showing each node's progress."""
        if not self._rich:
            _plain_print("\n--- Race Scoreboard ---")
            for node, p in race_state.get("participants", {}).items():
                elapsed = ""
                if p.get("completed_at") and race_state.get("started_at"):
                    elapsed = f" ({p['completed_at'] - race_state['started_at']:.1f}s)"
                _plain_print(f"  {node}: {p.get('status', '?')}{elapsed}")
            return

        c = self._console
        table = Table(
            title="[bold bright_blue]Race Scoreboard[/]",
            box=box.HEAVY_HEAD,
            border_style="bright_blue",
            header_style="bold white",
            expand=True,
            padding=(0, 1),
        )
        table.add_column("", width=3, justify="center")
        table.add_column("Node", style="bold", min_width=8)
        table.add_column("Branch", style="dim", max_width=35)
        table.add_column("Status", justify="center", min_width=12)
        table.add_column("Findings", justify="center", min_width=8)
        table.add_column("Time", justify="right", min_width=8)

        started = race_state.get("started_at", time.time())
        for node, p in race_state.get("participants", {}).items():
            status = p.get("status", "unknown")
            style, color, _role = NODE_STYLES.get(node, ("bold white", "white", "Node"))

            # Status with color
            st_style = STATUS_STYLE.get(status, "dim")
            status_text = Text(status.upper(), style=st_style)

            # Position indicator
            if status == "completed":
                pos = Text("*", style="bold green")
            elif status == "dispatched":
                pos = Text(">", style="yellow")
            elif status in ("failed", "timed_out", "dispatch_failed"):
                pos = Text("X", style="bold red")
            else:
                pos = Text("-", style="dim")

            # Elapsed time
            if p.get("completed_at"):
                elapsed = f"{p['completed_at'] - started:.1f}s"
            elif p.get("dispatched_at"):
                elapsed = f"{time.time() - started:.1f}s"
            else:
                elapsed = "-"

            # Finding count
            result = p.get("result", {}) or {}
            findings = str(len(result.get("findings", []))) if result.get("findings") else "-"

            branch = p.get("branch", "-")
            if len(branch) > 35:
                branch = "..." + branch[-32:]

            table.add_row(pos, Text(node, style=style), branch, status_text, findings, elapsed)

        c.print(table)
        c.print()

    def race_winner(self, winner: str, reason: str, evidence: list):
        """Dramatic winner announcement with evidence summary."""
        if not self._rich:
            _plain_print(f"\n*** WINNER: {winner} ***")
            _plain_print(f"Reason: {reason}")
            for e in evidence:
                _plain_print(f"  - {e}")
            return

        c = self._console
        style, color, _role = NODE_STYLES.get(winner, ("bold white", "white", "Node"))

        c.print()
        # Trophy banner
        trophy_text = Text()
        trophy_text.append(" WINNER ", style="bold white on dark_green")
        trophy_text.append(f"  {winner.upper()}  ", style=f"bold {color} on black")
        c.print(trophy_text, justify="center")
        c.print()

        # Reason
        body = Text()
        body.append("Decision: ", style="bold")
        body.append(reason, style="white")
        if evidence:
            body.append("\n\nEvidence:", style="bold")
            for entry in evidence:
                body.append(f"\n  {entry}", style="dim")

        c.print(Panel(body, title="[bold bright_green]Race Result[/]",
                       border_style="bright_green", padding=(1, 2)))
        c.print()

    # ── Gains Gate ──────────────────────────────────────────────────

    def gains_gate_display(self, result: dict):
        """Visual gains gate with check/X marks and severity colors."""
        if not self._rich:
            status = "PASSED" if result.get("passed") else "FAILED"
            _plain_print(f"\n=== Gains Gate: {status} ===")
            for c in result.get("checks", []):
                icon = "[+]" if c.get("passed") else "[X]"
                crit = " CRITICAL" if c.get("critical") and not c.get("passed") else ""
                _plain_print(f"  {icon} {c.get('name', '?')}: {c.get('message', '')}{crit}")
            _plain_print(f"  {result.get('passed_count', 0)}/{result.get('total', 0)} passed")
            _plain_print(f"  {result.get('recommendation', '')}")
            return

        c = self._console
        gate_passed = result.get("passed", False)
        title_style = "bold bright_green" if gate_passed else "bold bright_red"
        title_icon = "[bold green]PASSED[/]" if gate_passed else "[bold red]FAILED[/]"

        table = Table(
            title=f"[bold]Gains Gate[/] {title_icon}",
            box=box.ROUNDED,
            border_style="green" if gate_passed else "red",
            header_style="bold",
            expand=True,
            padding=(0, 1),
        )
        table.add_column("", width=3, justify="center")
        table.add_column("Check", style="bold", min_width=20)
        table.add_column("Result", min_width=30)
        table.add_column("Latency", justify="right", width=8)
        table.add_column("Crit", justify="center", width=4)

        for check in result.get("checks", []):
            passed = check.get("passed", False)
            critical = check.get("critical", False)
            icon = Text("+", style="bold green") if passed else Text("X", style="bold red")
            name = check.get("name", "?")
            msg = check.get("message", "")
            lat = f"{check.get('latency_ms', 0):.0f}ms"
            crit_mark = Text("!", style="bold red") if critical and not passed else (
                Text("*", style="dim") if critical else Text("-", style="dim"))

            msg_style = "green" if passed else "red"
            table.add_row(icon, name, Text(msg, style=msg_style), lat, crit_mark)

        c.print(table)

        # Summary bar
        total = result.get("total", 0)
        passed_count = result.get("passed_count", 0)
        failed_count = result.get("failed_count", 0)
        crit_fails = result.get("critical_failures", 0)
        duration = result.get("duration_ms", 0)

        summary = Text()
        summary.append(f" {passed_count}/{total} passed", style="bold green")
        if failed_count:
            summary.append(f"  {failed_count} failed", style="bold red")
        if crit_fails:
            summary.append(f"  {crit_fails} CRITICAL", style="bold white on red")
        summary.append(f"  {duration:.0f}ms", style="dim")
        c.print(Panel(summary, border_style="dim", padding=(0, 1)))

        # Recommendation
        rec = result.get("recommendation", "")
        if rec:
            rec_style = "green" if gate_passed else "red"
            c.print(Text(f"  {rec}", style=rec_style))
        c.print()

    # ── Evidence Chain ──────────────────────────────────────────────

    def evidence_chain(self, entries: list, verified: bool):
        """Display evidence chain with hash verification status."""
        if not self._rich:
            status = "VERIFIED" if verified else "BROKEN"
            _plain_print(f"\n=== Evidence Chain: {status} ({len(entries)} entries) ===")
            for e in entries:
                _plain_print(f"  #{e.get('seq', '?')} [{e.get('event_type', '?')}] "
                             f"{e.get('subject', '')} hash={e.get('entry_hash', '?')[:12]}...")
            return

        c = self._console
        chain_ok = verified
        title_icon = "[bold green]VERIFIED[/]" if chain_ok else "[bold red]CHAIN BROKEN[/]"

        table = Table(
            title=f"[bold]Evidence Chain[/] {title_icon}",
            box=box.SIMPLE_HEAVY,
            border_style="green" if chain_ok else "red",
            header_style="bold",
            expand=True,
        )
        table.add_column("#", justify="right", width=5, style="dim")
        table.add_column("Event", min_width=18, style="bold")
        table.add_column("Node", width=6, justify="center")
        table.add_column("Subject", min_width=20)
        table.add_column("Hash", width=14, style="dim cyan")
        table.add_column("Prev", width=14, style="dim")

        for entry in entries:
            seq = str(entry.get("seq", "?"))
            evt = entry.get("event_type", "?")
            node = entry.get("node_id", "?")
            subj = entry.get("subject", "")[:30]
            h = entry.get("entry_hash", "")[:12] + ".."
            prev = entry.get("prev_hash", "")[:12] + ".."
            node_style = NODE_STYLES.get(node, ("white", "white", ""))[0]
            table.add_row(seq, evt, Text(node, style=node_style), subj, h, prev)

        c.print(table)
        c.print()

    # ── Probe Findings ──────────────────────────────────────────────

    def probe_findings(self, results: list):
        """Display probe findings with severity-colored output."""
        if not self._rich:
            _plain_print("\n--- Probe Findings ---")
            for r in results:
                sev = r.get("severity", "info").upper()
                _plain_print(f"  [{sev}] {r.get('probe', '?')}: {r.get('message', '')}")
            return

        c = self._console
        table = Table(
            title="[bold]Probe Findings[/]",
            box=box.ROUNDED,
            border_style="bright_blue",
            header_style="bold",
            expand=True,
            padding=(0, 1),
        )
        table.add_column("Sev", width=8, justify="center")
        table.add_column("Probe", style="bold", min_width=16)
        table.add_column("Finding", min_width=30)
        table.add_column("Node", width=6, justify="center")

        for r in results:
            sev = r.get("severity", "info")
            sev_style = SEVERITY_STYLE.get(sev, "dim")
            sev_text = Text(sev.upper(), style=sev_style)
            probe = r.get("probe", "?")
            msg = r.get("message", "")
            node = r.get("node", "")
            node_style = NODE_STYLES.get(node, ("white", "white", ""))[0] if node else "dim"
            table.add_row(sev_text, probe, msg, Text(node, style=node_style) if node else Text("-", style="dim"))

        c.print(table)
        c.print()

    # ── Node Status Cards ───────────────────────────────────────────

    def node_status_cards(self, nodes: list):
        """Display fleet nodes as side-by-side Rich panels."""
        if not self._rich:
            _plain_print("\n=== Fleet Nodes ===")
            for n in nodes:
                name = n.get("name", n.get("node_id", "?"))
                status = n.get("status", "unknown")
                _plain_print(f"  [{name}] {status} | peers={n.get('peers', 0)} | uptime={n.get('uptime', '-')}")
            return

        c = self._console
        cards = []
        for n in nodes:
            name = n.get("name", n.get("node_id", "?"))
            status = n.get("status", "unknown")
            style, color, role = NODE_STYLES.get(name, ("bold white", "white", "Node"))
            nats = n.get("nats", n.get("nats_connected", False))
            peers = n.get("peers", n.get("peer_count", 0))
            uptime = n.get("uptime", n.get("uptime_s", 0))
            sessions = n.get("sessions", n.get("active_sessions", 0))

            body = Text()
            body.append(f"{name}", style=style)
            body.append(f" ({role})\n", style="dim")

            # Status line
            if status in ("ok", "healthy", "online"):
                body.append("ONLINE", style="bold green")
            elif status in ("degraded", "warn"):
                body.append("DEGRADED", style="bold yellow")
            else:
                body.append(status.upper(), style="bold red")

            body.append(f"\n\nNATS:     ", style="dim")
            body.append("connected" if nats else "disconnected",
                        style="green" if nats else "red")
            body.append(f"\nPeers:    ", style="dim")
            body.append(str(peers), style="cyan")
            body.append(f"\nUptime:   ", style="dim")
            body.append(str(uptime) if isinstance(uptime, str) else f"{int(uptime)}s", style="cyan")
            body.append(f"\nSessions: ", style="dim")
            body.append(str(sessions), style="cyan")

            cards.append(Panel(
                body,
                title=f"[{style}]{name.upper()}[/]",
                border_style=color,
                width=28,
                padding=(1, 2),
            ))

        c.print(Columns(cards, padding=(0, 2)))
        c.print()

    # ── Truth Ladder ────────────────────────────────────────────────

    def truth_ladder_display(self, stats: dict):
        """Visual truth ladder: Inbox -> Vision -> Snapshot -> DAO."""
        layers = [
            ("Inbox", stats.get("inbox", 0), "bright_blue"),
            ("Vision", stats.get("vision", 0), "cyan"),
            ("Snapshot", stats.get("snapshot", 0), "yellow"),
            ("DAO", stats.get("dao", 0), "bright_green"),
        ]

        if not self._rich:
            _plain_print("\n=== Truth Ladder ===")
            for name, count, _ in layers:
                bar = "#" * min(count, 40)
                _plain_print(f"  {name:>10} [{bar}] {count}")
            return

        c = self._console
        table = Table(
            title="[bold]Truth Ladder[/]",
            box=box.SIMPLE,
            border_style="bright_blue",
            show_header=False,
            expand=True,
            padding=(0, 1),
        )
        table.add_column("Layer", width=12, style="bold", justify="right")
        table.add_column("", width=3, justify="center")
        table.add_column("Bar", min_width=30)
        table.add_column("Count", width=6, justify="right", style="bold cyan")

        max_count = max((count for _, count, _ in layers), default=1) or 1
        for name, count, color in layers:
            bar_width = int((count / max_count) * 30) if max_count else 0
            bar = Text("=" * bar_width, style=f"bold {color}")
            arrow = Text("->", style="dim")
            table.add_row(Text(name, style=f"bold {color}"), arrow, bar, str(count))

        c.print(table)
        c.print()

    # ── Supervisor Progress ─────────────────────────────────────────

    def supervisor_progress(self, state: dict):
        """Display supervisor phase progression bar."""
        phases = [
            "init", "discover", "health_check", "lease_acquire",
            "dispatch", "racing", "first_complete", "cross_review",
            "rebuttal", "synthesis", "evidence_record", "done",
        ]
        current = state.get("phase", "init")
        current_idx = phases.index(current) if current in phases else 0

        if not self._rich:
            _plain_print(f"\n=== Supervisor: phase {current_idx + 1}/{len(phases)} [{current}] ===")
            progress_bar = "=" * (current_idx + 1) + "-" * (len(phases) - current_idx - 1)
            _plain_print(f"  [{progress_bar}]")
            return

        c = self._console
        table = Table(
            title="[bold]Supervisor Phases[/]",
            box=box.SIMPLE,
            show_header=False,
            expand=True,
            padding=(0, 0),
        )
        table.add_column("Phase", min_width=40)

        row_text = Text()
        for i, phase in enumerate(phases):
            if i > 0:
                if i <= current_idx:
                    row_text.append(" -> ", style="bold green")
                else:
                    row_text.append(" -> ", style="dim")
            if i < current_idx:
                row_text.append(phase, style="bold green")
            elif i == current_idx:
                row_text.append(f"[{phase}]", style="bold bright_yellow")
            else:
                row_text.append(phase, style="dim")

        c.print(Panel(row_text, title=f"[bold]Phase {current_idx + 1}/{len(phases)}[/]",
                       border_style="bright_blue", padding=(0, 2)))
        c.print()

    # ── Fleet Banner ────────────────────────────────────────────────

    def fleet_banner(self):
        """Display the ContextDNA Multi-Fleet banner."""
        if not self._rich:
            _plain_print(FLEET_BANNER_ART)
            _plain_print("  ContextDNA Multi-Fleet Orchestration")
            return

        c = self._console
        art = Text(FLEET_BANNER_ART, style="bold bright_blue")
        subtitle = Text("  ContextDNA Multi-Fleet Orchestration", style="bold white")
        body = Text()
        body.append_text(art)
        body.append("\n")
        body.append_text(subtitle)
        c.print(Panel(body, border_style="bright_blue", padding=(0, 2)))
        c.print()

    # ── Doctor Display ──────────────────────────────────────────────

    def doctor_display(self, checks: list, summary: str = ""):
        """Display fleet doctor results with check/X marks."""
        if not self._rich:
            _plain_print("\n=== Fleet Doctor ===")
            for check in checks:
                status = check.get("status", "?")
                icon = {"pass": "[+]", "warn": "[~]", "fail": "[!]", "skip": "[-]"}.get(status, "[?]")
                _plain_print(f"  {icon} {check.get('code', '')} {check.get('name', '')}: {check.get('message', '')}")
            if summary:
                _plain_print(f"\n  {summary}")
            return

        c = self._console
        table = Table(
            title="[bold]Fleet Doctor[/]",
            box=box.ROUNDED,
            border_style="bright_blue",
            header_style="bold",
            expand=True,
            padding=(0, 1),
        )
        table.add_column("", width=3, justify="center")
        table.add_column("Code", width=7, style="dim")
        table.add_column("Check", style="bold", min_width=18)
        table.add_column("Result", min_width=30)
        table.add_column("ms", justify="right", width=6)

        for check in checks:
            status = check.get("status", "?")
            if status == "pass":
                icon = Text("+", style="bold green")
                msg_style = "green"
            elif status == "warn":
                icon = Text("~", style="bold yellow")
                msg_style = "yellow"
            elif status == "fail":
                icon = Text("!", style="bold red")
                msg_style = "red"
            else:
                icon = Text("-", style="dim")
                msg_style = "dim"

            code = check.get("code", "")
            name = check.get("name", "?")
            msg = check.get("message", "")
            lat = f"{check.get('latency_ms', 0):.0f}"

            table.add_row(icon, code, name, Text(msg, style=msg_style), lat)

        c.print(table)
        if summary:
            c.print(Text(f"  {summary}", style="bold"))
        c.print()
