# Multi-Fleet Examples

Three runnable scripts that demonstrate core multifleet primitives without
pulling in the full daemon stack. Useful for smoke-testing a fresh install,
learning the pub/sub patterns, or debugging a stuck peer.

## Prerequisites

```bash
pip install nats-py     # bare minimum
# or: pip install multifleet   # full package

# Start NATS locally
nats-server -p 4222 &
```

## Examples

| # | File | What it shows |
|---|------|---------------|
| 01 | `01_minimal_send_receive.py` | Pub + sub on `fleet.all.*` / `fleet.{node}.*` |
| 02 | `02_peer_discovery.py` | Heartbeat-based peer liveness map |
| 03 | `03_request_reply.py` | Request/reply pattern (same mechanism as node-ID collision detect) |

## Run

```bash
# Example 1 — open two terminals
MULTIFLEET_NODE_ID=alice python examples/01_minimal_send_receive.py
MULTIFLEET_NODE_ID=bob   python examples/01_minimal_send_receive.py

# Example 2 — peer discovery
MULTIFLEET_NODE_ID=alpha python examples/02_peer_discovery.py
MULTIFLEET_NODE_ID=beta  python examples/02_peer_discovery.py

# Example 3 — request/reply (responder first, then requester)
MULTIFLEET_ROLE=responder python examples/03_request_reply.py &
MULTIFLEET_ROLE=requester python examples/03_request_reply.py
```

All examples respect `NATS_URL` env var. Default: `nats://127.0.0.1:4222`.
