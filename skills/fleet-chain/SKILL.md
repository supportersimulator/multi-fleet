---
name: fleet-chain
description: Chain orchestration — multi-step task dependencies across fleet nodes with automatic sequencing
---

# Fleet Chain Orchestration

Chains are ordered sequences of tasks dispatched across fleet nodes. Each step runs only after the previous step succeeds. If any step fails, the chain halts.

## How It Works

1. Submit a chain with an ordered array of steps (node + task pairs)
2. Daemon registers the chain and dispatches step 0
3. On step completion, the daemon advances to the next step
4. Remote steps send completion back to the originating node via NATS
5. Chain status is queryable at any time

## HTTP Endpoints

### POST /chain — Start a chain

```bash
curl -sf -X POST http://127.0.0.1:8855/chain \
  -H "Content-Type: application/json" \
  -d '{
    "chain_id": "deploy-v2",
    "steps": [
      {"node": "mac1", "task": "Run test suite", "timeout_s": 300},
      {"node": "mac2", "task": "Build artifacts", "timeout_s": 600},
      {"node": "mac1", "task": "Deploy to staging", "timeout_s": 120}
    ]
  }'
```

Response: `{"chain_id": "deploy-v2", "status": "running", "steps": 3}`

### GET /chains — List all chains

```bash
curl -sf http://127.0.0.1:8855/chains | python3 -m json.tool
```

Returns: `{"chains": {"deploy-v2": {...}}, "count": 1}`

### GET /chain/<id> — Get chain status

```bash
curl -sf http://127.0.0.1:8855/chain/deploy-v2 | python3 -m json.tool
```

Returns: `{"chain_id": "...", "status": "running|done|failed", "current": 1, "total": 3, "steps": [...], "results": [...]}`

## Step Format

Each step in the `steps` array:

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `node` | No | self | Target node ID (e.g. `mac2`) |
| `task` | Yes | — | Task prompt sent to the target |
| `timeout_s` | No | 300 | Max seconds for this step |

## Chain Lifecycle

```
POST /chain → running → step 0 dispatched → step 0 done → step 1 dispatched → ... → done
                                           → step N fails → chain status = "failed"
```

- **running** — currently executing a step
- **done** — all steps completed successfully
- **failed** — a step failed; chain halted at that step

## Configuration

Chains use the same task dispatch mechanism as `fleet-send` type=task. Each step is sent via `send_with_fallback` (8-priority channel cascade). Timeouts are per-step.

Env vars:
- `FLEET_ACK_TIMEOUT_S` — ACK timeout for chain step delivery (default 60s)
- `FLEET_ACK_MAX_RETRIES` — retry count before marking step as failed (default 3)

## When to Use

- Multi-node deployments requiring ordered steps
- Build-test-deploy pipelines across machines
- Sequential data processing where each step depends on the prior result
- Any workflow where step N depends on step N-1 completing
