"""EventBus WebSocket server — the transport hub for ContextDNA events.

Provides:
- EventBus: in-process pub/sub with wildcard matching and ring buffer
- EventBusServer: WebSocket server (port 8031) for cross-language clients
- SSEHandler: read-only SSE fallback for clients that can't use WebSocket

All events conform to ContextDNA Event v1 schema (see event_types.py).
"""

import asyncio
import fnmatch
import json
import logging
import time
from collections import defaultdict, deque
from http import HTTPStatus
from typing import Callable, Optional

from .event_types import create_event, validate_namespace

logger = logging.getLogger("multifleet.event_bus")

# ── Defaults ──

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8031
DEFAULT_RING_SIZE = 1000
MAX_CLIENTS = 10
HEARTBEAT_INTERVAL_S = 30


# ═══════════════════════════════════════════════════════════════════
# EventBus — in-process pub/sub core
# ═══════════════════════════════════════════════════════════════════

class EventBus:
    """Async-first event bus with wildcard subscriptions and ring buffer.

    Thread-safe for emit() from sync code (schedules into the event loop).
    Handlers are called with the full event dict.
    """

    def __init__(self, ring_size: int = DEFAULT_RING_SIZE):
        self._handlers: dict[str, list[Callable]] = defaultdict(list)
        self._once_handlers: dict[str, list[Callable]] = defaultdict(list)
        self._ring: deque = deque(maxlen=ring_size)
        self._stats = {"emitted": 0, "delivered": 0, "dropped": 0, "errors": 0}

    # ── Subscription API ──

    def on(self, pattern: str, handler: Callable) -> None:
        """Subscribe to events matching pattern (supports * and ? wildcards).

        Pattern examples:
          'injection.*'  — matches injection.completed, injection.failed
          '*'            — matches everything
          'fleet.peer.*' — matches fleet.peer.online, fleet.peer.offline
        """
        self._handlers[pattern].append(handler)

    def once(self, pattern: str, handler: Callable) -> None:
        """Subscribe for a single matching event, then auto-unsubscribe."""
        self._once_handlers[pattern].append(handler)

    def off(self, pattern: str, handler: Callable) -> None:
        """Unsubscribe a handler from a pattern.

        No-op if the handler is not registered for this pattern.
        """
        if pattern in self._handlers:
            try:
                self._handlers[pattern].remove(handler)
            except ValueError:
                pass
            if not self._handlers[pattern]:
                del self._handlers[pattern]
        if pattern in self._once_handlers:
            try:
                self._once_handlers[pattern].remove(handler)
            except ValueError:
                pass
            if not self._once_handlers[pattern]:
                del self._once_handlers[pattern]

    # ── Emit ──

    def emit(
        self,
        event_type: str,
        payload: dict,
        source: str = "python",
        correlation_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> dict:
        """Emit an event. Returns the created event dict.

        Creates a ContextDNA Event v1, appends to ring buffer, dispatches
        to all matching handlers. Handler exceptions are caught per-handler
        and never crash the bus.
        """
        event = create_event(
            event_type=event_type,
            payload=payload,
            source=source,
            correlation_id=correlation_id,
            session_id=session_id,
        )

        # Ring buffer (oldest dropped silently when full)
        if len(self._ring) == self._ring.maxlen:
            self._stats["dropped"] += 1
        self._ring.append(event)
        self._stats["emitted"] += 1

        # Dispatch to persistent handlers
        for pattern, handlers in list(self._handlers.items()):
            if self._match(pattern, event_type):
                for handler in list(handlers):
                    try:
                        result = handler(event)
                        # Support async handlers
                        if asyncio.iscoroutine(result):
                            asyncio.ensure_future(result)
                        self._stats["delivered"] += 1
                    except Exception as exc:
                        self._stats["errors"] += 1
                        logger.warning(
                            "Handler %s failed for %s: %s",
                            getattr(handler, "__name__", "?"),
                            event_type,
                            exc,
                        )

        # Dispatch to once handlers (remove after firing)
        patterns_to_remove = []
        for pattern, handlers in list(self._once_handlers.items()):
            if self._match(pattern, event_type):
                for handler in list(handlers):
                    try:
                        result = handler(event)
                        if asyncio.iscoroutine(result):
                            asyncio.ensure_future(result)
                        self._stats["delivered"] += 1
                    except Exception as exc:
                        self._stats["errors"] += 1
                        logger.warning(
                            "Once-handler %s failed for %s: %s",
                            getattr(handler, "__name__", "?"),
                            event_type,
                            exc,
                        )
                patterns_to_remove.append(pattern)

        for pattern in patterns_to_remove:
            self._once_handlers.pop(pattern, None)

        return event

    # ── Query ──

    def recent(self, limit: int = 50, pattern: Optional[str] = None) -> list:
        """Get recent events from ring buffer, optionally filtered by pattern.

        Returns newest-first (most recent at index 0).
        """
        events = list(self._ring)
        events.reverse()  # newest first

        if pattern:
            events = [e for e in events if self._match(pattern, e["type"])]

        return events[:limit]

    @property
    def stats(self) -> dict:
        """Return a copy of bus statistics."""
        return dict(self._stats)

    # ── Pattern matching ──

    @staticmethod
    def _match(pattern: str, event_type: str) -> bool:
        """Check if pattern matches event type.

        Supports:
          '*'              — matches everything
          'injection.*'    — matches injection.completed (fnmatch)
          'injection.done' — exact match
        """
        if pattern == "*":
            return True
        # Use fnmatch for wildcard support (*, ?)
        return fnmatch.fnmatch(event_type, pattern)


# ═══════════════════════════════════════════════════════════════════
# EventBusServer — WebSocket transport
# ═══════════════════════════════════════════════════════════════════

class EventBusServer:
    """WebSocket server that bridges external clients to the EventBus.

    Clients connect via WebSocket and send JSON messages:
      {"type": "subscribe", "patterns": ["injection.*", "health.*"]}
      {"type": "unsubscribe", "patterns": ["health.*"]}
      {"type": "publish", "event": {"type": "ide.file_changed", "payload": {...}}}

    The server pushes matching events to subscribed clients:
      {"type": "event", "event": {...ContextDNA Event v1...}}
    """

    def __init__(
        self,
        bus: EventBus,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
    ):
        self._bus = bus
        self._host = host
        self._port = port
        self._clients: set = set()
        self._subscriptions: dict = {}  # websocket -> set of patterns
        self._server = None
        self._sse_clients: list = []  # SSE asyncio.Queue per client

    async def handle_client(self, websocket):
        """Handle a WebSocket client connection."""
        if len(self._clients) >= MAX_CLIENTS:
            await websocket.close(1013, "Max clients reached")
            return

        self._clients.add(websocket)
        self._subscriptions[websocket] = set()
        logger.info("WS client connected (%d total)", len(self._clients))

        try:
            async for raw in websocket:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    await websocket.send(json.dumps({
                        "type": "error",
                        "message": "Invalid JSON",
                    }))
                    continue

                msg_type = msg.get("type")

                if msg_type == "subscribe":
                    patterns = msg.get("patterns", [])
                    for p in patterns:
                        self._subscriptions[websocket].add(p)
                    await websocket.send(json.dumps({
                        "type": "subscribed",
                        "patterns": list(self._subscriptions[websocket]),
                    }))

                elif msg_type == "unsubscribe":
                    patterns = msg.get("patterns", [])
                    for p in patterns:
                        self._subscriptions[websocket].discard(p)
                    await websocket.send(json.dumps({
                        "type": "unsubscribed",
                        "patterns": list(self._subscriptions[websocket]),
                    }))

                elif msg_type == "publish":
                    event_data = msg.get("event", {})
                    et = event_data.get("type", "")
                    if not et:
                        await websocket.send(json.dumps({
                            "type": "error",
                            "message": "Missing event.type",
                        }))
                        continue
                    self._bus.emit(
                        event_type=et,
                        payload=event_data.get("payload", {}),
                        source=event_data.get("source", "websocket"),
                        correlation_id=event_data.get("correlation_id"),
                    )

                elif msg_type == "ping":
                    await websocket.send(json.dumps({"type": "pong"}))

                else:
                    await websocket.send(json.dumps({
                        "type": "error",
                        "message": f"Unknown message type: {msg_type}",
                    }))

        except Exception:
            # websockets.ConnectionClosed or other — clean up below
            pass
        finally:
            self._clients.discard(websocket)
            self._subscriptions.pop(websocket, None)
            logger.info("WS client disconnected (%d remaining)", len(self._clients))

    def _on_event(self, event: dict) -> None:
        """Forward event to subscribed WebSocket clients (sync callback).

        Schedules async sends — safe to call from sync EventBus dispatch.
        Also forwards to SSE clients.
        """
        event_type = event.get("type", "")

        # WebSocket clients
        for ws, patterns in list(self._subscriptions.items()):
            for pattern in patterns:
                if EventBus._match(pattern, event_type):
                    msg = json.dumps({"type": "event", "event": event})
                    asyncio.ensure_future(self._safe_send(ws, msg))
                    break  # one match is enough per client

        # SSE clients (all events — SSE is unfiltered)
        for queue in list(self._sse_clients):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass  # drop if SSE client is slow

    async def _safe_send(self, ws, msg: str) -> None:
        """Send to a WebSocket client, catching errors."""
        try:
            await ws.send(msg)
        except Exception:
            # Client probably disconnected — will be cleaned up in handle_client
            pass

    async def start(self) -> None:
        """Start the WebSocket server (blocks forever)."""
        import websockets

        self._bus.on("*", self._on_event)
        logger.info("EventBus WebSocket server starting on %s:%d", self._host, self._port)

        async with websockets.serve(
            self.handle_client,
            self._host,
            self._port,
            process_request=self._http_handler,
        ):
            await asyncio.Future()  # run forever

    async def start_background(self) -> None:
        """Start server as a background task (for embedding in daemon).

        Returns immediately. The server runs in the current event loop.
        """
        import websockets

        self._bus.on("*", self._on_event)
        self._server = await websockets.serve(
            self.handle_client,
            self._host,
            self._port,
            process_request=self._http_handler,
        )
        logger.info("EventBus WebSocket server running on %s:%d (background)", self._host, self._port)

    async def stop(self) -> None:
        """Stop the WebSocket server."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            logger.info("EventBus WebSocket server stopped")

    # ── HTTP handler for SSE and health ──

    async def _http_handler(self, path, request_headers):
        """Handle non-WebSocket HTTP requests (SSE, health).

        Returns (status, headers, body) for HTTP responses,
        or None to proceed with WebSocket upgrade.
        """
        if path == "/health":
            from multifleet.protocol import PROTOCOL_VERSION
            from multifleet.metrics import fleet_metrics
            body = json.dumps({
                "status": "ok",
                "protocol_version": PROTOCOL_VERSION,
                "clients": len(self._clients),
                "sse_clients": len(self._sse_clients),
                "bus_stats": self._bus.stats,
                "metrics": fleet_metrics.snapshot(),
            }).encode()
            return (
                HTTPStatus.OK,
                [("Content-Type", "application/json")],
                body,
            )

        if path == "/events":
            # SSE endpoint — return headers and start streaming
            # websockets doesn't support long-lived SSE easily, so we return
            # recent events as a JSON array for simple polling
            events = self._bus.recent(limit=50)
            body = json.dumps(events).encode()
            return (
                HTTPStatus.OK,
                [("Content-Type", "application/json"),
                 ("Cache-Control", "no-cache")],
                body,
            )

        if path == "/stats":
            body = json.dumps(self._bus.stats).encode()
            return (
                HTTPStatus.OK,
                [("Content-Type", "application/json")],
                body,
            )

        # Proceed with WebSocket upgrade for all other paths
        return None


# ═══════════════════════════════════════════════════════════════════
# SSEHandler — simple SSE transport (standalone, no framework)
# ═══════════════════════════════════════════════════════════════════

class SSEHandler:
    """Simple SSE transport via asyncio HTTP for clients that can't use WebSocket.

    Provides a GET /events endpoint that streams events as text/event-stream.
    Designed to be started alongside the WebSocket server on a different port
    if needed, or used standalone.
    """

    def __init__(self, bus: EventBus, host: str = DEFAULT_HOST, port: int = 8032):
        self._bus = bus
        self._host = host
        self._port = port
        self._clients: list[asyncio.StreamWriter] = []

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle an incoming HTTP connection for SSE."""
        # Read the request line
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
        except (asyncio.TimeoutError, ConnectionError):
            writer.close()
            return

        request_str = request_line.decode("utf-8", errors="replace")

        if "GET /events" in request_str:
            await self._stream_events(writer)
        elif "GET /health" in request_str:
            from multifleet.protocol import PROTOCOL_VERSION as _PV
            from multifleet.metrics import fleet_metrics as _fm
            body = json.dumps({"status": "ok", "protocol_version": _PV, "sse_clients": len(self._clients), "metrics": _fm.snapshot()}).encode()
            response = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                b"\r\n" + body
            )
            writer.write(response)
            await writer.drain()
            writer.close()
        else:
            writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()

    async def _stream_events(self, writer: asyncio.StreamWriter) -> None:
        """Stream SSE events to a connected client."""
        # Send SSE headers
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/event-stream\r\n"
            b"Cache-Control: no-cache\r\n"
            b"Connection: keep-alive\r\n"
            b"\r\n"
        )
        await writer.drain()

        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._clients.append(writer)

        # Register a handler that puts events on the queue
        def on_event(event: dict):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

        self._bus.on("*", on_event)

        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_INTERVAL_S)
                    data = json.dumps(event)
                    writer.write(f"event: {event.get('type', 'unknown')}\ndata: {data}\n\n".encode())
                    await writer.drain()
                except asyncio.TimeoutError:
                    # Send keep-alive comment
                    writer.write(b": keepalive\n\n")
                    await writer.drain()
        except (ConnectionError, BrokenPipeError, OSError):
            pass
        finally:
            self._bus.off("*", on_event)
            if writer in self._clients:
                self._clients.remove(writer)
            try:
                writer.close()
            except Exception:
                pass

    async def start(self) -> None:
        """Start the SSE HTTP server (blocks forever)."""
        server = await asyncio.start_server(
            self._handle_connection, self._host, self._port
        )
        logger.info("SSE server running on %s:%d", self._host, self._port)
        async with server:
            await server.serve_forever()

    async def start_background(self) -> asyncio.Server:
        """Start SSE server as background task."""
        server = await asyncio.start_server(
            self._handle_connection, self._host, self._port
        )
        logger.info("SSE server running on %s:%d (background)", self._host, self._port)
        return server
