---
description: "Wake a sleeping or offline machine in the fleet"
disable-model-invocation: true
---

Invoke the multi-fleet:fleet-wake skill and follow it exactly as presented to you. Then ask the user which node to wake. If they already specified a node, run: `python3 tools/fleet_nerve_send.py --wake <node-id>` and display the results.
