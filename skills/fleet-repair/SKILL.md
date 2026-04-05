---
name: fleet-repair
description: 4-level repair escalation for broken fleet channels — notify, guide, assist, remote
---

# Fleet Repair

When a message falls back to P3+ channels, it means P1-P2 are broken. The repair system diagnoses and fixes connectivity issues through a 4-level escalation chain.

## Escalation Levels

| Level | Action | Automation | Human needed? |
|-------|--------|------------|---------------|
| **L1 Notify** | Log the failure, alert the fleet dashboard | Fully automatic | No |
| **L2 Guide** | Send repair instructions to the target node's session | Fully automatic | Human reads instructions |
| **L3 Assist** | Spawn a repair agent on the target via SSH | Fully automatic | No |
| **L4 Remote** | Execute repair commands directly via SSH | Requires approval | Yes (approve) |

## How It Works

```
Message delivery falls back to P3+
  → L1: Log to fleet dashboard + local repair log
  → L2: Send context message with repair steps to target node
  → Wait 120s — did P1/P2 recover?
  → L3: SSH into target, spawn repair agent (claude -p)
  → Wait 300s — did P1/P2 recover?
  → L4: Present remote fix commands to human for approval
```

## Triggering a Repair

### Automatic (self-healing)

Repairs trigger automatically when the fallback delivery system detects degraded channels. No manual action needed for L1-L3.

### Manual repair request

```bash
# Send a repair request to a specific node
curl -sf -X POST http://127.0.0.1:8855/repair -H "Content-Type: application/json" \
  -d '{"target": "<node-id>", "channel": "nats", "level": "assist"}'

# Check repair status
curl -s http://127.0.0.1:8855/repair/status | python3 -m json.tool
```

### Via fleet CLI

```bash
python3 tools/fleet_nerve_nats.py repair <node-id>
python3 tools/fleet_nerve_nats.py repair <node-id> --level guide
python3 tools/fleet_nerve_nats.py repair <node-id> --channel http --level remote
```

## What Each Level Fixes

### L1 Notify
- Records which channel failed and when
- Updates fleet dashboard with degraded status
- No corrective action — observation only

### L2 Guide
- Sends a `context` message to the target with diagnostic steps:
  - Check if daemon is running (`pgrep -f fleet_nerve`)
  - Check if port is listening (`lsof -i :8855`)
  - Check NATS connectivity (`nats pub test "ping"`)
- Target's Claude session sees this on next prompt

### L3 Assist
- SSH into target node (using config.json credentials)
- Spawn a `claude -p` repair agent with diagnostic prompt
- Agent checks daemon status, restarts if needed, verifies connectivity
- Results written to `/tmp/atlas-agent-results/fleet-repair-<id>.log`

### L4 Remote
- Presents specific commands to the human operator:
  - Restart daemon: `ssh <user>@<host> "launchctl kickstart -k gui/$(id -u)/com.multifleet.nerve"`
  - Restart NATS: `ssh <user>@<host> "brew services restart nats-server"`
  - Re-establish tunnel: `ssh <user>@<host> "bash ~/fleet/scripts/fleet-tunnel.sh restart"`
- Commands execute only after human approval

## Repair Log

All repair actions are logged to the fleet dashboard and locally:

```bash
# View recent repairs
curl -s http://127.0.0.1:8855/repair/log | python3 -m json.tool

# Filter by node
curl -s "http://127.0.0.1:8855/repair/log?node=<node-id>" | python3 -m json.tool
```

## Configuration

In `.multifleet/config.json`:

```json
{
  "repair": {
    "autoEscalate": true,
    "l2WaitSeconds": 120,
    "l3WaitSeconds": 300,
    "l4RequiresApproval": true,
    "maxRetriesPerHour": 3
  }
}
```

| Key | Default | Description |
|-----|---------|-------------|
| `autoEscalate` | `true` | Automatically escalate through levels |
| `l2WaitSeconds` | `120` | Wait time before escalating L2 to L3 |
| `l3WaitSeconds` | `300` | Wait time before escalating L3 to L4 |
| `l4RequiresApproval` | `true` | Require human approval for remote execution |
| `maxRetriesPerHour` | `3` | Rate limit to prevent repair storms |

## When to Use This Skill

- A node shows as `degraded` in the fleet dashboard
- Messages are consistently falling back to P3+ channels
- After network changes (new router, VPN, IP change)
- After a node reboot when daemon didn't auto-start
