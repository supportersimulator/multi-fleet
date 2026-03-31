---
description: "Quick fleet health check — who's online, who's idle, what's running"
disable-model-invocation: true
---

Invoke the multi-fleet:fleet-status skill and follow it exactly as presented to you. Then immediately run: `curl -s http://127.0.0.1:8855/fleet/live | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['fleetStatus'])"` and display the result.
