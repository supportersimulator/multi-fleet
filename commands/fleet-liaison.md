---
description: "Interact with the fleet liaison agent — check status, read messages, escalate"
disable-model-invocation: true
---

Invoke the multi-fleet:fleet-liaison skill and follow it exactly as presented to you. Parse the user's subcommand:

- `status` (default if no subcommand) — show liaison state and pending counts
- `read` — get and clear batched messages
- `escalate <message>` — send a message through the liaison to fleet peers
- `pause` — temporarily pause fleet monitoring
- `resume` — resume fleet monitoring
