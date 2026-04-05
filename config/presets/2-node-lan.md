# 2-Node LAN Preset

**When to use:** Two Macs on the same local network. The simplest setup.

## Topology

```
mac1 (chief) <--- NATS + HTTP ---> mac2 (worker)
```

## Setup

1. Copy to config: `cp config/presets/2-node-lan.json .multifleet/config.json`
2. Replace IP addresses with your actual LAN IPs
3. Install NATS on the chief: `brew install nats-server && nats-server -p 4222`
4. On each machine, set `MULTIFLEET_NODE_ID` to its node name

## How it works

- NATS runs on the chief node. Both nodes connect directly.
- P1 (NATS) and P2 (HTTP) are the primary channels.
- No SSH tunnels needed -- both machines are directly reachable.
- If NATS goes down, HTTP direct (P2) takes over automatically.

## Requirements

- Both machines on the same LAN (or reachable by IP)
- NATS server running on the chief node
- Port 8855 and 4222 open between machines
