# Cloud Relay Preset

**When to use:** Nodes behind NAT or on different networks. A cloud server
acts as the chief relay so nodes never need direct connectivity.

## Topology

```
home-mac ----\                    /----- office-mac
              >-- cloud-chief --<
              (NATS + relay)
```

## Setup

1. Copy to config: `cp config/presets/cloud-relay.json .multifleet/config.json`
2. Deploy a chief server (any small VPS):
   - Install NATS: `apt install nats-server`
   - Run the chief relay: `python3 tools/fleet_chief.py serve`
3. Set `FLEET_CHIEF_API_KEY` env var on each node
4. Replace `chief.example.com` with your server hostname
5. For NATS auth, generate credentials and place at the `credentials_file` path

## How it works

- All nodes connect outbound to the cloud chief (no inbound ports needed).
- P1 (NATS via cloud) is primary. P3 (Chief relay HTTP) is the fallback.
- P7 (Git push) works as an always-available async backup.
- Nodes behind NAT never need port forwarding or dynamic DNS.

## Requirements

- A small cloud server (1 vCPU, 512MB RAM is sufficient)
- NATS server and chief relay running on the cloud server
- Outbound HTTPS/NATS access from each node
- NATS credentials file if using authentication

## Cost

A $5/month VPS is sufficient for fleets up to ~50 nodes. NATS handles
thousands of connections on minimal resources.
