#!/usr/bin/env python3
"""
Superset Bridge — integrates multi-fleet transport with Superset local execution.

Multi-fleet is standalone transport (routes packets, doesn't interpret payload).
This bridge is the INTEGRATION SURFACE that connects fleet dispatch to Superset's
workspace and agent session management.

Architecture:
  Fleet dispatch → SupersetBridge → Superset MCP API → worktree + agent session
  Fleet status  ← SupersetBridge ← Superset MCP API ← workspace state

The bridge uses Superset's HTTP MCP endpoint (api.superset.sh) with API key auth.
API key is read from macOS Keychain (fleet-nerve / Superset_contextdna_key).

Usage:
  from multifleet.superset_bridge import SupersetBridge

  bridge = SupersetBridge()
  workspace = bridge.create_workspace("feature-auth-refactor")
  session = bridge.start_agent("claude", workspace["id"], "Implement auth refactor")
  status = bridge.list_workspaces()
"""

import json
import logging
import os
import subprocess
import urllib.request
import urllib.error
from typing import Optional

logger = logging.getLogger("fleet-nerve.superset-bridge")

SUPERSET_MCP_URL = "https://api.superset.sh/api/agent/mcp"
KEYCHAIN_SERVICE = "fleet-nerve"
KEYCHAIN_ACCOUNT = "Superset_contextdna_key"


def _get_api_key() -> Optional[str]:
    """Read Superset API key from env var or macOS Keychain."""
    # 1. Environment variable (fastest)
    key = os.environ.get("SUPERSET_API_KEY")
    if key:
        return key

    # 2. macOS Keychain
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE,
             "-a", KEYCHAIN_ACCOUNT, "-w"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        logger.warning("Failed to read Superset API key from Keychain")

    return None


def _mcp_call(method: str, params: dict, api_key: str, timeout: float = 15.0) -> dict:
    """Make a JSON-RPC call to Superset's MCP endpoint.

    Superset MCP uses JSON-RPC 2.0 over HTTP.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": f"tools/{method}",
        "params": params,
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        SUPERSET_MCP_URL,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            if "error" in body:
                logger.warning("Superset MCP error: %s", body["error"])
                return {"error": body["error"]}
            return body.get("result", body)
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")[:500]
        logger.warning("Superset MCP HTTP %d: %s", e.code, error_body)
        return {"error": f"HTTP {e.code}", "detail": error_body}
    except urllib.error.URLError as e:
        logger.warning("Superset MCP unreachable: %s", e.reason)
        return {"error": "unreachable", "detail": str(e.reason)}
    except Exception as e:
        logger.warning("Superset MCP call failed: %s", e)
        return {"error": str(e)}


class SupersetBridge:
    """Bridge between multi-fleet transport and Superset local execution.

    This is the integration surface — multi-fleet stays generic transport,
    Superset handles local worktree and agent management.
    """

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or _get_api_key()
        if not self.api_key:
            logger.warning("No Superset API key found. Bridge will operate in offline mode.")
        self._available = self.api_key is not None

    @property
    def available(self) -> bool:
        return self._available

    def create_workspace(self, branch_name: str, source_workspace_id: Optional[str] = None) -> dict:
        """Create a Superset workspace (git worktree) for a task.

        Args:
            branch_name: Git branch name for the worktree
            source_workspace_id: Optional — branch from existing workspace

        Returns:
            {"id": "...", "path": "...", "branch": "..."} or {"error": "..."}
        """
        if not self._available:
            return {"error": "Superset bridge not available (no API key)"}

        params = {"branch": branch_name}
        if source_workspace_id:
            params["sourceWorkspaceId"] = source_workspace_id

        result = _mcp_call("create_workspace", params, self.api_key)
        if "error" not in result:
            logger.info("Created Superset workspace: branch=%s", branch_name)
        return result

    def delete_workspace(self, workspace_id: str) -> dict:
        """Delete a Superset workspace."""
        if not self._available:
            return {"error": "Superset bridge not available"}

        return _mcp_call("delete_workspace", {"workspaceId": workspace_id}, self.api_key)

    def list_workspaces(self) -> dict:
        """List all Superset workspaces on this device."""
        if not self._available:
            return {"error": "Superset bridge not available"}

        return _mcp_call("list_workspaces", {}, self.api_key)

    def get_workspace_details(self, workspace_id: str) -> dict:
        """Get workspace details including tabs and panes."""
        if not self._available:
            return {"error": "Superset bridge not available"}

        return _mcp_call("get_workspace_details", {"workspaceId": workspace_id}, self.api_key)

    def start_agent(self, agent_type: str, task_id: str,
                    pane_id: Optional[str] = None) -> dict:
        """Launch an AI agent session in a Superset workspace.

        Args:
            agent_type: "claude", "codex", "gemini", "cursor-agent", etc.
            task_id: Superset task ID to assign the agent
            pane_id: Optional — specific pane to launch in

        Returns:
            Session info or error
        """
        if not self._available:
            return {"error": "Superset bridge not available"}

        params = {"app": agent_type, "taskId": task_id}
        if pane_id:
            params["paneId"] = pane_id

        result = _mcp_call("start_agent_session", params, self.api_key)
        if "error" not in result:
            logger.info("Started %s agent for task %s", agent_type, task_id)
        return result

    def start_agent_with_prompt(self, agent_type: str, prompt: str,
                                pane_id: Optional[str] = None) -> dict:
        """Launch an agent with a direct prompt (no task ID needed)."""
        if not self._available:
            return {"error": "Superset bridge not available"}

        params = {"app": agent_type, "prompt": prompt}
        if pane_id:
            params["paneId"] = pane_id

        result = _mcp_call("start_agent_session_with_prompt", params, self.api_key)
        if "error" not in result:
            logger.info("Started %s agent with prompt", agent_type)
        return result

    def create_task(self, title: str, description: str = "",
                    priority: str = "medium", labels: list = None) -> dict:
        """Create a task in Superset's task management."""
        if not self._available:
            return {"error": "Superset bridge not available"}

        params = {
            "tasks": [{
                "title": title,
                "description": description,
                "priority": priority,
            }]
        }
        if labels:
            params["tasks"][0]["labels"] = labels

        return _mcp_call("create_task", params, self.api_key)

    def list_tasks(self, status: Optional[str] = None) -> dict:
        """List tasks with optional status filter."""
        if not self._available:
            return {"error": "Superset bridge not available"}

        params = {}
        if status:
            params["status"] = status

        return _mcp_call("list_tasks", params, self.api_key)

    def list_devices(self) -> dict:
        """List online devices in the Superset organization.

        This is Superset's device discovery — complementary to
        multi-fleet's peer discovery via NATS/heartbeat.
        """
        if not self._available:
            return {"error": "Superset bridge not available"}

        return _mcp_call("list_devices", {}, self.api_key)

    def get_app_context(self) -> dict:
        """Get current Superset app state (active workspace, etc.)."""
        if not self._available:
            return {"error": "Superset bridge not available"}

        return _mcp_call("get_app_context", {}, self.api_key)

    def dispatch_fleet_task(self, branch_name: str, prompt: str,
                            agent_type: str = "claude") -> dict:
        """High-level: create workspace + task + agent in one call.

        This is the main integration point for fleet dispatch:
        1. Creates a Superset task
        2. Creates a workspace (worktree) for the branch
        3. Launches an agent session with the prompt

        Returns combined result or error at first failure.
        """
        if not self._available:
            return {"error": "Superset bridge not available"}

        # Step 1: Create task
        task = self.create_task(title=branch_name, description=prompt)
        if "error" in task:
            return {"error": f"Task creation failed: {task['error']}"}

        task_id = task.get("id") or (task.get("tasks", [{}])[0].get("id") if "tasks" in task else None)
        if not task_id:
            # Task created but ID not returned — try to proceed anyway
            logger.warning("Task created but no ID returned: %s", task)

        # Step 2: Create workspace
        workspace = self.create_workspace(branch_name)
        if "error" in workspace:
            return {"error": f"Workspace creation failed: {workspace['error']}", "task": task}

        # Step 3: Start agent
        if task_id:
            session = self.start_agent(agent_type, task_id)
        else:
            session = self.start_agent_with_prompt(agent_type, prompt)

        return {
            "task": task,
            "workspace": workspace,
            "session": session,
            "branch": branch_name,
            "agent": agent_type,
        }

    def health(self) -> dict:
        """Check Superset bridge health."""
        status = {
            "available": self._available,
            "api_key_source": "none",
            "mcp_url": SUPERSET_MCP_URL,
        }

        if os.environ.get("SUPERSET_API_KEY"):
            status["api_key_source"] = "env"
        elif self.api_key:
            status["api_key_source"] = "keychain"

        if self._available:
            # Quick connectivity check
            ctx = self.get_app_context()
            status["connected"] = "error" not in ctx
            if "error" in ctx:
                status["error"] = ctx["error"]

        return status
