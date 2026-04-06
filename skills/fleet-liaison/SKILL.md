---
name: fleet-liaison
description: Background relay agent that handles fleet communications so the coding agent stays focused — saves 20-30% context window
---

# Fleet Liaison Agent

Background agent that intercepts, batches, and triages fleet messages. The coding agent stays in flow — the liaison handles all fleet comms and only escalates what matters.

## When It Activates

- **Automatically** on session start (if fleet comms are expected)
- **On demand** when fleet message volume is high
- **Always** during multi-node coordination tasks (chain orchestration, consensus requests)

## How It Works

1. Liaison runs in a separate tmux session, monitoring fleet inbox and NATS
2. Incoming messages are classified by urgency using the escalation matrix
3. Low-priority messages are batched and summarized periodically
4. High-priority messages are injected into the coding agent immediately
5. The coding agent never polls fleet channels directly — liaison is the single gateway

## Escalation Matrix

| Level | Action | Example |
|-------|--------|---------|
| **SILENT** | Log only, no notification | Heartbeats, acks, sync messages |
| **INFO** | Batch into periodic summary (every 5min) | FYI context shares, status updates |
| **IMPORTANT** | Inject summary into coding session | Consensus requests, PR reviews |
| **URGENT** | Immediate inject into coding session | Breaking changes, sync holds |
| **CRITICAL** | Interrupt + macOS alert + inject | Conflicting verdicts, deploy failures, data loss risk |

Classification rules:
- `sync` type → SILENT
- `context` type → INFO (unless subject contains "BREAKING" or "URGENT")
- `task` type → IMPORTANT
- `alert` type → URGENT or CRITICAL (based on payload keywords)
- `broadcast` with "SYNC HOLD" → CRITICAL

## Checking Status

```bash
# Via MCP tool
fleet_liaison_status

# Via tmux
tmux has-session -t fleet-liaison 2>/dev/null && echo "running" || echo "stopped"

# Via launcher script
multi-fleet/scripts/fleet-liaison.sh status
```

## Batch Summary Format

The liaison produces periodic digests:

```
[Fleet Digest — 10:30-10:35]
- mac2: pushed auth fix to main (context)
- mac3: requests review of liaison PR (task)
- 3 heartbeats suppressed
```

## Integration

- **fleet-send**: Liaison uses fleet-send for outbound replies on behalf of coding agent
- **fleet-status**: Liaison monitors `/health` endpoints to track peer availability
- **fleet-repair**: If liaison detects P3+ delivery, triggers repair automatically

## Context Savings

Without liaison: every fleet message consumes coding agent context (polling, reading, responding).
With liaison: coding agent sees only escalated summaries. Measured savings: 20-30% context window on fleet-heavy sessions.
