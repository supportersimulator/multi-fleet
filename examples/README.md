# Multi-Fleet Examples

Runnable scripts demonstrating both low-level NATS primitives and the high-level fleet API.

## Prerequisites

```bash
pip install multifleet             # high-level API + NATS client
nats-server -p 4222 &              # local NATS (required for 01–03 NATS series)
```

## Low-Level NATS Primitives (mac1 series)

| # | File | What it shows |
|---|------|---------------|
| 01 | `01_minimal_send_receive.py` | Pub + sub on `fleet.all.*` / `fleet.{node}.*` |
| 02 | `02_peer_discovery.py` | Heartbeat-based peer liveness map |
| 03 | `03_request_reply.py` | Request/reply pattern (same mechanism as node-ID collision detect) |

```bash
# Example 1 — open two terminals
MULTIFLEET_NODE_ID=alice python examples/01_minimal_send_receive.py
MULTIFLEET_NODE_ID=bob   python examples/01_minimal_send_receive.py

# Example 2 — peer discovery
MULTIFLEET_NODE_ID=alpha python examples/02_peer_discovery.py
MULTIFLEET_NODE_ID=beta  python examples/02_peer_discovery.py

# Example 3 — request/reply
MULTIFLEET_ROLE=responder python examples/03_request_reply.py &
MULTIFLEET_ROLE=requester python examples/03_request_reply.py
```

All respect `NATS_URL` env var. Default: `nats://127.0.0.1:4222`.

## High-Level Fleet API (mac3 series)

| # | File | What it shows |
|---|------|---------------|
| 01 | `01_single_node_health.py` | Spawn daemon, poll `/health`, show peers + uptime |
| 02 | `02_invariance_gate.py` | Run Constitutional `validate_before_action` on 2 scenarios |
| 03 | `03_send_fleet_message.py` | Send via 10-priority channel cascade (P0 Discord → P7 git) |

```bash
# Single-node smoke test
python examples/01_single_node_health.py

# Constitutional validation demo
python examples/02_invariance_gate.py

# Send a message (requires daemon + peer)
python examples/03_send_fleet_message.py mac2
```

## Next Steps
- `fleet init` — scaffold config for your fleet
- `fleet status` — current fleet health
- `fleet doctor` — diagnose issues
- See `docs/getting-started.md` for multi-node setup.
