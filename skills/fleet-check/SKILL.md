---
name: fleet-check
description: Run the full 7-channel communication test — verifies every delivery path to a target node
---

# Fleet Check

End-to-end verification of all 7 communication channels to a target node. Identifies exactly which channels work and which are broken, so you know your fallback coverage.

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

## The 7-Channel Test Sequence

Each channel is tested in priority order. The test sends a probe message and waits for acknowledgment.

| Step | Channel | What it tests | Pass criteria |
|------|---------|--------------|---------------|
| P1 | NATS pub/sub | NATS server reachable, subscription active | ACK within 2s |
| P2 | HTTP direct | Target daemon port 8855 responding | HTTP 200 within 3s |
| P3 | Chief relay | Chief ingest + relay to target | ACK within 5s |
| P4 | Seed file | SSH write to target's seed directory | File exists within 5s |
| P5 | SSH direct | SSH command execution on target | Exit code 0 within 10s |
| P6 | Wake-on-LAN | Magic packet delivery (LAN only) | Health responds within 60s |
| P7 | Git push | Async message via git commit + push | Commit lands within 30s |

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

## When to Use This Skill

- After setting up a new node — verify all channels work
- After network changes (new WiFi, VPN, router config)
- When messages aren't arriving — identify which channel is broken
- Before relying on fleet for critical work — confirm redundancy
- Debugging: "why did my message fall back to P5?"
- Periodic health audit of fleet connectivity
