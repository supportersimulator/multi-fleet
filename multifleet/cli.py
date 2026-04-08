"""Fleet CLI — unified command-line interface for multi-fleet operations.

Usage:
    python3 -m multifleet.cli status
    python3 -m multifleet.cli race "Fix the auth flow"
    python3 -m multifleet.cli send mac1 "Hello from CLI"
    python3 -m multifleet.cli doctor
    python3 -m multifleet.cli gains-gate
    python3 -m multifleet.cli dashboard
    python3 -m multifleet.cli evidence verify
"""

import argparse
import json
import sys
import time
import urllib.request
import urllib.error

DAEMON = "http://127.0.0.1:8855"


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _get(path: str, timeout: float = 5.0) -> dict:
    """GET from daemon, return parsed JSON or error dict."""
    url = f"{DAEMON}{path}"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.URLError as exc:
        return {"_error": f"Daemon unreachable: {exc.reason}"}
    except TimeoutError:
        return {"_error": "Daemon request timed out"}
    except Exception as exc:
        return {"_error": str(exc)}


def _post(path: str, body: dict, timeout: float = 10.0) -> dict:
    """POST JSON to daemon, return parsed JSON or error dict."""
    url = f"{DAEMON}{path}"
    data = json.dumps(body).encode()
    try:
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.URLError as exc:
        return {"_error": f"Daemon unreachable: {exc.reason}"}
    except TimeoutError:
        return {"_error": "Daemon request timed out"}
    except Exception as exc:
        return {"_error": str(exc)}


def _die(msg: str):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def _check_error(data: dict):
    if "_error" in data:
        _die(data["_error"])


# ---------------------------------------------------------------------------
# Output formatting (rich if available, plain fallback)
# ---------------------------------------------------------------------------

try:
    from rich.console import Console
    from rich.table import Table
    _console = Console()
    _HAS_RICH = True
except ImportError:
    _HAS_RICH = False

try:
    from multifleet.theatrical import FleetTheater
    _theater = FleetTheater()
except ImportError:
    _theater = None


def _print_json(data: dict):
    """Pretty-print JSON data."""
    if _HAS_RICH:
        _console.print_json(json.dumps(data))
    else:
        print(json.dumps(data, indent=2))


def _ok(msg: str):
    if _HAS_RICH:
        _console.print(f"[bold green]OK[/] {msg}")
    else:
        print(f"OK  {msg}")


def _warn(msg: str):
    if _HAS_RICH:
        _console.print(f"[bold yellow]WARN[/] {msg}")
    else:
        print(f"WARN  {msg}")


def _fail(msg: str):
    if _HAS_RICH:
        _console.print(f"[bold red]FAIL[/] {msg}")
    else:
        print(f"FAIL  {msg}")


def _header(msg: str):
    if _HAS_RICH:
        _console.rule(f"[bold]{msg}[/]")
    else:
        print(f"\n=== {msg} ===")


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def cmd_status(_args):
    """Fleet health overview."""
    data = _get("/health")
    _check_error(data)

    # Try theatrical display first
    if _theater:
        node = data.get("nodeId", "?")
        peers = data.get("peers", {})
        nats_ok = data.get("nats_connected", False) or data.get("transport") == "nats"
        nodes_info = [{
            "name": node,
            "status": "ok" if data.get("status") == "ok" else "degraded",
            "nats": nats_ok,
            "peers": len(peers),
            "uptime_s": data.get("uptime_s", 0),
            "sessions": data.get("activeSessions", 0),
        }]
        for pid, info in peers.items():
            nodes_info.append({
                "name": pid,
                "status": "ok" if info.get("lastSeen", 9999) < 60 else "degraded",
                "nats": True,
                "peers": len(peers),
                "uptime_s": "-",
                "sessions": info.get("sessions", 0),
            })
        _theater.fleet_banner()
        _theater.node_status_cards(nodes_info)
        return

    # Fallback to plain output
    _header("Fleet Status")
    node = data.get("nodeId", "?")
    uptime = data.get("uptime_s", 0)
    peers = data.get("peers", {})
    nats = data.get("nats_connected", False)

    _ok(f"Node: {node}  |  Uptime: {int(uptime)}s  |  NATS: {'connected' if nats else 'disconnected'}")
    if peers:
        for pid, info in peers.items():
            age = time.time() - info.get("last_seen", 0) if info.get("last_seen") else -1
            status = f"{age:.0f}s ago" if age >= 0 else "unknown"
            print(f"  Peer {pid}: last seen {status}")
    else:
        print("  No peers detected")


def cmd_peers(_args):
    """List discovered fleet peers."""
    _header("Fleet Peers")
    data = _get("/peers/discovered")
    _check_error(data)
    peers = data.get("peers", {})
    mdns = data.get("mdns_active", False)
    print(f"mDNS active: {mdns}  |  Count: {data.get('count', 0)}")
    for pid, info in peers.items():
        src = info.get("source", "?")
        print(f"  {pid}: source={src}")


def cmd_channels(_args):
    """Show communication channel health."""
    _header("Channel Health")
    data = _get("/channels")
    _check_error(data)
    channels = data.get("channels", {})
    for name, state in channels.items():
        status = state.get("status", "unknown") if isinstance(state, dict) else state
        symbol = "+" if status in ("ok", "connected", True) else "-"
        print(f"  [{symbol}] {name}: {status}")


def cmd_send(args):
    """Send a fleet message to a specific node."""
    result = _post("/message", {
        "type": "context",
        "to": args.target,
        "payload": {"subject": "CLI message", "body": args.message},
    })
    _check_error(result)
    if result.get("delivered"):
        _ok(f"Delivered to {args.target} via {result.get('method', '?')}")
    else:
        _fail(f"Delivery failed: {result.get('error', 'unknown')}")


def cmd_broadcast(args):
    """Broadcast a message to all nodes."""
    result = _post("/message", {
        "type": "context",
        "to": "all",
        "payload": {"subject": "CLI broadcast", "body": args.message},
    })
    _check_error(result)
    if result.get("delivered"):
        _ok(f"Broadcast sent via {result.get('method', '?')}")
    else:
        _fail(f"Broadcast failed: {result.get('error', 'unknown')}")


def cmd_race(args):
    """Start a competitive branch race."""
    nodes = [n.strip() for n in args.nodes.split(",")] if args.nodes else None
    body = {"task": args.task, "timeout_s": args.timeout}
    if nodes:
        body["nodes"] = nodes

    # Theatrical race banner before dispatch
    if _theater:
        display_nodes = nodes or ["mac1", "mac2", "mac3"]
        _theater.race_banner(args.task, display_nodes)

    result = _post("/race", body)
    _check_error(result)
    if "error" in result:
        _fail(result["error"])
    elif _theater:
        _theater.race_scoreboard(result)
    else:
        _ok(f"Race started: {result.get('race_id', '?')}")
        _print_json(result)


def cmd_doctor(_args):
    """Run fleet diagnostics (local, no daemon required)."""
    try:
        from multifleet.doctor import FleetDoctor
        doctor = FleetDoctor()
        report = doctor.run()

        if _theater:
            checks_dicts = [c.to_dict() for c in report.checks]
            _theater.doctor_display(checks_dicts, report.summary())
            return

        _header("Fleet Doctor")
        for check in report.checks:
            sym = {"pass": "+", "warn": "~", "fail": "!", "skip": "-"}.get(check.status, "?")
            line = f"  [{sym}] {check.code} {check.name}: {check.message}"
            if check.status == "fail":
                _fail(line)
            elif check.status == "warn":
                _warn(line)
            else:
                print(line)
        print(f"\nSummary: {report.summary()}")
    except ImportError:
        # Fallback: try daemon endpoint
        data = _get("/doctor", timeout=15)
        _check_error(data)
        _print_json(data)


def cmd_gains_gate(args):
    """Run the fleet gains gate."""
    qs = "?quick=1" if args.quick else ""
    data = _get(f"/gains-gate{qs}", timeout=30)
    _check_error(data)

    if _theater:
        _theater.gains_gate_display(data)
        return

    _header("Gains Gate")
    passed = data.get("passed", False)
    checks = data.get("checks", [])
    for c in checks:
        name = c.get("name", "?")
        ok = c.get("passed", False)
        msg = c.get("message", "")
        if ok:
            _ok(f"{name}: {msg}")
        else:
            _fail(f"{name}: {msg}")
    print()
    if passed:
        _ok("Gains gate PASSED")
    else:
        _fail("Gains gate FAILED")


def cmd_dashboard(args):
    """Launch the fleet dashboard (terminal or web)."""
    # Web dashboard mode
    if getattr(args, "web", False):
        try:
            from multifleet.web_dashboard import serve
            port = getattr(args, "port", 8080) or 8080
            serve(port=port, open_browser=not getattr(args, "no_browser", False))
        except ImportError as exc:
            _die(f"Web dashboard unavailable: {exc}")
        return

    # Terminal dashboard with theatrical rendering
    if _theater:
        try:
            from multifleet.dashboard_data import FleetDashboardData
            dash = FleetDashboardData()
            snap = dash.snapshot()
            _theater.fleet_banner()
            # Node status cards
            if snap.get("nodes"):
                _theater.node_status_cards(snap["nodes"])
            # Probe findings
            if snap.get("probe_results"):
                _theater.probe_findings(snap["probe_results"])
            # Truth ladder
            if snap.get("truth_ladder"):
                _theater.truth_ladder_display(snap["truth_ladder"])
            # Supervisor progress
            sup = snap.get("supervisor", {})
            if sup.get("tasks"):
                for task in sup["tasks"]:
                    _theater.supervisor_progress(task)
            # Gains gate
            if snap.get("gains_gate") and "error" not in snap["gains_gate"]:
                _theater.gains_gate_display(snap["gains_gate"])
            # Evidence chain
            if snap.get("evidence"):
                _theater.evidence_chain(snap["evidence"], verified=True)
            return
        except Exception:
            pass  # fall through to plain dashboard

    # Fallback: fetch plain text dashboard from daemon
    url = f"{DAEMON}/dashboard"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(resp.read().decode())
    except urllib.error.URLError as exc:
        _die(f"Daemon unreachable: {exc.reason}")


def _cmd_dashboard_fallback(_args):
    """Dashboard endpoint returns text, need special handling."""
    url = f"{DAEMON}/dashboard"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(resp.read().decode())
    except urllib.error.URLError as exc:
        _die(f"Daemon unreachable: {exc.reason}")


def cmd_evidence(args):
    """Evidence ledger operations."""
    action = args.action
    if action == "verify":
        data = _get("/evidence/verify")
        _check_error(data)
        if _theater:
            entries = data.get("entries", data.get("chain", []))
            verified = data.get("verified", data.get("valid", False))
            _theater.evidence_chain(entries, verified)
            return
        _header("Evidence Chain Verification")
    elif action == "latest":
        _header("Latest Evidence Entries")
        data = _get("/evidence/latest")
        _check_error(data)
    elif action == "stats":
        _header("Evidence Stats")
        data = _get("/evidence/stats")
        _check_error(data)
    elif action == "query":
        _header("Evidence Query")
        data = _get("/evidence")
        _check_error(data)
    else:
        _die(f"Unknown evidence action: {action}")
        return
    _print_json(data)


def cmd_lease(args):
    """Lease operations (local library)."""
    _header(f"Lease: {args.action}")
    try:
        from multifleet.leases import LeaseManager
        mgr = LeaseManager()
        if args.action == "list":
            leases = mgr.list_leases()
            if not leases:
                print("  No active leases")
            for lid, info in leases.items():
                print(f"  {lid}: holder={info.get('holder', '?')} expires={info.get('expires_at', '?')}")
        elif args.action == "acquire":
            if not args.resource:
                _die("--resource required for acquire")
            result = mgr.acquire(args.resource)
            _ok(f"Acquired: {result}") if result else _fail("Acquire failed")
        elif args.action == "release":
            if not args.resource:
                _die("--resource required for release")
            result = mgr.release(args.resource)
            _ok(f"Released: {result}") if result else _fail("Release failed")
    except ImportError:
        _die("multifleet.leases not available")


def cmd_rebuttal(args):
    """Rebuttal / cross-rebuttal operations (local library)."""
    _header(f"Rebuttal: {args.action}")
    try:
        from multifleet.cross_rebuttal import CrossRebuttalManager
        mgr = CrossRebuttalManager()
        if args.action == "list":
            proposals = mgr.list_proposals()
            if not proposals:
                print("  No active proposals")
            for p in proposals:
                print(f"  {p.get('id', '?')}: {p.get('text', '')[:80]}")
        elif args.action == "propose":
            if not args.text:
                _die("--text required for propose")
            result = mgr.propose(args.text)
            _ok(f"Proposed: {result}")
        elif args.action == "accept":
            if not args.id:
                _die("--id required for accept")
            result = mgr.accept(args.id)
            _ok(f"Accepted: {result}")
        elif args.action == "rebut":
            if not args.id or not args.text:
                _die("--id and --text required for rebut")
            result = mgr.rebut(args.id, args.text)
            _ok(f"Rebutted: {result}")
    except ImportError:
        _die("multifleet.cross_rebuttal not available")


def cmd_truth(args):
    """Truth ladder operations."""
    if args.action == "stats":
        data = _get("/truth/stats")
        _check_error(data)
        if _theater:
            _theater.truth_ladder_display(data)
            return
        _header("Truth Ladder Stats")
        _print_json(data)
    elif args.action == "list":
        data = _get("/truth")
        _check_error(data)
        _header("Truth Ladder Entries")
        _print_json(data)


def cmd_supervisor(args):
    """Supervisor task state."""
    data = _get("/supervisor")
    _check_error(data)
    if args.action == "status" and _theater:
        tasks = data.get("tasks", [])
        if not tasks:
            _ok("No active supervisor tasks")
            return
        for task in tasks:
            _theater.supervisor_progress(task)
        return
    _header("Supervisor Tasks")
    _print_json(data)


def cmd_overlay(args):
    """Node overlay state."""
    if args.node:
        data = _get(f"/overlay/{args.node}")
    else:
        data = _get("/overlays")
    _check_error(data)
    _header("Node Overlays")
    _print_json(data)


def cmd_theater(args):
    """Launch the 5-zone surgical theater dashboard."""
    try:
        from multifleet.theater_dashboard import TheaterDashboard
        dashboard = TheaterDashboard(
            daemon_port=args.port,
            refresh_rate=args.refresh,
        )
        dashboard.run()
    except ImportError as exc:
        _die(f"Theater dashboard unavailable: {exc}")
    except Exception as exc:
        _die(f"Theater dashboard error: {exc}")


def cmd_probe(_args):
    """Run probe scan and display findings."""
    data = _get("/scan/results", timeout=15)
    _check_error(data)
    results = data.get("results", [])
    if _theater:
        _theater.probe_findings(results)
        return
    _header(f"Probe Findings ({len(results)} results)")
    _print_json(data)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="fleet",
        description="Multi-Fleet CLI — one command to rule them all",
    )
    sub = parser.add_subparsers(dest="command")

    # status
    sub.add_parser("status", help="Fleet health overview")

    # peers
    sub.add_parser("peers", help="List discovered fleet peers")

    # channels
    sub.add_parser("channels", help="Show communication channel health")

    # send
    send_p = sub.add_parser("send", help="Send a fleet message")
    send_p.add_argument("target", help="Target node ID (e.g. mac1)")
    send_p.add_argument("message", help="Message body")

    # broadcast
    bc_p = sub.add_parser("broadcast", help="Broadcast to all nodes")
    bc_p.add_argument("message", help="Message body")

    # race
    race_p = sub.add_parser("race", help="Start a competitive branch race")
    race_p.add_argument("task", help="Task description")
    race_p.add_argument("--nodes", help="Comma-separated node IDs")
    race_p.add_argument("--timeout", type=int, default=600, help="Timeout in seconds")

    # doctor
    sub.add_parser("doctor", help="Run fleet diagnostics")

    # gains-gate
    gate_p = sub.add_parser("gains-gate", help="Run gains gate checks")
    gate_p.add_argument("--quick", action="store_true", help="Quick mode (fewer checks)")

    # dashboard
    dash_p = sub.add_parser("dashboard", help="Show fleet dashboard")
    dash_p.add_argument("--web", action="store_true", help="Launch web dashboard in browser")
    dash_p.add_argument("--port", type=int, default=8080, help="Web dashboard port (default 8080)")
    dash_p.add_argument("--no-browser", action="store_true", help="Don't auto-open browser")

    # evidence
    ev_p = sub.add_parser("evidence", help="Evidence ledger operations")
    ev_p.add_argument("action", choices=["verify", "latest", "stats", "query"],
                       help="verify=chain integrity, latest=recent entries, stats=summary, query=all")

    # lease
    lease_p = sub.add_parser("lease", help="Distributed lease operations")
    lease_p.add_argument("action", choices=["list", "acquire", "release"])
    lease_p.add_argument("--resource", help="Resource ID for acquire/release")

    # rebuttal
    reb_p = sub.add_parser("rebuttal", help="Cross-rebuttal operations")
    reb_p.add_argument("action", choices=["list", "propose", "accept", "rebut"])
    reb_p.add_argument("--id", help="Proposal ID")
    reb_p.add_argument("--text", help="Proposal or rebuttal text")

    # truth
    truth_p = sub.add_parser("truth", help="Truth ladder operations")
    truth_p.add_argument("action", choices=["stats", "list"], nargs="?", default="stats",
                         help="stats=ladder summary, list=all entries")

    # supervisor
    sup_p = sub.add_parser("supervisor", help="Supervisor task state")
    sup_p.add_argument("action", choices=["status", "list"], nargs="?", default="status",
                       help="status=phase progress, list=all tasks")

    # overlay
    ov_p = sub.add_parser("overlay", help="Node overlay state")
    ov_p.add_argument("node", nargs="?", help="Specific node ID (omit for all)")

    # probe / scan
    sub.add_parser("probe", help="Run probe scan and show findings")

    # theater — 5-zone surgical theater dashboard
    theater_p = sub.add_parser("theater", help="Launch 5-zone surgical theater dashboard")
    theater_p.add_argument("--port", type=int, default=8855, help="Daemon port")
    theater_p.add_argument("--refresh", type=float, default=2.0, help="Refresh rate (seconds)")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(0)

    # Route to handlers
    handlers = {
        "status": cmd_status,
        "peers": cmd_peers,
        "channels": cmd_channels,
        "send": cmd_send,
        "broadcast": cmd_broadcast,
        "race": cmd_race,
        "doctor": cmd_doctor,
        "gains-gate": cmd_gains_gate,
        "dashboard": cmd_dashboard,
        "evidence": cmd_evidence,
        "lease": cmd_lease,
        "rebuttal": cmd_rebuttal,
        "truth": cmd_truth,
        "supervisor": cmd_supervisor,
        "overlay": cmd_overlay,
        "probe": cmd_probe,
        "theater": cmd_theater,
    }

    handler = handlers.get(args.command)
    if handler:
        handler(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
