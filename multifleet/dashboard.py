"""Fleet Dashboard — Live terminal UI for multi-fleet monitoring.

mac1's entry in the competitive branch race. Superset-inspired: clean, minimal, intuitive.
Uses Rich Live display with 2-second polling of the fleet nerve daemon.

Usage: python3 -m multifleet.dashboard
"""
import json, sys, threading, urllib.request
from datetime import datetime
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

DAEMON = "http://127.0.0.1:8855"


def _fetch(path: str) -> dict | None:
    try:
        with urllib.request.urlopen(f"{DAEMON}{path}", timeout=3) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _ago(s: int | float) -> str:
    s = int(s)
    if s < 60: return f"{s}s"
    if s < 3600: return f"{s//60}m {s%60}s"
    return f"{s//3600}h {(s%3600)//60}m"


def _dot(last_seen: int | float) -> Text:
    s = int(last_seen)
    style = "bold green" if s < 30 else ("bold yellow" if s < 120 else "bold red")
    return Text("\u25cf", style=style)


def _header(d: dict) -> Panel:
    node, branch = d.get("nodeId", "?"), d.get("git", {}).get("branch", "?")
    commit, up = d.get("git", {}).get("commit", "")[:50], _ago(d.get("uptime_s", 0))
    t = Text.assemble(
        ("Fleet Dashboard", "bold white"), (f" \u2014 {node} (chief)  ", "dim"),
        (datetime.now().strftime("%H:%M:%S"), "bold cyan"), "\n",
        (f" {branch}", "bold magenta"), (f" @ {commit}", "dim"),
        (f"  up {up}", "green"), (f"  sessions: {d.get('activeSessions', 0)}", "cyan"),
    )
    return Panel(t, border_style="bright_blue", padding=(0, 1))


def _peers(d: dict) -> Panel:
    t = Table(expand=True, header_style="bold", show_edge=False, pad_edge=False)
    for col, kw in [("", {"width": 2}), ("Node", {"style": "bold white", "min_width": 10}),
                    ("Seen", {"justify": "right"}), ("Sessions", {"justify": "center"}),
                    ("Transport", {"justify": "center"}), ("VS", {"justify": "center"}),
                    ("Ch", {"justify": "center"})]:
        t.add_column(col, **kw)
    ch = d.get("channel_health", {})
    for name in sorted(d.get("peers", {})):
        p = d["peers"][name]
        ls = p.get("lastSeen", 9999)
        score = ch.get(name, {}).get("score", "?")
        broken = ch.get(name, {}).get("broken", [])
        vs = Text("\u25cf", style="green" if p.get("vscode") == "active" else "dim")
        t.add_row(_dot(ls), name, _ago(ls), str(p.get("sessions", 0)),
                  p.get("transport", "?"), vs, Text(score, style="green" if not broken else "yellow"))
    return Panel(t, title="[bold]Peers[/]", border_style="blue", padding=(0, 1))


def _stats(d: dict) -> Panel:
    s = d.get("stats", {})
    t = Table(show_header=False, expand=True, show_edge=False, pad_edge=False)
    t.add_column("K", style="bold", min_width=10)
    t.add_column("V", justify="right", style="cyan")
    for k, v in [("Sent", s.get("sent", 0)), ("Received", s.get("received", 0)),
                 ("Broadcasts", s.get("broadcasts", 0)), ("Errors", s.get("errors", 0)),
                 ("Dedup Drop", d.get("dedup_dropped", 0)), ("Schema Rej", d.get("schema_rejected", 0))]:
        val = Text(str(v), style="bold red") if k == "Errors" and v > 0 else str(v)
        t.add_row(k, val)
    return Panel(t, title="[bold]Messages[/]", border_style="blue", padding=(0, 1))


def _surgeons(d: dict) -> Panel:
    sg, lines = d.get("surgeons", {}), []
    for name, icon in [("neurologist", "\u2764"), ("cardiologist", "\U0001f52e")]:
        st = sg.get(name, "unknown")
        lines.append(Text.assemble((f" {icon} {name.title()}: ", "bold"),
                                   (st.upper(), f"bold {'green' if st == 'ok' else 'red'}")))
    lines.append(Text.assemble((" \U0001f9e0 Atlas: ", "bold"), ("ACTIVE", "bold green")))
    ss = d.get("superset", {})
    ss_st, ss_s = ss.get("status", "unknown"), ss.get("stats", {})
    lines.append(Text(""))
    lines.append(Text.assemble(
        (" Superset: ", "bold"),
        (ss_st.upper(), f"bold {'green' if ss_st in ('configured','ok') else 'yellow'}"),
        (f"  c:{ss_s.get('calls',0)} ok:{ss_s.get('successes',0)} err:{ss_s.get('errors',0)}", "dim"),
    ))
    return Panel(Text("\n").join(lines), title="[bold]Surgeons & Bridge[/]", border_style="blue", padding=(0, 1))


def _sessions(d: dict) -> Panel:
    t = Table(expand=True, header_style="bold", show_edge=False, pad_edge=False)
    for col, kw in [("Session", {"min_width": 12}), ("Kind", {"justify": "center"}),
                    ("Entry", {"justify": "center"}), ("Idle", {"justify": "right"})]:
        t.add_column(col, **kw)
    for s in d.get("sessions", [])[:6]:
        t.add_row(s.get("sessionId", "?")[:12], s.get("kind", "?"),
                  s.get("entrypoint", "?").replace("claude-", ""),
                  _ago(s["idle_s"]) if "idle_s" in s else "-")
    return Panel(t, title="[bold]Sessions[/]", border_style="blue", padding=(0, 1))


def build_dashboard(data: dict | None) -> Layout:
    layout = Layout()
    if data is None:
        layout.update(Panel(Text("\n  Daemon unreachable\n  Retrying...\n", style="bold red"),
                            title="[bold red]OFFLINE[/]", border_style="red", expand=True))
        return layout
    layout.split_column(Layout(name="hdr", size=4), Layout(name="body"), Layout(name="ftr", size=2))
    layout["hdr"].update(_header(data))
    layout["body"].split_row(Layout(name="left", ratio=3), Layout(name="right", ratio=2))
    layout["left"].split_column(Layout(_peers(data), name="peers"),
                                Layout(_sessions(data), name="sess"))
    layout["right"].split_column(Layout(_stats(data), name="stats", size=10),
                                 Layout(_surgeons(data), name="surg"))
    tr, nurl = data.get("transport", "?"), data.get("natsUrl", "")
    layout["ftr"].update(Panel(
        Text(f" {tr} | {nurl} | q=quit  r=refresh  d=doctor", style="dim"), border_style="dim"))
    return layout


def main():
    console, stop = Console(), threading.Event()

    def _keys():
        import tty, termios
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while not stop.is_set():
                ch = sys.stdin.read(1)
                if ch == "q": stop.set()
                elif ch == "d":
                    try:
                        req = urllib.request.Request(f"{DAEMON}/doctor", method="POST")
                        with urllib.request.urlopen(req, timeout=5) as r:
                            console.print(f"[dim]{r.read().decode()[:200]}[/]")
                    except Exception as e:
                        console.print(f"[red]Doctor: {e}[/]")
        except Exception:
            pass
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    threading.Thread(target=_keys, daemon=True).start()
    try:
        with Live(build_dashboard(None), console=console, refresh_per_second=1, screen=True) as live:
            while not stop.is_set():
                live.update(build_dashboard(_fetch("/health")))
                stop.wait(2)
    except KeyboardInterrupt:
        stop.set()
    console.print("[dim]Dashboard stopped.[/]")


if __name__ == "__main__":
    main()
