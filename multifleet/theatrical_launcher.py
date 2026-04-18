"""Theatrical Launcher — singleton manager for auto-launching theatrical dashboard.

Checks if dashboard already serving, launches if not, opens browser tab.
Wires into 3-surgeons EventBus + fleet-nerve NATS to auto-launch on operations.

Usage:
    from multifleet.theatrical_launcher import ensure_theatrical, auto_wire_events

    # Ensure dashboard is running + open browser
    ensure_theatrical()

    # Wire event-based auto-launch (call once per session)
    auto_wire_events()

CLI:
    python3 -m multifleet.theatrical_launcher          # Launch + open browser
    python3 -m multifleet.theatrical_launcher --check   # Check only, no launch
    python3 -m multifleet.theatrical_launcher --wire    # Wire events + launch
"""

import json
import logging
import os
import socket
import subprocess
import sys
import time
import webbrowser
from typing import Optional

logger = logging.getLogger("fleet-nerve.theatrical-launcher")

# Ports to check (web_server default, daemon)
DASHBOARD_PORT = int(os.environ.get("FLEET_DASHBOARD_PORT", "8856"))
DAEMON_PORT = int(os.environ.get("FLEET_DAEMON_PORT", "8855"))

# PID file for tracking launched process
_PID_FILE = "/tmp/theatrical-dashboard.pid"

# Debounce: don't open browser more than once per N seconds
_BROWSER_COOLDOWN_S = 30
_last_browser_open = 0.0

# Events that trigger auto-launch
TRIGGER_EVENTS = [
    "cross_exam.*",
    "fleet.race.*",
    "gains_gate.*",
    "probe.*",
    "sentinel.*",
    "consensus.*",
    "fleet.dispatch.*",
]


def _port_open(port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
    """Check if a port is accepting connections."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (ConnectionRefusedError, TimeoutError, OSError):
        return False


def _check_health(port: int) -> Optional[dict]:
    """Quick health check via HTTP."""
    try:
        import urllib.request
        url = f"http://127.0.0.1:{port}/health"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def _read_pid() -> Optional[int]:
    """Read PID from pidfile, verify process alive."""
    try:
        with open(_PID_FILE) as f:
            pid = int(f.read().strip())
        # Check if process exists
        os.kill(pid, 0)
        return pid
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
        return None


def _write_pid(pid: int):
    """Write PID to pidfile."""
    with open(_PID_FILE, "w") as f:
        f.write(str(pid))


def _clear_pid():
    """Remove pidfile."""
    try:
        os.unlink(_PID_FILE)
    except FileNotFoundError:
        pass


def dashboard_status() -> dict:
    """Check theatrical dashboard status.

    Returns dict with: running, port, pid, url, health.
    """
    # Check if port is open
    for port in [DASHBOARD_PORT]:
        if _port_open(port):
            health = _check_health(port)
            pid = _read_pid()
            return {
                "running": True,
                "port": port,
                "pid": pid,
                "url": f"http://127.0.0.1:{port}",
                "health": health,
            }

    # Check pidfile for zombie
    pid = _read_pid()
    if pid:
        _clear_pid()

    return {"running": False, "port": None, "pid": None, "url": None, "health": None}


def launch_dashboard(port: int = DASHBOARD_PORT, background: bool = True) -> dict:
    """Launch theatrical dashboard web server.

    If already running, returns attach info. Otherwise starts new process.
    """
    status = dashboard_status()
    if status["running"]:
        logger.info("Dashboard already running on port %s", status["port"])
        return {"action": "attached", **status}

    # Launch as background process
    multifleet_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(multifleet_dir)

    cmd = [
        sys.executable, "-m", "multifleet.web_server",
        "--port", str(port),
    ]

    env = os.environ.copy()
    env.setdefault("MULTIFLEET_NODE_ID", _detect_node_id())

    if background:
        proc = subprocess.Popen(
            cmd,
            cwd=project_root,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        _write_pid(proc.pid)

        # Wait briefly for server to start
        for _ in range(10):
            time.sleep(0.3)
            if _port_open(port):
                break

        if _port_open(port):
            logger.info("Dashboard launched on port %d (pid %d)", port, proc.pid)
            return {
                "action": "launched",
                "running": True,
                "port": port,
                "pid": proc.pid,
                "url": f"http://127.0.0.1:{port}",
            }
        else:
            # Try to read stderr for error
            err = ""
            try:
                err = proc.stderr.read(500).decode() if proc.stderr else ""
            except Exception:
                pass
            return {"action": "failed", "running": False, "error": err or "timeout"}
    else:
        # Foreground (for __main__ use)
        from multifleet.web_server import start_fleet_server
        result = start_fleet_server(port=port)
        return {"action": "launched_foreground", **result}


def open_browser(url: Optional[str] = None, force: bool = False) -> bool:
    """Open browser to dashboard, with debounce.

    Returns True if browser was opened, False if debounced.
    """
    global _last_browser_open

    now = time.time()
    if not force and (now - _last_browser_open) < _BROWSER_COOLDOWN_S:
        logger.debug("Browser open debounced (%.0fs remaining)",
                      _BROWSER_COOLDOWN_S - (now - _last_browser_open))
        return False

    if url is None:
        status = dashboard_status()
        if not status["running"]:
            return False
        url = status["url"]

    webbrowser.open(url)
    _last_browser_open = now
    logger.info("Opened browser to %s", url)
    return True


def ensure_theatrical(open_tab: bool = True) -> dict:
    """Ensure theatrical dashboard is running. Open browser if requested.

    This is the main entry point for auto-launch triggers.
    """
    result = launch_dashboard()

    if result.get("running") and open_tab:
        opened = open_browser(result.get("url"))
        result["browser_opened"] = opened

    return result


def _detect_node_id() -> str:
    """Best-effort node ID detection."""
    # Check env first
    node = os.environ.get("MULTIFLEET_NODE_ID")
    if node:
        return node

    # Fall back to hostname (sanitized: strip .local, lowercase)
    import platform
    hostname = platform.node().lower().replace(".local", "")
    return hostname or "unknown"


# ── Event-Based Auto-Launch ────────────────────────────────────────

_events_wired = False


def auto_wire_events():
    """Wire into 3-surgeons EventBus to auto-launch on operations.

    Safe to call multiple times — only wires once.
    """
    global _events_wired
    if _events_wired:
        return

    _wire_eventbus()
    _wire_nats()
    _events_wired = True
    logger.info("Theatrical auto-launch events wired")


def _wire_eventbus():
    """Subscribe to 3-surgeons EventBus events."""
    try:
        from three_surgeons.ide.event_bus import EventBus
        bus = EventBus.instance()

        def _on_surgical_event(event):
            """Auto-launch on surgical operations."""
            logger.debug("Surgical event %s — ensuring theatrical", event.type)
            ensure_theatrical(open_tab=True)

        for pattern in TRIGGER_EVENTS:
            bus.on(pattern, _on_surgical_event)

        logger.info("Wired %d EventBus patterns for theatrical auto-launch",
                     len(TRIGGER_EVENTS))
    except ImportError:
        logger.debug("3-surgeons EventBus not available — skipping EventBus wiring")
    except Exception as e:
        logger.warning("Failed to wire EventBus: %s", e)


def _wire_nats():
    """Subscribe to fleet-nerve NATS subjects for auto-launch."""
    try:
        import asyncio
        import nats

        nats_url = os.environ.get("NATS_URL", "nats://127.0.0.1:4222")

        async def _subscribe():
            try:
                nc = await nats.connect(nats_url, connect_timeout=2)

                async def _handler(msg):
                    logger.debug("NATS trigger: %s", msg.subject)
                    ensure_theatrical(open_tab=True)

                # Subscribe to fleet operation subjects
                await nc.subscribe("fleet.race.>", cb=_handler)
                await nc.subscribe("fleet.dispatch.>", cb=_handler)
                await nc.subscribe("fleet.cross_exam.>", cb=_handler)
                logger.info("NATS theatrical triggers subscribed")
            except Exception as e:
                logger.debug("NATS subscription failed (non-fatal): %s", e)

        # Run in background — don't block
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_subscribe())
        except RuntimeError:
            # No event loop — skip NATS wiring
            logger.debug("No asyncio event loop — skipping NATS wiring")

    except ImportError:
        logger.debug("nats-py not available — skipping NATS wiring")
    except Exception as e:
        logger.warning("Failed to wire NATS: %s", e)


# ── CLI ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="Theatrical Dashboard Launcher")
    parser.add_argument("--check", action="store_true", help="Check status only")
    parser.add_argument("--wire", action="store_true", help="Wire events + launch")
    parser.add_argument("--no-browser", action="store_true", help="Don't open browser")
    parser.add_argument("--port", type=int, default=DASHBOARD_PORT)
    args = parser.parse_args()

    if args.check:
        status = dashboard_status()
        print(json.dumps(status, indent=2))
        sys.exit(0 if status["running"] else 1)

    result = ensure_theatrical(open_tab=not args.no_browser)
    print(json.dumps(result, indent=2, default=str))

    if args.wire:
        auto_wire_events()
        # Keep alive for NATS
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            pass
