# Quick Start

Get a 2-node fleet running in 5 minutes.

## Step 1: Install

```bash
pip install multifleet
```

## Step 2: Configure

```bash
mkdir -p .multifleet
cp config/config.template.json .multifleet/config.json
```

Edit `.multifleet/config.json` -- add one entry per machine:

```json
{
  "fleetId": "my-fleet",
  "nodes": {
    "mac1": {
      "host": "192.168.1.10",
      "port": 8855,
      "ssh_user": "user",
      "role": "chief"
    },
    "mac2": {
      "host": "192.168.1.11",
      "port": 8855,
      "ssh_user": "user",
      "role": "worker"
    }
  }
}
```

## Step 3: Start the Fleet Daemon

On each machine:

```bash
export MULTIFLEET_NODE_ID=mac1   # or mac2 on the other machine
fleet-nerve                       # starts daemon on port 8855
```

## Step 4: Check Health

```bash
fleet status
```

Expected output shows both nodes online with channel health:

```
Fleet: my-fleet (2 nodes)
  mac1 (chief)  ONLINE  P1:ok P2:ok
  mac2 (worker) ONLINE  P1:ok P2:ok
```

## Step 5: Send a Message

```bash
fleet send mac2 "Hello from the CLI"
```

Or via the MCP tool from inside Claude Code:

```
/fleet-send mac2 "Hello from Claude Code"
```

## Step 6: Dispatch a Task

Send work to another machine with full session context:

```
/fleet-task mac2 "Run the test suite and report failures"
```

The target machine spawns a Claude session with context inherited from the sender's active session.

## What Happens Next

- **Self-healing starts immediately.** If NATS goes down, messages fall through to HTTP, then SSH, then Git.
- **Idle productivity kicks in.** When a session goes idle for 60s, it picks up fleet backlog.
- **Probes scan continuously.** 19 probes monitor fleet health, code drift, and security surface.

## Run the Demo

For a full walkthrough of all capabilities:

```bash
bash multi-fleet/scripts/demo-multifleet.sh
```

Theatrical mode (slower pacing for presentations):

```bash
bash multi-fleet/scripts/demo-multifleet.sh --theatrical
```
