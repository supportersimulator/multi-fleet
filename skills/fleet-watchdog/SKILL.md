---
name: fleet-watchdog
description: Proactive fleet health monitoring — channel checks, threshold alerts, automatic repair triggers
---

# Fleet Watchdog

**Watchdog doesn't just monitor — it heals.** Continuous background monitor that checks fleet health, triggers repairs before humans notice problems, and redirects idle sessions to channel repair work. Runs as part of the Fleet Nerve daemon.

## What It Monitors

| Check | Interval | Threshold | Action on failure |
|-------|----------|-----------|-------------------|
| Peer heartbeat | 10s | Missing >60s | Mark offline, trigger wake |
| NATS connectivity | 30s | 2 consecutive failures | Trigger L1 repair |
| API key sources | 300s | Key using fallback (aws/none) | Notify fleet, recommend keychain setup |
| HTTP reachability | 30s | 3 consecutive failures | Trigger L2 repair |
| Daemon process | 60s | Not running | Restart via LaunchAgent |
| Tunnel health | 60s | autossh dead or latency >5s | Restart tunnel |
| Disk space | 300s | `/tmp/atlas-agent-results/` >1GB | Prune old results |
| Task timeout | 30s | Running >600s | Kill + report failure |
| Self idle | 30s | No NATS activity >5min | Check inbox/seed, broadcast idle, git pull |
| Peer idle | 30s | Heartbeat >10min or 0 sessions | Send idle-wake (max 1 per peer per 30min) |

## Reading Watchdog Status

```bash
# Current watchdog state
curl -s http://127.0.0.1:8855/watchdog/status | python3 -m json.tool
```

Response:
```json
{
  "running": true,
  "uptimeSeconds": 86400,
  "checks": {
    "heartbeat": {"status": "ok", "lastRun": "2s ago", "failures": 0},
    "nats": {"status": "ok", "lastRun": "15s ago", "failures": 0},
    "http": {"status": "degraded", "lastRun": "8s ago", "failures": 2, "target": "node2"},
    "tunnel": {"status": "n/a", "reason": "tunnel not enabled"},
    "disk": {"status": "ok", "lastRun": "120s ago", "usageMb": 45}
  },
  "repairsTriggered": 3,
  "lastRepair": "2h ago — node2 http L1"
}
```

### Watchdog log

```bash
# Recent watchdog events (last 50)
curl -s http://127.0.0.1:8855/watchdog/log | python3 -m json.tool

# Filter by severity
curl -s "http://127.0.0.1:8855/watchdog/log?severity=warn" | python3 -m json.tool
```

Log levels: `info` (routine checks), `warn` (threshold approached), `error` (check failed), `repair` (auto-repair triggered).

## Configuration

In `.multifleet/config.json`:

```json
{
  "watchdog": {
    "enabled": true,
    "heartbeatStaleSeconds": 60,
    "natsFailThreshold": 2,
    "httpFailThreshold": 3,
    "taskTimeoutSeconds": 600,
    "diskLimitMb": 1024,
    "autoRepairLevel": "assist",
    "logRetentionHours": 48
  }
}
```

| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `true` | Enable watchdog monitoring |
| `heartbeatStaleSeconds` | `60` | Mark peer offline after this many seconds |
| `natsFailThreshold` | `2` | Consecutive NATS failures before repair |
| `httpFailThreshold` | `3` | Consecutive HTTP failures before repair |
| `taskTimeoutSeconds` | `600` | Kill tasks running longer than this |
| `diskLimitMb` | `1024` | Prune agent results above this size |
| `autoRepairLevel` | `"assist"` | Maximum auto-repair level (notify/guide/assist) |
| `logRetentionHours` | `48` | How long to keep watchdog logs |
| `idleThresholdSeconds` | `300` | Mark self idle after this many seconds without NATS activity |
| `peerIdleThresholdSeconds` | `600` | Consider peer idle after this many seconds |
| `idleWakeCooldownSeconds` | `1800` | Min interval between idle-wake messages to same peer |

## Environment Variables

| Variable | Description |
|----------|-------------|
| `FLEET_WATCHDOG_DISABLED` | Set to `1` to disable watchdog entirely |
| `FLEET_WATCHDOG_INTERVAL` | Override base check interval (seconds) |

## Idle Session Redirection

**Idle sessions are redirected to channel repair first.** When watchdog detects a session idle >5 minutes:

1. Check channel health to all peers (`/channels` endpoint)
2. If any P1/P2 channels are broken, send a repair task to the idle session
3. If all channels healthy, check for pending fleet tasks in the inbox
4. If no tasks, session remains available for dispatch

**5-minute cooldown between repair attempts** per peer. This prevents repair storms when a peer is genuinely unreachable. The cooldown resets when a channel state changes.

```bash
# Check idle session redirection state
curl -s http://127.0.0.1:8855/watchdog/idle | python3 -m json.tool
# Shows: which sessions are idle, what repair work they've been assigned, cooldown timers
```

## Interaction with Fleet Repair

Watchdog is the **trigger source** for the repair escalation system. When a check crosses its failure threshold:

1. Watchdog creates a repair request at the configured `autoRepairLevel`
2. Repair system handles escalation (see `fleet-repair` skill)
3. Watchdog continues monitoring — if the repair succeeds, failure count resets
4. If repair fails and threshold is hit again, watchdog escalates one level higher

The `autoRepairLevel` cap prevents watchdog from executing remote commands without human oversight. Set to `"notify"` for monitoring-only mode.

## Self-Healing Protocol Integration

Watchdog enforces the fleet-wide invariant: **the fleet is not healthy until ALL nodes have P1+P2 operational.**

- Every 30s, watchdog checks P1 (NATS) and P2 (HTTP) to all peers
- Broken channels are logged and repair is triggered automatically
- Idle sessions are conscripted for repair work before anything else
- 5-minute cooldown between repair attempts per peer prevents storms
- Repair success is verified by re-testing the channel immediately after fix
## Idle Proactivity Protocol

When a node has no NATS messages sent or received for >5 minutes, the watchdog activates idle proactivity:

**Self-idle detection:**
1. Scans `.fleet-messages/<node>/` for unprocessed `.md` files
2. Checks `/tmp/fleet-seed-<node>.md` for pending seed messages
3. Broadcasts idle status to the fleet (`fleet.all.message` with type `idle`)
4. Triggers git pull if behind origin (reuses git check cooldown)
5. Logs the idle event at `info` level for observability
6. Resets the activity timer after firing (prevents 30s spam)

**Peer-idle detection:**
- Peers with `sessions: 0` in heartbeat data or last heartbeat >10 minutes old are considered idle
- Sends a direct `idle-wake` message: "You've been idle for Xmin. Any pending fleet work? Check plans."
- Rate-limited: maximum 1 idle-wake per peer per 30 minutes
- Uses `self.node_id` and heartbeat data only — no hardcoded node names

**Activity tracking:**
- `_touch_activity()` is called on every message send and receive
- The idle timer only fires when there is genuinely no NATS traffic
## Idle Detection Protocol

The watchdog detects idle peers and converts downtime into productive work. Built on top of the heartbeat check already running every 10s.

**How it works:**

1. **Heartbeat monitoring** — every 30s, evaluate each peer's activity state from heartbeat metadata
2. **Idle threshold** — a peer is "idle" if its last task completion was >5 minutes ago AND no active task is running
3. **Productive nudge** — when a peer crosses the idle threshold, send a nudge via the working fleet channel containing:
   - Pending backlog items (from `docs/plans/` incomplete markers)
   - Recent plan documents that need review
   - Suggested tasks based on recent git activity
4. **Fleet-wide idle** — if ALL peers are idle simultaneously, propose coordinated productive work: test suites, documentation passes, dependency upgrades, or technical debt reduction

**Implementation reference:** Extends the existing heartbeat check in the watchdog loop. The idle state is derived from heartbeat metadata fields (`lastTaskCompleted`, `activeTask`). Nudges are sent via the same NATS/HTTP channels used for fleet messaging.

```bash
# Check idle status of all peers
curl -s http://127.0.0.1:8855/watchdog/status | python3 -c "
import json, sys; d=json.load(sys.stdin)
for p,s in d.get('peers',{}).items():
    print(f'{p}: idle={s.get(\"idleSeconds\",\"?\")}s  lastTask={s.get(\"lastTask\",\"none\")}')
"
```

## Proactive Task Generation

When idle, the watchdog shifts from monitoring to productive work generation.

**Task discovery sources (checked in order):**

| Source | What to look for | Priority |
|--------|-----------------|----------|
| `docs/plans/` | Incomplete items, TODOs, unstarted phases | High |
| `.fleet-messages/` | Unanswered proposals, pending reviews | High |
| `git log --since=24h` | Recent changes lacking tests or docs | Medium |
| `docs/inbox/` | Unprocessed raw ideas needing triage | Low |
| Plugin/skill health | Broken or degraded capabilities | Medium |

**What the watchdog does with discovered tasks:**

1. **Generate task suggestions** — formatted as fleet messages with context, rationale, and estimated effort
2. **Send via fleet messaging** — routed to the most appropriate peer based on current focus (from `.claude/focus.json`)
3. **Spawn background agents** — for non-blocking investigation only (read-only exploration, never writes without approval):
   - Scan for test coverage gaps in recently changed files
   - Check for stale dependencies or security advisories
   - Identify dead code or unused imports in active project

```bash
# View generated task suggestions
curl -s http://127.0.0.1:8855/watchdog/suggestions | python3 -m json.tool

# View background investigation results
ls /tmp/atlas-agent-results/watchdog-*.json
```

## Big Picture Awareness

Idle time is study time. The watchdog uses downtime to build and maintain system-wide understanding.

**What the watchdog studies:**

- **CLAUDE.md** — project-wide instructions, protocols, invariants. Detects drift between documented and actual behavior
- **docs/MULTI-FLEET.md** — fleet architecture, communication patterns. Validates current topology matches docs
- **3-surgeons plugin health** — probes all three surgeons (`3s probe`), reports degradation
- **superpowers plugin health** — verifies skill availability and tool reachability
- **Technical debt identification** — flags files with high churn + low test coverage, TODOs older than 7 days, functions exceeding complexity thresholds
- **Missing test detection** — cross-references `git diff --stat` against test directories to find untested changes
- **Improvement proposals** — synthesizes findings into concrete suggestions posted to `.fleet-messages/` for fleet review

**Awareness outputs are written to:**
- `/tmp/atlas-agent-results/watchdog-awareness-<timestamp>.json` (detailed findings)
- Fleet message channel (summary proposals for peer review)

## Rate Limiting

Idle detection and proactive work are rate-limited to prevent noise and interruption of active work.

| Action | Limit | Rationale |
|--------|-------|-----------|
| Productive nudge per peer | 1 per 5 minutes | Avoid nagging — one nudge is enough |
| Self-assigned background task | 1 per 10 minutes | Prevent resource contention on local machine |
| Fleet-wide idle proposal | 1 per 15 minutes | Coordinated work needs breathing room |
| Background agent spawn | Max 3 concurrent | Stay within crash-prevention limits (CLAUDE.md: max 10 agents) |
| Big picture scan | 1 per 30 minutes | Study is low-priority, yields to any real work |

**Hard rules:**
- **Never interrupt active work** — if a peer's heartbeat shows `activeTask` is set, suppress all idle suggestions for that peer
- **Never send nudges during repairs** — if the repair system is active, idle detection pauses entirely
- **Cooldown after task assignment** — after a peer accepts a suggested task, suppress nudges for that peer for 10 minutes

**Configuration** (in `.multifleet/config.json`):

```json
{
  "watchdog": {
    "idleDetection": {
      "enabled": true,
      "idleThresholdSeconds": 300,
      "nudgeIntervalSeconds": 300,
      "selfTaskIntervalSeconds": 600,
      "maxBackgroundAgents": 3,
      "bigPictureScanIntervalSeconds": 1800
    }
  }
}
```

## When to Use This Skill

- Understanding why a node was marked offline or degraded
- Tuning alert thresholds for your network conditions
- Debugging why repairs keep triggering (check watchdog log for patterns)
- Disabling auto-repair during maintenance windows
- Monitoring fleet health over time (log retention)
- Configuring idle detection thresholds and nudge frequency
- Reviewing proactive task suggestions generated during idle time
- Checking big picture awareness findings and improvement proposals
