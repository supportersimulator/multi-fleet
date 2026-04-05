---
name: fleet-healer
description: Invariance layer — spawns background agents that automatically heal broken fleet communication channels until all reach ideal state
---

# Fleet Healer

The invariance layer for fleet communication. When channels degrade, healing agents spawn in the background — invisible to the user — and work autonomously to restore all channels to ideal state. The only acceptable steady state is **all channels working on all nodes**.

## When to Spawn Healing Agents

| Trigger | Source | Urgency |
|---------|--------|---------|
| `send_with_fallback()` falls through to P3+ | Delivery system | High — message used non-ideal channel |
| Watchdog detects stale peer (>90s heartbeat age) | Watchdog monitor | Medium — peer may be unreachable |
| Session start with degraded fleet health | Session init | Medium — inherited problem |
| `repair_with_escalation()` reaches L3+ | Repair system | High — lower levels failed |

## What Healing Agents Do

Each agent runs in the background via the Agent tool with `run_in_background=true`. They are invisible to the user and report results to `/tmp/atlas-agent-results/`.

### Channel Diagnoser

Probes all 8 channels to each peer, reports which work and why others fail.

```python
# Spawn a channel diagnoser for a specific peer
Agent(
    prompt="""Diagnose all fleet channels to peer 'mac1':
    1. Check NATS: python3 tools/fleet_nerve_nats.py ping mac1
    2. Check HTTP direct: curl -sf http://<mac1-ip>:8855/health
    3. Check HTTP tunnel: curl -sf http://127.0.0.1:<tunnel-port>/health
    4. Check SSH: ssh -o ConnectTimeout=5 <mac1-user>@<mac1-ip> echo ok
    5. Check seed file: ls -la .fleet-messages/mac1/
    Write results to /tmp/atlas-agent-results/heal-diagnose-mac1-$(date +%s).json""",
    run_in_background=True
)
```

### Daemon Restarter

If the target daemon is down, sends restart instructions via SSH seed.

```python
Agent(
    prompt="""Fleet daemon on 'mac3' is not responding.
    1. SSH in: ssh <mac3-user>@<mac3-ip> "pgrep -f fleet_nerve || echo DEAD"
    2. If dead, restart: ssh <mac3-user>@<mac3-ip> "launchctl kickstart -k gui/$(id -u)/com.multifleet.nerve"
    3. Wait 10s, verify: curl -sf http://<mac3-ip>:8855/health
    4. Write result to /tmp/atlas-agent-results/heal-daemon-mac3-$(date +%s).json""",
    run_in_background=True
)
```

### Tunnel Establisher

If direct HTTP fails, checks and creates SSH tunnels.

```python
Agent(
    prompt="""Direct HTTP to 'mac1' is failing. Establish SSH tunnel.
    1. Check existing tunnel: pgrep -f "ssh.*-L.*8856.*mac1"
    2. If none, create: ssh -f -N -L 8856:<mac1-ip>:8855 <mac1-user>@<mac1-ip>
    3. Verify tunnel: curl -sf http://127.0.0.1:8856/health
    4. If tunnel also fails, check if SSH itself works: ssh -o ConnectTimeout=5 <mac1-user>@<mac1-ip> echo ok
    5. Write result to /tmp/atlas-agent-results/heal-tunnel-mac1-$(date +%s).json""",
    run_in_background=True
)
```

### Config Updater

Detects IP changes and updates config (e.g., mac2 `.254` to `.196`).

```python
Agent(
    prompt="""Peer 'mac2' is unreachable at configured IP.
    1. Read current config: cat .multifleet/config.json | python3 -c "import sys,json; c=json.load(sys.stdin); print(c['peers']['mac2'])"
    2. Scan local network for mac2: arp -a | grep -i <mac2-mac-address>
    3. If new IP found, update config.json with new IP
    4. Re-probe: curl -sf http://<new-ip>:8855/health
    5. Write result to /tmp/atlas-agent-results/heal-config-mac2-$(date +%s).json""",
    run_in_background=True
)
```

### NATS Reconnector

If NATS peers are stale, attempts reconnect or tunnel restart.

```python
Agent(
    prompt="""NATS connectivity to 'mac3' is stale.
    1. Check local NATS: nats server check connection
    2. Check NATS routes: nats server report jetstream 2>/dev/null || nats server report connections
    3. If no route to mac3, check if NATS tunnel is alive: pgrep -f "ssh.*4222.*mac3"
    4. If tunnel dead, restart: ssh -f -N -L 4223:<mac3-ip>:4222 <mac3-user>@<mac3-ip>
    5. Verify: python3 tools/fleet_nerve_nats.py ping mac3
    6. Write result to /tmp/atlas-agent-results/heal-nats-mac3-$(date +%s).json""",
    run_in_background=True
)
```

## Healing Loop

The healing loop runs until convergence or escalation. Each iteration is a background agent.

```
while not all_channels_ideal():
  diagnose() -> identify broken channels per peer
  for each broken channel:
    spawn appropriate healer agent (background)
    wait(30s)
    re-probe channel
  if improved: log success, continue checking remaining
  if stuck after 3 attempts: escalate to human via seed file
```

### Practical Implementation

```python
# Master healer agent — orchestrates the loop
Agent(
    prompt="""Fleet Healer: restore all channels to ideal state.

    LOOP (max 3 iterations):
    1. Run: curl -s http://127.0.0.1:8855/health | python3 -m json.tool
    2. For each peer with non-ideal channels:
       a. If HTTP down: check tunnel, restart if needed
       b. If NATS stale: check NATS route, reconnect
       c. If daemon down: SSH restart
       d. If IP changed: detect and update config
    3. Wait 30s between attempts
    4. Re-probe after each fix

    EXIT CONDITIONS:
    - All channels ideal -> write success to /tmp/atlas-agent-results/heal-complete-$(date +%s).json
    - 3 iterations with no improvement -> write escalation to .fleet-messages/<node>/heal-escalation.md

    RULES:
    - SSH keys are in ~/.ssh/ (never pass as arguments)
    - Never send API keys via fleet messages
    - Config updates use detected IPs from arp/network scan only
    - Log every action to /tmp/atlas-agent-results/heal-log-$(date +%s).json""",
    run_in_background=True
)
```

## Invariance Guarantee

The fleet MUST converge to ideal communication state. This is not aspirational — it is an invariant.

| State | Response |
|-------|----------|
| All channels ideal | No action. Watchdog confirms every 30s. |
| One channel degraded | Healer spawns automatically. Fixes within 1-3 minutes. |
| Multiple channels degraded | Parallel healers for each broken channel. |
| All channels broken except seed | Seed file escalation to human. Healer writes instructions. |
| Stuck after 3 attempts | Human escalation via seed + fleet dashboard alert. |

If any channel degrades, healing agents activate automatically. The system does not require human intervention for L1-L3 fixes. Humans are only involved when automated healing exhausts its options.

## Secrets Protection in Healing

- SSH keys assumed in `~/.ssh/` — never passed as command arguments or embedded in messages
- API keys never sent via fleet messages or seed files
- Config updates use placeholder format in logs, actual IPs only in `config.json`
- Healing agent prompts reference config paths, not credential values
- All `/tmp/atlas-agent-results/heal-*` logs are sanitized (IPs shown, keys redacted)

## Integration with Other Skills

| Skill | Relationship |
|-------|-------------|
| `fleet-watchdog` | Watchdog detects problems, healer fixes them |
| `fleet-repair` | Repair handles escalation levels, healer orchestrates the loop |
| `fleet-tunnel` | Healer uses tunnel skills to re-establish broken tunnels |
| `fleet-wake` | Healer may trigger wake for unresponsive nodes before repair |
| `fleet-send` | After healing, delivery retries on restored channels |

## When to Use This Skill

- Fleet health shows any channel below ideal state
- Messages are consistently falling back to non-ideal channels (P3+)
- After network disruptions (router reboot, VPN change, IP reassignment)
- On session start when previous session left degraded channels
- When manual repair attempts have not resolved connectivity
