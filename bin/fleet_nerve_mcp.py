#!/usr/bin/env python3
"""
Fleet Nerve MCP Server — native Claude Code tool calls for fleet operations.

Exposes fleet operations as MCP tools with full NATS integration,
7-priority fallback messaging, repair escalation, and worker dispatch.

Tools:
  fleet_probe            — health check all fleet nodes
  fleet_send             — send message via 7-priority fallback chain
  fleet_repair           — trigger repair escalation on a target node
  fleet_broadcast        — send to all nodes
  fleet_worker_dispatch  — launch session-driver worker, send task, return result
  fleet_status           — fleet-wide dashboard data

Usage:
  python3 tools/fleet_nerve_mcp.py
"""

import asyncio
import json
import os
import socket
import sys
import time
import traceback
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

CONFIG_PATH = REPO_ROOT / ".multifleet" / "config.json"


# ── Config ──

def _load_config() -> dict:
    """Load fleet config from .multifleet/config.json."""
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    return {"nodes": {}, "chief": {}, "protocol": {}}


def _get_node_id() -> str:
    """Detect this node's ID from env or hostname."""
    env = os.environ.get("MULTIFLEET_NODE_ID")
    if env:
        return env
    try:
        from tools.fleet_nerve_config import detect_node_id
        return detect_node_id()
    except Exception:
        return socket.gethostname().split(".")[0].lower()


def _http_probe(url: str, timeout: int = 3) -> dict:
    """Probe a URL, return {"reachable": bool, "latency_ms": N, "error": ...}."""
    start = time.time()
    try:
        resp = urllib.request.urlopen(url, timeout=timeout)
        data = json.loads(resp.read())
        return {"reachable": True, "latency_ms": int((time.time() - start) * 1000), "data": data}
    except Exception as e:
        return {"reachable": False, "latency_ms": int((time.time() - start) * 1000), "error": str(e)}


def _surgeon_status() -> dict:
    """Check surgeon availability on this node."""
    try:
        from tools.multifleet_coordinator import surgeon_status
        return surgeon_status()
    except Exception as e:
        return {"error": str(e)}


def _get_nats_instance():
    """Get or create a FleetNerveNATS instance for async operations."""
    from tools.fleet_nerve_nats import FleetNerveNATS
    return FleetNerveNATS(node_id=_get_node_id())


def _run_async(coro):
    """Run an async coroutine from sync context."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, coro)
                return future.result(timeout=60)
        return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


# ── Tool Handlers ──

def fleet_probe() -> dict:
    """Health check all fleet nodes — returns per-node status, sessions, peers, surgeon availability.

    Probes each node's HTTP daemon and collects:
    - reachable: whether the daemon responded
    - latency_ms: round-trip time
    - sessions: active Claude sessions on the node
    - surgeons: cardiologist/neurologist availability
    - peers: which peers the node sees
    """
    config = _load_config()
    nodes = config.get("nodes", {})
    node_id = _get_node_id()
    results = {}

    for name, node_cfg in nodes.items():
        host = node_cfg.get("host", "")
        port = config.get("protocol", {}).get("tunnelReversePortBase", 8855)
        # Use localhost for self
        probe_host = "127.0.0.1" if name == node_id else host

        if not probe_host:
            results[name] = {"reachable": False, "error": "no host configured"}
            continue

        # Probe fleet daemon
        probe = _http_probe(f"http://{probe_host}:8855/fleet/live", timeout=3)
        entry = {
            "reachable": probe["reachable"],
            "latency_ms": probe.get("latency_ms", 0),
            "role": node_cfg.get("role", "unknown"),
            "host": host,
        }

        if probe["reachable"] and "data" in probe:
            live = probe["data"]
            entry["sessions"] = live.get("sessions", [])
            entry["peers"] = live.get("peers", {})
            entry["uptime_s"] = live.get("uptime_s", 0)
            entry["stats"] = live.get("stats", {})
        elif not probe["reachable"]:
            entry["error"] = probe.get("error", "unreachable")

        # Surgeon status (only for local node)
        if name == node_id:
            entry["surgeons"] = _surgeon_status()

        results[name] = entry

    return {
        "this_node": node_id,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "nodes": results,
        "fleet_id": config.get("fleetId", "unknown"),
    }


def fleet_send(target: str, type: str = "context", subject: str = "", body: str = "",
               use_cloud: bool = False) -> dict:
    """Send a message via 8-priority fallback chain (P0 Cloud > NATS > HTTP > Chief > Seed > SSH > WoL > Git).

    P0 Cloud is optional — only used when use_cloud=True or all local channels fail.

    Args:
        target: destination node ID (e.g. "mac2")
        type: message type — context, task, repair, heartbeat, etc.
        subject: short subject line
        body: message body
        use_cloud: if True, try P0 cloud channel first (before local channels)
    """
    import uuid

    node_id = _get_node_id()
    message = {
        "id": str(uuid.uuid4()),
        "type": type,
        "from": node_id,
        "to": target,
        "payload": {"subject": subject or body[:60], "body": body},
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    async def _do_send():
        nerve = _get_nats_instance()
        try:
            await nerve.connect()
            result = await nerve.send_with_fallback(target, message, use_cloud=use_cloud)
            return result
        finally:
            if nerve.nc and nerve.nc.is_connected:
                await nerve.nc.close()

    try:
        result = _run_async(_do_send())
        return {"sent": True, "target": target, "type": type, "result": result}
    except Exception as e:
        # Fallback: try direct HTTP if NATS unavailable
        try:
            from tools.fleet_nerve_config import load_peers
            peers, _ = load_peers()
            peer = peers.get(target)
            if not peer:
                return {"sent": False, "error": f"Unknown peer: {target}", "nats_error": str(e)}
            req_body = json.dumps(message).encode()
            req = urllib.request.Request(
                f"http://{peer['ip']}:{peer.get('port', 8855)}/message",
                data=req_body, headers={"Content-Type": "application/json"})
            resp = json.loads(urllib.request.urlopen(req, timeout=5).read())
            return {"sent": True, "target": target, "type": type, "channel": "http_fallback", "result": resp}
        except Exception as e2:
            return {"sent": False, "error": str(e), "http_fallback_error": str(e2)}


def fleet_repair(target: str, action: str = "restart_nats", broken_channels: str = "") -> dict:
    """Trigger repair escalation on a target node (4-level: notify > guide > assist > remote).

    Args:
        target: node to repair (e.g. "mac2")
        action: repair action — restart_nats, restart_daemon, full_restart
        broken_channels: comma-separated list of broken channels (e.g. "P1_nats,P2_http")
    """
    channels = [c.strip() for c in broken_channels.split(",") if c.strip()] if broken_channels else []

    async def _do_repair():
        nerve = _get_nats_instance()
        try:
            await nerve.connect()
            result = await nerve.repair_with_escalation(target, channels, action=action)
            return result
        finally:
            if nerve.nc and nerve.nc.is_connected:
                await nerve.nc.close()

    try:
        result = _run_async(_do_repair())
        return {"repair_initiated": True, "target": target, "action": action, "broken_channels": channels, "result": result}
    except Exception as e:
        return {"repair_initiated": False, "error": str(e), "target": target, "traceback": traceback.format_exc()}


def fleet_broadcast(type: str = "context", subject: str = "", body: str = "") -> dict:
    """Send a message to all fleet nodes via 7-priority fallback.

    Args:
        type: message type — context, announcement, task, etc.
        subject: short subject line
        body: message body
    """
    config = _load_config()
    nodes = config.get("nodes", {})
    node_id = _get_node_id()
    results = {}

    for name in nodes:
        if name == node_id:
            continue
        results[name] = fleet_send(target=name, type=type, subject=subject, body=body)

    sent_count = sum(1 for r in results.values() if r.get("sent"))
    return {
        "broadcast": True,
        "from": node_id,
        "type": type,
        "sent_count": sent_count,
        "total_targets": len(results),
        "results": results,
    }


def fleet_worker_dispatch(target: str, task: str, timeout: str = "300") -> dict:
    """Launch a session-driver worker on target node, send task, return result.

    Starts a detached Claude session on the target machine, sends the task
    prompt, waits for completion, and returns the response. Worker is stopped
    after task completes.

    Args:
        target: node to run worker on (e.g. "mac2", or "local" for this node)
        task: the prompt/task to send to the worker
        timeout: max seconds to wait for worker response (default 300)
    """
    timeout_int = int(timeout)

    try:
        from tools.fleet_remote_worker import FleetWorkerManager
        mgr = FleetWorkerManager()
        actual_target = target if target != "local" else mgr.node_id
        result = _run_async(mgr.dispatch(actual_target, task, timeout=timeout_int))
        return result
    except RuntimeError as e:
        if "session-driver" in str(e):
            return {"error": "claude-session-driver plugin not installed on this node", "target": target}
        return {"error": str(e), "target": target, "traceback": traceback.format_exc()}
    except Exception as e:
        return {"error": str(e), "target": target, "traceback": traceback.format_exc()}


def fleet_status() -> dict:
    """Fleet-wide dashboard data — all peers, sessions, git state, surgeon status.

    Returns comprehensive fleet state including:
    - All nodes with roles and connectivity
    - Active sessions per node
    - Surgeon (cardiologist/neurologist) availability
    - Git branch state
    - Protocol configuration
    """
    config = _load_config()
    nodes = config.get("nodes", {})
    node_id = _get_node_id()
    chief = config.get("chief", {})

    # Probe all nodes
    probe_data = fleet_probe()

    # Git state on local node
    git_state = {}
    try:
        import subprocess
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=5
        )
        short_sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=5
        )
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=5
        )
        git_state = {
            "branch": branch.stdout.strip(),
            "sha": short_sha.stdout.strip(),
            "dirty_files": len([l for l in status.stdout.strip().split("\n") if l.strip()]),
        }
    except Exception as e:
        git_state = {"error": str(e)}

    # Packet store summary (chief only)
    packet_summary = {}
    try:
        from tools.multifleet_packet_store import PacketStore
        store = PacketStore()
        packet_summary = store.fleet_summary()
    except Exception:
        packet_summary = {"note": "packet store only available on chief"}

    return {
        "this_node": node_id,
        "is_chief": node_id == chief.get("nodeId", ""),
        "fleet_id": config.get("fleetId", "unknown"),
        "protocol_version": config.get("protocol", {}).get("version", "unknown"),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "nodes": probe_data.get("nodes", {}),
        "git": git_state,
        "surgeons": _surgeon_status(),
        "packets": packet_summary,
    }


# ── MCP Server (stdin/stdout JSON-RPC) ──

TOOLS = [
    {
        "name": "fleet_probe",
        "description": "Health check all fleet nodes — returns per-node status, latency, sessions, peers, and surgeon availability. Like a fleet-wide ping sweep.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "fn": fleet_probe,
    },
    {
        "name": "fleet_send",
        "description": "Send a message to a fleet node via 8-priority fallback chain (P0 Cloud > NATS > HTTP > Chief > Seed > SSH > WoL > Git). P0 cloud is optional — only used when use_cloud=true or all local channels fail.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Destination node ID (e.g. mac2, mac3)"},
                "type": {"type": "string", "description": "Message type: context, task, repair, heartbeat", "default": "context"},
                "subject": {"type": "string", "description": "Short subject line for the message"},
                "body": {"type": "string", "description": "Full message body"},
                "use_cloud": {"type": "boolean", "description": "If true, try P0 cloud channel first (RemoteTrigger API)", "default": False},
            },
            "required": ["target", "body"],
        },
        "fn": fleet_send,
    },
    {
        "name": "fleet_repair",
        "description": "Trigger 4-level repair escalation on a target node (notify > guide > assist > remote SSH). Each level uses the 7-priority fallback chain.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Node to repair (e.g. mac2)"},
                "action": {"type": "string", "description": "Repair action: restart_nats, restart_daemon, full_restart", "default": "restart_nats"},
                "broken_channels": {"type": "string", "description": "Comma-separated broken channels (e.g. P1_nats,P2_http)"},
            },
            "required": ["target"],
        },
        "fn": fleet_repair,
    },
    {
        "name": "fleet_broadcast",
        "description": "Send a message to ALL fleet nodes (except self) via 7-priority fallback. Returns per-node send results.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "description": "Message type: context, announcement, task", "default": "context"},
                "subject": {"type": "string", "description": "Short subject line"},
                "body": {"type": "string", "description": "Full message body"},
            },
            "required": ["body"],
        },
        "fn": fleet_broadcast,
    },
    {
        "name": "fleet_worker_dispatch",
        "description": "Launch a Claude session-driver worker on a target node, send a task, wait for result. Worker runs in detached tmux, invisible to the user.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Node to run worker on (e.g. mac2, mac3, or 'local')"},
                "task": {"type": "string", "description": "Prompt/task to send to the worker session"},
                "timeout": {"type": "string", "description": "Max seconds to wait (default 300)", "default": "300"},
            },
            "required": ["target", "task"],
        },
        "fn": fleet_worker_dispatch,
    },
    {
        "name": "fleet_status",
        "description": "Fleet-wide dashboard — all nodes with roles, connectivity, sessions, git state, surgeon status, packet store summary. Comprehensive fleet overview.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "fn": fleet_status,
    },
]

TOOLS_BY_NAME = {t["name"]: t for t in TOOLS}


def handle_request(request: dict) -> dict:
    """Handle a single MCP JSON-RPC request."""
    method = request.get("method", "")

    if method == "initialize":
        return {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fleet-nerve", "version": "2.0.0"},
        }

    if method == "tools/list":
        return {
            "tools": [
                {"name": t["name"], "description": t["description"], "inputSchema": t["inputSchema"]}
                for t in TOOLS
            ]
        }

    if method == "tools/call":
        tool_name = request.get("params", {}).get("name", "")
        args = request.get("params", {}).get("arguments", {})
        tool = TOOLS_BY_NAME.get(tool_name)
        if not tool:
            return {"content": [{"type": "text", "text": json.dumps({"error": f"Unknown tool: {tool_name}"})}], "isError": True}
        try:
            result = tool["fn"](**args)
            text = json.dumps(result, indent=2) if not isinstance(result, str) else result
            return {"content": [{"type": "text", "text": text}]}
        except Exception as e:
            error_payload = {"error": str(e), "traceback": traceback.format_exc()}
            return {"content": [{"type": "text", "text": json.dumps(error_payload, indent=2)}], "isError": True}

    if method == "notifications/initialized":
        return None  # No response needed

    return {"error": {"code": -32601, "message": f"Method not found: {method}"}}


def main():
    """Run as MCP server on stdin/stdout (JSON-RPC)."""
    import io
    reader = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8")
    writer = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

    for line in reader:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            response = handle_request(request)
            if response is None:
                continue  # notification, no response
            response["jsonrpc"] = "2.0"
            response["id"] = request.get("id")
            writer.write(json.dumps(response) + "\n")
            writer.flush()
        except Exception as e:
            error = {"jsonrpc": "2.0", "id": None, "error": {"code": -1, "message": str(e)}}
            writer.write(json.dumps(error) + "\n")
            writer.flush()


if __name__ == "__main__":
    main()
