# Fleet Communications Protocol — Canonical Specification

**Status:** INVARIANT — baked into every installation
**Scope:** Single source of truth for all fleet communication behavior

---

## 1. Channel Priority Table

Every node follows the same ordered fallback chain. Try P1 first. On failure (timeout or error), fall to next. First confirmed delivery wins.

**Default timeout per channel: 30s max total cascade time.**

| Priority | Channel | Timeout | When to use | Requirements | Failure mode |
|----------|---------|---------|-------------|--------------|--------------|
| **P0** | RemoteTrigger cloud | 5s | Explicit invocation only, OR all-fail fallback (P1-P7 exhausted) | Cloud API credentials, internet | API error or timeout; log + alert sender |
| **P1** | NATS pub/sub | 3s | Always try first. Real-time, bidirectional, JetStream durable | NATS server reachable (port 4222, direct or via tunnel) | Target not subscribed or server down; fall to P2 |
| **P2** | HTTP direct | 5s | NATS unavailable or unresponsive | Target daemon running (port 8855) | Daemon down or port unreachable; fall to P3 |
| **P3** | Chief relay | 5s | Target daemon down, chief is up | Chief server running (port 8844) | Chief offline; fall to P4. Delivery eventual (target polls, up to 30s) |
| **P4** | Seed file via SSH | 10s | Network channels failed, target reachable via SSH | SSH credentials, target awake | Can't SSH or write to filesystem; fall to P5 |
| **P5** | SSH direct execution | 10s | Daemon/NATS down, machine awake, need command execution | SSH credentials | SSH auth failure or machine sleeping; fall to P6 |
| **P6** | Wake-on-LAN | 60s | Target machine is sleeping | WoL enabled, MAC address known, wired network (WiFi unreliable) | WoL disabled or WiFi-only; fall to P7 |
| **P7** | Git push | 30s | All real-time channels failed. Guaranteed eventual delivery | Git remote reachable | Push failure (auth, network); fall to P8 |
| **P8** | Direct text input | 2s | LAST RESORT. All P0-P7 exhausted | osascript, target has VS Code focused | Keystroke lost, wrong window focused |

### P0 Rules

P0 (RemoteTrigger cloud) is NOT part of the normal cascade. It activates in exactly two cases:

1. **Explicit invocation** — user or code explicitly requests cloud delivery
2. **All-fail fallback** — P1 through P7 have all been attempted and failed

P0 never fires during normal channel traversal. It is either the first choice (explicit) or the absolute last (all-fail).

### P8 Rules

See Section 5.

---

## 2. Self-Healing Invariant

**Every successful delivery via P3+ MUST trigger repair of P1-P2. The fleet is not healthy until ALL nodes have P1+P2 operational.**

When a message delivers on P3 or below:

1. Log the degradation: which channel worked, which failed, timestamp
2. Immediately send a `repair` message through the working channel to the target node
3. Target node attempts local self-repair (restart daemon, reconnect NATS)
4. Originating node re-probes P1-P2 after repair window (120s)
5. If still broken, escalate per Section 7

The fleet is in **ideal state** when every peer-to-peer link has P1 and P2 operational. Any state below this is actively being repaired.

---

## 3. Idle Protocol

Sessions idle >5min automatically pick up fleet work. Priority order:

1. **Channel repair** — fix any DEGRADED or BROKEN channels (Section 6)
2. **Plan items** — work next item from fleet plan/backlog
3. **Backlog** — lowest priority unclaimed work items

Idle detection requires BOTH conditions:
- No fleet message activity for 5+ minutes
- No Claude Code JSONL modification for 5+ minutes

A user actively coding is never "idle" regardless of fleet message silence.

---

## 4. Secrets Protection

**INVARIANT: Never log key values, session content, or auth tokens.**

Permitted in logs:
- Channel names (`nats`, `http`, `chief`, `ssh`)
- Peer names (`mac1`, `mac2`, `mac3`)
- Status codes (`200`, `timeout`, `refused`)
- Timestamps, latency, message IDs
- Message types (`task`, `context`, `alert`)

Forbidden in logs:
- API keys, tokens, passwords
- Message body content
- Session conversation text
- SSH private key material
- Environment variable values containing secrets

Violation of this invariant is a security incident. `except Exception: pass` near credential handling is forbidden — failures MUST be logged with channel/status only, never with credential content.

### 4.1 Message Authentication (HMAC)

All NATS messages are signed with HMAC-SHA256 using a fleet-wide shared secret. Implementation: `tools/fleet_nerve_security.py`.

**Signing**: Every outgoing message gets `_hmac` (hex digest) and `_signed_ts` (unix timestamp) fields. The HMAC covers the canonical JSON of the entire message (sorted keys, no whitespace, `_hmac` field excluded).

**Verification** (on every inbound message):
1. **Peer identity** — `from` field must match a known node ID from config. Unknown senders are rejected.
2. **HMAC signature** — recompute and compare (constant-time). Bad signatures are rejected.
3. **Replay prevention** — `_signed_ts` must be within 5 minutes of current time. Stale messages are rejected.

**Key management**:
- Stored in macOS Keychain as `fleet_nerve_hmac_key`
- On first run: auto-generates 32-byte random key and stores in keychain
- Override via `FLEET_NERVE_HMAC_KEY` env var (for CI/testing)
- Distribute to fleet: `security find-generic-password -s fleet_nerve_hmac_key -w | pbcopy`

**Graceful degradation**: If no HMAC key is available, messages are sent unsigned and unsigned messages are accepted. This prevents breaking existing fleet during key rollout. Once all nodes have the key, unsigned messages generate a log warning but are still accepted.

**Logging invariant**: Never log HMAC key or message content. Only log: message ID (first 8 chars), sender node ID, message type, and signature status (pass/fail/missing).

### 4.2 Session Gold Privacy

Session gold published on NATS (`fleet.sessions.*.gold`) is sanitized before publish. Only safe metadata is transmitted:
- `node_id`, `session_count`, `topic_keywords` (max 10, truncated to 50 chars each), `idle_s`, `session_id` (truncated to 8 chars), `timestamp`

Never published: full conversation text, file contents, code artifacts, API responses, error tracebacks, environment variables, credentials.

Implementation: `FleetSecurity.sanitize_gold_for_publish()` in `tools/fleet_nerve_security.py`.

---

## 5. Direct Text Input Rules (P8)

P8 (osascript keystroke injection) is ALWAYS the last resort. It exists because sometimes nothing else works.

**Constraints:**
- Rate limited: **1 keystroke injection per 30 seconds**
- Logged as **WARNING** level — every P8 invocation is a signal that 8 better channels failed
- Only attempted when P0-P7 have ALL been attempted and failed
- Requires target machine to have VS Code focused (best-effort detection)
- Content is plaintext only — no structured data, no JSON
- Maximum 500 characters per injection

**Every P8 invocation MUST trigger a repair cycle** for P1-P7. If P8 is firing regularly, the fleet is severely degraded.

---

## 6. Channel State Machine

Tracked per-peer, per-channel. Every `(peer, channel)` pair has exactly one state.

```
                 1 failure
  HEALTHY ──────────────────► DEGRADED
     ▲                            │
     │                            │ 2 more failures (3 total)
     │                            ▼
     │         repair succeeds   BROKEN
     └──────────────────────── HEALING ◄──── repair initiated
```

| State | Meaning | Behavior |
|-------|---------|----------|
| **HEALTHY** | Channel works. 0 recent failures. | Normal use in priority order |
| **DEGRADED** | 1 failure in last 5 minutes. May be transient. | Still attempt, but log. Probe health in background |
| **BROKEN** | 3+ failures in last 5 minutes. Channel is down. | Skip in cascade (save timeout). Trigger repair |
| **HEALING** | Repair in progress. | Skip in cascade. Monitor repair outcome |

**Transitions:**
- HEALTHY to DEGRADED: 1 delivery failure
- DEGRADED to BROKEN: 2 more failures (3 total within 5min window)
- BROKEN to HEALING: repair action initiated
- HEALING to HEALTHY: repair probe confirms channel works
- HEALING to BROKEN: repair probe fails
- DEGRADED to HEALTHY: successful delivery (auto-clears on next success)

**BROKEN channels are skipped** in the cascade to avoid wasting timeout budget. A full cascade with 2 BROKEN channels runs faster because it skips them.

**State reset:** All channel states reset to HEALTHY after 15 minutes of no failures (stale failure data expires).

---

## 7. Repair Escalation

When P1 or P2 enters BROKEN state, repair escalation begins automatically.

| Level | Action | Wait before next | Automation | Human needed? |
|-------|--------|-------------------|------------|---------------|
| **L1 Notify** | Log failure to fleet dashboard + local repair log | 0s (immediate) | Fully automatic | No |
| **L2 Guide** | Send repair instructions to target node's session via working channel | 120s | Fully automatic | Target session reads instructions |
| **L3 Background Agent** | SSH into target, spawn `claude -p` repair agent with diagnostic prompt | 300s | Fully automatic | No |
| **L4 SSH Remote** | Present specific fix commands to human for approval | N/A (terminal) | Requires approval | Yes |

**Escalation flow:**
```
Channel enters BROKEN state
  → L1: Log + dashboard alert (immediate)
  → L2: Send repair message to target (immediate)
  → Wait 120s — probe P1/P2
  → If recovered: done. If not:
  → L3: Spawn repair agent on target via SSH
  → Wait 300s — probe P1/P2
  → If recovered: done. If not:
  → L4: Surface commands to human. Only if failure >15min.
```

**Rate limit:** Maximum 3 repair escalations per node per hour. Prevents repair storms.

**Local-first principle:** NEVER directly SSH into another node to fix it as a first action. Always ask the target to fix itself first (L2). The local node knows its own state best. Remote intervention (L3-L4) is last resort.

**Repair message format:**
```json
{
  "type": "repair",
  "from": "<source>",
  "to": "<target>",
  "payload": {
    "subject": "NATS daemon not responding",
    "body": "Your NATS heartbeat is stale (>60s). Please restart.",
    "action": "restart_nats",
    "urgency": "high"
  }
}
```

Known auto-repair actions: `restart_nats`, `restart_daemon`, `restart_tunnel`, `git_pull`, `kill_zombie`. Unknown actions surface to user.

---

## 8. Fleet-Wide Goal

**The standing protocol is: always try preferred channels first, cascade down on failure (30s max per channel), and use whatever channel works to fix the broken ones. The fleet is self-healing — every node contributes to getting every other node to ideal communication state.**

Ideal state: every `(peer, channel)` pair for P1 and P2 is HEALTHY across the entire fleet. The fleet continuously converges toward this state. Degradation is detected in seconds, repair begins automatically, and human intervention is only requested when automated repair fails for 15+ minutes.

No node is a passive consumer. Every node:
- Monitors its own channel health
- Reports degradation to the fleet
- Repairs its own channels when asked
- Helps repair other nodes when it detects their degradation
- Uses idle time for repair before any other work

---

## 9. Installation Guarantee

**Any device installing this plugin gets automatic self-healing. No manual configuration needed beyond initial peer setup.**

On installation:
1. Peer registry is populated from `.multifleet/config.json` or `scripts/3s-network.local.conf`
2. Fleet Nerve daemon starts via LaunchAgent (auto-restarts on crash, survives reboots)
3. All channels are probed and initial state machine is populated
4. Heartbeat begins (UDP every 10s to all peers)
5. Idle watcher activates (checks every 60s)
6. Repair system arms automatically

The new node immediately:
- Appears in the fleet dashboard on all existing nodes
- Receives messages through whatever channels work
- Begins self-healing any broken channels
- Contributes to fleet-wide repair of other nodes

Zero ongoing maintenance. The protocol maintains itself.

---

## Retry Policy

| Channel | Retries | Backoff | Notes |
|---------|---------|---------|-------|
| P0 | 1 | 2s | Cloud API — minimal retry |
| P1 | 1 | 1s | Fast retry before falling to P2 |
| P2 | 1 | 1s | Fast retry before falling to P3 |
| P3 | 0 | N/A | Single attempt (eventual delivery via polling) |
| P4 | 0 | N/A | Single attempt (fire-and-forget write) |
| P5 | 0 | N/A | SSH is expensive — single attempt |
| P6 | WoL + poll | 10s intervals | 60s total wake window, then give up |
| P7 | 1 | 5s | Git push — retry once |
| P8 | 0 | N/A | Keystroke — single attempt, rate limited |

---

## Observability

Every send attempt produces a structured log entry:

```json
{
  "ts": "2026-04-05T14:30:00Z",
  "from": "mac1",
  "to": "mac2",
  "type": "task",
  "channel": "nats",
  "priority": "P1",
  "attempt": 1,
  "result": "ok",
  "latency_ms": 45,
  "channel_state": "HEALTHY"
}
```

Aggregated in `/health` as `stats.sends_by_channel`. Per-peer channel state exposed via `/peers` endpoint.

**No message body content in logs.** See Section 4.

---

## Message Type Routing

Message types influence which channels are appropriate:

| Type | Channels attempted | Behavior |
|------|-------------------|----------|
| `alert` | P1 only, retry 3x | Must confirm delivery. If all fail, escalate to macOS notify on sender |
| `task` | P1 through P3 | Needs active session on target. If none, queue on chief |
| `reply` | P1 through P2 | Sender is waiting. Fast channels only |
| `context` | P1 through P4 | Passive enrichment. Any channel works |
| `broadcast` | P1 only | Fire-and-forget to all peers. No fallback |
| `sync` | P1 through P3 | Silent bookkeeping. No escalation |
| `repair` | P1 through P5 | Uses whatever works. Critical for self-healing |
