---
name: fleet-events
description: Subscribe to realtime fleet events from the EventBus
---

# Fleet Events

Monitor realtime fleet activity via EventBus.

## Usage
- `fleet_status_snapshot` — get current fleet state
- `fleet_probe_scan` — run probes and get findings
- WebSocket: `ws://127.0.0.1:8031/events` — subscribe to fleet.* events
- SSE: `http://127.0.0.1:8033/events` — server-sent event stream

## Event Types
- fleet.peer.online/offline
- fleet.message.sent/received
- fleet.health.degraded
- fleet.lease.acquired/released
- fleet.rebuttal.proposed/resolved
- fleet.probe.finding (NEW — probe results)

## Integration
The VS Code extension should:
1. Connect to WebSocket on port 8031
2. Subscribe to fleet.* events
3. Update status bar on fleet.peer.* events
4. Show notification on fleet.health.degraded
5. Badge activity bar on fleet.rebuttal.* events
