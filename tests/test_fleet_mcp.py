"""
Tests for fleet_nerve_mcp.py — MCP server tool handlers and JSON-RPC protocol.
"""

import json
import os
import time
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

from tools.fleet_nerve_mcp import (
    handle_request,
    fleet_probe,
    fleet_send,
    fleet_broadcast,
    fleet_status,
    fleet_worker_dispatch,
    TOOLS,
    TOOLS_BY_NAME,
)


class TestMCPProtocol:
    """JSON-RPC protocol handling — initialize, tools/list, tools/call."""

    def test_initialize_returns_protocol_version(self):
        result = handle_request({"method": "initialize", "id": 1})
        assert result["protocolVersion"] == "2024-11-05"
        assert "tools" in result["capabilities"]
        assert result["serverInfo"]["name"] == "fleet-nerve"

    def test_tools_list_returns_all_tools(self):
        result = handle_request({"method": "tools/list", "id": 2})
        tools = result["tools"]
        assert len(tools) >= 18  # Grows as features are added (25 as of fleet_race)
        names = {t["name"] for t in tools}
        # Original 6 + 12 added tools
        assert names >= {
            "fleet_probe", "fleet_send", "fleet_repair",
            "fleet_broadcast", "fleet_worker_dispatch", "fleet_status",
            "fleet_dashboard", "fleet_work", "fleet_work_next",
            "fleet_tasks", "fleet_wake", "fleet_historian",
            "fleet_channels", "fleet_sessions", "fleet_inject",
            "fleet_relay", "fleet_discover", "fleet_send_smart",
        }

    def test_tools_list_has_schemas(self):
        result = handle_request({"method": "tools/list", "id": 3})
        for tool in result["tools"]:
            assert "inputSchema" in tool
            assert tool["inputSchema"]["type"] == "object"

    def test_unknown_tool_returns_error(self):
        result = handle_request({
            "method": "tools/call",
            "id": 4,
            "params": {"name": "nonexistent_tool", "arguments": {}},
        })
        assert result["isError"] is True
        error = json.loads(result["content"][0]["text"])
        assert "Unknown tool" in error["error"]

    def test_unknown_method_returns_error(self):
        result = handle_request({"method": "unknown/method", "id": 5})
        assert "error" in result
        assert result["error"]["code"] == -32601

    def test_notification_initialized_returns_none(self):
        result = handle_request({"method": "notifications/initialized"})
        assert result is None


class TestFleetProbe:
    """fleet_probe tool — health check all nodes."""

    def test_probe_returns_structure(self):
        with patch("tools.fleet_nerve_mcp._load_config") as mock_cfg:
            mock_cfg.return_value = {
                "nodes": {"mac1": {"host": "127.0.0.1", "role": "chief"}},
                "chief": {"nodeId": "mac1"},
                "protocol": {},
                "fleetId": "test",
            }
            with patch("tools.fleet_nerve_mcp._get_node_id", return_value="mac1"):
                with patch("tools.fleet_nerve_mcp._http_probe") as mock_probe:
                    mock_probe.return_value = {"reachable": False, "latency_ms": 0, "error": "refused"}
                    with patch("tools.fleet_nerve_mcp._surgeon_status", return_value={"neurologist": "down"}):
                        result = fleet_probe()

        assert "this_node" in result
        assert "nodes" in result
        assert "timestamp" in result
        assert result["this_node"] == "mac1"

    def test_probe_handles_empty_config(self):
        with patch("tools.fleet_nerve_mcp._load_config", return_value={"nodes": {}, "protocol": {}}):
            with patch("tools.fleet_nerve_mcp._get_node_id", return_value="test"):
                result = fleet_probe()
                assert result["nodes"] == {}


class TestFleetSend:
    """fleet_send tool — message sending via MCP."""

    def test_send_returns_sent_true_on_success(self):
        with patch("tools.fleet_nerve_mcp._run_async") as mock_run:
            mock_run.return_value = {"delivered": True, "channel": "P1_nats"}
            result = fleet_send(target="mac2", type="context", body="hello")
            assert result["sent"] is True
            assert result["target"] == "mac2"

    def test_send_falls_back_to_http(self):
        """When NATS fails, fleet_send falls back to HTTP direct."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"delivered": true, "channel": "P1_nats"}'
        with patch("tools.fleet_nerve_mcp._run_async", side_effect=Exception("NATS down")):
            with patch("urllib.request.urlopen", return_value=mock_resp):
                with patch("multifleet.config.load_peers", return_value=(
                    {"mac2": {"ip": "10.0.0.2", "port": 8855}}, None,
                )):
                    result = fleet_send(target="mac2", body="hello")
                    assert result["sent"] is True
                    assert result["channel"] == "http_fallback"

    def test_send_unknown_peer_returns_error(self):
        """When NATS fails and peer is unknown, returns error."""
        with patch("tools.fleet_nerve_mcp._run_async", side_effect=Exception("NATS down")):
            with patch("multifleet.config.load_peers", return_value=({}, None)):
                result = fleet_send(target="unknown", body="hello")
                assert result["sent"] is False
                assert "Unknown peer" in result.get("error", "")


class TestFleetBroadcast:
    """fleet_broadcast tool — send to all nodes except self."""

    def test_broadcast_skips_self(self):
        with patch("tools.fleet_nerve_mcp._load_config") as mock_cfg:
            mock_cfg.return_value = {
                "nodes": {"mac1": {}, "mac2": {}, "mac3": {}},
                "chief": {}, "protocol": {},
            }
            with patch("tools.fleet_nerve_mcp._get_node_id", return_value="mac1"):
                with patch("tools.fleet_nerve_mcp.fleet_send") as mock_send:
                    mock_send.return_value = {"sent": True}
                    result = fleet_broadcast(body="hello all")

        assert result["broadcast"] is True
        assert result["total_targets"] == 2  # mac2 + mac3 (not mac1)
        assert result["sent_count"] == 2

    def test_broadcast_counts_failures(self):
        with patch("tools.fleet_nerve_mcp._load_config") as mock_cfg:
            mock_cfg.return_value = {
                "nodes": {"mac1": {}, "mac2": {}, "mac3": {}},
                "chief": {}, "protocol": {},
            }
            with patch("tools.fleet_nerve_mcp._get_node_id", return_value="mac1"):
                with patch("tools.fleet_nerve_mcp.fleet_send") as mock_send:
                    mock_send.side_effect = [
                        {"sent": True},   # mac2
                        {"sent": False},   # mac3
                    ]
                    result = fleet_broadcast(body="hello")

        assert result["sent_count"] == 1
        assert result["total_targets"] == 2


class TestFleetWorkerDispatch:
    """fleet_worker_dispatch tool — session-driver integration."""

    def test_dispatch_returns_result(self):
        """Dispatch with mocked _run_async returns the worker result dict."""
        mock_mgr = MagicMock()
        with patch("tools.fleet_nerve_mcp.FleetWorkerManager", mock_mgr, create=True):
            with patch("tools.fleet_nerve_mcp._run_async") as mock_run:
                mock_run.return_value = {
                    "target": "mac2", "response": "tests passed", "duration_s": 45,
                }
                result = fleet_worker_dispatch(target="mac2", task="run tests")
                assert result["target"] == "mac2"
                assert "response" in result or "error" in result

    def test_dispatch_handles_missing_plugin(self):
        with patch("tools.fleet_nerve_mcp._run_async", side_effect=RuntimeError("session-driver not found")):
            result = fleet_worker_dispatch(target="mac2", task="run tests")
            assert "error" in result
            assert "session-driver" in result["error"]

    def test_dispatch_local_alias(self):
        """'local' target maps to the current node."""
        with patch("tools.fleet_remote_worker.FleetWorkerManager") as MockMgr:
            instance = MockMgr.return_value
            instance.node_id = "mac1"
            instance.dispatch = AsyncMock(return_value={
                "target": "mac1", "response": "done", "duration_s": 10,
            })
            with patch("tools.fleet_nerve_mcp._run_async") as mock_run:
                mock_run.return_value = {"target": "mac1", "response": "done", "duration_s": 10}
                result = fleet_worker_dispatch(target="local", task="check health")
                assert result["target"] == "mac1"
