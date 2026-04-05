---
name: fleet-send
description: Send a message to another machine in the fleet — context, task, alert, or broadcast with 7-priority fallback delivery
---

# Fleet Send

Send a message to one or more machines in the fleet. Messages are routed by type — each type has different delivery semantics. The 7-priority fallback chain ensures delivery even on hostile networks.

## How It Works

1. You compose a message with a type, target, and payload
2. The daemon tries all 7 delivery channels in priority order (NATS, HTTP, Chief relay, Seed file, SSH, WoL, Git)
3. First successful delivery wins — the channel used is logged
4. If P1-P2 fail and a lower channel succeeds, a self-healing repair request is sent automatically
5. Target processes the message based on type (seed file, agent spawn, notification, etc.)

## Sending Messages

### Direct peer-to-peer (preferred)

```bash
# Send context (passive — injected on next prompt)
curl -sf -X POST http://<target-ip>:8855/message -H "Content-Type: application/json" \
  -d '{"type":"context","from":"<this-node>","to":"<target>","payload":{"subject":"Auth bug fixed","body":"Fixed the session token race condition in middleware.py line 42. You can pull main now."}}'

# Send task (autonomous — spawns agent on target)
curl -sf -X POST http://<target-ip>:8855/message -H "Content-Type: application/json" \
  -d '{"type":"task","from":"<this-node>","to":"<target>","payload":{"subject":"Run 3s cross-exam on auth module","body":"Full cross-examination of authentication middleware. Check session token storage compliance. Report findings."}}'

# Send alert (interrupt — focuses VS Code + macOS notification)
curl -sf -X POST http://<target-ip>:8855/message -H "Content-Type: application/json" \
  -d '{"type":"alert","from":"<this-node>","to":"<target>","payload":{"subject":"Conflicting verdicts on branch X","body":"STOP: mac2 and mac3 produced contradictory test results on feature/auth. Need human adjudication."}}'

# Send reply (references an original message)
curl -sf -X POST http://<target-ip>:8855/message -H "Content-Type: application/json" \
  -d '{"type":"reply","from":"<this-node>","to":"<target>","payload":{"subject":"Re: cross-exam results","body":"3 issues found. See /tmp/atlas-agent-results/fleet-task-abc123.log","ref":"msg-abc123"}}'

# Broadcast to all nodes
curl -sf -X POST http://127.0.0.1:8855/message -H "Content-Type: application/json" \
  -d '{"type":"broadcast","from":"<this-node>","payload":{"subject":"Pull main","body":"New feature landed — fleet-worker tmux isolation. Everyone pull and restart daemons."}}'
```

### Via CLI script

```bash
# Basic send (uses chief relay as transport)
CHIEF_INGEST_URL=http://<chief-ip>:8844 ./scripts/fleet-send.sh <target> "subject" "body"

# With priority (p1=alert, p2=task, p3=context)
CHIEF_INGEST_URL=http://<chief-ip>:8844 ./scripts/fleet-send.sh <target> "subject" "body" p1

# Broadcast to all nodes
./scripts/fleet-send.sh all "Pull main" "New feature landed"
```

### Via Python

```python
import requests, json

def fleet_send(target: str, msg_type: str, subject: str, body: str, ref: str = None):
    """Send a fleet message to a target node."""
    payload = {"subject": subject, "body": body}
    if ref:
        payload["ref"] = ref

    msg = {
        "type": msg_type,
        "from": os.environ.get("MULTIFLEET_NODE_ID", "unknown"),
        "to": target,
        "payload": payload
    }

    # Try direct peer-to-peer first
    try:
        r = requests.post(f"http://{target_ip}:8855/message",
                         json=msg, timeout=3)
        if r.status_code == 200:
            return {"delivered": True, "channel": "http_direct"}
    except requests.ConnectionError:
        pass

    # Fallback to chief relay
    try:
        r = requests.post(f"http://{chief_ip}:8844/message",
                         json=msg, timeout=5)
        if r.status_code == 200:
            return {"delivered": True, "channel": "chief_relay"}
    except requests.ConnectionError:
        pass

    return {"delivered": False, "error": "all channels failed"}
```

## Message Types

| Type | Delivery | Disrupts? | When to use |
|------|----------|-----------|-------------|
| `context` | Seed file, injected on next prompt | No | Enriching another session's context |
| `task` | Spawns `claude -p` agent on target | No | Autonomous work on another machine |
| `reply` | Seed file with `ref` linking to original | No | Responding to a specific request |
| `alert` | Focus VS Code + macOS notification | Yes | Urgent, needs human attention now |
| `broadcast` | Seed file on ALL nodes | No | Fleet-wide announcements |
| `sync` | Silent state bookkeeping | No | Internal coordination (heartbeats, acks) |

## Choosing the Right Type

| Scenario | Type | Why |
|----------|------|-----|
| "Pull latest and run tests" | `task` | Autonomous — no human needed on target |
| "FYI: I fixed the auth bug" | `context` | Passive enrichment — target sees it next prompt |
| "STOP: conflicting verdicts" | `alert` | Needs human attention immediately |
| "Everyone pull main" | `broadcast` | Fleet-wide, no specific target |
| "Here are the cross-exam results" | `reply` | Links back to original request via `ref` |
| "Session state update" | `sync` | Silent bookkeeping, no user notification |

## Delivery Confirmation

The daemon returns delivery status:

```json
{
  "status": "published",
  "messageId": "msg-abc123",
  "channel": "http_direct",
  "timestamp": "2026-04-05T10:30:00Z"
}
```

Check if a message was received on the target:

```bash
# On the target machine — view inbox
curl -s http://127.0.0.1:8855/inbox | python3 -m json.tool

# On the sender — check outbox for failed deliveries
curl -s http://127.0.0.1:8855/stats | python3 -c "
import sys, json
s = json.load(sys.stdin)
print(f'Sent: {s[\"sent\"]}, Failed: {s[\"errors\"]}, Retrying: {s[\"outbox_pending\"]}')
"
```

## Error Handling

### Target unreachable

If all 7 channels fail, the message is queued in the local outbox with exponential backoff retry (5s, 15s, 45s, 2min, 5min max). Check the outbox:

```bash
curl -s http://127.0.0.1:8855/stats | python3 -m json.tool
```

### Invalid target node

```bash
# Returns 400 if the target is not in the peer registry
# {"error": "unknown target: node99", "knownPeers": ["mac1", "mac2", "mac3"]}
```

### Daemon not running

If `curl` gets connection refused on port 8855, the local daemon is down:

```bash
# Check daemon status
pgrep -f fleet_nerve

# Restart via LaunchAgent
launchctl kickstart -k gui/$(id -u)/com.multifleet.nerve

# Or manually
MULTIFLEET_NODE_ID=$(hostname -s) python3 tools/fleet_nerve_daemon.py --port 8855 &
```

### Message too large

Payload body is limited to 64KB. For larger data, write to a shared location and send the path:

```bash
# Instead of sending large output inline, write to /tmp and reference it
curl -sf -X POST http://<target-ip>:8855/message -H "Content-Type: application/json" \
  -d '{"type":"context","from":"mac2","to":"mac1","payload":{"subject":"Large results","body":"Results written to /tmp/atlas-agent-results/cross-exam-abc.log on mac2. SSH to read."}}'
```

## Integration with Other Skills

- **fleet-task**: Use `type: "task"` to dispatch autonomous work (see fleet-task skill for details on session gold injection)
- **fleet-status**: Check target is online before sending (`/fleet/live`)
- **fleet-wake**: If target is offline, wake it first, then send
- **fleet-check**: If sends keep failing, run the 7-channel test to diagnose which channels are broken
- **fleet-repair**: Automatic — if a send falls back to P3+, a repair request triggers for the broken channels
- **fleet-worker**: Include `"useWorker": true` in task payload to route to tmux worker pool instead of inline agent

## When to Use This Skill

- Sharing context between machines (what you learned, what you fixed)
- Dispatching work to another node
- Alerting a human on another machine about something urgent
- Broadcasting fleet-wide announcements (pull main, restart daemons)
- Sending results back from a task agent to the original requester
