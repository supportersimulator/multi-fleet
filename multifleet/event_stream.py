"""Fleet Event Stream — HTTP Server-Sent Events for IDE consumption.

Alternative to WebSocket for extensions that prefer SSE.
Serves /events as text/event-stream and /status as JSON snapshot.
Runs on a configurable port (default 8033) using stdlib only.
"""

import json
import logging
import queue
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional

logger = logging.getLogger("fleet-nerve.event-stream")

# Global registry so handlers can access shared state
_event_queues: list[queue.Queue] = []
_event_queues_lock = threading.Lock()
_aggregator = None  # Set via start_server()

# Ring buffer for recent events (so new SSE clients get history)
_recent_events: list[dict] = []
_MAX_RECENT = 50


def push_event(event: dict) -> None:
    """Push an event to all connected SSE clients and the recent buffer."""
    global _recent_events
    _recent_events.append(event)
    if len(_recent_events) > _MAX_RECENT:
        _recent_events = _recent_events[-_MAX_RECENT:]

    with _event_queues_lock:
        dead: list[queue.Queue] = []
        for q in _event_queues:
            try:
                q.put_nowait(event)
            except queue.Full:
                dead.append(q)
        for q in dead:
            try:
                _event_queues.remove(q)
            except ValueError:
                pass


class EventStreamHandler(BaseHTTPRequestHandler):
    """HTTP request handler for SSE and status endpoints."""

    # Suppress default stderr logging per request
    def log_message(self, format, *args):
        logger.debug(format, *args)

    def do_GET(self):
        if self.path == "/events":
            self._handle_sse()
        elif self.path == "/status":
            self._handle_status()
        elif self.path == "/health":
            self._handle_health()
        else:
            self.send_error(404, "Not Found")

    def _handle_sse(self):
        """Stream events as text/event-stream (Server-Sent Events)."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        q: queue.Queue = queue.Queue(maxsize=100)
        with _event_queues_lock:
            _event_queues.append(q)

        try:
            # Send recent events as replay
            for event in list(_recent_events):
                line = f"event: {event.get('type', 'unknown')}\ndata: {json.dumps(event)}\n\n"
                self.wfile.write(line.encode("utf-8"))
            self.wfile.flush()

            # Stream new events with keepalive
            while True:
                try:
                    event = q.get(timeout=30.0)
                    line = f"event: {event.get('type', 'unknown')}\ndata: {json.dumps(event)}\n\n"
                    self.wfile.write(line.encode("utf-8"))
                    self.wfile.flush()
                except queue.Empty:
                    # Send keepalive comment
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionError, OSError):
            pass
        finally:
            with _event_queues_lock:
                try:
                    _event_queues.remove(q)
                except ValueError:
                    pass

    def _handle_status(self):
        """Return aggregated fleet status as JSON."""
        if _aggregator is not None:
            body = json.dumps(_aggregator.snapshot(), indent=2)
        else:
            body = json.dumps({"error": "aggregator not configured"})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def _handle_health(self):
        """Simple health check."""
        body = json.dumps({"status": "ok", "sse_clients": len(_event_queues)})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))


def start_server(
    host: str = "127.0.0.1",
    port: int = 8033,
    aggregator=None,
    daemon: bool = True,
) -> HTTPServer:
    """Start the SSE event stream server in a background thread.

    Args:
        host: Bind address.
        port: Bind port.
        aggregator: FleetStatusAggregator instance for /status endpoint.
        daemon: If True, server thread is a daemon (dies with main process).

    Returns:
        The HTTPServer instance (call .shutdown() to stop).
    """
    global _aggregator
    _aggregator = aggregator

    server = HTTPServer((host, port), EventStreamHandler)
    thread = threading.Thread(target=server.serve_forever, name="fleet-event-stream")
    thread.daemon = daemon
    thread.start()
    logger.info("Event stream server running on %s:%d", host, port)
    return server
