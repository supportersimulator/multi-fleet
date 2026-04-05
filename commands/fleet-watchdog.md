---
description: "View fleet watchdog status — health checks, thresholds, repair triggers"
disable-model-invocation: true
---

Invoke the multi-fleet:fleet-watchdog skill and follow it exactly as presented to you. Then immediately run: `curl -s http://127.0.0.1:8855/watchdog/status | python3 -m json.tool` and display the results.
