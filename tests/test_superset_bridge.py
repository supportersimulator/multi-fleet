"""Tests for Superset MCP bridge integration."""

import json
from unittest.mock import patch, MagicMock
import pytest

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from multifleet.superset_bridge import SupersetBridge, _load_api_key, _parse_sse_response

# Test fixtures
FAKE_KEY = "tk_test"


def _mock_bridge():
    return SupersetBridge(api_key=FAKE_KEY)


class TestApiKeyResolution:
    """Test API key resolution from Keychain and env."""

    def test_keychain_first(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="sk_from_keychain\n")
            key = _load_api_key()
            assert key == "sk_from_keychain"

    def test_env_fallback(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=44, stdout="")
            with patch.dict("os.environ", {"SUPERSET_API_KEY": "sk_from_env"}):
                key = _load_api_key()
                assert key == "sk_from_env"

    def test_no_key_returns_none(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=44, stdout="")
            with patch.dict("os.environ", {}, clear=True):
                assert _load_api_key() is None


class TestSseParser:
    """Test SSE response parsing."""

    def test_parse_sse_data_line(self):
        raw = 'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n\n'
        result = _parse_sse_response(raw)
        assert result["result"]["ok"] is True

    def test_parse_plain_json(self):
        raw = '{"jsonrpc":"2.0","id":1,"result":{"ok":true}}'
        result = _parse_sse_response(raw)
        assert result["result"]["ok"] is True

    def test_parse_error_response(self):
        raw = 'data: {"jsonrpc":"2.0","id":1,"error":{"code":-32600,"message":"bad"}}\n'
        result = _parse_sse_response(raw)
        assert "error" in result

    def test_unparseable_returns_error(self):
        result = _parse_sse_response("not json at all")
        assert "error" in result


class TestBridgeUnavailable:
    """Test graceful degradation when no API key."""

    def test_no_key_means_unavailable(self):
        bridge = SupersetBridge(api_key=None)
        assert not bridge.is_available()

    def test_methods_return_empty(self):
        bridge = SupersetBridge(api_key=None)
        assert bridge.list_workspaces() == []
        assert bridge.list_tasks() == []
        assert bridge.list_devices() == []

    def test_create_returns_error(self):
        bridge = SupersetBridge(api_key=None)
        result = bridge.create_workspace("proj-1", "feat-x")
        assert result.get("_available") is False


class TestCallTool:
    """Test the core _call_tool transport."""

    def test_successful_call(self):
        bridge = _mock_bridge()
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'data: {"jsonrpc":"2.0","id":1,"result":{"id":"ws-1"}}\n'
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = bridge._call_tool("list_workspaces", {})
            assert result == {"id": "ws-1"}
            assert bridge.stats["successes"] == 1

    def test_http_error(self):
        bridge = _mock_bridge()
        from urllib.error import HTTPError
        with patch("urllib.request.urlopen", side_effect=HTTPError("", 401, "Unauth", {}, None)):
            result = bridge._call_tool("list_workspaces", {})
            assert result["_available"] is False
            assert bridge.stats["errors"] == 1

    def test_unreachable(self):
        bridge = _mock_bridge()
        from urllib.error import URLError
        with patch("urllib.request.urlopen", side_effect=URLError("Connection refused")):
            result = bridge._call_tool("list_workspaces", {})
            assert result["_available"] is False
            assert "Unreachable" in result["error"]

    def test_timeout(self):
        bridge = _mock_bridge()
        with patch("urllib.request.urlopen", side_effect=TimeoutError()):
            result = bridge._call_tool("list_workspaces", {}, timeout=5)
            assert result["_available"] is False
            assert bridge.stats["timeouts"] == 1

    def test_rpc_error(self):
        bridge = _mock_bridge()
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'data: {"jsonrpc":"2.0","id":1,"error":{"code":-32600,"message":"bad request"}}\n'
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = bridge._call_tool("create_task", {"title": "x"})
            assert result["_available"] is False
            assert bridge.stats["errors"] == 1


class TestWorkspaces:
    """Test workspace CRUD."""

    def test_create_workspace(self):
        bridge = _mock_bridge()
        with patch.object(bridge, "_call_tool") as mock:
            mock.return_value = {"id": "ws-1", "branch": "feat-x"}
            result = bridge.create_workspace("proj-1", "feat-x")
            assert result["branch"] == "feat-x"
            mock.assert_called_once_with("create_workspace", {
                "projectId": "proj-1", "branch": "feat-x", "baseBranch": "main"
            })

    def test_create_workspace_custom_base(self):
        bridge = _mock_bridge()
        with patch.object(bridge, "_call_tool") as mock:
            mock.return_value = {"id": "ws-2"}
            bridge.create_workspace("proj-1", "feat-y", base_branch="develop")
            args = mock.call_args[0][1]
            assert args["baseBranch"] == "develop"

    def test_delete_workspace(self):
        bridge = _mock_bridge()
        with patch.object(bridge, "_call_tool") as mock:
            mock.return_value = {"deleted": True}
            bridge.delete_workspace("ws-1")
            mock.assert_called_once()

    def test_list_workspaces_returns_list(self):
        bridge = _mock_bridge()
        with patch.object(bridge, "_call_tool") as mock:
            mock.return_value = [{"id": "ws-1"}, {"id": "ws-2"}]
            result = bridge.list_workspaces()
            assert len(result) == 2


class TestAgents:
    """Test agent session management."""

    def test_launch_agent_with_task(self):
        bridge = _mock_bridge()
        with patch.object(bridge, "_call_tool") as mock:
            mock.return_value = {"sessionId": "s-1"}
            result = bridge.launch_agent("ws-1", task_id="t-1")
            assert result["sessionId"] == "s-1"
            args = mock.call_args[0][1]
            assert args["agent"] == "claude"
            assert args["taskId"] == "t-1"

    def test_launch_agent_with_prompt(self):
        bridge = _mock_bridge()
        with patch.object(bridge, "_call_tool") as mock:
            mock.return_value = {"sessionId": "s-2"}
            bridge.launch_agent_with_prompt("ws-1", "Fix the bug", agent="codex")
            args = mock.call_args[0][1]
            assert args["agent"] == "codex"
            assert args["prompt"] == "Fix the bug"

    def test_launch_on_remote_device(self):
        bridge = _mock_bridge()
        with patch.object(bridge, "_call_tool") as mock:
            mock.return_value = {"sessionId": "s-3"}
            bridge.launch_agent("ws-1", device_id="device-mac2")
            args = mock.call_args[0][1]
            assert args["deviceId"] == "device-mac2"


class TestTasks:
    """Test task management."""

    def test_create_task(self):
        bridge = _mock_bridge()
        with patch.object(bridge, "_call_tool") as mock:
            mock.return_value = {"id": "t-1"}
            result = bridge.create_task("Fix auth")
            assert result["id"] == "t-1"

    def test_list_tasks_with_filter(self):
        bridge = _mock_bridge()
        with patch.object(bridge, "_call_tool") as mock:
            mock.return_value = [{"id": "t-1"}]
            result = bridge.list_tasks(status="active")
            assert len(result) == 1
            mock.assert_called_once_with("list_tasks", {"status": "active"})

    def test_update_task(self):
        bridge = _mock_bridge()
        with patch.object(bridge, "_call_tool") as mock:
            mock.return_value = {"id": "t-1", "status": "done"}
            bridge.update_task("t-1", {"status": "done"})
            args = mock.call_args[0][1]
            assert args["taskId"] == "t-1"
            assert args["status"] == "done"


class TestDiscovery:
    """Test device and project discovery."""

    def test_list_devices(self):
        bridge = _mock_bridge()
        with patch.object(bridge, "_call_tool") as mock:
            mock.return_value = [{"id": "d-1", "name": "mac1"}, {"id": "d-2", "name": "mac2"}]
            result = bridge.list_devices()
            assert len(result) == 2

    def test_list_projects(self):
        bridge = _mock_bridge()
        with patch.object(bridge, "_call_tool") as mock:
            mock.return_value = [{"id": "p-1", "name": "superrepo"}]
            result = bridge.list_projects()
            assert result[0]["name"] == "superrepo"


class TestObservability:
    """Test stats and health."""

    def test_stats_increment(self):
        bridge = _mock_bridge()
        assert bridge.stats["calls"] == 0

    def test_is_available_checks_connectivity(self):
        bridge = _mock_bridge()
        with patch.object(bridge, "_call_tool") as mock:
            mock.return_value = ["active", "done"]
            assert bridge.is_available()

    def test_is_available_false_on_error(self):
        bridge = _mock_bridge()
        with patch.object(bridge, "_call_tool") as mock:
            mock.return_value = {"_available": False, "error": "timeout"}
            assert not bridge.is_available()
