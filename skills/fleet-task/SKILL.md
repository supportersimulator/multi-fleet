---
name: fleet-task
description: Dispatch autonomous work to another machine — session-aware agents that work without human interaction
---

# Fleet Task

The killer feature of Multi-Fleet. Send a task to another machine and an autonomous agent handles it — with full awareness of what that machine's interactive session is working on.

## How It Works

1. You send a `task` message to a target node
2. Target's Fleet Nerve daemon extracts "gold" from the active session (recent conversation, topics, context)
3. Daemon spawns `claude -p` with the task prompt + session gold injected
4. Agent works autonomously (up to 25 turns by default)
5. Results written to `/tmp/atlas-agent-results/fleet-task-<id>.log`
6. Completion status sent back to sender via reply message

**No human interaction needed at any point on the target machine.**

## Sending a Task

```bash
# Via direct HTTP
curl -sf -X POST http://<target-ip>:8855/message -H "Content-Type: application/json" \
  -d '{
    "type": "task",
    "from": "mac3",
    "to": "mac1",
    "payload": {
      "subject": "Run 3s cross-exam on auth module",
      "body": "Full cross-examination of the authentication middleware. Check for session token storage compliance issues. Report findings."
    }
  }'
```

## Monitoring Task Progress

```bash
# On the target machine
curl -s http://127.0.0.1:8855/tasks/live | python3 -m json.tool

# Or from any machine via fleet/live
curl -s http://127.0.0.1:8855/fleet/live | python3 -c "
import sys,json
for n in json.load(sys.stdin)['nodes']:
  for t in n.get('tasks',[]):
    if t.get('running'): print(f'{n[\"name\"]}: {t[\"id\"]} running ({t[\"age\"]}s)')
"
```

## Task Lifecycle

```
pending → running → completed | failed | timeout
```

- **completed**: Agent finished within turn limit, exit code 0
- **failed**: Agent errored, exit code 1
- **timeout**: Agent hit max turns (25) or wall clock limit (600s)

## Session Gold Injection

The target daemon calls session historian to extract context from the active JSONL:
- What the human is currently working on
- Recent tool calls and their results
- Topics and themes from the conversation

This context is prepended to the task prompt so the agent understands the local situation. A task agent on mac1 knows what mac1's human session is doing — it doesn't work blind.

## Best Practices

- **Be specific**: "Run the auth test suite and report failures" > "check tests"
- **One task per message**: Don't bundle unrelated work
- **Monitor via dashboard**: Use the productivity view to watch task progress
- **Check results**: Task logs are in `/tmp/atlas-agent-results/` on the target machine
