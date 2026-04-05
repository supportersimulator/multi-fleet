---
description: "Trigger repair escalation for a broken fleet channel"
disable-model-invocation: true
---

Invoke the multi-fleet:fleet-repair skill and follow it exactly as presented to you. Then ask the user which node to repair. If they already specified a node, run: `curl -sf -X POST http://127.0.0.1:8855/repair -H "Content-Type: application/json" -d '{"target": "<node-id>"}'` and display the results.
