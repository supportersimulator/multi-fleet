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
_professor_bridge = None
_server_instance: Optional[HTTPServer] = None
_server_thread: Optional[threading.Thread] = None
_ide_panel_server = None
_panel_bus_bridge = None
_panel_engine_bridge = None


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


def _get_professor_bridge():
    """Lazy-init professor bridge."""
    global _professor_bridge
    if _professor_bridge is None:
        try:
            from multifleet.professor_bridge import get_professor_bridge
            _professor_bridge = get_professor_bridge()
        except Exception as e:
            logger.warning("Could not init professor bridge: %s", e)
    return _professor_bridge


def _get_ide_panel_server():
    """Lazy-init IDE panel server."""
    global _ide_panel_server
    if _ide_panel_server is None:
        try:
            from multifleet.ide_panels import IDEPanelServer
            _ide_panel_server = IDEPanelServer(
                daemon_port=DAEMON_PORT,
                professor_bridge=_get_professor_bridge(),
            )
        except Exception as e:
            logger.warning("Could not init IDE panel server: %s", e)
    return _ide_panel_server


def _get_panel_bus_bridge():
    """Lazy-init panel bus bridge."""
    global _panel_bus_bridge
    if _panel_bus_bridge is None:
        try:
            from multifleet.panel_bus_bridge import PanelBusBridge
            _panel_bus_bridge = PanelBusBridge.get()
        except Exception as e:
            logger.warning("Could not init panel bus bridge: %s", e)
    return _panel_bus_bridge



def _get_panel_engine_bridge():
    """Lazy-init panel engine bridge."""
    global _panel_engine_bridge
    if _panel_engine_bridge is None:
        try:
            from multifleet.panel_engine_bridge import PanelEngineBridge
            _panel_engine_bridge = PanelEngineBridge(
                ide_panel_server=_get_ide_panel_server(),
                panel_bus_bridge=_get_panel_bus_bridge(),
            )
        except Exception as e:
            logger.warning("Could not init panel engine bridge: %s", e)
    return _panel_engine_bridge


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
        elif path.startswith("/api/professor"):
            self._serve_professor(path)
        elif path == "/metrics":
            self._serve_prometheus()
        elif path == "/events":
            self._serve_sse()
        elif path in ("/arbiter", "/arbiter/"):
            self._serve_arbiter()
        elif path == "/arbiter/pending":
            self._serve_json(self._get_arbiter_pending())
        elif path == "/regression":
            self._serve_regression()
        elif path == "/api/regression":
            self._serve_json(self._get_regression())
        elif path.startswith("/api/panels"):
            self._serve_panels(path)
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

    # ── Route handlers ──

    def _serve_arbiter(self):
        try:
            from multifleet.arbiter_view import build_arbiter_html
            html = build_arbiter_html(self.daemon_port)
        except ImportError:
            html = "<html><body><h1>Fleet Arbiter</h1><p>arbiter_view module not available</p></body></html>"
        self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")

    def _get_arbiter_pending(self):
        data = _fetch_daemon("/arbiter/pending")
        return data or {"decisions": [], "count": 0}

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

    def _serve_regression(self):
        """Render the regression dashboard as HTML."""
        try:
            from multifleet.regression_dashboard import RegressionDashboard
            repo_root = os.environ.get("MULTIFLEET_REPO_ROOT", ".")
            dash = RegressionDashboard(repo_path=repo_root)
            html = dash.render_html()
        except Exception as e:
            logger.warning("regression dashboard failed: %s", e)
            html = f"<html><body><h1>Regression Dashboard</h1><p>Error: {e}</p></body></html>"
        self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")

    def _get_regression(self) -> dict:
        """Return regression metrics as JSON."""
        try:
            from multifleet.regression_dashboard import RegressionDashboard
            repo_root = os.environ.get("MULTIFLEET_REPO_ROOT", ".")
            dash = RegressionDashboard(repo_path=repo_root)
            return dash.collect_metrics()
        except Exception as e:
            logger.warning("regression API failed: %s", e)
            return {"error": str(e)}

    def _serve_panels(self, path: str):
        """Route /api/panels/* to IDEPanelServer methods."""
        panel_server = _get_ide_panel_server()
        if panel_server is None:
            self._serve_json({"error": "IDE panel server not available"})
            return

        # Parse query params
        query_str = self.path.split("?", 1)[1] if "?" in self.path else ""
        params = {}
        for part in query_str.split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                params[k] = v

        sub = path[len("/api/panels"):].strip("/")

        if sub == "" or sub == "all":
            self._serve_json(panel_server.all_panels())
        elif sub == "fleet-status":
            self._serve_json(panel_server.fleet_status_panel())
        elif sub == "evidence-timeline":
            hours = float(params.get("hours", "24"))
            self._serve_json(panel_server.evidence_timeline_panel(hours=hours))
        elif sub == "synthesis-decisions":
            limit = int(params.get("limit", "20"))
            self._serve_json(panel_server.synthesis_decisions_panel(limit=limit))
        elif sub == "probe-findings":
            self._serve_json(panel_server.probe_findings_panel())
        elif sub == "wisdom-feedback":
            self._serve_json(panel_server.wisdom_feedback_panel())
        elif sub == "engine-format":
            self._serve_panels_engine_format()
        elif sub == "poll":
            self._serve_panels_poll()
        elif sub == "timestamps":
            self._serve_panels_timestamps()
        else:
            self.send_error(404, f"Unknown panel: {sub}")

    def _serve_panels_poll(self):
        """Return all event-bus-driven panel caches with timestamps.

        Clients poll this endpoint and compare timestamps to local state,
        fetching only panels whose updated_at has changed.
        """
        bridge = _get_panel_bus_bridge()
        if bridge is None:
            self._serve_json({"error": "Panel bus bridge not available"})
            return
        self._serve_json(bridge.get_all_panels())

    def _serve_panels_timestamps(self):
        """Return just panel timestamps for cheap freshness checks."""
        bridge = _get_panel_bus_bridge()
        if bridge is None:
            self._serve_json({"error": "Panel bus bridge not available"})
            return
        self._serve_json(bridge.get_timestamps())

    def _serve_panels_engine_format(self):
        """Return all panels in TS engine-compatible format."""
        bridge = _get_panel_engine_bridge()
        if bridge is None:
            self._serve_json({"error": "Panel engine bridge not available"})
            return
        self._serve_json(bridge.engine_format())

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

    def _serve_professor(self, path: str):
        """Handle /api/professor and /api/professor/panel endpoints."""
        bridge = _get_professor_bridge()
        if bridge is None:
            self._serve_json({"error": "Professor bridge not available"})
            return

        sub = path.split("?")[0]
        if sub == "/api/professor/panel":
            self._serve_json(bridge.get_panel_data())
        elif sub == "/api/professor":
            # Extract topic from query string
            query_string = self.path.split("?", 1)[1] if "?" in self.path else ""
            params = {}
            for pair in query_string.split("&"):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    params[k] = v.replace("+", " ").replace("%20", " ")
            topic = params.get("topic", "")
            if not topic:
                self._serve_json({"error": "Missing 'topic' query parameter"})
                return
            self._serve_json(bridge.query(topic))
        else:
            self.send_error(404, f"Unknown professor endpoint: {sub}")

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
