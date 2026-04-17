"""5-Zone Surgical Theater Dashboard — dramatic live fleet visualization.

Zones:
1. NODE RAIL: Fleet nodes with status, branch, health score
2. ACTIVE THEATER: Current race/task with probe findings per node
3. CHIEF SYNTHESIS: Leading branch, challenger, dissent, evidence
4. EVIDENCE LEDGER: Scrolling evidence stream with hash chain
5. TRUTH LADDER: Inbox -> Vision -> Snapshot -> DAO progression

Uses Rich Live for real-time terminal updates. Polls daemon every 2s.
Gracefully degrades when daemon is unreachable — never crashes.

Usage:
    python3 -m multifleet.theater_dashboard
    fleet theater
"""

import json
import sys
import threading
import time
import urllib.request
from datetime import datetime
from typing import Optional

try:
    from rich import box
    from rich.console import Console
    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    HAS_RICH = True
except ImportError:
    HAS_RICH = False

# ── Constants ─────────────────────────────────────────────────────────

DAEMON = "http://127.0.0.1:{port}"

NODE_STYLES = {
    "mac1": ("bold cyan", "cyan", "Chief"),
    "mac2": ("bold magenta", "magenta", "Worker"),
    "mac3": ("bold green", "green", "Worker"),
}

STATUS_ICONS = {
    "online": ("\u25cf", "bold green"),       # filled circle
    "stale": ("\u25cf", "bold yellow"),
    "offline": ("\u25cb", "bold red"),         # empty circle
    "unknown": ("\u25cb", "dim"),
    "self": ("\u2605", "bold bright_white"),   # star
}

SEVERITY_STYLE = {
    "critical": "bold red",
    "high": "red",
    "medium": "yellow",
    "low": "green",
    "info": "dim",
}

THEATER_BANNER = r"""
  _______ _    _ ______       _______ ______ _____
 |__   __| |  | |  ____|   /\|__   __|  ____|  __ \
    | |  | |__| | |__     /  \  | |  | |__  | |__) |
    | |  |  __  |  __|   / /\ \ | |  |  __| |  _  /
    | |  | |  | | |____ / ____ \| |  | |____| | \ \
    |_|  |_|  |_|______/_/    \_\_|  |______|_|  \_\
"""


def _ago(s: float) -> str:
    """Human-readable time-ago string."""
    s = int(s)
    if s < 0:
        return "?"
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    return f"{s // 3600}h {(s % 3600) // 60}m"


# ── Data Fetcher ──────────────────────────────────────────────────────

class TheaterDataFetcher:
    """Fetches all data needed by the 5-zone dashboard from the daemon.

    Never raises — returns empty/default dicts on any failure.
    Caches aggressively to avoid hammering daemon on 2s refresh.
    """

    def __init__(self, port: int = 8855, cache_ttl: float = 4.0):
        self._base = f"http://127.0.0.1:{port}"
        self._cache: dict[str, tuple[float, object]] = {}
        self._cache_ttl = cache_ttl

    def fetch_all(self) -> dict:
        """Single call to get everything the dashboard needs."""
        health = self._get("/health") or {}
        return {
            "health": health,
            "nodes": self._build_nodes(health),
            "races": (self._get("/races") or {}).get("races", []),
            "evidence": (self._get("/evidence/latest") or {}).get("entries", []),
            "truth": self._get("/truth/stats") or {},
            "supervisor": self._get("/supervisor") or {"tasks": []},
            # Read cached probe results for dashboard refreshes; explicit scans
            # should remain user-triggered, not tied to every page poll.
            "probe_results": ((self._get("/probes") or self._get("/scan/results") or {}).get("results", [])),
            "gains_gate": self._get("/gains-gate?quick=1") or {},
            "timestamp": time.time(),
            "online": bool(health),
        }

    def _build_nodes(self, health: dict) -> list:
        if not health:
            return []
        nodes = []
        ch = health.get("channel_health", {})
        # Self
        nodes.append({
            "id": health.get("nodeId", "?"),
            "status": "self",
            "branch": health.get("git", {}).get("branch", "?"),
            "sessions": health.get("activeSessions", 0),
            "uptime_s": health.get("uptime_s", 0),
            "score": "self",
            "nats": health.get("nats_connected", False) or health.get("transport") == "nats",
        })
        # Peers
        for name, peer in health.get("peers", {}).items():
            ls = peer.get("lastSeen", 9999)
            status = "online" if ls < 30 else ("stale" if ls < 120 else "offline")
            peer_ch = ch.get(name, {})
            nodes.append({
                "id": name,
                "status": status,
                "branch": peer.get("branch", "?"),
                "sessions": peer.get("sessions", 0),
                "last_seen_s": ls,
                "score": peer_ch.get("score", "?"),
                "nats": True,
                "transport": peer.get("transport", "?"),
            })
        return nodes

    def _get(self, path: str, timeout: float = 3.0) -> Optional[dict]:
        now = time.time()
        cached = self._cache.get(path)
        if cached and (now - cached[0]) < self._cache_ttl:
            return cached[1]
        try:
            url = f"{self._base}{path}"
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                data = json.loads(resp.read())
                self._cache[path] = (now, data)
                return data
        except Exception:
            return None


# ── Theater Dashboard ─────────────────────────────────────────────────

class TheaterDashboard:
    """5-Zone Surgical Theater — live terminal dashboard for fleet operations.

    Zone 1 (NODE RAIL):       Fleet nodes with status, branch, health
    Zone 2 (ACTIVE THEATER):  Current race/task with probe findings
    Zone 3 (EVIDENCE LEDGER): Scrolling evidence stream with hashes
    Zone 4 (TRUTH LADDER):    Inbox -> Vision -> Snapshot -> DAO
    Zone 5 (CHIEF SYNTHESIS): Leading branch, dissent, gains gate
    """

    def __init__(self, daemon_port: int = 8855, refresh_rate: float = 2.0):
        if not HAS_RICH:
            raise ImportError(
                "'rich' package required for TheaterDashboard. Install: pip install rich>=13.0"
            )
        self._port = daemon_port
        self._refresh = refresh_rate
        self._console = Console()
        self._fetcher = TheaterDataFetcher(port=daemon_port)
        self._stop = threading.Event()

    def run(self):
        """Launch the live 5-zone dashboard. Blocks until 'q' or Ctrl-C."""
        self._start_key_listener()
        try:
            with Live(
                self._build_offline_layout(),
                console=self._console,
                refresh_per_second=1,
                screen=True,
            ) as live:
                while not self._stop.is_set():
                    data = self._fetcher.fetch_all()
                    layout = self._build_layout(data)
                    live.update(layout)
                    self._stop.wait(self._refresh)
        except KeyboardInterrupt:
            self._stop.set()
        self._console.print("[dim]Theater closed.[/]")

    def snapshot(self) -> Layout:
        """Single render for non-live use (testing, CLI one-shot)."""
        data = self._fetcher.fetch_all()
        return self._build_layout(data)

    # ── Layout Construction ───────────────────────────────────────────

    def _build_layout(self, data: dict) -> Layout:
        """Assemble the 5-zone layout from fetched data."""
        if not data.get("online"):
            return self._build_offline_layout()

        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="main", ratio=3),
            Layout(name="bottom", ratio=2),
            Layout(name="footer", size=2),
        )

        # Main: node rail (left) + active theater (right)
        layout["main"].split_row(
            Layout(name="nodes", ratio=1, minimum_size=26),
            Layout(name="theater", ratio=2),
        )

        # Bottom: evidence + truth + synthesis
        layout["bottom"].split_row(
            Layout(name="evidence", ratio=1),
            Layout(name="truth", ratio=1),
            Layout(name="synthesis", ratio=1),
        )

        layout["header"].update(self._render_header(data))
        layout["nodes"].update(self._render_nodes(data))
        layout["theater"].update(self._render_theater(data))
        layout["evidence"].update(self._render_evidence(data))
        layout["truth"].update(self._render_truth(data))
        layout["synthesis"].update(self._render_synthesis(data))
        layout["footer"].update(self._render_footer(data))

        return layout

    def _build_offline_layout(self) -> Layout:
        layout = Layout()
        body = Text()
        body.append(THEATER_BANNER, style="bold red")
        body.append("\n\n  Daemon unreachable", style="bold red")
        body.append(f"\n  Polling http://127.0.0.1:{self._port} ...", style="dim")
        body.append("\n\n  Press q to quit", style="dim")
        layout.update(Panel(body, title="[bold red]SURGICAL THEATER -- OFFLINE[/]",
                            border_style="red", expand=True))
        return layout

    # ── Zone Renderers ────────────────────────────────────────────────

    def _render_header(self, data: dict) -> Panel:
        """Fleet banner with timestamp and node identity."""
        health = data.get("health", {})
        node_id = health.get("nodeId", "?")
        branch = health.get("git", {}).get("branch", "?")
        uptime = _ago(health.get("uptime_s", 0))
        now = datetime.now().strftime("%H:%M:%S")

        t = Text.assemble(
            (" SURGICAL THEATER ", "bold white on dark_blue"),
            ("  ", ""),
            (f"{node_id}", "bold cyan"),
            (f" @ {branch}", "dim"),
            (f"  up {uptime}", "green"),
            (f"  {now}", "bold bright_cyan"),
        )
        return Panel(t, border_style="bright_blue", padding=(0, 1))

    def _render_nodes(self, data: dict) -> Panel:
        """Zone 1: Node rail with health scores."""
        nodes = data.get("nodes", [])
        table = Table(
            show_header=True,
            header_style="bold",
            expand=True,
            show_edge=False,
            pad_edge=False,
            padding=(0, 1),
        )
        table.add_column("", width=2)
        table.add_column("Node", style="bold", min_width=6)
        table.add_column("Branch", max_width=20, no_wrap=True)
        table.add_column("S", justify="center", width=3)
        table.add_column("Score", justify="center", width=5)

        for node in nodes:
            nid = node.get("id", "?")
            status = node.get("status", "unknown")
            icon_char, icon_style = STATUS_ICONS.get(status, STATUS_ICONS["unknown"])
            icon = Text(icon_char, style=icon_style)

            node_style = NODE_STYLES.get(nid, ("bold white", "white", "Node"))
            name_text = Text(nid, style=node_style[0])

            branch = node.get("branch", "?")
            if len(branch) > 20:
                branch = ".." + branch[-18:]

            sessions = str(node.get("sessions", 0))
            score = str(node.get("score", "?"))

            table.add_row(icon, name_text, Text(branch, style="dim"), sessions, score)

        return Panel(table, title="[bold bright_blue]NODE RAIL[/]",
                     border_style="bright_blue", padding=(0, 0))

    def _render_theater(self, data: dict) -> Panel:
        """Zone 2: Active race/task with probe findings per node."""
        races = data.get("races", [])
        probes = data.get("probe_results", [])
        supervisor = data.get("supervisor", {})
        tasks = supervisor.get("tasks", [])

        body = Text()

        # Active race
        if races:
            race = races[0]  # most recent
            body.append("ACTIVE RACE", style="bold bright_yellow")
            body.append(f"\n  Task: {race.get('task', '?')}", style="white")
            body.append(f"\n  Status: ", style="dim")
            status = race.get("status", "?")
            st_style = "bold green" if status in ("completed", "done") else (
                "bold yellow" if status == "racing" else "bold red")
            body.append(status.upper(), style=st_style)

            # Participants
            participants = race.get("participants", {})
            if participants:
                body.append("\n", style="")
                for node, info in participants.items():
                    pstatus = info.get("status", "?")
                    ns = NODE_STYLES.get(node, ("white", "white", ""))
                    body.append(f"\n  {node}: ", style=ns[0])
                    body.append(pstatus, style="bold green" if pstatus == "completed" else "yellow")
                    findings = info.get("result", {}) or {}
                    fc = len(findings.get("findings", []))
                    if fc:
                        body.append(f" ({fc} findings)", style="dim")
        elif tasks:
            task = tasks[0]
            phase = task.get("phase", "?")
            body.append("SUPERVISOR", style="bold bright_cyan")
            body.append(f"\n  Phase: {phase}", style="white")
        else:
            body.append("NO ACTIVE RACE", style="dim")
            body.append("\n  Fleet idle — awaiting dispatch", style="dim")

        # Probe findings summary
        if probes:
            body.append("\n\n")
            body.append("PROBE FINDINGS", style="bold bright_blue")
            for p in probes[:6]:
                sev = p.get("severity", "info")
                sev_style = SEVERITY_STYLE.get(sev, "dim")
                body.append(f"\n  ", style="")
                body.append(f"[{sev.upper()}]", style=sev_style)
                body.append(f" {p.get('probe', '?')}: ", style="bold")
                body.append(p.get("message", "")[:50], style="white")
            if len(probes) > 6:
                body.append(f"\n  ... +{len(probes) - 6} more", style="dim")

        return Panel(body, title="[bold bright_yellow]ACTIVE THEATER[/]",
                     border_style="bright_yellow", padding=(1, 1))

    def _render_evidence(self, data: dict) -> Panel:
        """Zone 3: Scrolling evidence stream with hash chain."""
        entries = data.get("evidence", [])

        table = Table(
            show_header=True,
            header_style="bold",
            expand=True,
            show_edge=False,
            pad_edge=False,
            padding=(0, 0),
        )
        table.add_column("#", width=4, justify="right", style="dim")
        table.add_column("Event", min_width=10, style="bold")
        table.add_column("Subject", min_width=12, no_wrap=True)
        table.add_column("Hash", width=10, style="dim cyan")

        if entries:
            for entry in entries[-8:]:  # last 8, scrolling effect
                seq = str(entry.get("seq", "?"))
                evt = entry.get("event_type", "?")[:14]
                subj = entry.get("subject", "")[:20]
                h = entry.get("entry_hash", "")[:8] + ".."
                table.add_row(seq, evt, subj, h)
        else:
            table.add_row("-", "empty", "no evidence yet", "-")

        return Panel(table, title="[bold bright_green]EVIDENCE LEDGER[/]",
                     border_style="bright_green", padding=(0, 0))

    def _render_truth(self, data: dict) -> Panel:
        """Zone 4: Truth ladder Inbox -> Vision -> Snapshot -> DAO."""
        stats = data.get("truth", {})
        layers = [
            ("DAO", stats.get("dao", 0), "bright_green", "\u2605"),     # star
            ("Snapshot", stats.get("snapshot", 0), "yellow", "\u25a0"),  # square
            ("Vision", stats.get("vision", 0), "cyan", "\u25b2"),       # triangle
            ("Inbox", stats.get("inbox", 0), "bright_blue", "\u25cf"),  # circle
        ]

        max_count = max((c for _, c, _, _ in layers), default=1) or 1
        body = Text()
        for name, count, color, icon in layers:
            bar_len = int((count / max_count) * 16) if max_count else 0
            body.append(f" {icon} ", style=f"bold {color}")
            body.append(f"{name:>8}", style=f"bold {color}")
            body.append(" ")
            body.append("\u2588" * bar_len, style=color)
            body.append(f" {count}\n", style="bold")

        if not any(c for _, c, _, _ in layers):
            body = Text("  No truth ladder data\n  Awaiting first entry", style="dim")

        return Panel(body, title="[bold bright_cyan]TRUTH LADDER[/]",
                     border_style="bright_cyan", padding=(0, 0))

    def _render_synthesis(self, data: dict) -> Panel:
        """Zone 5: Chief synthesis — gains gate, leading branch, decisions."""
        gains = data.get("gains_gate", {})
        races = data.get("races", [])

        body = Text()

        # Gains gate status
        if gains and "error" not in gains:
            passed = gains.get("passed", False)
            body.append("GAINS GATE ", style="bold")
            if passed:
                body.append("PASSED", style="bold green")
            else:
                body.append("FAILED", style="bold red")
            checks = gains.get("checks", [])
            p_count = sum(1 for c in checks if c.get("passed"))
            body.append(f"\n  {p_count}/{len(checks)} checks OK", style="dim")

            # Show failed checks
            failed = [c for c in checks if not c.get("passed")]
            for c in failed[:3]:
                body.append(f"\n  X {c.get('name', '?')}", style="red")
        else:
            body.append("GAINS GATE ", style="bold")
            body.append("N/A", style="dim")

        # Leading branch from latest race
        if races:
            race = races[0]
            winner = race.get("winner")
            if winner:
                body.append("\n\n")
                body.append("LEADING BRANCH", style="bold bright_yellow")
                ns = NODE_STYLES.get(winner, ("white", "white", ""))
                body.append(f"\n  Winner: ", style="dim")
                body.append(winner, style=ns[0])
                reason = race.get("reason", "")
                if reason:
                    body.append(f"\n  {reason[:40]}", style="dim")

        if not gains and not races:
            body.append("\n  Awaiting synthesis data", style="dim")

        return Panel(body, title="[bold bright_magenta]CHIEF SYNTHESIS[/]",
                     border_style="bright_magenta", padding=(0, 1))

    def _render_footer(self, data: dict) -> Panel:
        """Status bar with controls."""
        node_count = len(data.get("nodes", []))
        evidence_count = len(data.get("evidence", []))
        t = Text.assemble(
            (f" {node_count} nodes", "cyan"),
            (f"  {evidence_count} evidence entries", "green"),
            ("  |  q=quit  r=refresh", "dim"),
        )
        return Panel(t, border_style="dim", padding=(0, 0))

    # ── Key Listener ──────────────────────────────────────────────────

    def _start_key_listener(self):
        """Background thread for keyboard input (q=quit, r=force refresh)."""
        def _listen():
            try:
                import tty
                import termios
                fd = sys.stdin.fileno()
                old = termios.tcgetattr(fd)
                try:
                    tty.setcbreak(fd)
                    while not self._stop.is_set():
                        ch = sys.stdin.read(1)
                        if ch == "q":
                            self._stop.set()
                        elif ch == "r":
                            self._fetcher._cache.clear()
                except Exception:
                    pass
                finally:
                    termios.tcsetattr(fd, termios.TCSADRAIN, old)
            except Exception:
                pass  # non-tty environment, skip key listener

        t = threading.Thread(target=_listen, daemon=True)
        t.start()


# ── Standalone entry ──────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Surgical Theater Dashboard")
    parser.add_argument("--port", type=int, default=8855, help="Daemon port")
    parser.add_argument("--refresh", type=float, default=2.0, help="Refresh rate (seconds)")
    args = parser.parse_args()

    dashboard = TheaterDashboard(daemon_port=args.port, refresh_rate=args.refresh)
    dashboard.run()


if __name__ == "__main__":
    main()
