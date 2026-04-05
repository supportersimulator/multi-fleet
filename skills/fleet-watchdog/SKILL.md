---
name: fleet-watchdog
description: Proactive fleet health monitoring — channel checks, threshold alerts, automatic repair triggers
---

# Fleet Watchdog

Continuous background monitor that checks fleet health and triggers repairs before humans notice problems. Runs as part of the Fleet Nerve daemon.

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

## Environment Variables

| Variable | Description |
|----------|-------------|
| `FLEET_WATCHDOG_DISABLED` | Set to `1` to disable watchdog entirely |
| `FLEET_WATCHDOG_INTERVAL` | Override base check interval (seconds) |

## Interaction with Fleet Repair

Watchdog is the **trigger source** for the repair escalation system. When a check crosses its failure threshold:

1. Watchdog creates a repair request at the configured `autoRepairLevel`
2. Repair system handles escalation (see `fleet-repair` skill)
3. Watchdog continues monitoring — if the repair succeeds, failure count resets
4. If repair fails and threshold is hit again, watchdog escalates one level higher

The `autoRepairLevel` cap prevents watchdog from executing remote commands without human oversight. Set to `"notify"` for monitoring-only mode.

## When to Use This Skill

- Understanding why a node was marked offline or degraded
- Tuning alert thresholds for your network conditions
- Debugging why repairs keep triggering (check watchdog log for patterns)
- Disabling auto-repair during maintenance windows
- Monitoring fleet health over time (log retention)
