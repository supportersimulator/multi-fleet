"""Tests for multifleet.event_stream — SSE server and status endpoint."""

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from multifleet import event_stream
from multifleet.event_stream import EventStreamHandler, push_event, start_server


# ═══════════════════════════════════════════════════════════════════
# push_event tests
# ═══════════════════════════════════════════════════════════════════


class TestPushEvent:
    def setup_method(self):
        """Reset module-level state between tests."""
        event_stream._recent_events = []
        with event_stream._event_queues_lock:
            event_stream._event_queues.clear()

    def test_push_adds_to_recent(self):
        push_event({"type": "fleet.peer.online", "payload": {}})
        assert len(event_stream._recent_events) == 1
        assert event_stream._recent_events[0]["type"] == "fleet.peer.online"

    def test_recent_buffer_capped(self):
        for i in range(60):
            push_event({"type": f"event.{i}", "payload": {}})
        assert len(event_stream._recent_events) == event_stream._MAX_RECENT

    def test_push_delivers_to_queues(self):
        import queue
        q = queue.Queue(maxsize=10)
        with event_stream._event_queues_lock:
            event_stream._event_queues.append(q)

        push_event({"type": "test.event", "payload": {"x": 1}})

        msg = q.get_nowait()
        assert msg["type"] == "test.event"
        assert msg["payload"]["x"] == 1


# ═══════════════════════════════════════════════════════════════════
# HTTP server integration tests
# ═══════════════════════════════════════════════════════════════════


@pytest.fixture
def server():
    """Start a real event stream server on an ephemeral port, tear down after test."""
    event_stream._recent_events = []
    event_stream._aggregator = None
    with event_stream._event_queues_lock:
        event_stream._event_queues.clear()

    srv = start_server(host="127.0.0.1", port=0, daemon=True)
    port = srv.server_address[1]
    # Give server thread time to start
    time.sleep(0.1)
    yield srv, port
    srv.shutdown()


class TestStatusEndpoint:
    def test_status_returns_json(self, server):
        srv, port = server
        resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=2)
        assert resp.status == 200
        data = json.loads(resp.read().decode("utf-8"))
        # Without aggregator, should return error
        assert "error" in data

    def test_status_with_aggregator(self, server):
        srv, port = server
        mock_agg = MagicMock()
        mock_agg.snapshot.return_value = {"node_id": "mac1", "health_dot": "green"}
        event_stream._aggregator = mock_agg

        resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=2)
        data = json.loads(resp.read().decode("utf-8"))
        assert data["node_id"] == "mac1"
        assert data["health_dot"] == "green"


class TestHealthEndpoint:
    def test_health_returns_ok(self, server):
        srv, port = server
        resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2)
        data = json.loads(resp.read().decode("utf-8"))
        assert data["status"] == "ok"
        assert "sse_clients" in data


class TestSSEEndpoint:
    def test_sse_response_headers(self, server):
        srv, port = server
        # Use a short timeout — SSE is a streaming response, so we need to
        # read just enough to verify headers and then close.
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request("GET", "/events")
        resp = conn.getresponse()
        assert resp.status == 200
        assert "text/event-stream" in resp.getheader("Content-Type", "")
        assert resp.getheader("Cache-Control") == "no-cache"
        conn.close()

    def test_sse_receives_pushed_events(self, server):
        """Verify SSE replay includes events pushed before connection."""
        srv, port = server

        # Push an event first so it appears in replay
        push_event({"type": "fleet.test", "payload": {"val": 42}})

        received_lines = []

        def read_sse():
            import socket as _socket
            s = _socket.create_connection(("127.0.0.1", port), timeout=2)
            s.sendall(b"GET /events HTTP/1.0\r\nHost: localhost\r\n\r\n")
            s.settimeout(2.0)
            try:
                while True:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    received_lines.append(chunk.decode("utf-8", errors="replace"))
            except (TimeoutError, OSError):
                pass
            finally:
                s.close()

        reader = threading.Thread(target=read_sse, daemon=True)
        reader.start()
        reader.join(timeout=5)

        full_text = "".join(received_lines)
        assert "fleet.test" in full_text


class TestNotFoundEndpoint:
    def test_unknown_path_returns_404(self, server):
        srv, port = server
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request("GET", "/unknown")
        resp = conn.getresponse()
        assert resp.status == 404
        conn.close()


class TestGracefulNoEvents:
    def test_empty_recent_buffer(self):
        """push_event works even when no queues are registered."""
        event_stream._recent_events = []
        with event_stream._event_queues_lock:
            event_stream._event_queues.clear()

        # Should not raise
        push_event({"type": "test.noop", "payload": {}})
        assert len(event_stream._recent_events) == 1
