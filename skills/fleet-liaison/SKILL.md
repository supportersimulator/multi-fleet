---
name: fleet-liaison
description: Interact with the fleet liaison agent — check status, read batched messages, escalate outbound, pause/resume monitoring
---

# Fleet Liaison

Interface for the coding agent to interact with the background fleet liaison. The liaison handles all fleet communication so you stay focused on code.

## Commands

### `/fleet-liaison status`

Show liaison state: pending message counts, active rebuttals, uptime.

```bash
# Read liaison state
cat /tmp/fleet-liaison-state.json 2>/dev/null | python3 -c "
import sys, json
try:
    s = json.load(sys.stdin)
    p = s.get('stats', {})
    print(f'Liaison: {\"PAUSED\" if s.get(\"paused\") else \"ACTIVE\"}')
    print(f'Last poll: {s.get(\"last_poll\", \"never\")}')
    print(f'Pending: {p.get(\"info\",0)} info, {p.get(\"urgent\",0)} urgent, {p.get(\"critical\",0)} critical')
    print(f'Total received: {p.get(\"received\",0)} | Sent: {p.get(\"sent\",0)} | Malformed: {p.get(\"malformed\",0)}')
    rb = s.get('active_rebuttals', [])
    if rb:
        print(f'Active rebuttals: {len(rb)}')
        for r in rb:
            print(f'  - {r.get(\"task_id\",\"?\")} phase={r.get(\"phase\",\"?\")}')
except Exception:
    print('Liaison not running or state file missing')
"

# Read batch summary
cat /tmp/fleet-liaison-batch.json 2>/dev/null | python3 -c "
import sys, json
try:
    b = json.load(sys.stdin)
    p = b.get('pending', {})
    print(f'Batch updated: {b.get(\"updated\",\"?\")}')
    print(f'Queued: {p.get(\"info\",0)} info, {p.get(\"urgent\",0)} urgent, {p.get(\"critical\",0)} critical')
except Exception:
    print('No batch file')
"
```

### `/fleet-liaison read`

Get and clear all batched messages. Returns the full batch JSON, then resets the batch file.

```bash
# Read batch
BATCH=$(cat /tmp/fleet-liaison-batch.json 2>/dev/null)
if [ -n "$BATCH" ]; then
    echo "$BATCH" | python3 -c "
import sys, json
b = json.load(sys.stdin)
items = b.get('items', [])
if not items:
    print('No pending messages')
else:
    for i in items:
        level = i.get('level','?')
        marker = {'CRITICAL':'!!!','URGENT':'!!','INFO':'.'}
        print(f'[{marker.get(level,\"?\")}] [{level}] {i.get(\"from\",\"?\")} | {i.get(\"type\",\"?\")} | {i.get(\"subject\",\"\")}')
        if i.get('summary'):
            print(f'    {i[\"summary\"]}')
"
    # Clear batch after reading
    echo '{"updated":"'$(date -u +%Y-%m-%dT%H:%M:%SZ)'","pending":{"info":0,"urgent":0,"critical":0},"items":[]}' > /tmp/fleet-liaison-batch.json
else
    echo "No batch file — liaison may not be running"
fi
```

### `/fleet-liaison escalate <message>`

Send a structured packet through the liaison to a fleet peer. The liaison handles channel selection and delivery.

```bash
# Write to outbox for liaison to pick up and deliver
cat > /tmp/fleet-outbox-${MULTIFLEET_NODE_ID:-$(hostname -s)}/liaison-$(date +%s).json << 'MSG'
{
  "protocol": "3s-lan/v1",
  "message_type": "<type>",
  "from": "${MULTIFLEET_NODE_ID}",
  "to": "<target>",
  "task_id": null,
  "timestamp": "<ISO8601>",
  "payload": {
    "subject": "<subject>",
    "body": "<body>"
  }
}
MSG
```

Or send directly via daemon if available:

```bash
curl -sf -X POST http://127.0.0.1:8855/message \
  -H "Content-Type: application/json" \
  -d '{
    "type": "<message_type>",
    "from": "'${MULTIFLEET_NODE_ID:-$(hostname -s)}'",
    "to": "<target>",
    "payload": {
      "subject": "<subject>",
      "body": "<body>"
    }
  }'
```

### `/fleet-liaison pause`

Temporarily pause fleet monitoring. Liaison stops polling but retains state.

```bash
# Set paused flag
python3 -c "
import json, os
path = '/tmp/fleet-liaison-state.json'
try:
    with open(path) as f:
        s = json.load(f)
except Exception:
    s = {}
s['paused'] = True
with open(path, 'w') as f:
    json.dump(s, f)
print('Fleet liaison PAUSED — no new messages will be processed')
print('Use /fleet-liaison resume to restart monitoring')
"
```

### `/fleet-liaison resume`

Resume fleet monitoring after a pause.

```bash
# Clear paused flag
python3 -c "
import json
path = '/tmp/fleet-liaison-state.json'
try:
    with open(path) as f:
        s = json.load(f)
except Exception:
    s = {}
s['paused'] = False
with open(path, 'w') as f:
    json.dump(s, f)
print('Fleet liaison RESUMED — monitoring active')
"
```

## When to Use

- **Starting a coding session** — run `status` to check for pending fleet messages
- **Deep focus** — `pause` to silence fleet, `resume` when ready
- **Checking in** — `read` to pull batched messages at a natural break point
- **Sending updates** — `escalate` to push verdicts, status, or context to peers
- **Rebuttal cycles** — `status` shows active rebuttal phase and pending critiques

## Integration

| Component | Relationship |
|-----------|-------------|
| **fleet-liaison agent** | Background agent that does the actual monitoring |
| **fleet-protocol** | Liaison uses P0-P7 cascade for all sends |
| **fleet-coordinator** | Coordinator dispatches tasks; liaison handles comms |
| **fleet-verdict / fleet-rebuttal** | Liaison tracks rebuttal cycle state |
| **coding agent** | Reads `/tmp/fleet-liaison-batch.json`, invokes this skill |
