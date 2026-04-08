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
  fleet_dashboard        — visual fleet status (formatted for display)
  fleet_work             — view work backlog for this node
  fleet_work_next        — get next unclaimed work item
  fleet_tasks            — active Claude sessions / agents across fleet
  fleet_wake             — wake a sleeping fleet node
  fleet_historian        — trigger session historian on a node
  fleet_channels         — channel state for all peers
  fleet_sessions         — session gold from a node (historian peer sessions)
  fleet_inject           — inject message into active Claude session via --resume
  fleet_relay            — queue a message for chief store-and-forward relay
  fleet_discover         — discover fleet peers from config + heartbeats
  fleet_send_smart       — session-aware smart routing to a peer
  fleet_doctor           — run 6 diagnostic health checks on this node
  fleet_race             — start a Competitive Branch Race across fleet nodes
  fleet_race_status      — check race progress or list all races
  fleet_cross_rebuttal   — orchestrate cross-machine rebuttal protocol (4-phase cycle)

Usage:
  python3 tools/fleet_nerve_mcp.py
"""

import asyncio
import json
import logging
import os
import socket
import sqlite3
import subprocess
import sys
import time
import traceback
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Productive Waiter singleton ──
_productive_waiter = None


def _get_productive_waiter():
    """Lazy-init singleton ProductiveWaiter for this node."""
    global _productive_waiter
    if _productive_waiter is None:
        from multifleet.productive_wait import ProductiveWaiter
        _productive_waiter = ProductiveWaiter(node_id=_get_node_id())
    return _productive_waiter

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Superrepo fallback: if running inside the superrepo, also add its root
# so that `tools.*` imports work for modules not yet in the multifleet package.
_SUPERREPO_ROOT = REPO_ROOT.parent
if (_SUPERREPO_ROOT / "tools").is_dir():
    sys.path.insert(1, str(_SUPERREPO_ROOT))

import logging

logger = logging.getLogger("fleet-nerve-mcp")

# Rate limiter (prevents runaway agents from flooding the fleet)
try:
    from multifleet.rate_limit import RateLimiter
    _rate_limiter = RateLimiter()
except ImportError:
    _rate_limiter = None  # type: ignore

# Audit trail (optional — graceful if not available)
try:
    from multifleet.audit import get_audit_trail
except ImportError:
    get_audit_trail = None  # type: ignore

_audit_instance = None

def _get_audit():
    global _audit_instance
    if _audit_instance is None and get_audit_trail is not None:
        _audit_instance = get_audit_trail(node_id=NODE_ID)
    return _audit_instance

def _sanitize_args(args: dict) -> dict:
    """Strip secret values from tool arguments for audit logging."""
    if not args:
        return {}
    return {k: ("***" if any(s in k.lower() for s in ("secret", "token", "password", "key")) else v) for k, v in args.items()}

# Error counting (for fleet_doctor and observability)
_error_counts: dict[str, int] = {}

def _count_error(tool: str):
    _error_counts[tool] = _error_counts.get(tool, 0) + 1

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
        from multifleet.config import detect_node_id
        return detect_node_id()
    except ImportError:
        try:
            from tools.fleet_nerve_config import detect_node_id
            return detect_node_id()
        except Exception as e:
            logger.debug(f"Node ID detection fallback failed: {e}")
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


def _daemon_get(path: str, host: str = "127.0.0.1", port: int = 8855, timeout: int = 5) -> dict:
    """GET from fleet daemon HTTP endpoint. Returns parsed JSON or error dict."""
    try:
        url = f"http://{host}:{port}{path}"
        resp = urllib.request.urlopen(url, timeout=timeout)
        return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}


def _daemon_post(path: str, body: dict, host: str = "127.0.0.1", port: int = 8855, timeout: int = 10) -> dict:
    """POST JSON to fleet daemon HTTP endpoint. Returns parsed JSON or error dict."""
    try:
        url = f"http://{host}:{port}{path}"
        req_body = json.dumps(body).encode()
        req = urllib.request.Request(url, data=req_body, headers={"Content-Type": "application/json"})
        resp = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}


def _resolve_host(target: str) -> tuple:
    """Resolve target node to (host, port). Returns ('127.0.0.1', 8855) for self."""
    node_id = _get_node_id()
    if target == node_id or target == "local" or target == "self":
        return ("127.0.0.1", 8855)
    config = _load_config()
    nodes = config.get("nodes", {})
    node_cfg = nodes.get(target, {})
    host = node_cfg.get("host", "")
    port = config.get("protocol", {}).get("tunnelReversePortBase", 8855)
    if not host:
        # Try load_peers fallback
        try:
            try:
                from multifleet.config import load_peers
            except ImportError:
                from tools.fleet_nerve_config import load_peers
            peers, _ = load_peers()
            peer = peers.get(target, {})
            host = peer.get("ip", "")
            port = peer.get("port", 8855)
        except Exception as e:
            logger.debug(f"Peer config lookup failed for {target}: {e}")
    return (host, port) if host else ("", 8855)


def _surgeon_status() -> dict:
    """Check surgeon availability on this node."""
    try:
        try:
            from multifleet.coordinator import surgeon_status
        except ImportError:
            from tools.multifleet_coordinator import surgeon_status
        return surgeon_status()
    except Exception as e:
        return {"error": str(e)}


def _get_nats_instance():
    """Get or create a FleetNerveNATS instance for async operations."""
    try:
        from multifleet.nats_client import FleetNerveNATS
    except ImportError:
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
    from multifleet.packet_registry import create_envelope

    node_id = _get_node_id()
    message = create_envelope(
        from_node=node_id,
        to=target,
        type=type,
        payload={"subject": subject or body[:60], "body": body},
    )
    # Backward compat: daemon reads "from"/"to", envelope has "from_node"/"to_node"
    message["from"] = message["from_node"]
    message["to"] = message["to_node"]

    # Run FleetSendGate before sending
    try:
        from multifleet.invariance_gates import FleetSendGate
        gate_result = FleetSendGate().check(message, rate_limiter=_rate_limiter)
        if not gate_result.passed:
            return {"sent": False, "gate_blocked": True, "gate": gate_result.to_dict()}
    except ImportError:
        pass  # Gate module not available — proceed without gate

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
            try:
                from multifleet.config import load_peers
            except ImportError:
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

    # Run FleetRepairGate before executing repair
    try:
        from multifleet.invariance_gates import FleetRepairGate
        gate_result = FleetRepairGate().check(target, action)
        if not gate_result.passed:
            return {"repair_initiated": False, "gate_blocked": True, "gate": gate_result.to_dict()}
    except ImportError:
        pass  # Gate module not available — proceed without gate

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

    When the task is a JSON task_brief (has "message_type": "task_brief" or
    "type": "task_brief"), a cross-machine RebuttalSession is automatically
    created to track verdicts and critiques across fleet nodes.

    Args:
        target: node to run worker on (e.g. "mac2", or "local" for this node)
        task: the prompt/task to send to the worker
        timeout: max seconds to wait for worker response (default 300)
    """
    timeout_int = int(timeout)

    # Detect task_brief and create a cross-rebuttal session if applicable
    rebuttal_info = None
    try:
        task_data = json.loads(task) if isinstance(task, str) else None
    except (json.JSONDecodeError, TypeError):
        task_data = None

    if task_data and task_data.get("message_type") in ("task_brief",) or (
        task_data and task_data.get("type") == "task_brief"
    ):
        try:
            cross_mgr = _get_cross_rebuttal_manager()
            if cross_mgr is not None:
                session = cross_mgr.start_session(task_data)
                cross_mgr.advance_session(session.task_id)
                rebuttal_info = {
                    "rebuttal_session": session.task_id,
                    "phase": session.phase.value,
                    "expected_machines": session.expected_machines,
                }
                logger.info("Created cross-rebuttal session %s for dispatch to %s",
                            session.task_id[:8], target)
        except Exception as e:
            logger.warning("Failed to create cross-rebuttal session: %s", e)

    # ── Productive waiting: register wait + work backlog while blocked ──
    waiter = _get_productive_waiter()
    task_id = f"dispatch-{target}-{int(time.time())}"
    wait_state = waiter.start_wait(task_id, description=f"dispatch to {target}")
    productive_work_done = []

    # Work backlog items while dispatch is in-flight
    backlog_item = waiter.get_next_work()
    while backlog_item:
        wait_state.productive_task = backlog_item.get("description", str(backlog_item))
        wait_state.productive_started_at = time.time()
        productive_work_done.append(backlog_item)
        logger.info("Productive wait: completed backlog item %s while waiting for %s",
                     backlog_item.get("id", "?"), target)
        backlog_item = waiter.get_next_work()

    try:
        try:
            from multifleet.worker import FleetWorkerManager
        except ImportError:
            from tools.fleet_remote_worker import FleetWorkerManager
        mgr = FleetWorkerManager()
        actual_target = target if target != "local" else mgr.node_id
        result = _run_async(mgr.dispatch(actual_target, task, timeout=timeout_int))

        # Notify waiter that reply arrived
        waiter.notify_reply(task_id, result)
        waiter.clear_completed()

        if rebuttal_info:
            result["rebuttal"] = rebuttal_info
        if productive_work_done:
            result["productive_work_done"] = len(productive_work_done)
        return result
    except RuntimeError as e:
        waiter.notify_reply(task_id, {"error": str(e)})
        waiter.clear_completed()
        if "session-driver" in str(e):
            return {"error": "claude-session-driver plugin not installed on this node", "target": target}
        return {"error": str(e), "target": target, "traceback": traceback.format_exc()}
    except Exception as e:
        waiter.notify_reply(task_id, {"error": str(e)})
        waiter.clear_completed()
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
        try:
            from multifleet.packet_store import PacketStore
        except ImportError:
            from tools.multifleet_packet_store import PacketStore
        store = PacketStore()
        packet_summary = store.fleet_summary()
    except Exception as e:
        logger.debug(f"Packet store unavailable: {e}")
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


def fleet_dashboard() -> dict:
    """Visual fleet status — formatted dashboard with node table, activity summary.

    Returns structured data suitable for display including per-node status rows,
    online/offline counts, active session counts, and activity summary.
    """
    probe_data = fleet_probe()
    nodes_data = probe_data.get("nodes", {})

    dashboard = {
        "fleet_id": probe_data.get("fleet_id", "unknown"),
        "this_node": probe_data.get("this_node", "unknown"),
        "timestamp": probe_data.get("timestamp", ""),
        "summary": {
            "total": len(nodes_data),
            "online": sum(1 for n in nodes_data.values() if n.get("reachable")),
            "offline": sum(1 for n in nodes_data.values() if not n.get("reachable")),
            "total_sessions": sum(len(n.get("sessions", [])) for n in nodes_data.values()),
        },
        "nodes": [],
    }

    for name, info in nodes_data.items():
        row = {
            "node": name,
            "role": info.get("role", "?"),
            "status": "ONLINE" if info.get("reachable") else "OFFLINE",
            "latency_ms": info.get("latency_ms", 0),
            "sessions": len(info.get("sessions", [])),
            "uptime_s": info.get("uptime_s", 0),
        }
        if info.get("error"):
            row["error"] = info["error"]
        dashboard["nodes"].append(row)

    # Try visual formatter if available
    try:
        try:
            from multifleet.visual import fleet_status_table, fleet_activity_summary
        except ImportError:
            from tools.fleet_nerve_visual import fleet_status_table, fleet_activity_summary
        nodes_list = [{"name": n, **info} for n, info in nodes_data.items()]
        dashboard["formatted_table"] = fleet_status_table(nodes_list)
        dashboard["formatted_summary"] = fleet_activity_summary(nodes_list)
    except Exception as e:
        logger.debug(f"Dashboard visual formatting unavailable: {e}")

    return dashboard


def fleet_work(node: str = "") -> dict:
    """View work backlog — active and pending work items for a node.

    Args:
        node: node ID to check (default: this node). Use 'all' for fleet-wide.
    """
    if node == "all":
        return _daemon_get("/work/all")
    host, port = ("127.0.0.1", 8855)
    if node and node != _get_node_id():
        host, port = _resolve_host(node)
        if not host:
            return {"error": f"Cannot resolve host for node: {node}"}
    return _daemon_get("/work", host=host, port=port)


def fleet_work_next(node: str = "") -> dict:
    """Get the next unclaimed work item from the backlog.

    Args:
        node: node to query (default: this node)
    """
    host, port = ("127.0.0.1", 8855)
    if node and node != _get_node_id():
        host, port = _resolve_host(node)
        if not host:
            return {"error": f"Cannot resolve host for node: {node}"}
    return _daemon_get("/work/next", host=host, port=port)


def fleet_tasks() -> dict:
    """Active Claude sessions / agents across the fleet.

    Probes all fleet nodes and returns their active sessions with idle times,
    branches, and session IDs. Useful for understanding what work is happening.
    """
    config = _load_config()
    nodes = config.get("nodes", {})
    node_id = _get_node_id()
    all_tasks = []

    for name in nodes:
        host, port = _resolve_host(name)
        if not host:
            all_tasks.append({"node": name, "error": "unresolvable"})
            continue
        health = _daemon_get("/health", host=host, port=port, timeout=3)
        if "error" in health:
            all_tasks.append({"node": name, "error": health["error"]})
            continue
        sessions = health.get("sessions", [])
        for s in sessions:
            full_sid = s.get("sessionId", s.get("id", "?"))
            all_tasks.append({
                "node": name,
                "session_id": full_sid,
                "display_id": full_sid[:12],
                "idle_s": s.get("idle_s", 0),
                "cwd": s.get("cwd", ""),
                "branch": s.get("branch", ""),
            })

    return {
        "this_node": node_id,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total_sessions": len([t for t in all_tasks if "session_id" in t]),
        "tasks": all_tasks,
    }


def fleet_wake(target: str) -> dict:
    """Wake a sleeping fleet node using multi-strategy approach.

    Strategies tried in order:
    1. Health check — already awake?
    2. SSH ping — machine awake but daemon stopped? Start it.
    3. WoL magic packet — machine sleeping? Send wake-on-LAN.

    Args:
        target: node ID to wake (e.g. "mac2")
    """
    import subprocess

    try:
        try:
            from multifleet.config import load_peers
        except ImportError:
            from tools.fleet_nerve_config import load_peers
        peers, _ = load_peers()
    except Exception as e:
        return {"error": f"Cannot load peer config: {e}"}

    peer = peers.get(target)
    if not peer:
        return {"error": f"Unknown peer: {target}", "known": list(peers.keys())}

    ip = peer["ip"]
    port = peer.get("port", 8855)
    user = peer.get("user", os.environ.get("FLEET_SSH_USER", os.environ.get("USER", "")))

    # Strategy 1: Health check — already awake?
    health = _daemon_get("/health", host=ip, port=port, timeout=3)
    if "error" not in health:
        return {
            "status": "already_awake",
            "target": target,
            "uptime_s": health.get("uptime_s", 0),
            "sessions": health.get("activeSessions", 0),
        }

    # Strategy 2: SSH ping
    try:
        result = subprocess.run(
            ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=5",
             "-o", "BatchMode=yes", f"{user}@{ip}", "echo ALIVE"],
            capture_output=True, text=True, timeout=8,
        )
        if "ALIVE" in result.stdout:
            # Machine awake, start daemon
            subprocess.run(
                ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes",
                 f"{user}@{ip}",
                 "export PATH=/opt/homebrew/bin:/usr/local/bin:$PATH; source ~/.zshrc 2>/dev/null; "
                 "cd ~/dev/er-simulator-superrepo && bash scripts/fleet-nerve-setup.sh 2>&1 | tail -2"],
                capture_output=True, text=True, timeout=15,
            )
            # Caffeinate to prevent re-sleep
            subprocess.Popen(
                ["ssh", "-o", "BatchMode=yes", f"{user}@{ip}", "caffeinate -dims -t 10800 &"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            time.sleep(3)
            # Verify daemon
            health2 = _daemon_get("/health", host=ip, port=port, timeout=3)
            if "error" not in health2:
                return {
                    "status": "woke_via_ssh",
                    "target": target,
                    "sessions": health2.get("activeSessions", 0),
                    "caffeinate": "3hr",
                }
            return {"status": "ssh_reachable_daemon_failed", "target": target,
                    "hint": f"ssh {user}@{ip} 'tail /tmp/fleet-nerve.log'"}
    except Exception as e:
        logger.debug(f"SSH wake attempt for {target} failed: {e}")

    # Strategy 3: WoL magic packet
    mac_addr = peer.get("mac_address")
    if not mac_addr:
        try:
            arp = subprocess.run(["arp", "-a"], capture_output=True, text=True, timeout=5)
            for line in arp.stdout.split("\n"):
                if ip in line:
                    parts = line.split()
                    for p in parts:
                        if ":" in p and len(p) >= 11:
                            mac_addr = p
                            break
        except Exception as e:
            logger.debug(f"ARP lookup for MAC address failed: {e}")

    wol_sent = False
    if mac_addr:
        try:
            mac_bytes = bytes.fromhex(mac_addr.replace(":", "").replace("-", ""))
            magic = b'\xff' * 6 + mac_bytes * 16
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.sendto(magic, ('<broadcast>', 9))
            sock.close()
            wol_sent = True
        except Exception as e:
            return {"status": "wol_failed", "target": target, "error": str(e)}

    # Brief poll (15s) for the node to come up
    for _ in range(3):
        time.sleep(5)
        health3 = _daemon_get("/health", host=ip, port=port, timeout=3)
        if "error" not in health3:
            return {
                "status": "woke_via_wol",
                "target": target,
                "sessions": health3.get("activeSessions", 0),
            }

    return {
        "status": "wake_pending",
        "target": target,
        "wol_sent": wol_sent,
        "mac_address": mac_addr or "unknown",
        "hint": "Node may take up to 60s to wake. Re-probe after.",
    }


def fleet_historian(target: str = "", mode: str = "full") -> dict:
    """Trigger session historian on a fleet node.

    Extracts session gold, cleans cache, feeds the memory pipeline.

    Args:
        target: node to run historian on (default: this node)
        mode: 'full' (extract+analyze+cleanup), 'fast' (extract+clean dead), 'cleanup' (cleanup only)
    """
    host, port = ("127.0.0.1", 8855)
    if target and target != _get_node_id() and target not in ("local", "self"):
        host, port = _resolve_host(target)
        if not host:
            return {"error": f"Cannot resolve host for node: {target}"}

    endpoint = f"/historian/{mode}" if mode in ("full", "fast", "cleanup") else "/historian/full"
    result = _daemon_get(endpoint, host=host, port=port, timeout=60)
    result["target"] = target or _get_node_id()
    result["mode"] = mode
    return result


def fleet_channels(target: str = "") -> dict:
    """Channel state for all peers — shows which communication channels are up/down per peer.

    Returns per-peer channel health (P1_nats, P2_http, P5_ssh, etc.) with
    latency, last success/failure timestamps.

    Args:
        target: node to query (default: this node)
    """
    host, port = ("127.0.0.1", 8855)
    if target and target != _get_node_id() and target not in ("local", "self"):
        host, port = _resolve_host(target)
        if not host:
            return {"error": f"Cannot resolve host for node: {target}"}
    return _daemon_get("/channels", host=host, port=port)


def fleet_sessions(target: str = "") -> dict:
    """Session gold from a node — active sessions with extracted context.

    Returns active sessions and their accumulated gold (context extracted
    by the session historian). Useful for understanding what each session
    has been working on.

    Args:
        target: node to query (default: this node)
    """
    host, port = ("127.0.0.1", 8855)
    if target and target != _get_node_id() and target not in ("local", "self"):
        host, port = _resolve_host(target)
        if not host:
            return {"error": f"Cannot resolve host for node: {target}"}
    return _daemon_get("/sessions/gold", host=host, port=port)


def fleet_inject(message: str, target: str = "", session_id: str = "") -> dict:
    """Inject a message into an active Claude session via --resume.

    Uses the daemon's /inject endpoint which auto-detects the active session
    if no session_id is provided. Falls back to seed file if --resume fails.

    Args:
        message: the message to inject into the session
        target: node to inject on (default: this node)
        session_id: specific session ID (optional — auto-detects if omitted)
    """
    if not message:
        return {"error": "message is required"}

    host, port = ("127.0.0.1", 8855)
    if target and target != _get_node_id() and target not in ("local", "self"):
        host, port = _resolve_host(target)
        if not host:
            return {"error": f"Cannot resolve host for node: {target}"}

    body = {"message": message}
    if session_id:
        # Normalize truncated session IDs to full UUIDs before sending to daemon
        try:
            from multifleet.sessions import normalize_session_id
            session_id = normalize_session_id(session_id)
        except ImportError:
            pass  # graceful degradation — daemon also normalizes
        body["session_id"] = session_id

    result = _daemon_post("/inject", body, host=host, port=port)
    result["target"] = target or _get_node_id()
    return result


def fleet_relay(target: str, type: str = "context", subject: str = "", body: str = "",
                priority: int = 2) -> dict:
    """Queue a message for chief store-and-forward relay.

    Messages are queued on the chief node and delivered when the target
    comes online. The chief flushes pending relay messages on heartbeat.

    Args:
        target: destination node ID
        type: message type — context, task, repair, etc.
        subject: short subject line
        body: message body
        priority: message priority (1=highest, 5=lowest, default 2)
    """
    from multifleet.packet_registry import create_envelope

    node_id = _get_node_id()
    config = _load_config()
    chief_id = config.get("chief", {}).get("nodeId", "")

    message = create_envelope(
        from_node=node_id,
        to=target,
        type=type,
        payload={"subject": subject or body[:60], "body": body},
    )
    message["from"] = message["from_node"]
    message["to"] = message["to_node"]
    message["priority"] = priority

    # Route to chief for relay
    chief_host, chief_port = _resolve_host(chief_id) if chief_id else ("127.0.0.1", 8855)
    if not chief_host:
        # Fallback: post to local daemon which may be chief
        chief_host, chief_port = "127.0.0.1", 8855

    result = _daemon_post("/relay/queue", message, host=chief_host, port=chief_port)
    result["relay_via"] = chief_id or "local"
    result["target"] = target
    return result


def fleet_discover() -> dict:
    """Discover fleet peers from config, heartbeat registry, and live probing.

    Combines:
    1. Static config from .multifleet/config.json
    2. Peer registry from fleet_nerve_config.py
    3. Live daemon /peers endpoint (heartbeat-discovered peers)

    Returns merged peer list with reachability status.
    """
    discovered = {}

    # Source 1: Static config
    config = _load_config()
    nodes = config.get("nodes", {})
    for name, cfg in nodes.items():
        discovered[name] = {
            "source": "config",
            "host": cfg.get("host", ""),
            "role": cfg.get("role", "unknown"),
            "reachable": None,
        }

    # Source 2: Peer registry
    try:
        try:
            from multifleet.config import load_peers
        except ImportError:
            from tools.fleet_nerve_config import load_peers
        peers, chief = load_peers()
        for name, peer in peers.items():
            if name in discovered:
                discovered[name]["ip"] = peer.get("ip", "")
                discovered[name]["port"] = peer.get("port", 8855)
                discovered[name]["user"] = peer.get("user", "")
                discovered[name]["source"] = "config+peers"
            else:
                discovered[name] = {
                    "source": "peers",
                    "ip": peer.get("ip", ""),
                    "port": peer.get("port", 8855),
                    "user": peer.get("user", ""),
                    "reachable": None,
                }
    except Exception as e:
        logger.debug(f"Peer config load for discovery failed: {e}")

    # Source 3: Live daemon peers (heartbeat-discovered)
    live_peers = _daemon_get("/peers")
    if "peers" in live_peers:
        for peer_info in live_peers["peers"]:
            name = peer_info.get("node_id", peer_info.get("name", ""))
            if name and name not in discovered:
                discovered[name] = {
                    "source": "heartbeat",
                    "reachable": None,
                }
            if name and name in discovered:
                discovered[name]["last_heartbeat"] = peer_info.get("last_seen", peer_info.get("timestamp"))

    # Probe reachability for all discovered peers
    node_id = _get_node_id()
    for name, info in discovered.items():
        host = info.get("ip") or info.get("host", "")
        if name == node_id:
            host = "127.0.0.1"
        if not host:
            info["reachable"] = False
            continue
        port = info.get("port", 8855)
        probe = _http_probe(f"http://{host}:{port}/health", timeout=2)
        info["reachable"] = probe["reachable"]
        info["latency_ms"] = probe.get("latency_ms", 0)

    return {
        "this_node": node_id,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "peer_count": len(discovered),
        "online": sum(1 for d in discovered.values() if d.get("reachable")),
        "peers": discovered,
    }


def fleet_send_smart(target: str, type: str = "context", subject: str = "", body: str = "",
                     priority: int = 2) -> dict:
    """Session-aware smart routing — picks best delivery method based on target state.

    Unlike fleet_send (brute-force fallback chain), send_smart probes the target
    first and routes based on session state:
    - Active session → --resume injection (instant)
    - Idle session → seed file + notification
    - No session → spawn background agent or queue for relay

    Args:
        target: destination node ID
        type: message type — context, task, alert, etc.
        subject: short subject line
        body: message body
        priority: message priority (1=highest, 5=lowest, default 2)
    """
    from multifleet.packet_registry import create_envelope

    node_id = _get_node_id()
    message = create_envelope(
        from_node=node_id,
        to=target,
        type=type,
        payload={"subject": subject or body[:60], "body": body},
    )
    message["from"] = message["from_node"]
    message["to"] = message["to_node"]
    message["priority"] = priority

    # Route through local daemon's /send-smart endpoint
    result = _daemon_post("/send-smart", {
        "target": target,
        "type": type,
        "from": node_id,
        "priority": priority,
        "payload": message["payload"],
        "id": message["id"],
    })
    result["target"] = target
    result["type"] = type
    return result


# ── Fleet Doctor ──

FLEET_NERVE_PORT = int(os.environ.get("FLEET_NERVE_PORT", "8855"))
NODE_ID = _get_node_id()

# Keychain key names to probe (first match per group wins)
_REQUIRED_KEY_GROUPS = [
    ("OPENAI_API_KEY", "Context_DNA_OPENAI"),
    ("DEEPSEEK_API_KEY", "Context_DNA_Deepseek"),
]


def _timed_check(code: str, name: str, critical: bool, fn):
    """Run a check function, capture timing and exceptions."""
    t0 = time.monotonic()
    try:
        passed, message = fn()
        latency = round((time.monotonic() - t0) * 1000, 1)
        return {"code": code, "name": name, "passed": passed, "message": message, "latency_ms": latency, "critical": critical}
    except Exception as e:
        latency = round((time.monotonic() - t0) * 1000, 1)
        return {"code": code, "name": name, "passed": False, "message": f"Exception: {e}", "latency_ms": latency, "critical": critical}


def _check_daemon_health() -> dict:
    """FN-0001: Check fleet daemon HTTP health endpoint."""
    def _run():
        data = _daemon_get("/health", port=FLEET_NERVE_PORT, timeout=5)
        if "error" in data:
            return False, f"Daemon unreachable: {data['error']}"
        status = data.get("status", "unknown")
        uptime = data.get("uptime_s", 0)
        transport = data.get("transport", "unknown")
        if status == "ok":
            return True, f"Daemon healthy (uptime: {int(uptime)}s, transport: {transport})"
        return False, f"Daemon status: {status}"
    return _timed_check("FN-0001", "daemon_health", True, _run)


def _check_nats_connectivity() -> dict:
    """FN-0002: Check NATS transport from daemon health response."""
    def _run():
        data = _daemon_get("/health", port=FLEET_NERVE_PORT, timeout=5)
        if "error" in data:
            return False, f"Cannot check NATS — daemon unreachable: {data['error']}"
        transport = data.get("transport", "unknown")
        nats_url = data.get("nats_url", "")
        if transport == "nats":
            msg = "NATS connected"
            if nats_url:
                msg += f" ({nats_url})"
            return True, msg
        return False, f"Transport is '{transport}', not NATS"
    return _timed_check("FN-0002", "nats_connectivity", True, _run)


def _check_peer_reachability() -> dict:
    """FN-0003: Check per-peer channel health scores from daemon."""
    def _run():
        data = _daemon_get("/health", port=FLEET_NERVE_PORT, timeout=5)
        if "error" in data:
            return False, f"Cannot check peers — daemon unreachable: {data['error']}"
        channel_health = data.get("channel_health", {})
        if not channel_health:
            # Try peers field as fallback
            peers = data.get("peers", [])
            if peers:
                return True, f"{len(peers)} peer(s) known: {', '.join(str(p) for p in peers)}"
            return False, "No peer data available"
        scores = []
        reachable = 0
        for peer, health in channel_health.items():
            if isinstance(health, dict):
                up = health.get("up", 0)
                total = health.get("total", 7)
                scores.append(f"{peer}: {up}/{total}")
                if up > 0:
                    reachable += 1
            else:
                scores.append(f"{peer}: {health}")
                reachable += 1  # assume reachable if reported
        msg = ", ".join(scores) if scores else "no peers"
        return reachable > 0, msg
    return _timed_check("FN-0003", "peer_reachability", False, _run)


def _check_keychain_secrets() -> dict:
    """FN-0004: Check macOS Keychain for required API keys."""
    def _run():
        found = []
        missing = []
        for key_group in _REQUIRED_KEY_GROUPS:
            group_found = False
            for key_name in key_group:
                try:
                    result = subprocess.run(
                        ["security", "find-generic-password", "-s", "fleet-nerve", "-a", key_name, "-w"],
                        capture_output=True, text=True, timeout=5,
                    )
                    if result.returncode == 0 and result.stdout.strip():
                        found.append(key_name)
                        group_found = True
                        break
                except (subprocess.TimeoutExpired, FileNotFoundError):
                    pass
            if not group_found:
                missing.append(" or ".join(key_group))
        if found:
            msg = f"Found: {', '.join(found)}"
            if missing:
                msg += f"; missing: {', '.join(missing)}"
            return True, msg
        return False, f"No API keys found. Missing: {', '.join(missing)}"
    return _timed_check("FN-0004", "keychain_secrets", True, _run)


def _check_message_store() -> dict:
    """FN-0005: Check message store SQLite database integrity."""
    def _run():
        db_path = Path.home() / ".fleet-nerve" / "messages.db"
        if not db_path.exists():
            return False, f"Database not found: {db_path}"
        conn = sqlite3.connect(str(db_path), timeout=5)
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()
            if integrity[0] != "ok":
                return False, f"Integrity check failed: {integrity[0]}"
            # Count messages
            try:
                row_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            except sqlite3.OperationalError:
                row_count = "unknown (table may not exist)"
            return True, f"Store OK ({row_count} messages)"
        finally:
            conn.close()
    return _timed_check("FN-0005", "message_store", True, _run)


def _check_python_deps() -> dict:
    """FN-0006: Check Python version and key dependencies."""
    def _run():
        parts = []
        py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        py_ok = sys.version_info >= (3, 10)
        parts.append(f"Python {py_ver} {'OK' if py_ok else 'NEED >=3.10'}")
        # Check nats
        try:
            import nats as _nats
            nats_ver = getattr(_nats, "__version__", "installed")
            parts.append(f"nats: {nats_ver}")
        except ImportError:
            parts.append("nats: MISSING")
        # Check mcp (optional)
        try:
            import mcp as _mcp
            mcp_ver = getattr(_mcp, "__version__", "installed")
            parts.append(f"mcp: {mcp_ver}")
        except ImportError:
            parts.append("mcp: not installed (optional)")
        all_ok = py_ok  # nats/mcp are informational
        return all_ok, "; ".join(parts)
    return _timed_check("FN-0006", "python_deps", False, _run)


def _make_recommendation(checks: list) -> str:
    """Generate a human-readable recommendation from check results."""
    critical_failed = [c for c in checks if not c["passed"] and c["critical"]]
    non_critical_failed = [c for c in checks if not c["passed"] and not c["critical"]]
    if critical_failed:
        names = ", ".join(c["name"] for c in critical_failed)
        return f"CRITICAL: {len(critical_failed)} critical check(s) failed ({names}). Fix before fleet operations."
    if non_critical_failed:
        names = ", ".join(c["name"] for c in non_critical_failed)
        return f"All critical checks passed. {len(non_critical_failed)} non-critical issue(s): {names}."
    return "All checks passed. Fleet node is healthy."


def fleet_doctor() -> dict:
    """Run diagnostic health checks on this fleet node.

    Runs 6 sequential checks (daemon, NATS, peers, keychain, store, deps)
    and returns a structured health report with pass/fail per check.
    """
    checks = []
    checks.append(_check_daemon_health())
    checks.append(_check_nats_connectivity())
    checks.append(_check_peer_reachability())
    checks.append(_check_keychain_secrets())
    checks.append(_check_message_store())
    checks.append(_check_python_deps())

    passed = sum(1 for c in checks if c["passed"])
    failed = len(checks) - passed
    critical_failed = sum(1 for c in checks if not c["passed"] and c["critical"])

    return {
        "doctor": "fleet-nerve",
        "version": "2.0.0",
        "node_id": NODE_ID,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "passed": passed,
        "failed": failed,
        "total": len(checks),
        "critical_failures": critical_failed,
        "checks": checks,
        "recommendation": _make_recommendation(checks),
    }


def fleet_onboard(node_id: str = "") -> dict:
    """Run fleet onboarding wizard — check setup and return guide.

    Runs 10 checks (python, nats, daemon, keychain, superset, config,
    ssh, probes, surgeons, skills) and returns status + markdown guide.
    """
    try:
        from multifleet.onboarding import OnboardingWizard
        wiz = OnboardingWizard(node_id=node_id)
        status = wiz.check_all()
        status["guide"] = wiz.render_guide()
        return status
    except ImportError as e:
        return {"error": f"onboarding module not available: {e}"}


def fleet_gains_gate(quick: bool = False) -> dict:
    """Run fleet gains gate — structured health verification.

    Runs 6 (quick) or 10 (full) health checks and returns a structured
    pass/fail result. Critical failures block the gate; non-critical
    issues are reported but don't block.
    """
    try:
        from multifleet.gains_gate import FleetGainsGate
        gate = FleetGainsGate(daemon_port=FLEET_NERVE_PORT)
        result = gate.run(quick=quick)
        return result.to_dict()
    except ImportError as e:
        return {
            "passed": False,
            "error": f"gains_gate module not available: {e}",
            "total": 0,
            "checks": [],
        }
    except Exception as e:
        _count_error("fleet_gains_gate")
        return {
            "passed": False,
            "error": str(e),
            "total": 0,
            "checks": [],
        }


# ── Invariance Gate Check ──

def fleet_gate_check(gate: str, target: str = "", action: str = "",
                     branch: str = "", quick: bool = True) -> dict:
    """Run a fleet invariance gate on demand.

    Args:
        gate: which gate to run — "send", "repair", or "merge"
        target: target node (required for send/repair gates)
        action: repair action (for repair gate)
        branch: branch name (for merge gate)
        quick: quick mode for merge gate gains sub-check
    """
    try:
        from multifleet.invariance_gates import (
            FleetSendGate, FleetRepairGate, FleetMergeGate,
        )
    except ImportError as e:
        return {"error": f"invariance_gates module not available: {e}"}

    gate_lower = gate.lower()

    if gate_lower == "send":
        if not target:
            return {"error": "target is required for send gate"}
        # Build a minimal test packet
        from multifleet.packet_registry import create_envelope
        node_id = _get_node_id()
        packet = create_envelope(
            from_node=node_id, to=target, type="context",
            payload={"subject": "gate_check", "body": "gate_check"},
        )
        packet["from"] = packet["from_node"]
        packet["to"] = packet["to_node"]
        result = FleetSendGate().check(packet, rate_limiter=_rate_limiter)
        return result.to_dict()

    elif gate_lower == "repair":
        if not target:
            return {"error": "target is required for repair gate"}
        result = FleetRepairGate().check(target, action or "restart_nats")
        return result.to_dict()

    elif gate_lower == "merge":
        if not branch:
            return {"error": "branch is required for merge gate"}
        gains = None
        if quick:
            try:
                from multifleet.gains_gate import FleetGainsGate
                gains = FleetGainsGate(daemon_port=FLEET_NERVE_PORT)
            except ImportError:
                pass
        result = FleetMergeGate().check(branch, gains_gate=gains)
        return result.to_dict()

    else:
        return {"error": f"Unknown gate: '{gate}'. Valid: send, repair, merge"}


# ── Lease Management ──

def _get_lease_manager():
    """Get or create a LeaseManager instance (lazy init)."""
    global _lease_manager
    try:
        return _lease_manager
    except NameError:
        pass
    try:
        from multifleet.leases import LeaseManager
        from multifleet.store import FleetNerveStore
        config = _load_config()
        chief_id = config.get("chief", {}).get("nodeId", "")
        store = FleetNerveStore()
        _lease_manager = LeaseManager(store, NODE_ID, chief_node_id=chief_id or None,
                                       daemon_port=FLEET_NERVE_PORT)
        return _lease_manager
    except Exception as e:
        logger.warning(f"LeaseManager init failed: {e}")
        return None


def _get_rebuttal_manager():
    """Get or create a RebuttalManager instance (lazy init)."""
    global _rebuttal_manager
    try:
        return _rebuttal_manager
    except NameError:
        pass
    try:
        from multifleet.rebuttal import RebuttalManager
        from multifleet.store import FleetNerveStore
        store = FleetNerveStore()
        _rebuttal_manager = RebuttalManager(store, NODE_ID)
        return _rebuttal_manager
    except Exception as e:
        logger.warning(f"RebuttalManager init failed: {e}")
        return None


def _get_evidence_ledger():
    """Get or create an EvidenceLedger instance (lazy init)."""
    global _evidence_ledger
    try:
        return _evidence_ledger
    except NameError:
        pass
    try:
        from multifleet.evidence_ledger import EvidenceLedger
        _evidence_ledger = EvidenceLedger()
        return _evidence_ledger
    except Exception as e:
        logger.warning(f"EvidenceLedger init failed: {e}")
        return None


def _get_eventbus():
    """Get or create a FleetEventBus instance (lazy init)."""
    global _fleet_eventbus
    try:
        return _fleet_eventbus
    except NameError:
        pass
    try:
        from multifleet.eventbus import FleetEventBus
        _fleet_eventbus = FleetEventBus(node_id=NODE_ID, daemon_port=FLEET_NERVE_PORT)
        return _fleet_eventbus
    except Exception as e:
        logger.warning(f"FleetEventBus init failed: {e}")
        return None


def fleet_lease(action: str, resource_id: str = "", lease_type: str = "short_task") -> dict:
    """Manage resource leases — acquire, release, renew, query, or list."""
    mgr = _get_lease_manager()
    if mgr is None:
        return {"error": "LeaseManager not available"}

    eventbus = _get_eventbus()

    if action == "acquire":
        if not resource_id:
            return {"error": "resource_id required for acquire"}
        result = mgr.acquire(resource_id, lease_type=lease_type)
        if result.get("acquired") and eventbus:
            eventbus.lease_changed(resource_id, "acquired", NODE_ID)
        # Record in evidence ledger
        evidence = _get_evidence_ledger()
        if evidence:
            evidence.record("lease_granted", NODE_ID, f"lease {'acquired' if result.get('acquired') else 'denied'}: {resource_id}",
                            {"resource_id": resource_id, "lease_type": lease_type,
                             "acquired": result.get("acquired", False), "holder": result.get("holder", "")})
        return result
    elif action == "release":
        if not resource_id:
            return {"error": "resource_id required for release"}
        result = mgr.release(resource_id)
        if result.get("released") and eventbus:
            eventbus.lease_changed(resource_id, "released", NODE_ID)
        return result
    elif action == "renew":
        if not resource_id:
            return {"error": "resource_id required for renew"}
        return mgr.renew(resource_id)
    elif action == "query":
        if not resource_id:
            return {"error": "resource_id required for query"}
        result = mgr.query(resource_id)
        return result if result else {"lease": None, "resource_id": resource_id}
    elif action == "list":
        leases = mgr.list_active(node_id=resource_id if resource_id else None)
        return {"leases": leases, "count": len(leases), "stats": mgr.stats}
    else:
        return {"error": f"unknown action: {action}, expected: acquire|release|renew|query|list"}


def fleet_rebuttal(action: str, proposal_id: str = "", subject: str = "",
                   body: str = "", reason: str = "", scope: str = "general",
                   resolution: str = "") -> dict:
    """Manage fleet proposals and rebuttals — structured consensus protocol.

    Actions: propose, review, accept, rebut, counter, resolve, list, get.
    """
    mgr = _get_rebuttal_manager()
    if mgr is None:
        return {"error": "RebuttalManager not available"}

    eventbus = _get_eventbus()

    if action == "propose":
        if not subject or not body:
            return {"error": "subject and body required for propose"}
        result = mgr.propose(subject, body, scope=scope)
        if result and eventbus:
            eventbus.emit("rebuttal.proposed", {
                "proposal_id": result["proposal_id"], "subject": subject,
                "proposer": NODE_ID, "scope": scope,
            }, broadcast=True)
        return result
    elif action == "review":
        if not proposal_id:
            return {"error": "proposal_id required for review"}
        return mgr.review(proposal_id)
    elif action == "accept":
        if not proposal_id:
            return {"error": "proposal_id required for accept"}
        result = mgr.accept(proposal_id, reason=reason)
        if result and result.get("state") == "accepted":
            if eventbus:
                eventbus.emit("rebuttal.resolved", {
                    "proposal_id": proposal_id, "resolution": "accepted",
                    "actor": NODE_ID,
                }, broadcast=True)
            evidence = _get_evidence_ledger()
            if evidence:
                evidence.record("rebuttal_resolved", NODE_ID,
                                f"accepted: {result.get('subject', proposal_id[:8])}",
                                {"proposal_id": proposal_id, "resolution": "accepted",
                                 "reason": reason})
        return result
    elif action == "rebut":
        if not proposal_id or not reason:
            return {"error": "proposal_id and reason required for rebut"}
        return mgr.rebut(proposal_id, reason, counter=body)
    elif action == "counter":
        if not proposal_id or not body:
            return {"error": "proposal_id and body required for counter"}
        return mgr.counter_propose(proposal_id, body)
    elif action == "resolve":
        if not proposal_id or not resolution:
            return {"error": "proposal_id and resolution required for resolve"}
        result = mgr.resolve(proposal_id, resolution)
        if result and result.get("state") == "resolved":
            if eventbus:
                eventbus.emit("rebuttal.resolved", {
                    "proposal_id": proposal_id, "resolution": resolution,
                    "actor": NODE_ID,
                }, broadcast=True)
            evidence = _get_evidence_ledger()
            if evidence:
                evidence.record("rebuttal_resolved", NODE_ID,
                                f"resolved: {result.get('subject', proposal_id[:8])}",
                                {"proposal_id": proposal_id, "resolution": resolution})
        return result
    elif action == "list":
        proposals = mgr.list_active(state=scope if scope != "general" else None)
        return {"proposals": proposals, "count": len(proposals), "stats": mgr.stats}
    elif action == "get":
        if not proposal_id:
            return {"error": "proposal_id required for get"}
        result = mgr.get(proposal_id)
        return result if result else {"error": f"proposal {proposal_id} not found"}
    else:
        return {"error": f"unknown action: {action}, expected: propose|review|accept|rebut|counter|resolve|list|get"}


# ── Cross-Machine Rebuttal (task_brief -> verdict -> critique -> synthesis) ──

_cross_rebuttal_manager = None
_chief_synthesis = None


def _get_cross_rebuttal_manager():
    """Get or create a cross-machine RebuttalManager (lazy init)."""
    global _cross_rebuttal_manager
    if _cross_rebuttal_manager is not None:
        return _cross_rebuttal_manager
    try:
        from multifleet.cross_rebuttal import RebuttalManager
        _cross_rebuttal_manager = RebuttalManager()
        return _cross_rebuttal_manager
    except Exception as e:
        logger.warning(f"CrossRebuttalManager init failed: {e}")
        return None


def _get_chief_synthesis():
    """Get or create a ChiefSynthesis engine (lazy init)."""
    global _chief_synthesis
    if _chief_synthesis is not None:
        return _chief_synthesis
    try:
        from multifleet.synthesis import ChiefSynthesis
        _chief_synthesis = ChiefSynthesis()
        return _chief_synthesis
    except Exception as e:
        logger.warning(f"ChiefSynthesis init failed: {e}")
        return None


def fleet_cross_rebuttal(action: str, task_id: str = "", payload: str = "") -> dict:
    """Orchestrate cross-machine rebuttal protocol.

    Actions:
    - start: Chief creates a rebuttal session from a task_brief payload (JSON).
             Payload must include objective, deadline_minutes, assigned_to.
    - verdict: Submit this machine's local_verdict for a task.
               Payload is the verdict JSON (must include confidence, summary).
    - critique: Submit this machine's critique of another's verdict.
                Payload is the critique JSON (must include reviewing, stance).
    - status: Check rebuttal session status for a task_id.
    - synthesize: Chief runs synthesis on collected verdicts/critiques.
    - list: List all active rebuttal sessions.
    """
    mgr = _get_cross_rebuttal_manager()
    if mgr is None:
        return {"error": "CrossRebuttalManager not available"}

    if action == "start":
        if not payload:
            return {"error": "payload (JSON task_brief) required for start"}
        try:
            brief = json.loads(payload) if isinstance(payload, str) else payload
        except json.JSONDecodeError as e:
            return {"error": f"invalid JSON payload: {e}"}
        session = mgr.start_session(brief)
        # Auto-advance to LOCAL_SURGERY so machines can start working
        mgr.advance_session(session.task_id)
        return {
            "status": "started",
            "task_id": session.task_id,
            "phase": session.phase.value,
            "expected_machines": session.expected_machines,
            "deadline_minutes": session.deadline_minutes,
        }

    elif action == "verdict":
        if not task_id:
            return {"error": "task_id required for verdict"}
        if not payload:
            return {"error": "payload (JSON verdict) required for verdict"}
        try:
            verdict = json.loads(payload) if isinstance(payload, str) else payload
        except json.JSONDecodeError as e:
            return {"error": f"invalid JSON payload: {e}"}
        verdict.setdefault("from", NODE_ID)
        verdict["task_id"] = task_id
        result = mgr.handle_incoming_packet("local_verdict", verdict)
        return result

    elif action == "critique":
        if not task_id:
            return {"error": "task_id required for critique"}
        if not payload:
            return {"error": "payload (JSON critique) required for critique"}
        try:
            critique = json.loads(payload) if isinstance(payload, str) else payload
        except json.JSONDecodeError as e:
            return {"error": f"invalid JSON payload: {e}"}
        critique.setdefault("from", NODE_ID)
        critique["task_id"] = task_id
        # Advance to UNDER_REVIEW if still at VERDICTS_COLLECTED
        session = mgr.get_session(task_id)
        if session:
            from multifleet.cross_rebuttal import RebuttalPhase
            if session.phase == RebuttalPhase.VERDICTS_COLLECTED:
                session.advance(RebuttalPhase.UNDER_REVIEW)
        result = mgr.handle_incoming_packet("critique", critique)
        return result

    elif action == "status":
        if not task_id:
            return {"error": "task_id required for status"}
        session = mgr.get_session(task_id)
        if not session:
            return {"error": f"no session for task {task_id}"}
        return {
            "task_id": session.task_id,
            "phase": session.phase.value,
            "expected_machines": session.expected_machines,
            "verdicts": list(session.verdicts.keys()),
            "verdicts_count": len(session.verdicts),
            "critiques": list(session.critiques.keys()),
            "critiques_count": len(session.critiques),
            "has_decision": session.chief_decision is not None,
            "is_expired": session.is_expired(),
            "deadline_minutes": session.deadline_minutes,
        }

    elif action == "synthesize":
        if not task_id:
            return {"error": "task_id required for synthesize"}
        session = mgr.get_session(task_id)
        if not session:
            return {"error": f"no session for task {task_id}"}
        engine = _get_chief_synthesis()
        if engine is None:
            return {"error": "ChiefSynthesis engine not available"}
        synthesis_result = engine.synthesize(task_id, session.verdicts)
        decision = synthesis_result.to_chief_decision()
        # Record decision on session and advance to resolved
        result = mgr.handle_incoming_packet("chief_decision", decision)
        return {
            "status": "synthesized",
            "task_id": task_id,
            "decision": decision,
            "session_phase": result.get("phase", "unknown"),
        }

    elif action == "list":
        active = mgr.active_sessions()
        all_sessions = mgr.all_sessions()
        return {
            "active": [s.to_dict() for s in active],
            "active_count": len(active),
            "total_count": len(all_sessions),
        }

    else:
        return {"error": f"unknown action: {action}, expected: start|verdict|critique|status|synthesize|list"}


def fleet_evidence(action: str, event_type: str = "", node_id: str = "",
                   subject: str = "", payload: str = "",
                   since: str = "", limit: str = "50") -> dict:
    """Manage fleet evidence ledger — tamper-evident append-only log.

    Actions: record, query, verify, latest, stats.
    """
    ledger = _get_evidence_ledger()
    if ledger is None:
        return {"error": "EvidenceLedger not available"}

    if action == "record":
        if not event_type or not node_id:
            return {"error": "event_type and node_id required for record"}
        try:
            payload_dict = json.loads(payload) if payload else {}
        except json.JSONDecodeError:
            payload_dict = {"raw": payload}
        return ledger.record(event_type, node_id or NODE_ID, subject, payload_dict)
    elif action == "query":
        kwargs = {}
        if event_type:
            kwargs["event_type"] = event_type
        if node_id:
            kwargs["node_id"] = node_id
        if since:
            try:
                kwargs["since"] = float(since)
            except ValueError:
                pass
        try:
            kwargs["limit"] = int(limit)
        except ValueError:
            kwargs["limit"] = 50
        entries = ledger.query(**kwargs)
        return {"entries": entries, "count": len(entries)}
    elif action == "verify":
        lim = None
        try:
            lim = int(limit) if limit and limit != "50" else None
        except ValueError:
            pass
        return ledger.verify_chain(limit=lim)
    elif action == "latest":
        try:
            count = int(limit) if limit else 10
        except ValueError:
            count = 10
        entries = ledger.latest(count=count)
        return {"entries": entries, "count": len(entries)}
    elif action == "stats":
        return ledger.stats
    else:
        return {"error": f"unknown action: {action}, expected: record|query|verify|latest|stats"}


# ── Fleet Race ──

_fleet_race = None

def _get_fleet_race():
    """Get or create a FleetRace instance (lazy init)."""
    global _fleet_race
    if _fleet_race is not None:
        return _fleet_race
    try:
        from multifleet.fleet_race import FleetRace
        _fleet_race = FleetRace(daemon_port=FLEET_NERVE_PORT, node_id=NODE_ID)
        return _fleet_race
    except Exception as e:
        logger.warning(f"FleetRace init failed: {e}")
        return None


def fleet_race(task: str, nodes: str = "", timeout: str = "600") -> dict:
    """Start a Competitive Branch Race across fleet nodes.

    Each node works in an isolated worktree on the same task.
    First to finish triggers cross-review. Fleet debates and converges.

    Args:
        task: The task/problem description to race on.
        nodes: Comma-separated node IDs (default: all available).
        timeout: Max seconds for the race (default 600).
    """
    racer = _get_fleet_race()
    if racer is None:
        return {"error": "FleetRace not available"}

    node_list = [n.strip() for n in nodes.split(",") if n.strip()] if nodes else None
    try:
        timeout_s = int(timeout)
    except ValueError:
        timeout_s = 600

    # Record in audit
    audit = _get_audit()
    if audit:
        audit.record("race_start", "fleet_race", "", "starting", 0,
                      metadata={"task": task[:100], "nodes": nodes})

    result = racer.start(task, nodes=node_list, timeout_s=timeout_s)

    # Emit event
    eventbus = _get_eventbus()
    if eventbus:
        eventbus.emit("race.started", {
            "race_id": result.get("race_id"),
            "task": task[:100],
            "participants": list(result.get("participants", {}).keys()),
            "status": result.get("status"),
        }, broadcast=True)

    return result


def fleet_race_status(race_id: str = "", action: str = "status") -> dict:
    """Check race progress or list all races.

    Args:
        race_id: Race ID to check (required for status/submit).
        action: status (default), list, or submit.
    """
    racer = _get_fleet_race()
    if racer is None:
        return {"error": "FleetRace not available"}

    if action == "list":
        races = racer.list_races()
        return {"races": races, "count": len(races)}
    elif action == "status":
        if not race_id:
            return {"error": "race_id required for status"}
        return racer.status(race_id)
    elif action == "submit":
        if not race_id:
            return {"error": "race_id required for submit"}
        # submit_result is called internally or via daemon endpoint
        return {"error": "use POST /race/<id>/submit to submit results"}
    else:
        return {"error": f"unknown action: {action}, expected: status|list|submit"}


# ── Task Orchestrator ──

_task_orchestrator = None

def _get_task_orchestrator():
    """Get or create a TaskOrchestrator instance (lazy init)."""
    global _task_orchestrator
    if _task_orchestrator is not None:
        return _task_orchestrator
    try:
        from multifleet.task_orchestrator import TaskOrchestrator
        _task_orchestrator = TaskOrchestrator(daemon_port=FLEET_NERVE_PORT, node_id=NODE_ID)
        return _task_orchestrator
    except Exception as e:
        logger.warning(f"TaskOrchestrator init failed: {e}")
        return None


def fleet_orchestrate(action: str = "run", task: str = "", criteria: str = "",
                      parallel: str = "false", nodes: str = "",
                      task_id: str = "") -> dict:
    """Run the full task lifecycle — idea to wisdom in one command.

    Actions:
      run    — execute full lifecycle (inbox -> probes -> execute -> review -> promote -> record)
      status — check task progress by task_id
      list   — list all orchestrated tasks

    Args:
        action: run, status, or list
        task: Task description (required for run)
        criteria: Comma-separated done criteria (required for run)
        parallel: "true" to use fleet_race across nodes (default: "false")
        nodes: Comma-separated node IDs for parallel execution
        task_id: Task ID for status action
    """
    orch = _get_task_orchestrator()
    if orch is None:
        return {"error": "TaskOrchestrator not available"}

    if action == "run":
        if not task:
            return {"error": "task description required for run action"}
        if not criteria:
            return {"error": "criteria required (comma-separated done conditions)"}
        criteria_list = [c.strip() for c in criteria.split(",") if c.strip()]
        is_parallel = parallel.lower() in ("true", "1", "yes")
        node_list = [n.strip() for n in nodes.split(",") if n.strip()] if nodes else None

        audit = _get_audit()
        if audit:
            audit.record("orchestrate_start", "fleet_orchestrate", "", "starting", 0,
                          metadata={"task": task[:100], "parallel": is_parallel})

        result = orch.run_task(task, done_criteria=criteria_list,
                               parallel=is_parallel, nodes=node_list)

        eventbus = _get_eventbus()
        if eventbus:
            eventbus.emit("orchestrator.complete", {
                "task_id": result.get("id"),
                "status": result.get("status"),
                "parallel": is_parallel,
                "elapsed_s": result.get("elapsed_s", 0),
            }, broadcast=True)

        return result

    elif action == "status":
        if not task_id:
            return {"error": "task_id required for status action"}
        return orch.status(task_id)

    elif action == "list":
        tasks = orch.list_tasks()
        return {"tasks": tasks, "count": len(tasks)}

    else:
        return {"error": f"unknown action: {action}, expected: run|status|list"}


# ── Liaison ──

_liaison_instance = None

def _get_liaison():
    global _liaison_instance
    if _liaison_instance is None:
        try:
            from multifleet.liaison import create_liaison
            _liaison_instance = create_liaison(NODE_ID)
        except Exception:
            return None
    return _liaison_instance


def fleet_liaison_status() -> dict:
    """Return liaison state: pending reviews, active rebuttals, unread count, summary."""
    liaison = _get_liaison()
    if liaison is None:
        return {"error": "Liaison not available", "status": "unavailable"}
    return liaison.to_dict()


def fleet_status_snapshot() -> dict:
    """Return aggregated fleet status snapshot — daemon health, peers, liaison, rebuttals, leases, recent events, and health dot."""
    try:
        from multifleet.status_aggregator import FleetStatusAggregator
        aggregator = FleetStatusAggregator(
            daemon_url=f"http://127.0.0.1:{os.environ.get('FLEET_DAEMON_PORT', '8855')}",
            node_id=NODE_ID,
        )
        return aggregator.snapshot()
    except Exception as e:
        return {"error": str(e), "traceback": traceback.format_exc()}


# ── Hierarchy Tool ──

def fleet_hierarchy(action: str, node_id: str = "", role: str = "",
                    captain_id: str = "", cluster_id: str = "",
                    capabilities: str = "", from_node: str = "",
                    to_node: str = "", candidates: str = "") -> dict:
    """Manage fleet hierarchy — roles, routing, elections.

    Actions:
      status   — show current hierarchy (nodes, roles, chief)
      promote  — promote a node to captain (requires node_id)
      elect    — run chief election (requires candidates as comma-separated node IDs)
      route    — show routing path between two nodes (requires from_node, to_node)
      register — register a node (requires node_id, role; optional captain_id, cluster_id, capabilities)
      nats     — show NATS subject for a node (requires node_id)
    """
    try:
        from multifleet.hierarchy import FleetHierarchy, NodeRole
    except ImportError:
        return {"error": "hierarchy module not available"}

    config = _load_config()
    hierarchy = FleetHierarchy(config)

    if action == "status":
        data = hierarchy.to_dict()
        data["node_count"] = len(data.get("nodes", {}))
        # Summarize by role
        roles = {}
        for n in data.get("nodes", {}).values():
            r = n.get("role", "unknown")
            roles[r] = roles.get(r, 0) + 1
        data["role_summary"] = roles
        return data

    if action == "register":
        if not node_id or not role:
            return {"error": "register requires node_id and role"}
        try:
            nr = NodeRole(role)
        except ValueError:
            return {"error": f"Invalid role: {role}. Use: chief, captain, worker"}
        caps = [c.strip() for c in capabilities.split(",") if c.strip()] if capabilities else []
        node = hierarchy.register_node(
            node_id=node_id, role=nr,
            captain_id=captain_id or None,
            cluster_id=cluster_id or None,
            capabilities=caps or None,
        )
        return {"registered": node.to_dict()}

    if action == "promote":
        if not node_id:
            return {"error": "promote requires node_id"}
        ok = hierarchy.promote_to_captain(node_id, cluster_id=cluster_id or None)
        return {"promoted": ok, "node_id": node_id}

    if action == "elect":
        if not candidates:
            return {"error": "elect requires candidates (comma-separated node IDs)"}
        cands = [c.strip() for c in candidates.split(",") if c.strip()]
        try:
            winner = hierarchy.elect_chief(cands)
            return {"elected_chief": winner, "candidates": cands}
        except ValueError as e:
            return {"error": str(e)}

    if action == "route":
        if not from_node or not to_node:
            return {"error": "route requires from_node and to_node"}
        path = hierarchy.route_message(from_node, to_node)
        return {"from": from_node, "to": to_node, "path": path, "hops": len(path) - 1}

    if action == "nats":
        if not node_id:
            return {"error": "nats requires node_id"}
        subject = hierarchy.nats_subject(node_id)
        subs = hierarchy.nats_subscribe_subjects(node_id)
        return {"node_id": node_id, "publish_subject": subject, "subscribe_subjects": subs}

    return {"error": f"Unknown action: {action}. Use: status, register, promote, elect, route, nats"}


def fleet_metrics(format: str = "prometheus", action: str = "collect") -> dict:
    """Export fleet metrics — per-node or fleet-wide aggregated.

    Actions:
        collect: Collect metrics from all reachable nodes (default)
        aggregate: Fleet-wide totals across all nodes
        trends: Message rates, error rates over time window
        productivity: Fleet productivity score (0-100)
        prometheus: Prometheus text exposition (per-node, legacy compat)

    Args:
        format: Output format — 'prometheus' (text exposition) or 'json' (raw snapshot)
        action: What to compute — collect, aggregate, trends, productivity, prometheus
    """
    try:
        from multifleet.fleet_metrics import FleetMetricsAggregator
        fleet_agg = FleetMetricsAggregator(daemon_port=FLEET_NERVE_PORT)
    except Exception as e:
        return {"error": f"FleetMetricsAggregator unavailable: {e}"}

    if action == "aggregate":
        try:
            return fleet_agg.aggregate()
        except Exception as e:
            return {"error": str(e), "traceback": traceback.format_exc()}

    if action == "trends":
        try:
            # Need at least one collect first
            fleet_agg.collect()
            return fleet_agg.trends()
        except Exception as e:
            return {"error": str(e), "traceback": traceback.format_exc()}

    if action == "productivity":
        try:
            return fleet_agg.productivity_score()
        except Exception as e:
            return {"error": str(e), "traceback": traceback.format_exc()}

    if action == "prometheus" or format == "prometheus":
        try:
            text = fleet_agg.to_prometheus()
            return {"format": "prometheus", "content_type": "text/plain; version=0.0.4", "body": text}
        except Exception as e:
            return {"error": str(e), "traceback": traceback.format_exc()}

    # Default: collect (json)
    try:
        nodes = fleet_agg.collect()
        return {
            "node_id": NODE_ID,
            "nodes": {nid: nm.to_dict() for nid, nm in nodes.items()},
        }
    except Exception as e:
        return {"error": str(e), "traceback": traceback.format_exc()}


def fleet_branches(format: str = "ascii") -> dict:
    """Visualize branch convergence across fleet machines.

    Scans remote branches for fleet prefixes (fleet/, mac1/, mac2/, mac3/,
    race/, worktree/), shows divergence from main, file conflicts between
    branches, and recommends merge order.

    Args:
        format: Output format — ascii (default), html, or json.
    """
    try:
        from multifleet.branch_convergence import BranchConvergence
    except ImportError:
        return {"error": "BranchConvergence module not available"}

    repo = str(REPO_ROOT.parent) if (REPO_ROOT.parent / ".git").exists() else str(REPO_ROOT)
    conv = BranchConvergence(repo)

    if format == "json":
        return conv.to_dict()
    elif format == "html":
        branches = conv.scan_branches()
        return {"html": conv.render_html(branches), "branch_count": len(branches)}
    else:
        branches = conv.scan_branches()
        recs = conv.merge_recommendations(branches)
        return {
            "visualization": conv.render_ascii(branches),
            "branch_count": len(branches),
            "recommendations": recs,
        }


# ── Vision Packets — structured brainstorm/adjudication/escalation ──────

_brainstorm_store: dict = {}  # in-memory: brainstorm_id -> BrainstormPacket
_surgeon_decision_store: dict = {}  # decision_id -> SurgeonDecisionPacket
_vision_contract_store: dict = {}  # contract_id -> VisionContract
_execution_ledger_store: dict = {}  # ledger_id -> ExecutionLedger
_escalation_store: dict = {}  # escalation_id -> ChiefEscalationPacket
_chief_decision_v2_store: dict = {}  # decision_id -> ChiefDecisionPacket


def _get_vision_packets():
    """Lazy import vision_packets module."""
    try:
        from multifleet.vision_packets import (
            BrainstormPacket, SurgeonDecisionPacket, VisionContract,
            ExecutionLedger, ChiefEscalationPacket, ChiefDecisionPacket,
        )
        return {
            "BrainstormPacket": BrainstormPacket,
            "SurgeonDecisionPacket": SurgeonDecisionPacket,
            "VisionContract": VisionContract,
            "ExecutionLedger": ExecutionLedger,
            "ChiefEscalationPacket": ChiefEscalationPacket,
            "ChiefDecisionPacket": ChiefDecisionPacket,
        }
    except Exception as e:
        logger.warning(f"vision_packets import failed: {e}")
        return None


def fleet_brainstorm(action: str, task_id: str = "", brainstorm_id: str = "",
                     title: str = "", description: str = "", pros: str = "",
                     cons: str = "", confidence: str = "", option_id: str = "",
                     surgeon: str = "", vote: str = "", reasoning: str = "",
                     objections: str = "", done_criteria: str = "",
                     node_id: str = "", reason: str = "", urgency: str = "medium",
                     decision: str = "", modifications: str = "",
                     memory_refs: str = "", step_description: str = "",
                     step_id: str = "", step_status: str = "",
                     evidence: str = "", escalation_id: str = "") -> dict:
    """Vision packet operations — structured brainstorm, adjudicate, lock, execute, escalate.

    Actions:
      create         — start a brainstorm for a task
      add_option     — add an option to a brainstorm
      adjudicate     — add a surgeon opinion; auto-checks consensus
      lock_vision    — lock a VisionContract from a decided brainstorm
      execute_init   — create an ExecutionLedger from a VisionContract
      execute_step   — advance a step in an ExecutionLedger
      escalate       — escalate to chief when consensus fails
      chief_decide   — record chief's authoritative decision
      status         — get current state of a brainstorm/task
      list           — list all active brainstorms
    """
    vp = _get_vision_packets()
    if vp is None:
        return {"error": "vision_packets module not available"}

    BrainstormPacket = vp["BrainstormPacket"]
    SurgeonDecisionPacket = vp["SurgeonDecisionPacket"]
    VisionContract = vp["VisionContract"]
    ExecutionLedger = vp["ExecutionLedger"]
    ChiefEscalationPacket = vp["ChiefEscalationPacket"]
    ChiefDecisionPacket = vp["ChiefDecisionPacket"]

    effective_node = node_id or NODE_ID

    if action == "create":
        if not task_id:
            return {"error": "task_id required for create"}
        bp = BrainstormPacket(task_id=task_id, node_id=effective_node)
        _brainstorm_store[bp.id] = bp
        return {"brainstorm_id": bp.id, "task_id": task_id, "status": "created"}

    elif action == "add_option":
        bp = _brainstorm_store.get(brainstorm_id)
        if bp is None:
            return {"error": f"brainstorm {brainstorm_id} not found"}
        if not title or not description:
            return {"error": "title and description required"}
        kwargs = {}
        if pros:
            kwargs["pros"] = [p.strip() for p in pros.split(",")]
        if cons:
            kwargs["cons"] = [c.strip() for c in cons.split(",")]
        if confidence:
            kwargs["confidence"] = float(confidence)
        opt = bp.add_option(title, description, **kwargs)
        return {"option_id": opt.id, "brainstorm_id": brainstorm_id,
                "total_options": len(bp.options)}

    elif action == "adjudicate":
        bp = _brainstorm_store.get(brainstorm_id)
        if bp is None:
            return {"error": f"brainstorm {brainstorm_id} not found"}
        if not surgeon or not vote or not reasoning:
            return {"error": "surgeon, vote, and reasoning required"}
        # Get or create the SurgeonDecisionPacket for this brainstorm
        sdp = None
        for s in _surgeon_decision_store.values():
            if s.brainstorm_id == brainstorm_id:
                sdp = s
                break
        if sdp is None:
            sdp = SurgeonDecisionPacket(brainstorm_id=brainstorm_id, task_id=bp.task_id)
            _surgeon_decision_store[sdp.id] = sdp
        obj_list = [o.strip() for o in objections.split(",")] if objections else []
        conf = float(confidence) if confidence else 0.5
        sdp.add_opinion(surgeon, vote, conf, reasoning, objections=obj_list)
        consensus = sdp.check_consensus()
        result = {
            "decision_id": sdp.id, "brainstorm_id": brainstorm_id,
            "opinions_count": len(sdp.opinions), "consensus": consensus,
        }
        if consensus:
            result["selected_option"] = sdp.selected_option
        else:
            sdp.requires_chief = len(sdp.opinions) >= 3
            result["requires_chief"] = sdp.requires_chief
        return result

    elif action == "lock_vision":
        if not brainstorm_id:
            return {"error": "brainstorm_id required"}
        bp = _brainstorm_store.get(brainstorm_id)
        if bp is None:
            return {"error": f"brainstorm {brainstorm_id} not found"}
        # Find the decision
        sdp = None
        for s in _surgeon_decision_store.values():
            if s.brainstorm_id == brainstorm_id:
                sdp = s
                break
        criteria = [c.strip() for c in done_criteria.split(",")] if done_criteria else []
        if not criteria:
            return {"error": "done_criteria required (comma-separated)"}
        approach = ""
        if sdp and sdp.selected_option:
            opt = bp.get_option(sdp.selected_option)
            approach = opt.title if opt else sdp.selected_option
        vc = VisionContract(
            task_id=bp.task_id,
            description=description or f"Vision for task {bp.task_id}",
            done_criteria=criteria,
            selected_approach=approach,
            surgeon_decision_id=sdp.id if sdp else "",
        )
        vc.lock(effective_node)
        _vision_contract_store[vc.id] = vc
        return {"contract_id": vc.id, "task_id": bp.task_id,
                "locked_by": effective_node, "done_criteria": criteria,
                "selected_approach": approach}

    elif action == "execute_init":
        contract_id = brainstorm_id  # reuse param for contract lookup
        if not contract_id:
            # Try finding by task_id
            for vc in _vision_contract_store.values():
                if vc.task_id == task_id:
                    contract_id = vc.id
                    break
        vc = _vision_contract_store.get(contract_id)
        if vc is None:
            return {"error": f"vision contract not found (id={contract_id}, task={task_id})"}
        el = ExecutionLedger(
            task_id=vc.task_id,
            vision_contract_id=vc.id,
            node_id=effective_node,
        )
        # Auto-create steps from done_criteria
        for criterion in vc.done_criteria:
            el.add_step(criterion)
        _execution_ledger_store[el.id] = el
        return {"ledger_id": el.id, "task_id": vc.task_id,
                "steps": len(el.steps), "progress": 0.0}

    elif action == "execute_step":
        ledger_id = brainstorm_id  # reuse param
        el = _execution_ledger_store.get(ledger_id)
        if el is None:
            return {"error": f"execution ledger {ledger_id} not found"}
        if not step_id or not step_status:
            return {"error": "step_id and step_status required"}
        found = el.advance_step(step_id, step_status, evidence=evidence)
        if not found:
            return {"error": f"step {step_id} not found in ledger {ledger_id}"}
        return {"ledger_id": ledger_id, "step_id": step_id,
                "status": step_status, "progress": el.progress,
                "is_blocked": el.is_blocked}

    elif action == "escalate":
        if not task_id:
            return {"error": "task_id required for escalate"}
        esc_reason = reason or "no_consensus"
        esc = ChiefEscalationPacket(
            task_id=task_id,
            from_node=effective_node,
            reason=esc_reason,
            urgency=urgency,
            brainstorm_id=brainstorm_id,
            evidence_refs=[e.strip() for e in evidence.split(",")] if evidence else [],
        )
        # Find surgeon decision if exists
        for s in _surgeon_decision_store.values():
            if s.brainstorm_id == brainstorm_id:
                esc.surgeon_decision_id = s.id
                break
        _escalation_store[esc.id] = esc
        return {"escalation_id": esc.id, "task_id": task_id,
                "reason": esc_reason, "urgency": urgency}

    elif action == "chief_decide":
        if not escalation_id:
            return {"error": "escalation_id required for chief_decide"}
        esc = _escalation_store.get(escalation_id)
        if esc is None:
            return {"error": f"escalation {escalation_id} not found"}
        if not decision:
            return {"error": "decision required (approve|reject|modify|defer|split)"}
        cd = ChiefDecisionPacket(
            escalation_id=escalation_id,
            task_id=esc.task_id,
            decision=decision,
            reasoning=reasoning or "",
            memory_refs=[r.strip() for r in memory_refs.split(",")] if memory_refs else [],
            modifications=[m.strip() for m in modifications.split(",")] if modifications else [],
        )
        _chief_decision_v2_store[cd.id] = cd
        return {"chief_decision_id": cd.id, "escalation_id": escalation_id,
                "task_id": esc.task_id, "decision": decision}

    elif action == "status":
        result = {"task_id": task_id, "brainstorms": [], "decisions": [],
                  "contracts": [], "ledgers": [], "escalations": []}
        for bp in _brainstorm_store.values():
            if not task_id or bp.task_id == task_id:
                result["brainstorms"].append(bp.to_dict())
        for sdp in _surgeon_decision_store.values():
            if not task_id or sdp.task_id == task_id:
                result["decisions"].append(sdp.to_dict())
        for vc in _vision_contract_store.values():
            if not task_id or vc.task_id == task_id:
                result["contracts"].append(vc.to_dict())
        for el in _execution_ledger_store.values():
            if not task_id or el.task_id == task_id:
                result["ledgers"].append(el.to_dict())
        for esc in _escalation_store.values():
            if not task_id or esc.task_id == task_id:
                result["escalations"].append(esc.to_dict())
        return result

    elif action == "list":
        return {
            "brainstorms": [{"id": bp.id, "task_id": bp.task_id, "options": len(bp.options)}
                            for bp in _brainstorm_store.values()],
            "count": len(_brainstorm_store),
        }

    else:
        return {"error": f"unknown action: {action}, expected: create|add_option|adjudicate|lock_vision|execute_init|execute_step|escalate|chief_decide|status|list"}


# ── Node Overlay + Acceptance Gate ──────────────────────────────


def fleet_overlay(node_id: str = "", action: str = "get") -> dict:
    """Query or update node overlay state."""
    if action == "get" and node_id:
        return _daemon_get(f"/overlay/{node_id}")
    elif action == "list" or not node_id:
        return _daemon_get("/overlays")
    elif action == "update" and node_id:
        return _daemon_post("/overlay", {"node_id": node_id})
    else:
        return {"error": f"Unknown action: {action}. Use: get, list, update"}


def fleet_accept(task_id: str, branch: str = "", context: str = "") -> dict:
    """Run acceptance gate for a task before merge."""
    body: dict = {"task_id": task_id, "branch": branch}
    if context:
        try:
            body["context"] = json.loads(context)
        except (json.JSONDecodeError, TypeError):
            body["context"] = {}
    return _daemon_post("/accept", body)


# ── Invariance Rules ──

def fleet_invariance(action: str = "check_all", rule_id: str = "",
                     state_json: str = "") -> dict:
    """Run GhostScan invariance rules — 7 machine-checkable protocol enforcement checks.

    Actions:
      check_all  — run all 7 invariance rules against state (default)
      check_one  — run a specific rule by ID (requires rule_id)
      list       — list all rule IDs and names

    Args:
        action: check_all, check_one, or list
        rule_id: specific rule ID for check_one (e.g. INV-001)
        state_json: JSON string of protocol state to check against
    """
    try:
        from multifleet.invariance_rules import InvarianceEngine
    except ImportError as e:
        return {"error": f"invariance_rules module not available: {e}"}

    engine = InvarianceEngine()

    if action == "list":
        return {
            "rules": [
                {"rule_id": rid, "name": name}
                for rid, name, _ in engine._rules
            ],
            "count": len(engine._rules),
        }

    # Parse state
    state: dict = {}
    if state_json:
        try:
            state = json.loads(state_json)
        except (json.JSONDecodeError, TypeError):
            return {"error": f"Invalid state_json: could not parse as JSON"}

    if action == "check_one":
        if not rule_id:
            return {"error": "rule_id required for check_one"}
        result = engine.check_one(rule_id, state)
        return result.to_dict()

    # Default: check_all
    results = engine.check_all(state)
    passed = sum(1 for r in results if r.passed)
    failed = len(results) - passed
    violations = [r.to_dict() for r in results if not r.passed]
    return {
        "total": len(results),
        "passed": passed,
        "failed": failed,
        "all_passed": failed == 0,
        "results": [r.to_dict() for r in results],
        "violations": violations,
    }


def _fleet_server_tool(action: str = "status", port: int = 8856) -> dict:
    """Start/stop/status the fleet web server."""
    try:
        from multifleet.web_server import fleet_server_tool
        return fleet_server_tool(action=action, port=port)
    except ImportError as e:
        return {"error": f"web_server module not available: {e}"}


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
    {
        "name": "fleet_dashboard",
        "description": "Visual fleet status with formatted node table and activity summary. Returns structured data with online/offline counts, session totals, and per-node status rows.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "fn": fleet_dashboard,
    },
    {
        "name": "fleet_work",
        "description": "View work backlog — active and pending work items for a node. Use node='all' for fleet-wide backlog.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node": {"type": "string", "description": "Node ID to check (default: this node). Use 'all' for fleet-wide.", "default": ""},
            },
            "required": [],
        },
        "fn": fleet_work,
    },
    {
        "name": "fleet_work_next",
        "description": "Get the next unclaimed work item from the backlog. Returns the highest-priority pending item.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node": {"type": "string", "description": "Node to query (default: this node)", "default": ""},
            },
            "required": [],
        },
        "fn": fleet_work_next,
    },
    {
        "name": "fleet_tasks",
        "description": "Active Claude sessions / agents across the entire fleet. Shows session IDs, idle times, branches, and working directories for all nodes.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "fn": fleet_tasks,
    },
    {
        "name": "fleet_wake",
        "description": "Wake a sleeping fleet node. Tries: (1) health check, (2) SSH + start daemon, (3) WoL magic packet. Returns wake status.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Node ID to wake (e.g. mac2, mac3)"},
            },
            "required": ["target"],
        },
        "fn": fleet_wake,
    },
    {
        "name": "fleet_historian",
        "description": "Trigger session historian on a fleet node. Extracts session gold, analyzes with LLM, cleans dead session cache.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Node to run historian on (default: this node)", "default": ""},
                "mode": {"type": "string", "description": "Mode: 'full' (extract+analyze+cleanup), 'fast' (extract+clean), 'cleanup' (cleanup only)", "default": "full"},
            },
            "required": [],
        },
        "fn": fleet_historian,
    },
    {
        "name": "fleet_channels",
        "description": "Channel state for all peers — shows which communication channels (NATS, HTTP, SSH, etc.) are up or down per peer, with latency and timestamps.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Node to query (default: this node)", "default": ""},
            },
            "required": [],
        },
        "fn": fleet_channels,
    },
    {
        "name": "fleet_sessions",
        "description": "Session gold from a node — active sessions with their extracted context from the historian. Shows what each session has been working on.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Node to query (default: this node)", "default": ""},
            },
            "required": [],
        },
        "fn": fleet_sessions,
    },
    {
        "name": "fleet_inject",
        "description": "Inject a message into an active Claude session via --resume. Auto-detects session if no ID given. Falls back to seed file if --resume fails.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Message to inject into the session"},
                "target": {"type": "string", "description": "Node to inject on (default: this node)", "default": ""},
                "session_id": {"type": "string", "description": "Specific session ID (optional — auto-detects if omitted)", "default": ""},
            },
            "required": ["message"],
        },
        "fn": fleet_inject,
    },
    {
        "name": "fleet_relay",
        "description": "Queue a message for chief store-and-forward relay. Message is held on the chief and delivered when the target node comes online (via heartbeat flush).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Destination node ID"},
                "type": {"type": "string", "description": "Message type: context, task, repair, etc.", "default": "context"},
                "subject": {"type": "string", "description": "Short subject line"},
                "body": {"type": "string", "description": "Full message body"},
                "priority": {"type": "integer", "description": "Priority 1-5 (1=highest, default 2)", "default": 2},
            },
            "required": ["target", "body"],
        },
        "fn": fleet_relay,
    },
    {
        "name": "fleet_discover",
        "description": "Discover fleet peers from config, peer registry, and heartbeat data. Probes reachability for all discovered nodes. Like a fleet-wide peer scan.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "fn": fleet_discover,
    },
    {
        "name": "fleet_send_smart",
        "description": "Session-aware smart routing. Unlike fleet_send (brute-force fallback), this probes the target first and picks the best delivery: --resume for active sessions, seed file for idle, background agent spawn, or relay queue.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Destination node ID"},
                "type": {"type": "string", "description": "Message type: context, task, alert, etc.", "default": "context"},
                "subject": {"type": "string", "description": "Short subject line"},
                "body": {"type": "string", "description": "Full message body"},
                "priority": {"type": "integer", "description": "Priority 1-5 (1=highest, default 2)", "default": 2},
            },
            "required": ["target", "body"],
        },
        "fn": fleet_send_smart,
    },
    {
        "name": "fleet_doctor",
        "description": "Run diagnostic health checks on this fleet node. Returns structured report with 6 checks: daemon health, NATS connectivity, peer reachability, keychain secrets, message store integrity, and Python dependencies.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "fn": fleet_doctor,
    },
    {
        "name": "fleet_onboard",
        "description": "Run fleet onboarding wizard — 10 checks (python, nats, daemon, keychain, superset, config, ssh, probes, surgeons, skills) with step-by-step setup guide. Use for new nodes or to verify a node is fully configured.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node_id": {"type": "string", "description": "Override node ID (default: auto-detect)", "default": ""},
            },
            "required": [],
        },
        "fn": fleet_onboard,
    },
    {
        "name": "fleet_gains_gate",
        "description": "Run fleet health verification gate. Returns structured pass/fail with per-check results. Use before merges, deployments, or phase transitions.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "quick": {"type": "boolean", "description": "Quick mode (6 checks) vs full mode (10 checks)", "default": False},
            },
            "required": [],
        },
        "fn": fleet_gains_gate,
    },
    {
        "name": "fleet_gate_check",
        "description": "Run a fleet invariance gate on demand. Gates: 'send' (validates packet schema/auth/rate), 'repair' (verifies repair is warranted), 'merge' (pre-merge lease/consensus/health). Returns structured pass/fail.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "gate": {"type": "string", "enum": ["send", "repair", "merge"], "description": "Which gate to run"},
                "target": {"type": "string", "description": "Target node (required for send/repair gates)"},
                "action": {"type": "string", "description": "Repair action (for repair gate)", "default": "restart_nats"},
                "branch": {"type": "string", "description": "Branch name (for merge gate)"},
                "quick": {"type": "boolean", "description": "Quick mode for merge gate gains sub-check", "default": True},
            },
            "required": ["gate"],
        },
        "fn": fleet_gate_check,
    },
    {
        "name": "fleet_lease",
        "description": "Manage resource leases — acquire, release, renew, or query ownership to prevent dual-work across nodes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["acquire", "release", "renew", "query", "list"], "description": "Lease action"},
                "resource_id": {"type": "string", "description": "Resource to lease (branch name, file path, task ID)"},
                "lease_type": {"type": "string", "enum": ["heartbeat", "short_task", "long_task", "exclusive"], "default": "short_task"},
            },
            "required": ["action"],
        },
        "fn": fleet_lease,
    },
    {
        "name": "fleet_rebuttal",
        "description": "Manage fleet proposals and rebuttals — structured consensus protocol for disagreements. Actions: propose (create), review, accept, rebut (with reason), counter (counter-propose), resolve (final decision), list (active), get (by ID).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["propose", "review", "accept", "rebut", "counter", "resolve", "list", "get"], "description": "Rebuttal action"},
                "proposal_id": {"type": "string", "description": "Proposal UUID (required for all actions except propose/list)"},
                "subject": {"type": "string", "description": "Proposal subject (for propose)"},
                "body": {"type": "string", "description": "Proposal body (for propose) or counter-proposal text (for counter/rebut)"},
                "reason": {"type": "string", "description": "Reason for accept/rebut"},
                "scope": {"type": "string", "description": "Scope: general, architecture, infra, breaking (for propose). For list: filter by state.", "default": "general"},
                "resolution": {"type": "string", "description": "Resolution text (for resolve)"},
            },
            "required": ["action"],
        },
        "fn": fleet_rebuttal,
    },
    {
        "name": "fleet_evidence",
        "description": "Tamper-evident evidence ledger — append-only log with hash chain for fleet decisions, lease grants, rebuttals, gains gate results, and repairs. Actions: record (append entry), query (search with filters), verify (check hash chain integrity), latest (recent entries), stats (ledger summary).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["record", "query", "verify", "latest", "stats"], "description": "Evidence ledger action"},
                "event_type": {"type": "string", "description": "Event type: merge_decision, lease_granted, rebuttal_resolved, gains_gate_result, channel_repair, consensus_reached"},
                "node_id": {"type": "string", "description": "Node that produced the evidence (for record/query filter)"},
                "subject": {"type": "string", "description": "Short description of the event (for record)"},
                "payload": {"type": "string", "description": "JSON payload with event details (for record)"},
                "since": {"type": "string", "description": "Unix timestamp — filter entries after this time (for query)"},
                "limit": {"type": "string", "description": "Max entries to return (default 50 for query, 10 for latest)", "default": "50"},
            },
            "required": ["action"],
        },
        "fn": fleet_evidence,
    },
    {
        "name": "fleet_race",
        "description": "Start a Competitive Branch Race — parallel problem-solving across fleet nodes. Each node works in an isolated worktree, then the fleet debates and converges to the best solution. The hero feature.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "The task/problem to race on"},
                "nodes": {"type": "string", "description": "Comma-separated node IDs (default: all available)"},
                "timeout": {"type": "string", "description": "Max seconds for the race (default 600)", "default": "600"},
            },
            "required": ["task"],
        },
        "fn": fleet_race,
    },
    {
        "name": "fleet_race_status",
        "description": "Check race progress or list all races. Actions: status (get race state by ID), list (all races).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "race_id": {"type": "string", "description": "Race ID to check"},
                "action": {"type": "string", "enum": ["status", "list"], "description": "Action: status (default) or list", "default": "status"},
            },
            "required": [],
        },
        "fn": fleet_race_status,
    },
    {
        "name": "fleet_liaison_status",
        "description": "Fleet Liaison Agent state — pending reviews, active rebuttals, unread message count, and one-line summary. Lightweight status check for the liaison background agent.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "fn": fleet_liaison_status,
    },
    {
        "name": "fleet_status_snapshot",
        "description": "Aggregated fleet status snapshot — daemon health, peers, liaison state, rebuttals, leases, recent events, and health dot (green/yellow/red). Single-source fleet status for IDE consumption.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "fn": fleet_status_snapshot,
    },
    {
        "name": "fleet_cross_rebuttal",
        "description": "Orchestrate cross-machine rebuttal protocol — the 4-phase cycle (task_brief -> local verdicts -> cross-critique -> chief synthesis). Actions: start, verdict, critique, status, synthesize, list.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["start", "verdict", "critique", "status", "synthesize", "list"], "description": "Rebuttal action"},
                "task_id": {"type": "string", "description": "Task ID (required for verdict, critique, status, synthesize)"},
                "payload": {"type": "string", "description": "JSON payload (task_brief for start, verdict/critique data for verdict/critique)"},
            },
            "required": ["action"],
        },
        "fn": fleet_cross_rebuttal,
    },
    {
        "name": "fleet_hierarchy",
        "description": "Manage fleet hierarchy — roles (chief/captain/worker), routing paths, NATS subjects, and chief elections. Actions: status, register, promote, elect, route, nats.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["status", "register", "promote", "elect", "route", "nats"], "description": "Hierarchy action"},
                "node_id": {"type": "string", "description": "Node ID (for register, promote, nats)"},
                "role": {"type": "string", "enum": ["chief", "captain", "worker"], "description": "Node role (for register)"},
                "captain_id": {"type": "string", "description": "Captain to assign worker to (for register)"},
                "cluster_id": {"type": "string", "description": "Cluster/region ID (for register, promote)"},
                "capabilities": {"type": "string", "description": "Comma-separated capabilities (for register)"},
                "from_node": {"type": "string", "description": "Source node (for route)"},
                "to_node": {"type": "string", "description": "Destination node (for route)"},
                "candidates": {"type": "string", "description": "Comma-separated candidate node IDs (for elect)"},
            },
            "required": ["action"],
        },
        "fn": fleet_hierarchy,
    },
    {
        "name": "fleet_metrics",
        "description": "Fleet-wide metrics aggregation — collect from all nodes, aggregate totals, compute trends (msg/min, errors/min), productivity score (0-100), or export Prometheus text. Actions: collect, aggregate, trends, productivity, prometheus.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "What to compute: collect (per-node), aggregate (fleet totals), trends (rates over time), productivity (score 0-100), prometheus (text exposition)", "default": "collect", "enum": ["collect", "aggregate", "trends", "productivity", "prometheus"]},
                "format": {"type": "string", "description": "Output format: 'prometheus' or 'json' (only used with collect action)", "default": "json"},
            },
            "required": [],
        },
        "fn": fleet_metrics,
    },
    {
        "name": "fleet_branches",
        "description": "Branch convergence visualization — shows how fleet branches relate to main. Detects file conflicts between branches, recommends merge order (oldest/least-conflict first). Formats: ascii (default), html, json.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "format": {"type": "string", "enum": ["ascii", "html", "json"], "description": "Output format (default: ascii)", "default": "ascii"},
            },
            "required": [],
        },
        "fn": fleet_branches,
    },
    {
        "name": "fleet_brainstorm",
        "description": "Vision packets — structured brainstorm, 3-surgeon adjudication, vision contracts, execution tracking, and chief escalation. The fleet 'thinks together' through typed artifacts.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["create", "add_option", "adjudicate", "lock_vision", "execute_init", "execute_step", "escalate", "chief_decide", "status", "list"], "description": "Vision packet action"},
                "task_id": {"type": "string", "description": "Task ID (for create, escalate, status)"},
                "brainstorm_id": {"type": "string", "description": "Brainstorm ID (for add_option, adjudicate, lock_vision; also used as contract/ledger ID for execute_*)"},
                "title": {"type": "string", "description": "Option title (for add_option)"},
                "description": {"type": "string", "description": "Option or contract description"},
                "pros": {"type": "string", "description": "Comma-separated pros (for add_option)"},
                "cons": {"type": "string", "description": "Comma-separated cons (for add_option)"},
                "confidence": {"type": "string", "description": "Confidence 0-1 (for add_option, adjudicate)"},
                "option_id": {"type": "string", "description": "Option ID reference"},
                "surgeon": {"type": "string", "enum": ["atlas", "cardiologist", "neurologist"], "description": "Surgeon role (for adjudicate)"},
                "vote": {"type": "string", "description": "Option ID to vote for, or 'abstain' (for adjudicate)"},
                "reasoning": {"type": "string", "description": "Surgeon reasoning or chief reasoning"},
                "objections": {"type": "string", "description": "Comma-separated objections (for adjudicate)"},
                "done_criteria": {"type": "string", "description": "Comma-separated done criteria (for lock_vision)"},
                "node_id": {"type": "string", "description": "Override node ID (default: this node)"},
                "reason": {"type": "string", "description": "Escalation reason (for escalate)"},
                "urgency": {"type": "string", "enum": ["low", "medium", "high", "critical"], "description": "Escalation urgency"},
                "decision": {"type": "string", "enum": ["approve", "reject", "modify", "defer", "split"], "description": "Chief decision (for chief_decide)"},
                "modifications": {"type": "string", "description": "Comma-separated modifications (for chief_decide)"},
                "memory_refs": {"type": "string", "description": "Comma-separated DAO/wisdom refs (for chief_decide)"},
                "escalation_id": {"type": "string", "description": "Escalation ID (for chief_decide)"},
                "step_id": {"type": "string", "description": "Step ID (for execute_step)"},
                "step_status": {"type": "string", "enum": ["pending", "in_progress", "done", "blocked", "skipped"], "description": "Step status (for execute_step)"},
                "evidence": {"type": "string", "description": "Evidence reference (for execute_step, escalate)"},
            },
            "required": ["action"],
        },
        "fn": fleet_brainstorm,
    },
    {
        "name": "fleet_overlay",
        "description": "Node overlay state — the 5-strip view of what each fleet node 'thinks' (epistemic, surgeon, invariant, evidence, probe). Actions: get (single node), list (all nodes), update.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node_id": {"type": "string", "description": "Node ID to query (required for get/update)"},
                "action": {"type": "string", "enum": ["get", "list", "update"], "description": "Overlay action (default: get)", "default": "get"},
            },
            "required": [],
        },
        "fn": fleet_overlay,
    },
    {
        "name": "fleet_accept",
        "description": "Acceptance gate — pre-merge verification that prevents shipping without evidence. Checks: probe findings, surgeon resolution, invariants, evidence exists, mirror drift, gains gate.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID to evaluate"},
                "branch": {"type": "string", "description": "Branch being merged"},
                "context": {"type": "string", "description": "JSON context: surgeon_state, invariant_state, mirror_drift_detected/acknowledged, probe_context"},
            },
            "required": ["task_id"],
        },
        "fn": fleet_accept,
    },
    {
        "name": "fleet_invariance",
        "description": "GhostScan invariance rules — 7 machine-checkable protocol enforcement checks. Actions: check_all (run all rules), check_one (specific rule by ID), list (show all rule IDs).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["check_all", "check_one", "list"], "description": "Action to perform (default: check_all)", "default": "check_all"},
                "rule_id": {"type": "string", "description": "Rule ID for check_one (e.g. INV-001)"},
                "state_json": {"type": "string", "description": "JSON string of protocol state to check against"},
            },
            "required": [],
        },
        "fn": fleet_invariance,
    },
    {
        "name": "fleet_orchestrate",
        "description": "Run the full task lifecycle — idea to wisdom in one command. Ties together: truth ladder (inbox), probes (baseline), fleet race or supervisor (execute), acceptance gate (review), truth promotion (wisdom), and evidence ledger (audit). Actions: run (full lifecycle), status (check task), list (all tasks).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["run", "status", "list"], "description": "Action: run (full lifecycle), status (check task), list (all tasks)", "default": "run"},
                "task": {"type": "string", "description": "Task description (required for run)"},
                "criteria": {"type": "string", "description": "Comma-separated done criteria (required for run)"},
                "parallel": {"type": "string", "description": "true to use fleet_race across nodes (default: false)", "default": "false"},
                "nodes": {"type": "string", "description": "Comma-separated node IDs for parallel execution"},
                "task_id": {"type": "string", "description": "Task ID for status action"},
            },
            "required": [],
        },
        "fn": fleet_orchestrate,
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
            "serverInfo": {"name": "fleet-nerve", "version": "3.1.0"},
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
        # Rate limit check (before any work)
        if _rate_limiter is not None:
            allowed, reason = _rate_limiter.check(tool_name)
            if not allowed:
                logger.warning("rate_limited tool=%s reason=%s", tool_name, reason)
                return {"content": [{"type": "text", "text": json.dumps({"error": reason, "rate_limited": True})}], "isError": True}
        t0 = time.monotonic()
        try:
            result = tool["fn"](**args)
            latency = round((time.monotonic() - t0) * 1000, 1)
            logger.info("tool=%s latency=%.1fms status=ok", tool_name, latency)
            audit = _get_audit()
            if audit:
                audit.record("tool_call", tool_name, args.get("target", ""), "ok", latency, metadata=_sanitize_args(args))
            text = json.dumps(result, indent=2) if not isinstance(result, str) else result
            return {"content": [{"type": "text", "text": text}]}
        except Exception as e:
            latency = round((time.monotonic() - t0) * 1000, 1)
            logger.error("tool=%s latency=%.1fms error=%s", tool_name, latency, e)
            _count_error(tool_name)
            audit = _get_audit()
            if audit:
                audit.record("error", tool_name, args.get("target", ""), "error", latency, error=str(e))
            error_payload = {"error": str(e), "traceback": traceback.format_exc()}
            return {"content": [{"type": "text", "text": json.dumps(error_payload, indent=2)}], "isError": True}

    if method == "notifications/initialized":
        return None  # No response needed

    return {"error": {"code": -32601, "message": f"Method not found: {method}"}}


def main():
    """Run as MCP server on stdin/stdout (JSON-RPC)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [fleet-mcp/%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,  # MCP uses stdout for JSON-RPC
    )
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
