# 3-Node SSH Tunnel Preset

**When to use:** Three machines that can reach each other via SSH but may not
have direct NATS connectivity (different subnets, firewalls, VPN).

## Topology

```
mac1 (chief)
  |--- SSH tunnel (port 4223) ---> mac2 (worker)
  |--- SSH tunnel (port 4224) ---> mac3 (worker)
```

## Setup

1. Copy to config: `cp config/presets/3-node-ssh.json .multifleet/config.json`
2. Replace hostnames and usernames with your actual values
3. Ensure SSH key auth is set up between machines (`ssh-copy-id`)
4. Install NATS on the chief: `brew install nats-server && nats-server -p 4222`
5. Use `/fleet-tunnel` to start tunnels, or let the daemon auto-start them

## How it works

- NATS runs on the chief. Workers connect via SSH tunnels.
- Each tunnel forwards a local port to the remote NATS port.
- P1 (NATS via tunnel) is primary. P5 (SSH direct) is the reliable fallback.
- If a tunnel drops, the self-healing system detects and restarts it.

## Requirements

- SSH key-based auth between all nodes
- NATS server on the chief node
- No direct port access needed between machines (SSH handles transport)
