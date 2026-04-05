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

# Superrepo fallback: if running inside the superrepo, also add its root
# so that `tools.*` imports work for modules not yet in the multifleet package.
_SUPERREPO_ROOT = REPO_ROOT.parent
if (_SUPERREPO_ROOT / "tools").is_dir():
    sys.path.insert(1, str(_SUPERREPO_ROOT))

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
        except Exception:
            pass
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
        except Exception:
            pass
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
        try:
            from multifleet.worker import FleetWorkerManager
        except ImportError:
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
        try:
            from multifleet.packet_store import PacketStore
        except ImportError:
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
    except Exception:
        pass

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
            all_tasks.append({
                "node": name,
                "session_id": s.get("sessionId", s.get("id", "?"))[:12],
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
    except Exception:
        pass

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
        except Exception:
            pass

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
    import uuid

    node_id = _get_node_id()
    config = _load_config()
    chief_id = config.get("chief", {}).get("nodeId", "")

    message = {
        "id": str(uuid.uuid4()),
        "type": type,
        "from": node_id,
        "to": target,
        "priority": priority,
        "payload": {"subject": subject or body[:60], "body": body},
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

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
    except Exception:
        pass

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
    import uuid

    node_id = _get_node_id()
    message = {
        "id": str(uuid.uuid4()),
        "type": type,
        "from": node_id,
        "to": target,
        "priority": priority,
        "payload": {"subject": subject or body[:60], "body": body},
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

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
