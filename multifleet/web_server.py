"""Fleet Web Server -- serves dashboard + API on configurable port.

Combines: dashboard_html, status_aggregator, event_stream, ide_bridge,
prometheus_export into a single web server. Integrates with mac2's
existing web_dashboard.py HTML generation.

Usage:
    python3 -m multifleet.web_server              # Serve on :8856
    python3 -m multifleet.web_server --port 9090

Routes:
    /               Dashboard HTML (5-zone layout)
    /dashboard      Alias for /
    /api/status     JSON status snapshot
    /api/probes     Latest probe results
    /api/decisions  Recent synthesis decisions
    /api/branches   Branch convergence data
    /metrics        Prometheus text format
    /events         SSE event stream
    /extension/*    IDE bridge endpoints
    /health         Quick health check
"""

from http.server import HTTPServer, BaseHTTPRequestHandler
import json
import logging
import os
import queue
import threading
import time
from typing import Optional

logger = logging.getLogger("fleet-nerve.web-server")

DEFAULT_PORT = 8856
DAEMON_PORT = int(os.environ.get("FLEET_DAEMON_PORT", "8855"))
EVENTBUS_PORT = int(os.environ.get("FLEET_EVENTBUS_PORT", "8031"))

# ── Shared state for SSE ──
_sse_queues: list[queue.Queue] = []
_sse_lock = threading.Lock()

# ── Singleton references (set via start_fleet_server) ──
_aggregator = None
_ide_bridge = None
_arbiter_view = None
_server_instance: Optional[HTTPServer] = None
_server_thread: Optional[threading.Thread] = None


def _get_aggregator():
    """Lazy-init status aggregator."""
    global _aggregator
    if _aggregator is None:
        try:
            from multifleet.status_aggregator import FleetStatusAggregator
            node_id = os.environ.get("MULTIFLEET_NODE_ID", "unknown")
            _aggregator = FleetStatusAggregator(node_id=node_id)
        except Exception as e:
            logger.warning("Could not init aggregator: %s", e)
    return _aggregator


def _get_ide_bridge():
    """Lazy-init IDE bridge."""
    global _ide_bridge
    if _ide_bridge is None:
        try:
            from multifleet.ide_bridge import IDEBridge
            _ide_bridge = IDEBridge(aggregator=_get_aggregator())
        except Exception as e:
            logger.warning("Could not init IDE bridge: %s", e)
    return _ide_bridge


def _get_arbiter_view():
    """Lazy-init arbiter view."""
    global _arbiter_view
    if _arbiter_view is None:
        try:
            from multifleet.arbiter_view import ArbiterView
            from multifleet.synthesis import ChiefSynthesis
            from multifleet.evidence_ledger import EvidenceLedger
            from multifleet.cross_rebuttal import RebuttalManager
            _arbiter_view = ArbiterView(
                synthesis=ChiefSynthesis(),
                evidence_ledger=EvidenceLedger(),
                rebuttal_mgr=RebuttalManager(),
            )
        except Exception as e:
            logger.warning("Could not init arbiter view: %s", e)
    return _arbiter_view


def _fetch_daemon(path: str, timeout: float = 1.5) -> Optional[dict]:
    """Fetch JSON from fleet daemon. Short timeout to avoid blocking."""
    import urllib.request
    try:
        url = f"http://127.0.0.1:{DAEMON_PORT}{path}"
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _push_sse(event: dict) -> None:
    """Push event to all connected SSE clients."""
    with _sse_lock:
        dead = []
        for q in _sse_queues:
            try:
                q.put_nowait(event)
            except queue.Full:
                dead.append(q)
        for q in dead:
            try:
                _sse_queues.remove(q)
            except ValueError:
                pass


class FleetWebHandler(BaseHTTPRequestHandler):
    """HTTP handler for the unified fleet web server."""

    # Class-level config
    daemon_port = DAEMON_PORT
    eventbus_port = EVENTBUS_PORT

    def do_GET(self):
        path = self.path.split("?")[0]  # strip query params

        if path in ("/", "/dashboard"):
            self._serve_dashboard()
        elif path == "/api/status":
            self._serve_json(self._get_status())
        elif path == "/api/probes":
            self._serve_json(self._get_probes())
        elif path == "/api/decisions":
            self._serve_json(self._get_decisions())
        elif path == "/api/branches":
            self._serve_json(self._get_branches())
        elif path == "/api/probe-review":
            self._serve_json(self._get_probe_review())
        elif path == "/api/surgeon-status":
            self._serve_json(self._get_surgeon_status())
        elif path == "/metrics":
            self._serve_prometheus()
        elif path == "/events":
            self._serve_sse()
        elif path == "/arbiter/pending":
            self._serve_arbiter_pending()
        elif path.startswith("/arbiter/"):
            self._serve_arbiter_view(path)
        elif path.startswith("/extension/"):
            self._serve_extension(path)
        elif path == "/health":
            self._serve_json({
                "status": "ok",
                "type": "fleet-web-server",
                "port": self.server.server_address[1],
                "uptime_s": round(time.time() - _server_start_time, 1)
                if _server_start_time else 0,
            })
        else:
            self.send_error(404, f"Not found: {path}")

    def do_POST(self):
        path = self.path.split("?")[0]

        if path.startswith("/arbiter/") and path.count("/") >= 3:
            self._handle_arbiter_action(path)
        else:
            self.send_error(404, f"Not found: {path}")

    def _handle_arbiter_action(self, path: str):
        """Handle POST /arbiter/<task_id>/<action>."""
        parts = path.strip("/").split("/")
        if len(parts) < 3:
            self.send_error(400, "Invalid arbiter path")
            return
        task_id = parts[1]
        action = parts[2]

        # Read POST body
        content_length = int(self.headers.get("Content-Length", 0))
        body = {}
        if content_length > 0:
            raw = self.rfile.read(content_length)
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                body = {}

        arbiter = _get_arbiter_view()
        if arbiter is None:
            self._serve_json({"error": "Arbiter view not available"})
            return

        if action == "approve":
            result = arbiter.approve(task_id)
        elif action == "override":
            chosen = body.get("chosen_branch", "")
            reason = body.get("reason", body.get("feedback", ""))
            if not chosen:
                self._serve_json({"error": "chosen_branch required"})
                return
            result = arbiter.override(task_id, chosen, reason)
        elif action == "request_changes":
            feedback = body.get("feedback", "")
            if not feedback:
                self._serve_json({"error": "feedback required"})
                return
            result = arbiter.request_changes(task_id, feedback)
        elif action == "defer":
            result = arbiter.defer(task_id)
        else:
            self.send_error(400, f"Unknown arbiter action: {action}")
            return

        self._serve_json(result)

    def _serve_arbiter_view(self, path: str):
        """Serve HTML arbiter view for a specific task."""
        parts = path.strip("/").split("/")
        if len(parts) < 2:
            self.send_error(400, "Missing task_id")
            return
        task_id = parts[1]

        arbiter = _get_arbiter_view()
        if arbiter is None:
            self._send(503, b"Arbiter view not available", "text/plain")
            return

        decision = arbiter.present_decision(task_id)
        html_content = arbiter.render_html(decision)
        self._send(200, html_content.encode("utf-8"), "text/html; charset=utf-8")

    def _serve_arbiter_pending(self):
        """Serve list of pending arbiter decisions as JSON."""
        arbiter = _get_arbiter_view()
        if arbiter is None:
            self._serve_json({"pending": [], "count": 0, "error": "arbiter not available"})
            return
        pending = arbiter.list_pending()
        self._serve_json({"pending": pending, "count": len(pending)})

    # ── Route handlers ──

    def _serve_dashboard(self):
        try:
            from multifleet.web_dashboard import build_dashboard_html
            html = build_dashboard_html(self.daemon_port, self.eventbus_port)
        except ImportError:
            html = "<html><body><h1>Fleet Dashboard</h1><p>web_dashboard module not available</p></body></html>"
        self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")

    def _serve_json(self, data):
        body = json.dumps(data, indent=2, default=str).encode("utf-8")
        self._send(200, body, "application/json")

    def _serve_prometheus(self):
        try:
            from multifleet.prometheus_export import generate_prometheus_text
            node_id = os.environ.get("MULTIFLEET_NODE_ID", "unknown")
            text = generate_prometheus_text(
                node_id=node_id,
                daemon_url=f"http://127.0.0.1:{self.daemon_port}",
                include_health=False,  # avoid blocking on daemon timeout
            )
        except Exception:
            text = "# fleet_web_server_up 1\n"
        self._send(200, text.encode("utf-8"),
                   "text/plain; version=0.0.4; charset=utf-8")

    def _serve_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        q: queue.Queue = queue.Queue(maxsize=100)
        with _sse_lock:
            _sse_queues.append(q)
        try:
            while True:
                try:
                    event = q.get(timeout=15)
                    data = json.dumps(event, default=str)
                    self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                    self.wfile.flush()
                except queue.Empty:
                    # Send keepalive comment
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with _sse_lock:
                try:
                    _sse_queues.remove(q)
                except ValueError:
                    pass

    def _serve_extension(self, path: str):
        bridge = _get_ide_bridge()
        sub = path[len("/extension/"):]

        if bridge is None:
            self._serve_json({"error": "IDE bridge not available"})
            return

        if sub == "status-bar":
            self._serve_json(bridge.get_status_bar_data())
        elif sub == "activity":
            self._serve_json(bridge.get_activity_bar_data())
        elif sub == "notifications":
            self._serve_json(bridge.get_pending_notifications())
        else:
            self.send_error(404, f"Unknown extension endpoint: {sub}")

    # ── Data fetchers ──

    def _get_status(self) -> dict:
        # Direct daemon query with short timeout — avoids blocking
        health = _fetch_daemon("/health", timeout=2.0)
        if health:
            return health
        return {"status": "offline", "error": "daemon unreachable"}

    def _get_probes(self) -> dict:
        daemon_data = _fetch_daemon("/health", timeout=2.0)
        if not daemon_data:
            return {"probes": [], "count": 0, "error": "daemon unreachable"}
        probes = daemon_data.get("probes", daemon_data.get("probe_results", []))
        return {"probes": probes, "count": len(probes)}

    def _get_decisions(self) -> dict:
        try:
            from multifleet.synthesis import SynthesisEngine
            engine = SynthesisEngine()
            decisions = engine.get_recent_decisions(limit=20)
            return {"decisions": decisions, "count": len(decisions)}
        except Exception:
            return {"decisions": [], "count": 0, "note": "synthesis module unavailable"}

    def _get_branches(self) -> dict:
        try:
            from multifleet.branch_convergence import BranchConvergence
            bc = BranchConvergence()
            data = bc.analyze()
            return data if isinstance(data, dict) else {"branches": data}
        except Exception as e:
            return {"branches": [], "error": str(e)}

    def _get_probe_review(self) -> dict:
        """Run probe_review and return results as JSON."""
        try:
            from multifleet.probe_review import run_probe_review
            repo_root = os.environ.get("MULTIFLEET_REPO_ROOT", ".")
            result = run_probe_review(
                repo_root=repo_root,
                task_intent="Fleet health check",
                consult_surgeons=False,
            )
            return result
        except Exception as e:
            logger.warning("probe-review endpoint failed: %s", e)
            return {"error": str(e), "probes": {}, "acceptance": {}}

    def _get_surgeon_status(self) -> dict:
        """Return surgeon adapter status."""
        try:
            from multifleet.surgeon_adapter import SurgeonAdapter
            adapter = SurgeonAdapter()
            return {
                "available": adapter.is_available(),
                "state": adapter.get_state(),
                "counters": adapter.get_counters(),
            }
        except ImportError:
            return {"available": False, "error": "surgeon_adapter not importable"}
        except Exception as e:
            logger.warning("surgeon-status endpoint failed: %s", e)
            return {"available": False, "error": str(e)}

    # ── Helpers ──

    def _send(self, status: int, body: bytes, content_type: str):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        """Route to logger instead of stderr."""
        logger.debug("request: %s", format % args)


# ── Server lifecycle ──

_server_start_time: float = 0


def start_fleet_server(port: int = DEFAULT_PORT, daemon_port: int = DAEMON_PORT,
                       eventbus_port: int = EVENTBUS_PORT) -> dict:
    """Start the fleet web server in a background thread.

    Returns dict with status, port, url.
    """
    global _server_instance, _server_thread, _server_start_time

    if _server_instance is not None:
        return {
            "status": "already_running",
            "port": _server_instance.server_address[1],
            "url": f"http://127.0.0.1:{_server_instance.server_address[1]}",
        }

    FleetWebHandler.daemon_port = daemon_port
    FleetWebHandler.eventbus_port = eventbus_port

    try:
        server = HTTPServer(("127.0.0.1", port), FleetWebHandler)
    except OSError as e:
        return {"status": "error", "error": str(e)}

    _server_instance = server
    _server_start_time = time.time()

    def _run():
        logger.info("Fleet web server started on port %d", port)
        server.serve_forever()

    _server_thread = threading.Thread(target=_run, daemon=True, name="fleet-web-server")
    _server_thread.start()

    return {
        "status": "started",
        "port": port,
        "url": f"http://127.0.0.1:{port}",
    }


def stop_fleet_server() -> dict:
    """Stop the fleet web server."""
    global _server_instance, _server_thread

    if _server_instance is None:
        return {"status": "not_running"}

    _server_instance.shutdown()
    _server_instance.server_close()
    _server_instance = None
    _server_thread = None

    return {"status": "stopped"}


def fleet_server_status() -> dict:
    """Get fleet web server status."""
    if _server_instance is None:
        return {"status": "stopped"}
    return {
        "status": "running",
        "port": _server_instance.server_address[1],
        "url": f"http://127.0.0.1:{_server_instance.server_address[1]}",
        "uptime_s": round(time.time() - _server_start_time, 1),
    }


# ── MCP tool function ──

def fleet_server_tool(action: str = "status", port: int = DEFAULT_PORT) -> dict:
    """MCP tool: start/stop/status the fleet web server.

    Args:
        action: "start", "stop", or "status"
        port: Port for the web server (default 8856)
    """
    if action == "start":
        return start_fleet_server(port=port)
    elif action == "stop":
        return stop_fleet_server()
    elif action == "status":
        return fleet_server_status()
    else:
        return {"error": f"Unknown action: {action}. Use start/stop/status."}


if __name__ == "__main__":
    import argparse
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    parser = argparse.ArgumentParser(description="Fleet Web Server")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    result = start_fleet_server(port=args.port)
    print(f"Fleet Web Server: {result}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        stop_fleet_server()
        print("\nStopped.")
