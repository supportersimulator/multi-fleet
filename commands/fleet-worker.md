---
description: "Manage tmux worker pool — dispatch tasks, list workers, read output"
disable-model-invocation: true
---

Invoke the multi-fleet:fleet-worker skill and follow it exactly as presented to you. Then immediately run: `curl -s http://127.0.0.1:8855/worker/status | python3 -m json.tool` and display the worker pool status.
