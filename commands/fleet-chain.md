---
description: "Submit or view multi-step task chains — orchestrate sequential work across fleet nodes"
disable-model-invocation: true
---

# /fleet-chain

Submit or view multi-step task chains that orchestrate sequential work across fleet nodes.

## Usage

```
/fleet-chain submit <chain-spec>   Submit a new chain for execution
/fleet-chain status [chain-id]     View status of running chains
/fleet-chain list                  List all chains (active + completed)
/fleet-chain cancel <chain-id>     Cancel a running chain
```

## Chain Spec Format

A chain is a sequence of steps, each dispatched to a target node:

```json
{
  "name": "build-and-test",
  "steps": [
    {"node": "mac1", "task": "Build the project", "timeout": 300},
    {"node": "mac2", "task": "Run integration tests", "timeout": 600},
    {"node": "mac3", "task": "Deploy to staging", "timeout": 120}
  ]
}
```

Steps execute sequentially. Each step waits for the previous to complete before dispatching. If a step fails, the chain halts and reports the failure.

## Examples

```bash
# Submit a chain
/fleet-chain submit '{"name":"sync-all","steps":[{"node":"mac1","task":"git pull"},{"node":"mac2","task":"git pull"},{"node":"mac3","task":"git pull"}]}'

# Check status
/fleet-chain status sync-all

# List all chains
/fleet-chain list
```
