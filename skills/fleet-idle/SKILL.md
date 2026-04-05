---
name: fleet-idle
description: Productive idle protocol — automatic work discovery and suggestion when fleet nodes are idle
---

# Fleet Productive Idle

When a fleet node has no active tasks for >5 minutes, the watchdog activates productive idle scanning. Instead of sitting silent, idle nodes discover and suggest useful work.

## How It Works

The `_productive_idle_scan()` method runs with a 10-minute cooldown. It scans three sources:

1. **Incomplete plan items** — `docs/plans/*.md` files with `[ ]`, `TODO`, or `UNRESOLVED` markers
2. **Unanswered fleet messages** — `.fleet-messages/<node>/` directories with pending `.md` files
3. **Untested commits** — `git log --since=24h` commits without "test" in the message

Results are written to `/tmp/fleet-watchdog-suggestions.json` and optionally broadcast to the fleet.

## HTTP Endpoint

### GET /watchdog/suggestions — View idle suggestions

```bash
curl -sf http://127.0.0.1:8855/watchdog/suggestions | python3 -m json.tool
```

Response:
```json
{
  "node": "mac1",
  "scanned_at": "2026-04-05T10:30:00Z",
  "suggestions": [
    {
      "source": "plans",
      "file": "docs/plans/2026-04-01-fleet-v3.md",
      "items": ["[ ] Implement chain timeout handling", "[ ] Add dispatch priority queue"]
    },
    {
      "source": "fleet-messages",
      "node": "mac2",
      "pending": ["consensus-request-auth-refactor.md"]
    },
    {
      "source": "untested-commits",
      "commits": ["a1b2c3d Fix dispatch routing", "d4e5f6g Add chain status endpoint"]
    }
  ]
}
```

If no scan has run yet: `{"suggestions": [], "note": "no scan yet -- idle threshold not reached"}`

## Suggestion Sources

| Source | What It Finds | Priority |
|--------|--------------|----------|
| `plans` | Incomplete TODOs in `docs/plans/` | High |
| `fleet-messages` | Unprocessed messages in any node's inbox | High |
| `untested-commits` | Last 24h commits without "test" in subject | Medium |

## Rate Limiting

- Scan runs at most once per 10 minutes (`_last_suggestion_scan` tracker)
- Idle threshold: 5 minutes of no NATS activity triggers watchdog idle mode
- Suggestions are written to disk, not injected into the user session

## Configuration

Idle detection thresholds are set in `.multifleet/config.json`:

```json
{
  "watchdog": {
    "idleDetection": {
      "enabled": true,
      "idleThresholdSeconds": 300,
      "nudgeIntervalSeconds": 300,
      "selfTaskIntervalSeconds": 600,
      "maxBackgroundAgents": 3
    }
  }
}
```

## Integration

The idle protocol is part of the watchdog loop. It runs automatically when:
- No NATS messages sent/received for 5+ minutes
- The daemon's watchdog is enabled (default: true)

See `fleet-watchdog` skill for full watchdog configuration and monitoring.

## When to Use

- Checking what productive work is available across the fleet
- Understanding why a node suggested a particular task
- Tuning idle thresholds for your workflow (longer cooldowns for busy environments)
