---
name: fleet-status
description: Quick fleet health check — who's online, who's idle, what's everyone working on
---

# Fleet Status

Instant situational awareness of the entire fleet. One command shows every node's state, active sessions, running tasks, and connectivity health.

## How It Works

1. Your local daemon collects UDP heartbeats from all peers (every 10s)
2. Heartbeats carry node status, git branch, session count, and task state
3. `/fleet/live` merges local state + cached heartbeats into a unified view
4. If a heartbeat is stale (>60s), the daemon does a 1s HTTP probe as fallback

## Quick Check

```bash
# One-liner summary — the most useful command in the fleet
curl -s http://127.0.0.1:8855/fleet/live | python3 -c "import sys,json;print(json.load(sys.stdin)['fleetStatus'])"
# -> "3/3 online: mac1 active, mac2 idle 5m, mac3 working"

# Full node details (JSON)
curl -s http://127.0.0.1:8855/fleet/live | python3 -m json.tool

# This node only
curl -s http://127.0.0.1:8855/health | python3 -m json.tool

# Peer registry with heartbeat timestamps
curl -s http://127.0.0.1:8855/peers | python3 -m json.tool
```

### Via the fleet status script

```bash
bash scripts/multi-fleet-status.sh
```

### Via Python

```python
import requests, json

def fleet_status():
    """Get fleet-wide status from local daemon."""
    try:
        r = requests.get("http://127.0.0.1:8855/fleet/live", timeout=3)
        data = r.json()
        print(data["fleetStatus"])
        for node in data.get("nodes", []):
            name = node["name"]
            status = node["status"]
            idle = node.get("idle_s", 0)
            tasks = node.get("activeTasks", 0)
            branch = node.get("git", {}).get("branch", "?")
            print(f"  {name}: {status} | idle {idle}s | tasks {tasks} | {branch}")
        return data
    except requests.ConnectionError:
        print("ERROR: Fleet Nerve daemon not running on localhost:8855")
        return None
```

## Status Classifications

| Status | Meaning | Condition |
|--------|---------|-----------|
| `working` | Has active fleet tasks running | activeTasks > 0 |
| `active` | Session recently active | idle_s < 300 (5 min) |
| `idle Nm` | Session quiet for N minutes | 300 <= idle_s < 900 |
| `dormant Nm` | Session quiet for N+ minutes | idle_s >= 900 (15 min) |
| `offline` | No heartbeat received | No UDP heartbeat in 60s AND HTTP probe fails |

## Full Response Format

```json
{
  "fleetStatus": "3/3 online: mac1 active, mac2 idle 5m, mac3 working",
  "timestamp": "2026-04-05T10:30:00Z",
  "nodes": [
    {
      "name": "mac1",
      "role": "chief",
      "status": "active",
      "idle_s": 42,
      "activeSessions": 1,
      "activeTasks": 0,
      "git": {
        "branch": "main",
        "lastCommit": "abc1234",
        "lastCommitMsg": "feat: fleet worker pool"
      },
      "surgeons": {
        "cardiologist": true,
        "neurologist": true,
        "atlas": true
      },
      "heartbeat": {
        "lastSeen": "2026-04-05T10:29:58Z",
        "transport": "udp",
        "stale": false
      }
    },
    {
      "name": "mac2",
      "role": "worker",
      "status": "idle 5m",
      "idle_s": 312,
      "activeSessions": 1,
      "activeTasks": 0,
      "git": {
        "branch": "feature/auth",
        "lastCommit": "def5678"
      }
    },
    {
      "name": "mac3",
      "role": "worker",
      "status": "working",
      "idle_s": 5,
      "activeSessions": 2,
      "activeTasks": 1,
      "taskSummary": "Running cross-exam on auth module (45s)"
    }
  ]
}
```

## Channel Health Display

Fleet status includes per-peer channel state. Check which channels are operational:

```bash
# Per-peer channel state table
curl -s http://127.0.0.1:8855/channels | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(f'{'Peer':<10} {'P1 NATS':<10} {'P2 HTTP':<10} {'P3 Chief':<10} {'P4 Seed':<10} {'P5 SSH':<10}')
print('-' * 60)
for peer, ch in data.get('peers', {}).items():
    row = f'{peer:<10}'
    for p in ['p1_nats','p2_http','p3_chief','p4_seed','p5_ssh']:
        st = ch.get(p, {}).get('status', '?')
        marker = 'OK' if st == 'pass' else 'BROKEN' if st == 'fail' else st.upper()
        row += f' {marker:<10}'
    print(row)
"
```

### Interpreting Channel Health

| State | Meaning | Action |
|-------|---------|--------|
| All P1+P2 OK | Healthy fleet | None needed |
| P1 BROKEN, P2 OK | NATS down | Check NATS server on chief, use `fleet-repair` |
| P1+P2 BROKEN | Direct channels down | Trigger `fleet-repair`, check firewall/tunnels |
| Only P5+ working | Network severely degraded | Use `fleet-tunnel` to establish SSH tunnels |

**BROKEN channels require repair.** The fleet is not healthy until ALL nodes have P1+P2 operational. When you see BROKEN channels in status output, recommend `fleet-repair` to the user or trigger it automatically.

## Data Sources

| Source | Freshness | How |
|--------|-----------|-----|
| Local node | Instant | Direct API call to own daemon |
| Remote peers | ~10s | Cached UDP heartbeats (sent every 10s) |
| Fallback | ~1s | HTTP probe if UDP heartbeat stale >60s |

## What Each Node Reports

- **Active session count** — how many Claude Code sessions are running
- **Idle time** — seconds since last JSONL activity or fleet message
- **Git branch and last commit** — what code the node is working on
- **3-Surgeons availability** — cardiologist (external LLM), neurologist (local LLM), atlas
- **Active task count** — fleet task agents currently running
- **Task log tails** — last line of output from running task agents

## Error Handling

### Daemon not running

```bash
# If curl gets connection refused
$ curl -s http://127.0.0.1:8855/fleet/live
# curl: (7) Failed to connect to 127.0.0.1 port 8855

# Fix: start the daemon
MULTIFLEET_NODE_ID=$(hostname -s) python3 tools/fleet_nerve_daemon.py --port 8855 &

# Or via LaunchAgent
launchctl kickstart -k gui/$(id -u)/com.multifleet.nerve
```

### Stale heartbeats

If a node shows `offline` but you know it's running, the UDP heartbeat may be blocked:

```bash
# Direct HTTP probe to verify
curl -s --max-time 2 http://<target-ip>:8855/health | python3 -m json.tool

# If HTTP works but UDP doesn't, the network is blocking UDP broadcast
# Solution: check firewall rules or use SSH tunnels (fleet-tunnel skill)
```

### Partial fleet visibility

If you only see some nodes, check peer configuration:

```bash
# View configured peers
cat .multifleet/config.json | python3 -c "
import sys, json
cfg = json.load(sys.stdin)
for name, info in cfg.get('nodes', {}).items():
    print(f'{name}: {info.get(\"host\", \"?\")} ({info.get(\"role\", \"?\")})')
"
```

## Integration with Other Skills

- **productivity-view**: Use `/fleet-dashboard` for a rich visual dashboard (fleet-status is the raw data behind it)
- **fleet-wake**: If a node shows `offline`, use fleet-wake to bring it online
- **fleet-send**: Check status before sending to know if the target is active, idle, or offline
- **fleet-task**: Check if a node is `working` before dispatching more tasks (avoid overloading)
- **fleet-watchdog**: Automated version of fleet-status — continuous monitoring with auto-repair triggers

## When to Use This Skill

- Before dispatching work — is the target node online and available?
- Debugging connectivity — which nodes can this machine see?
- Situational awareness — what is everyone working on right now?
- After network changes — did all nodes reconnect?
- Verifying fleet health after a wake or repair operation
- **Idle protocol**: if your session is idle >5min, run fleet-status to find work or broken channels to repair
