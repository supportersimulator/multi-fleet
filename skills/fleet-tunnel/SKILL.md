---
name: fleet-tunnel
description: SSH tunnel management for restricted networks — setup, start, stop, status
---

# Fleet Tunnel

When your network blocks non-SSH ports (double-NAT, corporate firewall, carrier-grade NAT), SSH tunnels carry all fleet traffic through port 22. NATS, HTTP, everything flows through the tunnel.

## Architecture

```
Worker Node                          Chief Node
+-----------+    SSH tunnel :22    +-----------+
| localhost |=====================>| NATS :4222|
|  :4222    |--- tunnel --------->| HTTP :8844|
|  :8855    |--- tunnel --------->| :8855     |
+-----------+                     +-----------+
```

The worker's local ports forward through SSH to the chief. All fleet traffic uses `localhost` addresses — no direct port exposure needed.

## Setup

### Install tunnels on a worker node

```bash
bash scripts/fleet-tunnel.sh install
```

This will:
1. Read chief host and ports from `.multifleet/config.json`
2. Create an autossh tunnel configuration
3. Install a LaunchAgent (macOS) or systemd unit (Linux) for persistence
4. Start the tunnel immediately
5. Verify connectivity through the tunnel

### Manual setup (if script unavailable)

```bash
# Forward NATS and HTTP through SSH to chief
autossh -M 0 -f -N \
  -L 4222:localhost:4222 \
  -L 8844:localhost:8844 \
  -o "ServerAliveInterval=30" \
  -o "ServerAliveCountMax=3" \
  ${FLEET_CHIEF_USER}@${FLEET_CHIEF_HOST}
```

## Management

```bash
# Check tunnel status
bash scripts/fleet-tunnel.sh status
# → tunnel: UP (pid 12345, uptime 2d 5h, latency 12ms)

# Stop tunnels
bash scripts/fleet-tunnel.sh stop

# Restart tunnels (after config change or network switch)
bash scripts/fleet-tunnel.sh restart

# Uninstall (remove LaunchAgent/systemd unit)
bash scripts/fleet-tunnel.sh uninstall
```

### Status via daemon API

```bash
# Check tunnel health from the fleet nerve daemon
curl -s http://127.0.0.1:8855/tunnel/status | python3 -m json.tool
```

Response:
```json
{
  "tunnelActive": true,
  "pid": 12345,
  "uptimeSeconds": 180300,
  "chiefReachable": true,
  "natsForwarded": true,
  "httpForwarded": true,
  "latencyMs": 12
}
```

## Configuration

In `.multifleet/config.json`:

```json
{
  "tunnel": {
    "enabled": false,
    "sshPort": 22,
    "forwards": [
      {"local": 4222, "remote": 4222, "desc": "NATS"},
      {"local": 8844, "remote": 8844, "desc": "Chief relay"}
    ],
    "keepAliveInterval": 30,
    "keepAliveCount": 3,
    "autoReconnect": true
  }
}
```

| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `false` | Enable SSH tunnel mode |
| `sshPort` | `22` | SSH port on the chief node |
| `forwards` | NATS + Chief | Port forwarding pairs (local:remote) |
| `keepAliveInterval` | `30` | SSH keepalive interval in seconds |
| `keepAliveCount` | `3` | Missed keepalives before reconnect |
| `autoReconnect` | `true` | autossh reconnects on failure |

## Environment Variables

| Variable | Description |
|----------|-------------|
| `FLEET_CHIEF_HOST` | Chief node hostname/IP (overrides config.json) |
| `FLEET_CHIEF_USER` | SSH user on chief (overrides config.json) |
| `FLEET_TUNNEL_SSH_PORT` | SSH port override |

## Prerequisites

- `autossh` installed (`brew install autossh` / `apt install autossh`)
- SSH key access to the chief node (no password prompts)
- Port 22 open between worker and chief

## When to Use This Skill

- Network blocks ports 4222, 8844, 8855 (most restrictive networks)
- Double-NAT or carrier-grade NAT prevents direct connections
- Corporate/university firewall only allows SSH
- After moving to a new network where direct connectivity fails
- When `fleet-check` shows P1 (NATS) and P2 (HTTP) failing but P5 (SSH) works
