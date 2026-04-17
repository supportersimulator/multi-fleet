# Getting Started with Multi-Fleet

Cross-machine AI collaboration with self-healing communication. Messages always deliver -- even when NATS is down, HTTP is blocked, and SSH is your only path.

## Prerequisites

- Python 3.10+
- NATS server (optional for single-node; required for P1 channel in multi-node)
- SSH key access between machines (for P4/P5 channels)

## Quick Install

```bash
pip install multifleet
```

Or from source (inside the superrepo):

```bash
pip install ./multi-fleet
```

Verify:

```bash
fleet --version
fleet doctor
```

## First Run (Single Node)

```bash
export MULTIFLEET_NODE_ID=mac1
fleet-nerve                        # starts daemon on port 8855
fleet status                       # check health
fleet doctor                       # run 6 diagnostic checks
```

The daemon starts with self-healing, probes, and idle productivity enabled by default.

## Multi-Node Setup

1. Install on each machine: `pip install multifleet`
2. Copy the config template:
   ```bash
   mkdir -p .multifleet
   cp config/config.template.json .multifleet/config.json
   ```
3. Edit `.multifleet/config.json` -- add one entry per machine:
   ```json
   {
     "fleetId": "my-fleet",
     "nodes": {
       "mac1": { "host": "192.168.1.10", "port": 8855, "role": "chief" },
       "mac2": { "host": "192.168.1.11", "port": 8855, "role": "worker" }
     }
   }
   ```
4. Set unique node IDs and start the daemon on each machine:
   ```bash
   export MULTIFLEET_NODE_ID=mac1   # mac2 on the other machine
   fleet-nerve
   ```
5. Verify connectivity:
   ```bash
   fleet status
   fleet send mac2 "Hello from mac1"
   ```

## See It Working

```bash
bash multi-fleet/scripts/demo-multifleet.sh
```

Theatrical mode (slower pacing for presentations):

```bash
bash multi-fleet/scripts/demo-multifleet.sh --theatrical
```

## Key Commands

| Command | Description |
|---------|-------------|
| `fleet status` | Show fleet health and node states |
| `fleet doctor` | Run 6 diagnostic checks |
| `fleet send <node> "message"` | Send a message to another node |
| `fleet dashboard` | Terminal dashboard |
| `fleet dashboard --web` | Browser dashboard with live updates |
| `/fleet-task <node> "task"` | Dispatch session-aware work to a remote node |
| `/fleet-check <node>` | Test all communication channels to a target |
| `/fleet-repair <node>` | Trigger repair escalation for broken channels |

## Architecture

- **9 communication channels (P0-P8)** -- Cloud, NATS, HTTP, Chief relay, seed file, SSH, WoL, Git push, direct text. First success wins.
- **Self-healing** -- when a low-priority channel delivers, broken higher-priority channels auto-repair through 4-level escalation.
- **Chief synthesis** -- pluggable analyzers aggregate fleet-wide intelligence with confidence-weighted verdicts.
- **3-Surgeon Protocol** -- 3 distinct LLMs (Claude, GPT-4.1, Qwen3) cross-examine every decision for corrigibility.
- **Session-aware tasks** -- dispatched agents inherit context from the target's active session.

## Next Steps

- [Installation details](getting-started/install.md) -- IDE setup, env vars, troubleshooting
- [Configuration](getting-started/config.md) -- full node config, presets, security
- [Quick Start walkthrough](getting-started/quickstart.md) -- step-by-step 2-node setup
- [Architecture](../ARCHITECTURE.md) -- 5-layer design, all 112 modules, data flow
- [Communication Protocol](../COMMS-PROTOCOL.md) -- channel specs, state machine, security
