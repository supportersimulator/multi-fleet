# Multi-Fleet Demo

## Quick Start

```bash
bash multi-fleet/scripts/demo-multifleet.sh
```

For recording or presentations, use theatrical mode (slower pacing, dramatic pauses):

```bash
bash multi-fleet/scripts/demo-multifleet.sh --theatrical
```

## What It Shows

The demo walks through six capabilities in sequence:

1. **Fleet Health** — discovers nodes, checks daemon status, shows self-healing triggers
2. **Probe Scan** — analyzes codebase for config drift, stale locks, version skew
3. **AI Rebuttal** — three nodes debate an implementation approach with evidence
4. **Human Arbiter** — approval interface where humans accept or override AI decisions
5. **Evidence Ledger** — SHA-256 hash-chained audit trail of every decision
6. **Fleet Dashboard** — 5-zone operational view (nodes, tasks, health, evidence, memory)

## Prerequisites

- Bash 4+ (macOS default or Homebrew)
- Python 3.8+ (for config parsing)
- `curl` (for live daemon checks)
- `shasum` (for evidence hash demo)

Optional (for live data instead of simulated):
- Fleet daemon running (`multi-fleet serve`)
- `.multifleet/config.json` configured with node addresses
- NATS server for P1 communication

## Expected Output

The demo runs in ~60 seconds (normal) or ~3 minutes (theatrical). It produces colored terminal output showing each capability. If fleet daemons are running, it shows live data; otherwise it uses simulated output to demonstrate the workflow.

Exit code is always 0. The demo is read-only and makes no changes to your codebase or fleet state.

## Related Demos

- `demo-fleet-race.sh` — full competitive branch race (5 min)
- `demo-setup.sh` — interactive fleet setup walkthrough
- `pre-demo-check.sh` — verify fleet readiness before demos
