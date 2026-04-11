# Multi-Fleet v2.0.0 Release Notes

**Release date:** 2026-04-05

## Highlights

Multi-Fleet v2.0.0 is a full rewrite of the cross-machine AI collaboration plugin. Messages now route through a 7-priority fallback chain — if NATS is down, HTTP takes over; if HTTP fails, it falls back through Chief relay, seed files, SSH, Wake-on-LAN, and finally Git push. Messages always deliver.

## What's new

**11 skills** covering every fleet operation: send messages, check status, dispatch tasks, wake machines, repair channels, manage SSH tunnels, run watchdog health checks, drive worker sessions, and view live productivity dashboards.

**2 agents** — a fleet-coordinator for orchestrating multi-node work and a fleet-worker for executing dispatched tasks autonomously with session context.

**4 hooks** — SessionStart (ingest pending messages), UserPromptSubmit (relay awareness), TeammateIdle (async rewake for queued work), Stop (flush outbound queue).

**6 MCP tools** exposed through the fleet-nerve server for programmatic fleet control.

**Self-healing repair** — when a message succeeds on a lower-priority channel (P3+), the system detects that higher-priority channels are broken and automatically sends a repair request through the working channel. Four levels: detect, diagnose, repair, verify.

**Proactive watchdog** — continuous health monitoring of all fleet nodes and communication channels. Surfaces degraded state before it causes delivery failures.

**Open-source ready** — template config, no hardcoded IPs or secrets, clean separation between plugin logic and deployment configuration.

## Upgrade from v1.0.0

1. Update config: `cp .multifleet/config.template.json .multifleet/config.json` and fill in your fleet topology
2. Install NATS on chief node: `brew install nats-server` or `apt install nats-server`
3. Start the daemon on each machine: `python3 tools/fleet_nerve_nats.py serve`
4. Existing HTTP transport continues to work as P2 fallback

## Requirements

- Python 3.10+
- `nats-py` (`pip install nats-py`)
- SSH key access between nodes
- NATS server on chief node
