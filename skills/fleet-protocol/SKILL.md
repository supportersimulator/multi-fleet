---
name: fleet-protocol
description: CORE SKILL — self-healing communication invariant, channel priority, repair escalation, idle productivity, secrets protection, and background healing agents
---

# Fleet Protocol

The self-healing communication invariant. Every machine installing multi-fleet gets this automatically. This skill defines HOW the fleet communicates, heals, and stays productive. All other fleet skills operate within this protocol.

**Invariant**: Messages ALWAYS deliver. If the ideal channel fails, the next one fires. If a non-ideal channel succeeds, the protocol heals the broken ones. No silent failures. No stuck states.

## Channel Priority Sequence

Every message tries channels in strict priority order. Each channel gets **30 seconds max** before the next one fires. First success wins.

```
P0: Cloud RemoteTrigger  (<5s)   -- cross-network, works through NAT/firewalls
P1: NATS pub/sub         (<2s)   -- real-time, bidirectional, lowest latency
P2: HTTP direct          (<3s)   -- peer-to-peer daemon API, no central dependency
P3: Chief relay          (<5s)   -- queues for offline nodes, central routing
P4: Seed file            (<5s)   -- write to target's seed dir (local or SSH)
P5: SSH direct           (<10s)  -- reliable, works through most firewalls
P6: Wake-on-LAN          (<60s)  -- wake sleeping machines, then retry P0-P5
P7: Git push             (<30s)  -- guaranteed eventual delivery via commit
```

### Channel characteristics

| Channel | Latency | Bidirectional | Works offline | Requires |
|---------|---------|---------------|---------------|----------|
| P0 Cloud | <5s | Yes | No | Internet + API key |
| P1 NATS | <100ms | Yes | No | NATS server running |
| P2 HTTP | <1s | Request/response | No | Target daemon on :8855 |
| P3 Chief | 1-2s | Via relay | Queues | Chief node online |
| P4 Seed | Next prompt | No | Partial | SSH or local access |
| P5 SSH | 2-5s | Command exec | No | SSH keys configured |
| P6 WoL | 10-60s | No | Wake only | LAN access + MAC addr |
| P7 Git | Async | No | Eventual | Shared git repo |

### 30-second rule

Each channel gets exactly 30 seconds. This is enforced in `send_with_fallback()`:

```python
# From tools/fleet_nerve_nats.py — simplified
async def send_with_fallback(self, target: str, message: dict) -> dict:
    """Try all channels in priority order. 30s max per channel."""
    channels = [
        ("P0_cloud",  self._send_p0_cloud),
        ("P1_nats",   self._send_p1_nats),
        ("P2_http",   self._send_p2_http),
        ("P3_chief",  self._send_p3_chief),
        ("P4_seed",   self._send_p4_seed),
        ("P5_ssh",    self._send_p5_ssh),
        ("P6_wol",    self._send_p6_wol),
        ("P7_git",    self._send_p7_git),
    ]

    errors = []
    for name, handler in channels:
        try:
            result = await asyncio.wait_for(handler(target, message), timeout=30.0)
            if result.get("delivered"):
                # If we fell back past P2, trigger self-healing
                if name >= "P3_chief":
                    failed = [e["channel"] for e in errors]
                    asyncio.create_task(
                        self.repair_with_escalation(target, failed)
                    )
                return {"delivered": True, "channel": name, "id": message["id"]}
        except (asyncio.TimeoutError, Exception) as e:
            errors.append({"channel": name, "error": str(e)})

    return {"delivered": False, "errors": errors}
```

### When each channel is used

| Scenario | Primary | Fallback |
|----------|---------|----------|
| Both nodes on same LAN, daemons running | P1 NATS | P2 HTTP |
| Target behind NAT, no VPN | P0 Cloud | P3 Chief |
| Target daemon down, SSH works | P5 SSH | P4 Seed |
| Target machine sleeping | P6 WoL | then retry P0-P5 |
| All real-time channels dead | P7 Git | eventual delivery |

## User Input Injection is LAST PRIORITY ONLY

**NEVER type into the user's Claude Code session input as a first action.** Keystroke injection via `osascript` disrupts the user's flow, may corrupt their in-progress typing, and feels invasive.

### Preferred delivery order (for reaching a Claude session)

```
1. NATS → daemon receives → injects as seed file (silent, next prompt)
2. HTTP → POST to daemon /message endpoint (silent, next prompt)
3. Seed file → SSH write to ~/.fleet-messages/<node>/ (silent, next prompt)
4. osascript keystroke → ABSOLUTE LAST RESORT (disruptive, may lose user input)
```

**Keystroke injection rules:**
- Only after ALL other channels have failed (P0 through P5)
- Only for `alert` type messages (never for `context` or `task`)
- Only when the target session appears idle (no keystrokes in >60s)
- Must prefix injected text with `[FLEET ALERT]` so the user knows it was injected
- Log the injection to the fleet dashboard as a `warn` event

```bash
# BAD — never do this as first action
osascript -e 'tell application "System Events" to keystroke "/fleet-read"'

# GOOD — deliver via daemon, session reads on next prompt
curl -sf -X POST http://127.0.0.1:8855/message \
  -H "Content-Type: application/json" \
  -d '{"type":"context","from":"mac2","to":"mac1","payload":{"subject":"FYI","body":"..."}}'
```

## Self-Healing Invariant

**When a message succeeds on a non-ideal channel (P3+), the protocol MUST heal the broken channels.** This is not optional. A fleet that degrades without repairing is a fleet that will eventually go dark.

### What triggers self-healing

Any delivery that falls back past P2 HTTP. If P0/P1/P2 were all available, the message would have used them. Falling back means something is broken.

### What self-healing does

1. **Log which channels failed and why** — error type, timestamp, target node
2. **Send a repair request via the working channel** — the target node receives instructions to fix its broken channels
3. **Escalate through 4 levels** with timeouts between each

### Repair escalation chain

```
L1 Notify  (0-60s)    — Log failure, update dashboard, send context to target
L2 Guide   (60-120s)  — Send diagnostic instructions to target's session
L3 Assist  (120-180s) — Spawn repair agent on target via SSH
L4 Remote  (180s+)    — Present remote fix commands to human for approval
```

**Critical constraints:**
- **L4 only fires after 3+ minutes of failure AND no active session on target** — never SSH-restart another node as the first action
- Each level waits for the previous to take effect before escalating
- Rate limited: max 3 repair attempts per hour per channel per node
- `repair_with_escalation()` returns immediately after the first level that succeeds

```python
# From tools/fleet_nerve_nats.py — simplified
async def repair_with_escalation(
    self, target: str, broken_channels: list[str], action: str = None
) -> dict:
    """4-level repair escalation. Stops on first success."""

    # L1: Notify — log + send context message
    await self.send_with_fallback(target, {
        "type": "context",
        "payload": {
            "subject": f"[FLEET REPAIR] Channels degraded: {', '.join(broken_channels)}",
            "body": f"Channels {broken_channels} failed delivering to you. "
                    f"Check: daemon running? NATS connected? Port 8855 open?"
        }
    })

    # Probe: did P1/P2 recover?
    probe = await self._send_p1_nats(target, {"type": "sync", "payload": {"subject": "probe"}})
    if probe.get("delivered"):
        return {"repaired": True, "level": 1}

    # L2: Guide — send detailed repair steps
    await asyncio.sleep(60)
    await self.send_with_fallback(target, {
        "type": "context",
        "payload": {
            "subject": "[FLEET REPAIR L2] Diagnostic steps",
            "body": "Run: pgrep -f fleet_nerve && lsof -i :8855 && nats pub test ping"
        }
    })

    probe = await self._send_p1_nats(target, {"type": "sync", "payload": {"subject": "probe"}})
    if probe.get("delivered"):
        return {"repaired": True, "level": 2}

    # L3: Assist — spawn repair agent via SSH
    await asyncio.sleep(60)
    peer = self._peers_config.get(target, {})
    if peer.get("user") and peer.get("ip"):
        subprocess.run([
            "ssh", f"{peer['user']}@{peer['ip']}",
            "claude", "-p", "Check fleet daemon: pgrep -f fleet_nerve, restart if needed, verify NATS"
        ], timeout=120)

    probe = await self._send_p1_nats(target, {"type": "sync", "payload": {"subject": "probe"}})
    if probe.get("delivered"):
        return {"repaired": True, "level": 3}

    # L4: Remote — present to human (ONLY after 3min+ AND no active session)
    await asyncio.sleep(60)
    return {
        "repaired": False, "level": 4,
        "commands": [
            f"ssh {peer.get('user','')}@{peer.get('ip','')} 'launchctl kickstart -k gui/$(id -u)/com.multifleet.nerve'",
            f"ssh {peer.get('user','')}@{peer.get('ip','')} 'brew services restart nats-server'",
        ],
        "note": "Present these commands to Aaron for approval. Do NOT execute without human confirmation."
    }
```

### Self-healing flowchart

```
Message delivered via P3+ channel
  │
  ├─ Log: which channels failed, error types, timestamps
  │
  ├─ L1 NOTIFY (immediate)
  │   ├─ Update fleet dashboard: node degraded
  │   ├─ Send context message to target with failure details
  │   └─ Probe P1/P2 → recovered? STOP
  │
  ├─ L2 GUIDE (after 60s)
  │   ├─ Send diagnostic instructions to target session
  │   └─ Probe P1/P2 → recovered? STOP
  │
  ├─ L3 ASSIST (after 120s)
  │   ├─ SSH into target, spawn claude -p repair agent
  │   └─ Probe P1/P2 → recovered? STOP
  │
  └─ L4 REMOTE (after 180s, ONLY if no active session)
      └─ Present remote fix commands to human for approval
```

## Idle Detection and Proactive Work

When fleet peers have no activity for 5+ minutes, idle time is productive time.

### Detection

The daemon tracks last activity per node via heartbeat timestamps:

```bash
# Check which nodes are idle
curl -s http://127.0.0.1:8855/fleet/live | python3 -c "
import sys, json
data = json.load(sys.stdin)
for node in data.get('nodes', []):
    idle = node.get('idle_seconds', 0)
    if idle > 300:
        print(f\"{node['id']}: idle {idle//60}m — available for work\")
"
```

### What to do with idle nodes

Ordered by priority:

1. **Check fleet plans/backlog** — are there queued tasks waiting for capacity?
   ```bash
   curl -s http://127.0.0.1:8855/tasks/queued | python3 -m json.tool
   ```

2. **Read docs/plans/ for the big picture** — understand what the fleet is building toward
   ```bash
   # On the idle node, read current plans
   ls docs/plans/*.md docs/vision/*.md 2>/dev/null
   ```

3. **Propose productive tasks via fleet messaging** — send suggestions, not commands
   ```bash
   curl -sf -X POST http://127.0.0.1:8855/message -H "Content-Type: application/json" \
     -d '{"type":"context","from":"mac1","to":"mac3","payload":{
       "subject":"Idle suggestion: run test suite",
       "body":"You have been idle 8m. Suggestion: run full test suite on context-dna and report results. Or pick from backlog: [task1, task2]."
     }}'
   ```

4. **Spawn background agents for non-blocking work** — use the worker pool
   ```bash
   curl -sf -X POST http://127.0.0.1:8855/message -H "Content-Type: application/json" \
     -d '{"type":"task","from":"mac1","to":"mac3","payload":{
       "subject":"Background: lint and type-check",
       "body":"Run linting and type-checking on all Python files. Report issues. Use worker pool (non-blocking).",
       "useWorker": true
     }}'
   ```

### Idle work constraints

- **Never dispatch to a node that just became idle** — wait the full 5 minutes
- **Prefer non-blocking worker pool tasks** — never disrupt a session that might resume
- **Suggest, don't command** — idle context messages are suggestions; idle task messages use `useWorker: true`
- **Check backlog first** — prioritize queued work over invented work

## Secrets Protection

**Zero hardcoded secrets. Zero committed IPs. All transit encrypted.**

### API keys

```python
# CORRECT — keys from Keychain or environment
import subprocess, os

def get_api_key(service: str) -> str:
    """Resolve API key: Keychain → AWS Secrets → env var. Never hardcoded."""
    # 1. macOS Keychain (preferred)
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass

    # 2. Environment variable
    env_key = f"{service.upper().replace('-', '_')}_API_KEY"
    if os.environ.get(env_key):
        return os.environ[env_key]

    # 3. AWS Secrets Manager (if available)
    try:
        result = subprocess.run(
            ["aws", "secretsmanager", "get-secret-value",
             "--secret-id", service, "--query", "SecretString", "--output", "text"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass

    raise RuntimeError(f"No API key found for {service}. Add to Keychain: "
                      f"security add-generic-password -s {service} -a fleet -w 'YOUR_KEY'")
```

```python
# WRONG — never do this
OPENAI_KEY = "sk-abc123..."  # FORBIDDEN — hardcoded secret
NATS_URL = "nats://${NATS_HOST}:4222"  # FORBIDDEN — hardcoded IP
```

### IP addresses

All IPs come from `.multifleet/config.json`, never from source code:

```python
# CORRECT — IP from config
import json

def get_peer_ip(node_id: str) -> str:
    with open(".multifleet/config.json") as f:
        config = json.load(f)
    return config["nodes"][node_id]["host"]
```

```bash
# config.json is .gitignored — never committed
# config.template.json has placeholders, no real IPs
```

### Transit encryption

| Channel | Encryption | How |
|---------|-----------|-----|
| P0 Cloud | TLS | HTTPS to cloud API |
| P1 NATS | TLS | `nats://` → `tls://` in production config |
| P2 HTTP | SSH tunnel | Daemon traffic routed through `fleet-tunnel` |
| P3 Chief | SSH tunnel | Same as P2 |
| P4 Seed | SSH | File written via `scp` or `ssh cat >` |
| P5 SSH | SSH | Native encryption |
| P7 Git | SSH/HTTPS | Git transport encryption |

### What to audit

```bash
# Check for leaked secrets in committed code
grep -rn "sk-\|api_key\s*=\s*['\"]" --include="*.py" --include="*.sh" --include="*.json" .
# Should return 0 results (excluding template placeholders)

# Check config.json is gitignored
grep "config.json" .gitignore
# Should match: .multifleet/config.json or equivalent
```

## Background Healing Agents

When broken channels are detected, the protocol spawns **invisible** background agents to diagnose and fix the problem. These agents never disrupt the user's active session.

### How it works

```python
async def spawn_healing_agent(self, target: str, broken_channels: list[str]):
    """Background agent: diagnose and fix broken channels. Invisible to user."""

    diagnosis_prompt = f"""You are a fleet healing agent. Diagnose and fix these broken channels
to {target}: {', '.join(broken_channels)}.

Steps:
1. DIAGNOSE — for each broken channel, determine root cause:
   - P0 Cloud: is internet reachable? API key valid? curl -sf https://api.example.com/health
   - P1 NATS: is nats-server running? nats pub test ping 2>&1
   - P2 HTTP: is daemon running? curl -sf http://{target_ip}:8855/health
   - P5 SSH: can we reach the host? ssh -o ConnectTimeout=5 {user}@{target_ip} echo ok

2. FIX — based on diagnosis:
   - Daemon down → launchctl kickstart -k gui/$(id -u)/com.multifleet.nerve
   - NATS down → brew services restart nats-server
   - Tunnel dead → bash scripts/fleet-tunnel.sh restart
   - IP changed → update .multifleet/config.json with new IP
   - Firewall blocking → establish SSH tunnel as workaround

3. VERIFY — after fix, probe the channel again to confirm repair.

4. REPORT — write results to /tmp/atlas-agent-results/fleet-heal-{target}-{timestamp}.log
   and send a sync message back to the fleet.

CONSTRAINTS:
- Do NOT restart services you did not diagnose as broken
- Do NOT modify config without backing up first
- Do NOT touch the user's active Claude session
- Time limit: 120 seconds
"""

    # Spawn via worker pool (tmux-isolated, invisible to user)
    subprocess.Popen([
        "claude", "-p", diagnosis_prompt,
        "--output-file", f"/tmp/atlas-agent-results/fleet-heal-{target}-{int(time.time())}.log"
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
```

### Agent lifecycle

```
Broken channel detected
  │
  ├─ Spawn healing agent in tmux worker pool
  │   ├─ Agent diagnoses each broken channel
  │   ├─ Agent attempts fix (safe actions only)
  │   ├─ Agent verifies fix worked
  │   └─ Agent writes results to /tmp/atlas-agent-results/
  │
  ├─ Results flow back to fleet via sync message
  │
  └─ Watchdog picks up recovery on next health check
```

### What agents can and cannot do

| Allowed | Forbidden |
|---------|-----------|
| Restart local daemon (`launchctl kickstart`) | Restart remote daemon as first action |
| Restart local NATS (`brew services restart`) | Modify another node's config |
| Re-establish SSH tunnel | Delete any data |
| Update local config (with backup) | Access secrets beyond what's in Keychain |
| Probe remote endpoints | Type into user's active session |
| Write results to `/tmp/atlas-agent-results/` | Run for longer than 120 seconds |

### Reading healing results

```bash
# Recent healing agent results
ls -la /tmp/atlas-agent-results/fleet-heal-*.log

# Latest result for a specific target
cat /tmp/atlas-agent-results/fleet-heal-mac2-*.log | tail -20

# Healing stats from daemon
curl -s http://127.0.0.1:8855/repair/log | python3 -c "
import sys, json
repairs = json.load(sys.stdin)
for r in repairs[-5:]:
    print(f\"{r['timestamp']} | {r['target']} | L{r['level']} | {r['result']}\")
"
```

## Complete Protocol Flow

Putting it all together — what happens when a node sends a message:

```
1. Compose message (type, target, payload)
2. Assign ID + timestamp if missing
3. Try P0 Cloud (30s timeout)
   ├─ Success → delivered, done
   └─ Fail → log error, try P1
4. Try P1 NATS (30s timeout)
   ├─ Success → delivered, done
   └─ Fail → log error, try P2
5. Try P2 HTTP (30s timeout)
   ├─ Success → delivered, done
   └─ Fail → log error, try P3
6. Try P3 Chief relay (30s timeout)
   ├─ Success → delivered + TRIGGER SELF-HEALING for P0-P2
   └─ Fail → try P4
7. Try P4 Seed file (30s timeout)
   ├─ Success → delivered + TRIGGER SELF-HEALING for P0-P3
   └─ Fail → try P5
8. Try P5 SSH (30s timeout)
   ├─ Success → delivered + TRIGGER SELF-HEALING for P0-P4
   └─ Fail → try P6
9. Try P6 WoL (30s timeout)
   ├─ Wake success → retry P0-P5 once
   └─ Fail → try P7
10. Try P7 Git (30s timeout)
    ├─ Success → delivered (eventual) + TRIGGER SELF-HEALING for P0-P6
    └─ Fail → message queued in outbox, exponential retry
```

## When to Use This Skill

- **Always** — this protocol is the foundation for all fleet communication
- Before implementing any new fleet feature, verify it respects the channel priority
- When debugging delivery failures, trace through this protocol to find the break point
- When adding a new channel, it must slot into this priority sequence
- When reviewing fleet code, verify self-healing triggers on P3+ fallback

## Integration with Other Skills

| Skill | Relationship |
|-------|-------------|
| **fleet-send** | Calls `send_with_fallback()` — the protocol's primary entry point |
| **fleet-check** | Tests each channel individually — protocol verification |
| **fleet-repair** | Implements the L1-L4 escalation chain defined here |
| **fleet-watchdog** | Continuously monitors channel health, triggers repair |
| **fleet-wake** | Implements P6 WoL channel |
| **fleet-tunnel** | Enables P2 HTTP through firewalls via SSH tunnel |
| **fleet-worker** | Executes background healing agents in tmux isolation |
| **fleet-config-gate** | Protects config changes that could break channels |
| **fleet-dispatch-gate** | Verifies target reachability before task dispatch |
