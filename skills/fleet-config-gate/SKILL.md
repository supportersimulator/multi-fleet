---
name: fleet-config-gate
description: HARD-GATE — verify safety and blast radius before changing fleet configuration, LaunchAgents, or daemon settings
allowed-tools:
  - Bash
  - Read
  - Grep
  - Glob
---

# Fleet Config Gate

## Purpose

Fleet configuration changes propagate across nodes. A bad config change can sever connectivity for the entire fleet, and a severed fleet cannot receive the fix. This gate ensures every config change is scoped, communicated, and reversible before it takes effect.

<HARD-GATE>
Do NOT apply fleet configuration changes until this gate passes. A config change that breaks connectivity cannot be remotely fixed — you must physically access each affected node.
</HARD-GATE>

## Auto-Invocation Triggers

Invoke this skill when ANY of these signals are present:

| Signal | Examples |
|--------|----------|
| Fleet config file change | `config.json`, `fleet.json`, node definitions |
| LaunchAgent modification | `~/Library/LaunchAgents/com.fleet.*` plist files |
| Daemon settings change | Port, auth, channel config, timeout values |
| NATS configuration | Server URLs, credentials, subject patterns |
| Network/connectivity change | Tailscale config, SSH keys, firewall rules |
| Watchdog configuration | Alert thresholds, check intervals, notification targets |

**If even ONE signal is present, this gate fires.**

## The 4-Check Protocol

### Check 1: Will this change affect all nodes or just this one?

Determine the blast radius before applying anything.

```bash
# Review the change scope
# Local-only: changes to this node's daemon, local LaunchAgent, local env
# Fleet-wide: changes to shared config, NATS subjects, auth credentials, protocol

# Check which nodes reference the config being changed
grep -r "CONFIG_KEY_OR_FILE" /path/to/fleet/config/
```

| Scope | Risk | Required Action |
|-------|------|----------------|
| **This node only** | Low | Proceed after checks 2-4 |
| **Subset of nodes** | Medium | Notify affected nodes before applying |
| **All nodes** | High | Coordinate rollout — apply to one node first, verify, then propagate |

**For fleet-wide changes**: NEVER apply to all nodes simultaneously. Always canary on one node first, verify connectivity, then roll out to remaining nodes.

### Check 2: Do other nodes need to be notified first?

Changes that alter communication contracts require advance notification.

**Must notify before changing**:
- NATS subject patterns (other nodes publish/subscribe to these)
- Authentication credentials or tokens
- Daemon API port or protocol
- Message format or schema
- Node ID or hostname references

**Notification method**:
```bash
# Send config change notification via current working channel
curl -sf -X POST http://127.0.0.1:8855/send -H "Content-Type: application/json" \
  -d '{
    "target": "TARGET_NODE",
    "message": "CONFIG_CHANGE_PENDING: [description of change]. Apply matching change within [timeframe] or connectivity will break.",
    "priority": "high"
  }'
```

**If notification cannot be delivered** (target offline): GATE FAILS for breaking changes. Queue the change for coordinated application when all affected nodes are online.

### Check 3: Is this change reversible?

Every config change must have a rollback path documented before application.

```bash
# ALWAYS back up current config before changing
cp config.json config.json.bak.$(date +%s)

# For LaunchAgents
cp ~/Library/LaunchAgents/com.fleet.daemon.plist ~/Library/LaunchAgents/com.fleet.daemon.plist.bak.$(date +%s)
```

| Reversibility | Examples | Gate Decision |
|--------------|---------|---------------|
| **Easy** | Additive config (new field, new node) | PASS |
| **Moderate** | Changed values with backup | PASS — backup taken |
| **Hard** | Credential rotation (old creds invalidated) | PASS only with coordinated rollout |
| **Impossible** | Deleted config without backup | FAIL — take backup first |

**Rule**: If you cannot articulate the exact rollback steps, the change is not ready to apply.

### Check 4: Could this break connectivity?

The most dangerous failure mode: a config change that severs the channel needed to fix it.

**Pre-change connectivity baseline**:
```bash
# Record current channel health BEFORE making changes
bash scripts/fleet-test-fallback.sh ALL_NODES > /tmp/fleet-pre-config-baseline.txt

# After change, compare
bash scripts/fleet-test-fallback.sh ALL_NODES > /tmp/fleet-post-config-check.txt
diff /tmp/fleet-pre-config-baseline.txt /tmp/fleet-post-config-check.txt
```

**High-risk connectivity changes** (require extra caution):
- Changing NATS server URL or credentials
- Modifying SSH keys or Tailscale config
- Altering daemon listen address or port
- Changing firewall or network rules

**For high-risk changes**: Apply the change, then IMMEDIATELY run fleet-check on all affected nodes. If any channel drops, revert within 60 seconds using the backup.

## Rollout Strategy for Fleet-Wide Changes

1. **Backup** all affected configs on all nodes
2. **Apply to canary node** (least critical node first)
3. **Verify canary** — run `fleet-check` against canary from another node
4. **Wait** — observe for at least 60 seconds for delayed failures
5. **Roll out** to remaining nodes one at a time, verifying after each
6. **Final verification** — full `fleet-check` across all nodes

## Emergency Bypass Protocol

For connectivity emergencies (fleet partitioned, node unreachable):

1. **Declare emergency** — state what is broken and why config change is urgent
2. **Apply with immediate verification** — change + fleet-check within 30 seconds
3. **If verification fails** — revert immediately using backup
4. **Post-mortem** — document what happened and whether the gate would have prevented it

## Integration

```
config change identified → fleet-config-gate → [apply change] → fleet-post-verification
```

This gate fires BEFORE any fleet configuration modification. Pair with `fleet-post-verification` to confirm the change did not introduce regressions.

## What NOT to Do

- **Do NOT change fleet config without a backup** — configs without backups are irreversible by definition.
- **Do NOT apply breaking changes to all nodes at once** — canary first, always.
- **Do NOT change connectivity config on a node you can only reach via that config** — you will lock yourself out.
- **Do NOT skip notification for contract-breaking changes** — other nodes will fail silently.
