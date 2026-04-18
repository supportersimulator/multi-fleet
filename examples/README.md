# Multi-Fleet Examples

Runnable scripts demonstrating fleet communication, messaging, and health monitoring.

## Prerequisites

```bash
# Install the package
pip install multifleet

# NATS server (required for NATS-based examples)
brew install nats-server   # macOS
# or: apt install nats-server  # Linux
nats-server -p 4222 &

# Set your node identity
export MULTIFLEET_NODE_ID=mynode
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MULTIFLEET_NODE_ID` | hostname | Unique identity for this node |
| `NATS_URL` | `nats://127.0.0.1:4222` | NATS server connection URL |
| `FLEET_DAEMON_URL` | `http://127.0.0.1:8855` | Fleet daemon HTTP endpoint |

## Examples

### 01 -- Hello Fleet (NATS basics)

Connect to NATS, send heartbeats, discover peers automatically.

```bash
# Terminal 1
MULTIFLEET_NODE_ID=alice python3 examples/01_hello_fleet.py

# Terminal 2
MULTIFLEET_NODE_ID=bob python3 examples/01_hello_fleet.py
```

Both nodes discover each other within seconds via heartbeat. Ctrl-C to quit.

### 02 -- Send Message (priority cascade)

Send a message to another node via the 9-priority channel cascade (P0 Cloud through P7 Git). Requires the fleet daemon running.

```bash
# Start daemon first
python3 -m multifleet.daemon &

# Send a context message
python3 examples/02_send_message.py mac2

# Send an alert (P1 only, 3x retry)
python3 examples/02_send_message.py mac2 --type alert

# Send a task assignment
python3 examples/02_send_message.py mac2 --type task --subject "run tests"
```

### 03 -- Health Monitor (live dashboard)

Poll the fleet daemon's `/health` endpoint and display a formatted dashboard with peers, channels, and surgeon availability.

```bash
# Start daemon first
python3 -m multifleet.daemon &

# Run the monitor (refreshes every 5 seconds)
python3 examples/03_health_monitor.py

# Custom interval
python3 examples/03_health_monitor.py --interval 10

# Single check (no loop)
python3 examples/03_health_monitor.py --once
```

## Additional Examples

These demonstrate lower-level NATS primitives and high-level API features:

| File | Description |
|------|-------------|
| `01_minimal_send_receive.py` | Raw NATS pub/sub on `fleet.all.*` and `fleet.{node}.*` |
| `02_peer_discovery.py` | Heartbeat-based peer liveness map with stale detection |
| `03_request_reply.py` | NATS request/reply pattern (same as node-ID collision detection) |
| `01_single_node_health.py` | Spawn daemon in-process, check `/health`, shut down |
| `02_invariance_gate.py` | Constitutional `validate_before_action` on safe vs destructive ops |
| `03_send_fleet_message.py` | Minimal message send via daemon HTTP API |

## Next Steps

- `docs/getting-started.md` -- full multi-node setup guide
- `COMMS-PROTOCOL.md` -- channel cascade specification
- `ARCHITECTURE.md` -- system design and module map
