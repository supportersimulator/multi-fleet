"""Tests for Superset bridge integration."""

import json
from unittest.mock import patch, MagicMock
import pytest

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from multifleet.superset_bridge import SupersetBridge, _load_api_key as _get_api_key

TEST_KEY = "tk_test"


class TestApiKeyResolution:
    """Test API key resolution from env and Keychain."""

    def test_env_var_takes_priority(self):
        with patch.dict("os.environ", {"SUPERSET_API_KEY": "sk_test_123"}):
            assert _get_api_key() == "sk_test_123"

    def test_keychain_fallback(self):
        with patch.dict("os.environ", {}, clear=True):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout="sk_live_from_keychain\n")
                key = _get_api_key()
                assert key == "sk_live_from_keychain"

    def test_no_key_returns_none(self):
        with patch.dict("os.environ", {}, clear=True):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=44, stdout="")  # not found
                assert _get_api_key() is None


class TestBridgeOfflineMode:
    """Test bridge behavior when no API key is available."""

    def test_offline_when_no_key(self):
        bridge = SupersetBridge(api_key=None)
        # Force unavailable
        bridge._available = False
        assert not bridge.available

    def test_offline_methods_return_error(self):
        bridge = SupersetBridge(api_key=None)
        bridge._available = False
        assert "error" in bridge.create_workspace("test")
        assert "error" in bridge.list_workspaces()
        assert "error" in bridge.start_agent("claude", "task-1")
        assert "error" in bridge.list_devices()
        assert "error" in bridge.dispatch_fleet_task("branch", "prompt")


class TestMcpCall:
    """Test the low-level MCP call function."""

    def test_successful_call(self):
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "jsonrpc": "2.0", "id": 1,
            "result": {"id": "ws-123", "branch": "feature-x"}
        }).encode()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            result = _mcp_call("create_workspace", {"branch": "feature-x"}, "sk_test")
            assert result == {"id": "ws-123", "branch": "feature-x"}

    def test_http_error_returns_error_dict(self):
        error = MagicMock()
        error.code = 401
        error.read.return_value = b"Unauthorized"

        with patch("urllib.request.urlopen", side_effect=__import__("urllib.error", fromlist=["HTTPError"]).HTTPError(
            url="", code=401, msg="Unauthorized", hdrs=None, fp=MagicMock(read=lambda: b"Unauthorized")
        )):
            result = _mcp_call("list_workspaces", {}, "bad_key")
            assert "error" in result

    def test_unreachable_returns_error(self):
        with patch("urllib.request.urlopen", side_effect=__import__("urllib.error", fromlist=["URLError"]).URLError("Connection refused")):
            result = _mcp_call("list_devices", {}, "sk_test")
            assert result["error"] == "unreachable"


class TestBridgeWorkspaces:
    """Test workspace management methods."""

    @pytest.fixture
    def bridge(self):
        return SupersetBridge(api_key=TEST_KEY)

    def test_create_workspace(self, bridge):
        with patch("multifleet.superset_bridge._mcp_call") as mock:
            mock.return_value = {"id": "ws-1", "path": "/tmp/wt", "branch": "feat-x"}
            result = bridge.create_workspace("feat-x")
            assert result["branch"] == "feat-x"
            mock.assert_called_once_with("create_workspace", {"branch": "feat-x"}, TEST_KEY)

    def test_create_workspace_from_source(self, bridge):
        with patch("multifleet.superset_bridge._mcp_call") as mock:
            mock.return_value = {"id": "ws-2"}
            bridge.create_workspace("feat-y", source_workspace_id="ws-1")
            mock.assert_called_once_with("create_workspace",
                {"branch": "feat-y", "sourceWorkspaceId": "ws-1"}, TEST_KEY)

    def test_list_workspaces(self, bridge):
        with patch("multifleet.superset_bridge._mcp_call") as mock:
            mock.return_value = [{"id": "ws-1"}, {"id": "ws-2"}]
            result = bridge.list_workspaces()
            assert len(result) == 2

    def test_delete_workspace(self, bridge):
        with patch("multifleet.superset_bridge._mcp_call") as mock:
            mock.return_value = {"deleted": True}
            result = bridge.delete_workspace("ws-1")
            assert result["deleted"]


class TestBridgeAgents:
    """Test agent session management."""

    @pytest.fixture
    def bridge(self):
        return SupersetBridge(api_key=TEST_KEY)

    def test_start_agent_with_task(self, bridge):
        with patch("multifleet.superset_bridge._mcp_call") as mock:
            mock.return_value = {"session_id": "s-1"}
            result = bridge.start_agent("claude", "task-1")
            assert result["session_id"] == "s-1"
            mock.assert_called_once_with("start_agent_session",
                {"app": "claude", "taskId": "task-1"}, TEST_KEY)

    def test_start_agent_with_prompt(self, bridge):
        with patch("multifleet.superset_bridge._mcp_call") as mock:
            mock.return_value = {"session_id": "s-2"}
            bridge.start_agent_with_prompt("codex", "Fix the auth bug")
            mock.assert_called_once_with("start_agent_session_with_prompt",
                {"app": "codex", "prompt": "Fix the auth bug"}, TEST_KEY)

    def test_start_agent_with_pane(self, bridge):
        with patch("multifleet.superset_bridge._mcp_call") as mock:
            mock.return_value = {"session_id": "s-3"}
            bridge.start_agent("gemini", "task-2", pane_id="pane-1")
            call_params = mock.call_args[0][1]
            assert call_params["paneId"] == "pane-1"


class TestBridgeTasks:
    """Test task management."""

    @pytest.fixture
    def bridge(self):
        return SupersetBridge(api_key=TEST_KEY)

    def test_create_task(self, bridge):
        with patch("multifleet.superset_bridge._mcp_call") as mock:
            mock.return_value = {"tasks": [{"id": "t-1"}]}
            result = bridge.create_task("Fix auth", description="Refactor login flow")
            assert "tasks" in result

    def test_create_task_with_labels(self, bridge):
        with patch("multifleet.superset_bridge._mcp_call") as mock:
            mock.return_value = {"tasks": [{"id": "t-2"}]}
            bridge.create_task("Deploy", labels=["urgent", "prod"])
            call_params = mock.call_args[0][1]
            assert call_params["tasks"][0]["labels"] == ["urgent", "prod"]

    def test_list_tasks_with_filter(self, bridge):
        with patch("multifleet.superset_bridge._mcp_call") as mock:
            mock.return_value = []
            bridge.list_tasks(status="active")
            mock.assert_called_once_with("list_tasks", {"status": "active"}, TEST_KEY)


class TestDispatchFleetTask:
    """Test the high-level dispatch integration."""

    @pytest.fixture
    def bridge(self):
        return SupersetBridge(api_key=TEST_KEY)

    def test_full_dispatch(self, bridge):
        with patch("multifleet.superset_bridge._mcp_call") as mock:
            mock.side_effect = [
                {"tasks": [{"id": "t-1"}]},  # create_task
                {"id": "ws-1", "branch": "feat-auth"},  # create_workspace
                {"session_id": "s-1"},  # start_agent
            ]
            result = bridge.dispatch_fleet_task("feat-auth", "Implement auth refactor")
            assert result["branch"] == "feat-auth"
            assert result["agent"] == "claude"
            assert mock.call_count == 3

    def test_dispatch_fails_on_task_error(self, bridge):
        with patch("multifleet.superset_bridge._mcp_call") as mock:
            mock.return_value = {"error": "quota exceeded"}
            result = bridge.dispatch_fleet_task("feat-x", "test")
            assert "error" in result

    def test_dispatch_fails_on_workspace_error(self, bridge):
        with patch("multifleet.superset_bridge._mcp_call") as mock:
            mock.side_effect = [
                {"tasks": [{"id": "t-1"}]},  # task OK
                {"error": "branch exists"},  # workspace fails
            ]
            result = bridge.dispatch_fleet_task("main", "test")
            assert "error" in result
            assert "task" in result  # task still returned


class TestBridgeHealth:
    """Test health check."""

    def test_health_available(self):
        bridge = SupersetBridge(api_key=TEST_KEY)
        with patch("multifleet.superset_bridge._mcp_call") as mock:
            mock.return_value = {"activeWorkspace": "ws-1"}
            health = bridge.health()
            assert health["available"]
            assert health["connected"]
            assert health["api_key_source"] == "keychain"

    def test_health_unavailable(self):
        bridge = SupersetBridge(api_key=None)
        bridge._available = False
        health = bridge.health()
        assert not health["available"]

    def test_health_connected_false_on_error(self):
        bridge = SupersetBridge(api_key="sk_test")
        with patch("multifleet.superset_bridge._mcp_call") as mock:
            mock.return_value = {"error": "timeout"}
            health = bridge.health()
            assert not health["connected"]
