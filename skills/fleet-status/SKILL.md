---
name: fleet-status
description: Quick fleet health check — who's online, who's idle, what's everyone working on
---

# Fleet Status

Instant situational awareness of the entire fleet.

## Quick Check

```bash
# One-liner summary
curl -s http://127.0.0.1:8855/fleet/live | python3 -c "import sys,json;print(json.load(sys.stdin)['fleetStatus'])"
# → "3/3 online: mac1 active, mac2 idle 5m, mac3 working"

# Full node details
curl -s http://127.0.0.1:8855/fleet/live | python3 -m json.tool
```

## Status Classifications

| Status | Meaning | idle_s threshold |
|--------|---------|-----------------|
| `working` | Has active fleet tasks running | activeTasks > 0 |
| `active` | Session recently active | < 300s (5m) |
| `idle Nm` | Session quiet for N minutes | 300-900s |
| `dormant Nm` | Session quiet for N+ minutes | > 900s (15m) |
| `offline` | No heartbeat received | No UDP heartbeat in 60s |

## Data Sources

- **Local node**: Direct API call (instant)
- **Remote peers**: Cached UDP heartbeats (updated every 10s, stale after 60s)
- **Fallback**: 1s HTTP probe if heartbeat cache is stale

## What Each Node Reports

- Active session count
- Idle time (seconds since last activity)
- Git branch and last commit
- 3-Surgeons availability (cardiologist, neurologist)
- Active task count and recent task log tails
