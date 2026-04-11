---
name: fleet-wake
description: Wake a sleeping or idle machine — WoL magic packet, SSH ping, or caffeinate with 3-strategy escalation
---

# Fleet Wake

Three strategies to bring a node back online, tried in order. Once awake, queued messages and tasks are delivered automatically.

## How It Works

1. Health check — is the target already up? If so, done.
2. SSH ping + caffeinate — is it awake but might sleep? Keep it alive.
3. Wake-on-LAN magic packet — is it sleeping? Send the magic packet and poll until it responds.

After any successful wake, the target's Fleet Nerve daemon auto-starts via LaunchAgent and processes queued messages.

## Wake Strategies

### Strategy 1: Health Check (already up?)

```bash
curl -s --max-time 2 http://<target-ip>:8855/health
```

If this responds, the node is awake. No action needed. This is always tried first to avoid unnecessary wake attempts.

### Strategy 2: SSH Ping + Caffeinate (awake but idle)

```bash
# Prevent sleep for 1 hour (3600 seconds)
ssh <user>@<target-ip> "caffeinate -dims -t 3600 &"

# Then verify daemon is running
ssh <user>@<target-ip> "curl -sf http://127.0.0.1:8855/health"
```

If the machine is awake but might sleep soon (e.g., lid about to close), `caffeinate` prevents sleep. The `-dims` flags prevent display, idle, disk, and system sleep.

### Strategy 3: Wake-on-LAN (fully sleeping)

```python
import socket

def wake_on_lan(mac_address: str, broadcast: str = '<broadcast>'):
    """Send a WoL magic packet to wake a sleeping machine."""
    mac_bytes = bytes.fromhex(mac_address.replace(":", "").replace("-", ""))
    magic = b'\xff' * 6 + mac_bytes * 16
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.sendto(magic, (broadcast, 9))
    sock.close()
```

Sends a magic packet to wake a fully sleeping machine. Requires:
- WoL enabled in macOS System Settings > Energy Saver > "Wake for network access"
- The target's MAC address in the peer registry or config
- Both machines on the same LAN (WoL does not cross routers)

## Using Fleet Wake

### Via CLI

```bash
# Wake a single node
python3 tools/fleet_nerve_send.py --wake <node-id>

# Wake ALL peers (before a fleet-wide operation)
python3 tools/fleet_nerve_send.py --wake-fleet
```

### Via daemon API

```bash
# Wake a specific node
curl -sf -X POST http://127.0.0.1:8855/wake -H "Content-Type: application/json" \
  -d '{"target": "<node-id>"}'

# Response:
# {"target": "mac3", "strategy": "wol", "awake": true, "waitedSeconds": 12}
```

### Via Python

```python
import requests

def wake_node(target: str) -> dict:
    """Wake a fleet node using the local daemon's wake endpoint."""
    try:
        r = requests.post("http://127.0.0.1:8855/wake",
                         json={"target": target}, timeout=65)
        return r.json()
    except requests.Timeout:
        return {"target": target, "awake": False, "error": "wake timed out after 60s"}
    except requests.ConnectionError:
        return {"target": target, "awake": False, "error": "local daemon not running"}
```

### Via fleet-swarm (automatic)

The `fleet-swarm.sh` orchestrator auto-wakes all peers before dispatching tasks:

```bash
# This wakes all nodes, then dispatches the task
bash scripts/fleet-swarm.sh "Run the full test suite across all machines"
```

## Wake Flow

```
Want to reach target node
  -> Strategy 1: GET target:8855/health
     |-- responds? Done. Node is awake.
     |-- no response (timeout 2s)
  -> Strategy 2: SSH caffeinate
     |-- connected? Wait 5s for daemon to start. Check health again. Done.
     |-- connection refused (machine sleeping or SSH down)
  -> Strategy 3: Send WoL magic packet
     |-- Poll health every 5s (max 60s)
     |-- Target wakes -> LaunchAgent starts Fleet Nerve -> health responds
     |-- Deliver queued messages
     |-- 60s with no response -> give up, report failure
```

## After Wake

Once a node is awake:
1. **LaunchAgent** starts the Fleet Nerve daemon automatically
2. **Outbox replay** — any queued messages from the chief relay or peer outboxes are delivered
3. **Heartbeat resumes** — UDP heartbeats start flowing, peers update their status caches
4. **Session detection** — if VS Code was open, existing sessions are detected and reported

## Configuration

MAC addresses for WoL are stored in `.multifleet/config.json`:

```json
{
  "nodes": {
    "mac1": {"host": "192.168.1.165", "user": "alice", "mac": "AA:BB:CC:DD:EE:01"},
    "mac2": {"host": "192.168.1.254", "user": "bob",   "mac": "AA:BB:CC:DD:EE:02"},
    "mac3": {"host": "192.168.1.191", "user": "charlie","mac": "AA:BB:CC:DD:EE:03"}
  },
  "wake": {
    "pollIntervalSeconds": 5,
    "maxWaitSeconds": 60,
    "caffeinateHours": 1,
    "broadcastAddress": "255.255.255.255"
  }
}
```

| Key | Default | Description |
|-----|---------|-------------|
| `nodes[].mac` | required | Target MAC address for WoL |
| `wake.pollIntervalSeconds` | `5` | Health poll interval after WoL send |
| `wake.maxWaitSeconds` | `60` | Give up after this many seconds |
| `wake.caffeinateHours` | `1` | How long caffeinate keeps machine awake |
| `wake.broadcastAddress` | `255.255.255.255` | Broadcast address for WoL packets |

## Error Handling

### WoL not working

```bash
# Verify WoL is enabled on the target (when it's awake)
ssh <user>@<target-ip> "system_profiler SPPowerDataType | grep -i wake"

# Common causes:
# - "Wake for network access" disabled in System Settings
# - WiFi doesn't support WoL reliably (use Ethernet if possible)
# - Machines on different subnets (WoL is LAN-only)
# - Wrong MAC address in config
```

### SSH connection refused

The machine may be in deep sleep or powered off (not just sleeping):

```bash
# Check if the machine is reachable at all
ping -c 1 -t 2 <target-ip>

# If ping works but SSH doesn't, sshd may be stopped
# This requires physical access to fix
```

### Machine powered off (not sleeping)

WoL only works for sleeping machines, not powered-off ones. If the machine is powered off, no remote wake is possible. The message will be queued and delivered when the machine next comes online.

### Caffeinate already running

Multiple caffeinate processes are harmless — the longest one wins. No special handling needed.

## Integration with Other Skills

- **fleet-status**: Check if a node is `offline` before attempting a wake
- **fleet-send**: After waking a node, send your message — queued messages are also auto-delivered
- **fleet-task**: The fleet-swarm orchestrator auto-wakes nodes before dispatching tasks
- **fleet-check**: If wake succeeds but communication fails, run fleet-check to diagnose which channels are broken
- **fleet-watchdog**: Watchdog auto-triggers wake when a peer's heartbeat goes stale

## When to Use This Skill

- A node shows `offline` in fleet-status and you need it for a task
- Before a fleet-wide operation (fleet-swarm auto-handles this)
- Keeping a node alive during a long fleet operation (caffeinate strategy)
- After hours — waking a home machine from the office for remote work
- Debugging: verifying WoL is properly configured across the fleet
