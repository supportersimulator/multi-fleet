# Multi-Fleet

Cross-machine AI collaboration for Claude Code. Real-time peer-to-peer messaging, session-aware task agents, and 7-priority fallback delivery across any number of machines.

## Quick Start

### 1. Configure your fleet

```bash
cp .multifleet/config.template.json .multifleet/config.json
```

Edit `config.json` — add one entry per node:

```json
{
  "fleetId": "my-fleet",
  "chief": {"nodeId": "node1", "host": "${CHIEF_IP}"},
  "nodes": {
    "node1": {"host": "${CHIEF_IP}", "role": "chief", "user": "alice"},
    "node2": {"host": "${WORKER_1_IP}", "role": "worker", "user": "bob"},
    "node3": {"host": "${WORKER_2_IP}", "role": "worker", "user": "charlie"}
  }
}
```

### 2. Install on each machine

```bash
# Set your node identity
export MULTIFLEET_NODE_ID=node2

# Start the daemon
python3 tools/fleet_nerve_nats.py serve

# Install the Claude Code plugin
# Add to ~/.claude/settings.json:
#   enabledPlugins: { "multi-fleet@multi-fleet-marketplace": true }
#   extraKnownMarketplaces: { "multi-fleet-marketplace": { "source": { "source": "directory", "path": "<repo>/multi-fleet" }}}
```

### 3. Send a message

```bash
# Via CLI
python3 tools/fleet_nerve_nats.py send node1 "Hello from node2"

# Via HTTP (daemon must be running)
curl -X POST http://127.0.0.1:8855/message \
  -H "Content-Type: application/json" \
  -d '{"type":"context","to":"node1","payload":{"subject":"Hello","body":"From node2"}}'

# Test the 7-priority fallback chain
bash scripts/fleet-test-fallback.sh node1
```

## Architecture

```
Chief Node                    Worker Nodes
+-----------+                 +-----------+
| NATS      |<--- P1 NATS -->| Daemon    |
| Server    |<--- P2 HTTP -->| :8855     |
| Ingest    |<--- P3 Chief ->|           |
| :8844     |     P4 Seed    |           |
+-----------+     P5 SSH     +-----------+
                  P6 WoL
                  P7 Git
```

Every message tries all 7 channels in priority order. First success wins.

## Communication Protocol

| Priority | Channel | Latency | Router-proof? |
|----------|---------|---------|---------------|
| P1 | NATS pub/sub | <100ms | With SSH tunnel |
| P2 | HTTP direct | <1s | No |
| P3 | Chief relay | 1-2s | No |
| P4 | Seed file | Next prompt | Via SSH |
| P5 | SSH direct | 2-5s | Yes |
| P6 | Wake-on-LAN | 10-60s | LAN only |
| P7 | Git push | Async | Yes (internet) |

**Self-healing:** When a message succeeds on P3+, it means P1-P2 are broken. The system automatically sends a repair request via the working channel.

## Features

- **7-priority fallback** — messages always deliver, even on hostile networks
- **VS Code detection** — knows if target has active session, idle, or closed
- **Session historian** — task agents inherit context from target's active session
- **Self-healing** — broken channels are automatically repaired via working ones
- **Scales to 100+** — peers discovered dynamically from config + heartbeats
- **No hardcoded values** — all from config.json or environment variables
- **Open-source ready** — template config, no IP leaks, clean separation

## Plugin Skills

| Skill | Description |
|-------|-------------|
| productivity-view | Live dashboard of fleet activity |
| fleet-send | Send messages to peers |
| fleet-status | Fleet health check |
| fleet-task | Dispatch autonomous work |
| fleet-wake | Wake sleeping machines |
| using-multi-fleet | Architecture overview |

## SSH Tunnels (for restricted networks)

If your router blocks non-SSH ports:

```bash
# On each worker node
bash scripts/fleet-tunnel.sh install
```

This creates persistent autossh tunnels through port 22. NATS, HTTP, everything flows through the tunnel. Survives reboots via LaunchAgent.

## Requirements

- Python 3.10+
- `nats-py` (`pip install nats-py`)
- SSH key access between nodes
- NATS server on chief node (`brew install nats-server` or `apt install nats-server`)
