#!/usr/bin/env python3
"""
Multi-Fleet Coordinator — cross-machine coordination layer.

Handles:
- Node registration / heartbeat
- Packet send/receive to chief
- Local packet queue (deferred when chief offline)
- Status reporting

Usage:
  python3 tools/multifleet_coordinator.py register
  python3 tools/multifleet_coordinator.py deregister
  python3 tools/multifleet_coordinator.py heartbeat
  python3 tools/multifleet_coordinator.py status
  python3 tools/multifleet_coordinator.py send <packet_type> <json_payload>
  python3 tools/multifleet_coordinator.py flush  # flush queued packets to chief
"""

import json
import logging
import os
import sys
import time
import socket
import subprocess

logger = logging.getLogger("fleet-nerve.coordinator")
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
CONFIG_PATH = REPO_ROOT / ".multifleet" / "config.json"
LOCAL_DIR = REPO_ROOT / ".contextdna" / "local"
QUEUE_DIR = REPO_ROOT / ".contextdna" / "packets"
NODE_STATE_PATH = LOCAL_DIR / "node_state.json"


def load_config():
    if not CONFIG_PATH.exists():
        return {}
    with open(CONFIG_PATH) as f:
        return json.load(f)


def get_node_id():
    """Determine this node's ID from hostname or local config."""
    # Check for explicit override
    override = os.environ.get("MULTIFLEET_NODE_ID")
    if override:
        return override
    # Map known hostnames
    hostname = socket.gethostname().lower()
    hostname_map = {
        "mac1": "mac1",
        "mac2": "mac2",
        "mac3": "mac3",
    }
    for key, node_id in hostname_map.items():
        if key in hostname:
            return node_id
    return hostname


def surgeon_status():
    """Quick probe: are cardiologist + neurologist reachable?"""
    status = {"cardiologist": False, "neurologist": False}
    # Cardiologist: check Context_DNA_OPENAI
    if os.environ.get("Context_DNA_OPENAI") or os.environ.get("Context_DNA_Deepseek"):
        status["cardiologist"] = True
    # Neurologist: try ollama endpoint
    try:
        import urllib.request
        req = urllib.request.urlopen("http://localhost:11434/api/tags", timeout=2)
        if req.status == 200:
            status["neurologist"] = True
    except Exception as e:
        logger.debug("Ollama neurologist check failed: %s", e)
        # Try MLX fallback
        try:
            req = urllib.request.urlopen("http://127.0.0.1:5044/v1/models", timeout=2)
            if req.status == 200:
                status["neurologist"] = True
        except Exception as e2:
            logger.debug("MLX neurologist fallback also failed: %s", e2)
            pass
    return status


def build_heartbeat(config):
    node_id = get_node_id()
    # Get active branch
    branch = "unknown"
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, cwd=REPO_ROOT
        )
        if result.returncode == 0:
            branch = result.stdout.strip()
    except Exception as e:
        logger.debug("Git branch detection failed, defaulting to 'unknown': %s", e)
        pass

    return {
        "type": "heartbeat",
        "nodeId": node_id,
        "fleetId": config.get("fleetId", "contextdna-main"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "active",
        "activeWorkspace": str(REPO_ROOT),
        "activeBranch": branch,
        "surgeonStatus": surgeon_status(),
    }


def send_to_chief(packet, config):
    """POST packet to chief ingest endpoint. Returns True on success."""
    ingest_url = config.get("chief", {}).get("ingestUrl")
    if not ingest_url:
        return False
    try:
        import urllib.request
        data = json.dumps(packet).encode("utf-8")
        req = urllib.request.Request(
            ingest_url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status < 300
    except Exception as e:
        return False


def queue_packet(packet):
    """Queue packet locally for deferred delivery when chief is offline."""
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    ts = int(time.time() * 1000)
    path = QUEUE_DIR / f"{ts}_{packet['type']}.json"
    with open(path, "w") as f:
        json.dump(packet, f, indent=2)
    return path


def flush_queue(config):
    """Attempt to send all queued packets to chief."""
    if not QUEUE_DIR.exists():
        print("[coordinator] No queued packets.")
        return
    queued = sorted(QUEUE_DIR.glob("*.json"))
    if not queued:
        print("[coordinator] Queue empty.")
        return
    sent = 0
    failed = 0
    for p in queued:
        with open(p) as f:
            packet = json.load(f)
        if send_to_chief(packet, config):
            p.unlink()
            sent += 1
        else:
            failed += 1
    print(f"[coordinator] Flushed {sent}/{len(queued)} packets. {failed} still queued.")


def save_node_state(state):
    LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    with open(NODE_STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def print_status(config):
    node_id = get_node_id()
    surgeons = surgeon_status()
    chief = config.get("chief", {})
    queued = len(list(QUEUE_DIR.glob("*.json"))) if QUEUE_DIR.exists() else 0

    # Check if we're the chief
    is_chief = (node_id == chief.get("nodeId", ""))

    print(f"Multi-Fleet Node Status")
    print(f"  Node ID    : {node_id} {'[CHIEF]' if is_chief else '[worker]'}")
    print(f"  Fleet ID   : {config.get('fleetId', 'contextdna-main')}")
    print(f"  Chief      : {chief.get('host', 'unknown')} ({chief.get('ingestUrl', 'N/A')})")
    print(f"  Surgeons   : cardiologist={'OK' if surgeons['cardiologist'] else 'MISSING'}, neurologist={'OK' if surgeons['neurologist'] else 'MISSING'}")
    print(f"  Queued pkts: {queued}")

    # Check chief reachability
    chief_reachable = send_to_chief({"type": "ping", "nodeId": node_id, "fleetId": config.get("fleetId", "")}, config) if not is_chief else True
    print(f"  Chief reach: {'YES' if chief_reachable else 'OFFLINE (packets will queue)'}")


def main():
    config = load_config()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"

    if cmd == "register":
        hb = build_heartbeat(config)
        if send_to_chief(hb, config):
            print(f"[coordinator] Registered with chief: {config.get('chief', {}).get('host', 'unknown')}")
        else:
            p = queue_packet(hb)
            print(f"[coordinator] Chief offline — registration queued: {p.name}")
        save_node_state(hb)

    elif cmd == "deregister":
        node_id = get_node_id()
        packet = {
            "type": "heartbeat",
            "nodeId": node_id,
            "fleetId": config.get("fleetId", "contextdna-main"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": "offline",
            "activeWorkspace": str(REPO_ROOT),
            "activeBranch": "N/A",
            "surgeonStatus": {"cardiologist": False, "neurologist": False},
        }
        send_to_chief(packet, config)  # best-effort

    elif cmd == "heartbeat":
        hb = build_heartbeat(config)
        if send_to_chief(hb, config):
            print(f"[coordinator] Heartbeat sent")
        else:
            queue_packet(hb)
            print(f"[coordinator] Chief offline — heartbeat queued")
        save_node_state(hb)

    elif cmd == "flush":
        flush_queue(config)

    elif cmd == "status":
        print_status(config)

    elif cmd == "send" and len(sys.argv) >= 4:
        packet_type = sys.argv[2]
        payload = json.loads(sys.argv[3])
        node_id = get_node_id()
        packet = {
            "type": packet_type,
            "nodeId": node_id,
            "fleetId": config.get("fleetId", "contextdna-main"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **payload,
        }
        if send_to_chief(packet, config):
            print(f"[coordinator] Packet sent: {packet_type}")
        else:
            p = queue_packet(packet)
            print(f"[coordinator] Chief offline — queued: {p.name}")
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
