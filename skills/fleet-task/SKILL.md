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

## Task Lifecycle

```
pending → running → completed | failed | timeout
```

| State | What's happening | Duration |
|-------|-----------------|----------|
| `pending` | Message received, queued. Daemon extracts session gold and prepares agent spawn. | 1-5s |
| `running` | `claude -p` agent active. Full access to target codebase, tools, and session context. | 10s-600s |
| `completed` | Agent finished within turn limit, exit code 0. Results written, reply sent. | Terminal |
| `failed` | Agent errored (exit code 1). Tool failure, permission issue, or impossible task. | Terminal |
| `timeout` | Agent hit max turns (25) or wall clock limit (600s). Partial results may exist. | Terminal |

`pending` always moves to `running` within 5s (or fails immediately). `running` always terminates — the daemon's watchdog kills agents exceeding the wall clock limit. No stuck states.

## Sending a Task

### Direct HTTP (preferred)

```bash
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

### With options

```bash
curl -sf -X POST http://<target-ip>:8855/message -H "Content-Type: application/json" \
  -d '{
    "type": "task",
    "from": "mac3",
    "to": "mac1",
    "payload": {
      "subject": "Run full test suite",
      "body": "Run pytest on the entire codebase. Report failures with file paths and error messages.",
      "maxTurns": 15,
      "timeoutSeconds": 300,
      "useWorker": true
    }
  }'
```

- `maxTurns` (default 25): Max agent conversation turns. Lower for simple tasks.
- `timeoutSeconds` (default 600): Wall clock limit. Daemon kills agent after this.
- `useWorker` (default false): Route to tmux worker pool instead of inline agent.

### Via CLI script

```bash
CHIEF_INGEST_URL=http://<chief-ip>:8844 ./scripts/fleet-send.sh mac1 "Run auth tests" "Run the auth test suite and report failures" p2
```

## Monitoring Task Progress

```bash
# On the target — live task list
curl -s http://127.0.0.1:8855/tasks/live | python3 -m json.tool

# From any machine — fleet-wide task overview
curl -s http://127.0.0.1:8855/fleet/live | python3 -c "
import sys, json
for n in json.load(sys.stdin)['nodes']:
    tasks = [t for t in n.get('tasks', []) if t.get('running')]
    if tasks:
        for t in tasks:
            print(f'{n[\"name\"]}: {t[\"id\"]} — {t.get(\"subject\",\"?\")} ({t[\"age\"]}s)')
    else:
        print(f'{n[\"name\"]}: idle')
"
```

## Collecting Results

Task agents write results to `/tmp/atlas-agent-results/` on the **target machine**.

```bash
# On the target
cat /tmp/atlas-agent-results/fleet-task-a1b2c3.log
ls -lt /tmp/atlas-agent-results/fleet-task-*.log | head -10

# From the sender via SSH
ssh <target-ip> "cat /tmp/atlas-agent-results/fleet-task-a1b2c3.log"
```

The agent also sends a `reply` message back to the sender with a summary:

```json
{
  "type": "reply",
  "from": "mac1",
  "to": "mac3",
  "payload": {
    "subject": "Re: Run 3s cross-exam on auth module",
    "body": "Completed. 3 issues found. Full log: /tmp/atlas-agent-results/fleet-task-a1b2c3.log",
    "ref": "msg-original-id",
    "taskState": "completed",
    "exitCode": 0
  }
}
```

Check your inbox for replies:

```bash
curl -s http://127.0.0.1:8855/inbox | python3 -c "
import sys, json
for m in json.load(sys.stdin).get('messages', []):
    if m.get('type') == 'reply':
        print(f'[{m[\"from\"]}] {m[\"payload\"][\"subject\"]}: {m[\"payload\"][\"body\"][:200]}')
"
```

## Session Gold Injection

The target daemon calls session historian to extract context from the active JSONL before spawning the agent:

1. **Extract**: Daemon reads the active Claude Code session JSONL on the target
2. **Mine**: Session historian extracts recent topics, tool calls, decisions, working context
3. **Inject**: Extracted gold prepended to the task prompt as system context
4. **Spawn**: Agent starts with full awareness of the target's current session state

A task agent on mac1 **knows what mac1's human session is doing** — it doesn't work blind. If the human is debugging auth middleware, the task agent avoids conflicting changes.

**Injected**: Current topic, recent tool calls, active file paths, key decisions, in-progress work warnings.
**Filtered out**: Full conversation history, secrets/credentials, unrelated prior sessions.

If no active session exists (machine idle), the agent runs without session gold — full codebase access but no session awareness.

## Error Handling

### Target offline

All 7 channels tried (P1 NATS through P7 Git). If all fail, task queued in local outbox with exponential backoff (5s, 15s, 45s, 2min, 5min max). Use `fleet-wake` to bring the target online first.

```bash
# Check outbox for stuck tasks
curl -s http://127.0.0.1:8855/stats | python3 -c "
import sys, json; s = json.load(sys.stdin)
print(f'Outbox pending: {s.get(\"outbox_pending\", 0)}, Failed: {s.get(\"errors\", 0)}')
"
```

### Agent timeout

Agent hit 25 turns or 600s. Partial results may exist in `/tmp/atlas-agent-results/`. Common causes: task too broad, agent stuck in loop, slow network ops. Fix by being more specific or increasing `maxTurns`/`timeoutSeconds`.

### Agent crash

Agent process died (OOM, signal). Daemon detects via process monitoring, sets state to `failed`. **No auto-retry** (prevents crash loops). Check logs: `tail -50 /tmp/fleet-nerve.log`. Resend manually after investigating.

### Task rejected

Target may reject if: worker pool full (retry in 60s or send without `useWorker`), payload exceeds 64KB (write to shared path instead), or node in maintenance mode.

## Examples

### Single task — run tests on another machine

```bash
curl -sf -X POST http://192.168.1.10:8855/message -H "Content-Type: application/json" \
  -d '{
    "type": "task", "from": "mac2", "to": "mac1",
    "payload": {
      "subject": "Run pytest on context-dna",
      "body": "cd context-dna && PYTHONPATH=. .venv/bin/python3 -m pytest tests/ -x --tb=short. Report pass/fail count and any failure details."
    }
  }'
```

### Multi-node swarm — parallel cross-examination

```bash
for NODE in mac1 mac2 mac3; do
  curl -sf -X POST http://127.0.0.1:8855/message -H "Content-Type: application/json" \
    -d "{
      \"type\": \"task\", \"from\": \"$(hostname -s)\", \"to\": \"${NODE}\",
      \"payload\": {
        \"subject\": \"Cross-examine auth module\",
        \"body\": \"Independent security review of auth middleware. Check: token storage, CSRF, session expiry, rate limiting. Independent analysis only.\"
      }
    }"
  echo "Dispatched to ${NODE}"
done
```

### Auto mode — fire and forget

```bash
REPLY=$(curl -sf -X POST http://192.168.1.10:8855/message -H "Content-Type: application/json" \
  -d '{
    "type": "task", "from": "mac2", "to": "mac1",
    "payload": {
      "subject": "Generate API docs",
      "body": "Scan all Python files in context-dna/. Generate markdown summary of every HTTP endpoint. Write to /tmp/atlas-agent-results/api-docs.md."
    }
  }')
echo "Dispatched — reply will arrive in inbox when done"
```

## Integration with Other Skills

- **fleet-status**: Check target is online before dispatching (`/fleet/live`)
- **fleet-send**: Tasks use the same delivery infrastructure. A task IS a `type: "task"` message
- **fleet-worker**: Set `"useWorker": true` for long-running tasks routed to tmux worker pool
- **fleet-check**: If delivery keeps failing, run 7-channel test to diagnose
- **fleet-repair**: P3+ fallback auto-triggers repair for broken P1/P2 channels
- **fleet-healer**: Background healing agents restore broken channels automatically
- **productivity-view**: Dashboard shows all running tasks fleet-wide — use for swarm monitoring

## Best Practices

- **Be specific**: "Run auth test suite, report failures with file paths" > "check tests"
- **One task per message**: Don't bundle unrelated work — dispatch multiple tasks
- **Set appropriate limits**: Simple tasks: `maxTurns: 10, timeoutSeconds: 120`. Complex: defaults (25/600)
- **Check target first**: Verify online via `fleet/live` before dispatching
- **Break large work into small tasks**: Timeout at 25 turns = task was too broad
- **Collect results promptly**: `/tmp/atlas-agent-results/` is not permanent storage
