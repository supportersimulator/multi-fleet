"""
Superset MCP Bridge — connects fleet-nerve to Superset's workspace/agent management.

Provides a Python interface to Superset's 18 MCP tools via HTTP+SSE transport.
Fleet nodes use this to create workspaces, launch agents, manage tasks, and
discover devices across the organization.

Auth: Bearer token from Keychain (fleet-nerve / Superset_contextdna_key) or
SUPERSET_API_KEY env var. Degrades gracefully when unavailable.

Transport: SSE (Server-Sent Events) — responses arrive as:
  event: message
  data: {"jsonrpc":"2.0","id":1,"result":{...}}

INVARIANT: Never log API keys. Only log tool name, latency, success/failure.
"""

import json
import logging
import os
import subprocess
import time
import urllib.request
import urllib.error
from typing import Any, Optional

logger = logging.getLogger("fleet-nerve.superset")

SUPERSET_MCP_URL = "https://api.superset.sh/api/agent/mcp"
TIMEOUT_DEFAULT = 15
TIMEOUT_AGENT = 30


def _load_api_key() -> Optional[str]:
    """Load Superset API key from Keychain, then env var.

    Returns None if neither available — bridge degrades gracefully.
    """
    # Try macOS Keychain first
    try:
        result = subprocess.run(
            [
                "security", "find-generic-password",
                "-s", "fleet-nerve",
                "-a", "Superset_contextdna_key",
                "-w",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            logger.debug("Loaded Superset API key from Keychain")
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        logger.debug("Keychain lookup failed: %s", type(e).__name__)

    # Fallback to env var
    key = os.environ.get("SUPERSET_API_KEY")
    if key:
        logger.debug("Loaded Superset API key from env var")
        return key

    logger.warning("No Superset API key found (Keychain + env). Bridge will be unavailable.")
    return None


def _parse_sse_response(raw: str) -> dict:
    """Extract JSON result from SSE response body.

    SSE format:
      event: message
      data: {"jsonrpc":"2.0","id":1,"result":{...}}

    Returns the parsed JSON dict, or an error dict on parse failure.
    """
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("data: "):
            try:
                return json.loads(line[6:])
            except json.JSONDecodeError as e:
                logger.error("SSE JSON parse failed: %s", e)
                return {"error": {"code": -32700, "message": f"JSON parse error: {e}"}}

    # No data: line found — try parsing entire response as JSON
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.error("No parseable SSE data in response (%d chars)", len(raw))
        return {"error": {"code": -32600, "message": "No SSE data in response"}}


class SupersetBridge:
    """Bridge between fleet-nerve and Superset's MCP API.

    Provides typed methods for workspace, agent, task, and discovery operations.
    All methods degrade gracefully — returning empty results with _available=False
    when the API key is missing or Superset is unreachable.
    """

    def __init__(self, api_key: str = None):
        self._api_key = api_key or _load_api_key()
        self._session_id = f"fleet-{os.environ.get('MULTIFLEET_NODE_ID', 'unknown')}"
        self._request_id = 0
        # Observability counters (ZSF)
        self._stats = {
            "calls": 0,
            "successes": 0,
            "errors": 0,
            "timeouts": 0,
        }

    # ── Core Transport ──

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _call_tool(self, tool_name: str, arguments: dict, timeout: int = TIMEOUT_DEFAULT) -> dict:
        """Call a Superset MCP tool via HTTP+SSE. Returns the result dict.

        On failure, returns {"_available": False, "error": "..."} instead of raising.
        """
        if not self._api_key:
            return {"_available": False, "error": "No API key configured"}

        self._stats["calls"] += 1
        request_id = self._next_id()
        t0 = time.monotonic()

        payload = json.dumps({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments,
            },
        }).encode("utf-8")

        req = urllib.request.Request(
            SUPERSET_MCP_URL,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {self._api_key}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
            parsed = _parse_sse_response(raw)
        except urllib.error.HTTPError as e:
            elapsed = time.monotonic() - t0
            self._stats["errors"] += 1
            logger.error("Superset %s HTTP %d (%.1fs)", tool_name, e.code, elapsed)
            return {"_available": False, "error": f"HTTP {e.code}: {e.reason}"}
        except urllib.error.URLError as e:
            elapsed = time.monotonic() - t0
            self._stats["errors"] += 1
            logger.error("Superset %s unreachable (%.1fs): %s", tool_name, elapsed, e.reason)
            return {"_available": False, "error": f"Unreachable: {e.reason}"}
        except TimeoutError:
            self._stats["timeouts"] += 1
            logger.error("Superset %s timed out (%ds)", tool_name, timeout)
            return {"_available": False, "error": f"Timeout after {timeout}s"}
        except Exception as e:
            self._stats["errors"] += 1
            logger.error("Superset %s unexpected error: %s", tool_name, e)
            return {"_available": False, "error": str(e)}

        elapsed = time.monotonic() - t0

        # Check for JSON-RPC error
        if "error" in parsed:
            self._stats["errors"] += 1
            err = parsed["error"]
            msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            logger.error("Superset %s RPC error (%.1fs): %s", tool_name, elapsed, msg)
            return {"_available": False, "error": msg}

        self._stats["successes"] += 1
        logger.info("Superset %s OK (%.1fs)", tool_name, elapsed)
        return parsed.get("result", parsed)

    # ── Workspace Operations ──

    def list_workspaces(self, device_id: str = None) -> list:
        """List all workspaces. If device_id omitted, uses local device."""
        args: dict[str, Any] = {}
        if device_id:
            args["deviceId"] = device_id
        result = self._call_tool("list_workspaces", args)
        if isinstance(result, dict) and result.get("_available") is False:
            return []
        return result if isinstance(result, list) else result.get("workspaces", [result])

    def create_workspace(
        self,
        project_id: str,
        branch: str,
        base_branch: str = "main",
        device_id: str = None,
    ) -> dict:
        """Create a new git worktree workspace."""
        args: dict[str, Any] = {
            "projectId": project_id,
            "branch": branch,
            "baseBranch": base_branch,
        }
        if device_id:
            args["deviceId"] = device_id
        return self._call_tool("create_workspace", args)

    def delete_workspace(self, workspace_id: str, device_id: str = None) -> dict:
        """Delete a workspace/worktree."""
        args: dict[str, Any] = {"workspaceId": workspace_id}
        if device_id:
            args["deviceId"] = device_id
        return self._call_tool("delete_workspace", args)

    def get_workspace_details(self, workspace_id: str, device_id: str = None) -> dict:
        """Get detailed workspace info including tabs and panes."""
        args: dict[str, Any] = {"workspaceId": workspace_id}
        if device_id:
            args["deviceId"] = device_id
        return self._call_tool("get_workspace_details", args)

    def update_workspace(self, workspace_id: str, updates: dict, device_id: str = None) -> dict:
        """Update workspace properties."""
        args: dict[str, Any] = {"workspaceId": workspace_id, **updates}
        if device_id:
            args["deviceId"] = device_id
        return self._call_tool("update_workspace", args)

    def switch_workspace(self, workspace_id: str, device_id: str = None) -> dict:
        """Switch the active workspace on a device."""
        args: dict[str, Any] = {"workspaceId": workspace_id}
        if device_id:
            args["deviceId"] = device_id
        return self._call_tool("switch_workspace", args)

    def get_app_context(self, device_id: str = None) -> dict:
        """Get the current application context (active workspace, project, etc.)."""
        args: dict[str, Any] = {}
        if device_id:
            args["deviceId"] = device_id
        return self._call_tool("get_app_context", args)

    # ── Agent Session Operations ──

    def launch_agent(
        self,
        workspace_id: str,
        task_id: str = None,
        agent: str = "claude",
        device_id: str = None,
    ) -> dict:
        """Launch an agent session in a workspace for a task."""
        args: dict[str, Any] = {
            "workspaceId": workspace_id,
            "agent": agent,
        }
        if task_id:
            args["taskId"] = task_id
        if device_id:
            args["deviceId"] = device_id
        return self._call_tool("start_agent_session", args, timeout=TIMEOUT_AGENT)

    def launch_agent_with_prompt(
        self,
        workspace_id: str,
        prompt: str,
        agent: str = "claude",
        device_id: str = None,
    ) -> dict:
        """Launch an agent session with a direct prompt."""
        args: dict[str, Any] = {
            "workspaceId": workspace_id,
            "prompt": prompt,
            "agent": agent,
        }
        if device_id:
            args["deviceId"] = device_id
        return self._call_tool("start_agent_session_with_prompt", args, timeout=TIMEOUT_AGENT)

    # ── Task Operations ──

    def create_task(self, title: str, description: str = "", priority: str = "medium") -> dict:
        """Create a task in Superset."""
        return self._call_tool("create_task", {
            "tasks": [{
                "title": title,
                "description": description,
                "priority": priority,
            }],
        })

    def update_task(self, task_id: str, updates: dict) -> dict:
        """Update a task's properties (status, title, description, etc.)."""
        return self._call_tool("update_task", {"taskId": task_id, **updates})

    def list_tasks(self, status: str = None) -> list:
        """List tasks, optionally filtered by status."""
        args: dict[str, Any] = {}
        if status:
            args["status"] = status
        result = self._call_tool("list_tasks", args)
        if isinstance(result, dict) and result.get("_available") is False:
            return []
        return result if isinstance(result, list) else result.get("tasks", [result])

    def get_task(self, task_id: str) -> dict:
        """Get a single task by ID."""
        return self._call_tool("get_task", {"taskId": task_id})

    def delete_task(self, task_id: str) -> dict:
        """Delete a task."""
        return self._call_tool("delete_task", {"taskId": task_id})

    def list_task_statuses(self) -> list:
        """List available task status values."""
        result = self._call_tool("list_task_statuses", {})
        if isinstance(result, dict) and result.get("_available") is False:
            return []
        return result if isinstance(result, list) else result.get("statuses", [result])

    # ── Discovery ──

    def list_devices(self) -> list:
        """List online devices in the organization. Key for fleet node discovery."""
        result = self._call_tool("list_devices", {})
        if isinstance(result, dict) and result.get("_available") is False:
            return []
        return result if isinstance(result, list) else result.get("devices", [result])

    def list_members(self) -> list:
        """List organization members."""
        result = self._call_tool("list_members", {})
        if isinstance(result, dict) and result.get("_available") is False:
            return []
        return result if isinstance(result, list) else result.get("members", [result])

    def list_projects(self, device_id: str = None) -> list:
        """List projects on a device."""
        args: dict[str, Any] = {}
        if device_id:
            args["deviceId"] = device_id
        result = self._call_tool("list_projects", args)
        if isinstance(result, dict) and result.get("_available") is False:
            return []
        return result if isinstance(result, list) else result.get("projects", [result])

    # ── Health / Observability ──

    def is_available(self) -> bool:
        """Check if Superset MCP is reachable and authenticated.

        Makes a lightweight list_task_statuses call as a health probe.
        """
        if not self._api_key:
            return False
        result = self._call_tool("list_task_statuses", {}, timeout=5)
        return not (isinstance(result, dict) and result.get("_available") is False)

    @property
    def stats(self) -> dict:
        """Return observability counters."""
        return dict(self._stats)
