---
name: fleet-post-verification
description: HARD-GATE — verify fleet health and connectivity after completing fleet work before claiming done
allowed-tools:
  - Bash
  - Read
  - Grep
  - Glob
---

# Fleet Post-Verification

## Purpose

"It worked" is not verification. Fleet work involves multiple nodes, multiple channels, and multiple failure modes that can silently degrade. This gate ensures that completed fleet work is verified across all affected nodes and channels before anyone claims "done."

<HARD-GATE>
Do NOT claim fleet work is complete until this verification passes. "Done" means all nodes verified, not "I finished on my node."
</HARD-GATE>

## Auto-Invocation Triggers

Invoke before ANY of these claims:

| Signal | Examples |
|--------|----------|
| Completion claim | "Done", "complete", "finished", "deployed" |
| Readiness claim | "Ready for use", "fleet is up", "all nodes connected" |
| Fix claim | "Connectivity restored", "channel fixed", "node recovered" |
| Config change applied | After any fleet-config-gate change has been applied |
| Remote dispatch completed | After fleet-dispatch-gate work returns results |

## The 4-Check Protocol

### Check 1: Run fleet-check on all target nodes

Every node touched by the work must pass a full channel check.

```bash
# Full 7-channel test on each affected node
for node in TARGET_NODES; do
  echo "=== Checking $node ==="
  bash scripts/fleet-test-fallback.sh "$node"
done
```

**Interpret results**:

| Result | Action |
|--------|--------|
| All 7 channels pass | PASS — proceed to check 2 |
| Primary channels pass, fallbacks degraded | PASS with warning — note degraded channels |
| Primary channel fails | FAIL — diagnose and fix before claiming done |
| Multiple channels fail | FAIL — likely a config or connectivity regression |

**If fleet-check was passing before your work and now fails**: Your change introduced a regression. Revert and investigate.

### Check 2: Verify all channels still operational

Beyond the target nodes, verify that your work did not affect non-target nodes.

```bash
# Quick health check across the full fleet
curl -sf http://127.0.0.1:8855/status | python3 -c "
import sys, json
status = json.load(sys.stdin)
for node in status.get('nodes', []):
    health = 'OK' if node.get('online') else 'OFFLINE'
    channels = node.get('channels_up', 0)
    total = node.get('channels_total', 7)
    print(f\"{node['id']}: {health} ({channels}/{total} channels)\")
"
```

**If any non-target node degraded**: Your change had unintended blast radius. Investigate the connection between your change and the degraded node before claiming completion.

### Check 3: Confirm no regressions in connectivity

Compare current state against the known-good baseline.

```bash
# If a pre-change baseline exists (from fleet-config-gate)
if [ -f /tmp/fleet-pre-config-baseline.txt ]; then
  bash scripts/fleet-test-fallback.sh ALL_NODES > /tmp/fleet-post-verification.txt
  diff /tmp/fleet-pre-config-baseline.txt /tmp/fleet-post-verification.txt
fi

# If no baseline, compare against last known-good state
# Check daemon logs for recent failures
curl -sf http://127.0.0.1:8855/logs?since=10m | python3 -c "
import sys, json
logs = json.load(sys.stdin)
errors = [l for l in logs if l.get('level') in ('error', 'warning')]
for e in errors:
    print(f\"[{e.get('level')}] {e.get('message', '')}\")
if not errors:
    print('No errors in last 10 minutes')
"
```

**Regression indicators**:
- Channel that was passing now fails
- Latency increased significantly (>2x baseline)
- New error messages in daemon logs
- Node that was online now shows offline

**If regression detected**: GATE FAILS. Do not claim completion. Diagnose the regression, fix it, and re-run this verification from check 1.

### Check 4: Check watchdog shows no new alerts

The fleet watchdog monitors long-term health. Verify it has not flagged anything since your work began.

```bash
# Check watchdog status
curl -sf http://127.0.0.1:8855/watchdog/status | python3 -c "
import sys, json
wd = json.load(sys.stdin)
alerts = wd.get('alerts', [])
if alerts:
    for a in alerts:
        print(f\"[{a.get('severity', 'unknown')}] {a.get('node', '?')}: {a.get('message', '')}\")
else:
    print('No watchdog alerts')
"

# Check for recent watchdog events
curl -sf http://127.0.0.1:8855/watchdog/events?since=30m | python3 -m json.tool
```

**Watchdog alert interpretation**:

| Alert Type | Action |
|-----------|--------|
| No alerts | PASS |
| Pre-existing alerts (before your work) | PASS — note them but do not block |
| New alerts since your work started | FAIL — investigate correlation with your changes |
| Alert resolved by your work | PASS — note the improvement |

## Verification Levels

Scale verification to the scope of work:

| Work Scope | Verification |
|------------|-------------|
| Single node, single task | fleet-check on target + watchdog check |
| Multi-node coordination | fleet-check on all involved nodes + full channel verify |
| Config change | All 4 checks + baseline comparison + 60s stability hold |
| Connectivity fix | All 4 checks + repeat fleet-check after 5 minutes (delayed failure detection) |

## Integration

```
[fleet work] → fleet-post-verification → completion claim
```

This gate pairs with the dispatch and config gates:

```
fleet-dispatch-gate → [remote work] → fleet-post-verification
fleet-config-gate → [config applied] → fleet-post-verification
```

## What NOT to Do

- **Do NOT claim "fleet is working" without running fleet-check** — the check is the proof.
- **Do NOT verify only the target node** — fleet changes can have blast radius beyond the target.
- **Do NOT ignore new watchdog alerts** — if they appeared during your work, they are likely related.
- **Do NOT skip the stability hold for config changes** — some failures are delayed by seconds or minutes.
