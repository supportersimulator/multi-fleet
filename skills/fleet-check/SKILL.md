---
name: fleet-check
description: Run the full 7-channel communication test — verifies every delivery path to a target node
---

# Fleet Check

End-to-end verification of all 8 communication channels (P1-P8) to a target node. Tests each channel in sequence with a 30-second per-channel timeout, reports state, and identifies exactly which channels work and which are broken.

## Running the Full Test

```bash
# Test all 7 channels to a specific node
bash scripts/fleet-test-fallback.sh <node-id>
```

### Via daemon API

```bash
# Run the 7-channel test
curl -sf -X POST http://127.0.0.1:8855/check -H "Content-Type: application/json" \
  -d '{"target": "<node-id>"}' | python3 -m json.tool

# Test a specific channel only
curl -sf -X POST http://127.0.0.1:8855/check -H "Content-Type: application/json" \
  -d '{"target": "<node-id>", "channel": "nats"}' | python3 -m json.tool
```

## The 8-Channel Test Sequence

Tests all 8 channels in sequence, reports state. Each channel gets a 30-second timeout (configurable per-channel below). The test sends a probe message and waits for acknowledgment.

| Step | Channel | What it tests | Pass criteria | Default timeout |
|------|---------|--------------|---------------|-----------------|
| P1 | NATS pub/sub | NATS server reachable, subscription active | ACK received | 2s |
| P2 | HTTP direct | Target daemon port 8855 responding | HTTP 200 | 3s |
| P3 | Chief relay | Chief ingest + relay to target | ACK received | 5s |
| P4 | Seed file | SSH write to target's seed directory | File exists | 5s |
| P5 | SSH direct | SSH command execution on target | Exit code 0 | 10s |
| P6 | Wake-on-LAN | Magic packet delivery (LAN only) | Health responds | 60s |
| P7 | Git push | Async message via git commit + push | Commit lands | 30s |
| P8 | Direct text | Manual copy-paste (LAST RESORT) | N/A — human-only | N/A |

**30-second per-channel timeout guidance:** For automated checks, cap each channel at 30s maximum even if the default is lower. This prevents a single broken channel from blocking the entire test suite. P8 is never tested automatically — it exists only as a human last resort.

## Interpreting Results

```json
{
  "target": "node2",
  "timestamp": "2026-04-05T10:30:00Z",
  "summary": "5/7 channels operational",
  "channels": {
    "p1_nats": {"status": "pass", "latencyMs": 45},
    "p2_http": {"status": "pass", "latencyMs": 120},
    "p3_chief": {"status": "pass", "latencyMs": 890},
    "p4_seed": {"status": "pass", "latencyMs": 2100},
    "p5_ssh": {"status": "pass", "latencyMs": 3200},
    "p6_wol": {"status": "skip", "reason": "target already online"},
    "p7_git": {"status": "fail", "reason": "git push rejected (non-fast-forward)"}
  },
  "recommendation": "All primary channels healthy. P7 git needs branch cleanup."
}
```

### Status values

| Status | Meaning |
|--------|---------|
| `pass` | Channel delivered and acknowledged |
| `fail` | Channel attempted but failed |
| `skip` | Channel not applicable (e.g., WoL when target is online) |
| `timeout` | Channel attempted but no response within deadline |

### Common failure patterns

| Pattern | Diagnosis | Fix |
|---------|-----------|-----|
| P1-P2 fail, P3+ pass | Firewall blocking direct ports | Use `fleet-tunnel` |
| P1 fail, P2 pass | NATS server down on chief | Restart NATS: check chief's nats-server process |
| P1-P3 fail, P4-P5 pass | Network issue, SSH still works | Enable tunnel mode |
| P1-P5 fail, P6 pass | Target was sleeping | Wake succeeded, re-test |
| All fail | Target offline or unreachable | Check physical connectivity |

## Test All Nodes

```bash
# Test every node in the fleet
for node in $(python3 -c "
import json
cfg = json.load(open('.multifleet/config.json'))
for n in cfg['nodes']: print(n)
"); do
  echo "=== Testing $node ==="
  bash scripts/fleet-test-fallback.sh "$node"
done
```

## Configuration

The test uses timeouts from `.multifleet/config.json`:

```json
{
  "check": {
    "natsTimeoutMs": 2000,
    "httpTimeoutMs": 3000,
    "chiefTimeoutMs": 5000,
    "seedTimeoutMs": 5000,
    "sshTimeoutMs": 10000,
    "wolTimeoutMs": 60000,
    "gitTimeoutMs": 30000,
    "skipOfflineWol": true
  }
}
```

## Protocol Verification

Fleet check is the diagnostic tool for the self-healing protocol. After running:

1. **All P1+P2 pass** — fleet is healthy, no action needed
2. **P1 or P2 fail** — trigger `fleet-repair` immediately. The fleet is not healthy until ALL nodes have P1+P2 operational
3. **Only P3+ pass** — communication works but is degraded. Repair is mandatory, not optional
4. **All fail** — target is truly unreachable. Try `fleet-wake` first

```bash
# Quick protocol health: just check P1+P2 for all peers
for node in $(python3 -c "
import json
cfg = json.load(open('.multifleet/config.json'))
for n in cfg['nodes']: print(n)
"); do
  echo -n "$node: "
  curl -sf -X POST http://127.0.0.1:8855/check -H "Content-Type: application/json" \
    -d "{\"target\": \"$node\", \"channels\": [\"nats\", \"http\"]}" 2>/dev/null | \
    python3 -c "import sys,json;d=json.load(sys.stdin);ch=d.get('channels',{});print(f'P1={ch.get(\"p1_nats\",{}).get(\"status\",\"?\")} P2={ch.get(\"p2_http\",{}).get(\"status\",\"?\")}')" 2>/dev/null || echo "unreachable"
done
```

## When to Use This Skill

- After setting up a new node — verify all channels work
- After network changes (new WiFi, VPN, router config)
- When messages aren't arriving — identify which channel is broken
- Before relying on fleet for critical work — confirm redundancy
- Debugging: "why did my message fall back to P5?"
- Periodic health audit of fleet connectivity
- **Self-healing verification**: confirm P1+P2 are restored after repair
