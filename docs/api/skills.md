# Skills

Multi-Fleet provides 28 skills (slash commands) organized by function.

## Core Skills

| Skill | Description |
|-------|-------------|
| `using-multi-fleet` | Architecture overview, role guide, skill index. Bootstrap skill. |
| `fleet-send` | Send messages (context, task, alert, broadcast) with 9-priority fallback. |
| `fleet-task` | Dispatch autonomous session-aware work to another machine. |
| `fleet-dispatch` | Remote worker dispatch with priority routing and result tracking. |
| `fleet-status` | Quick health check -- who's online, idle, working. |
| `fleet-check` | Full 7-channel communication test to a target node. |
| `fleet-repair` | 4-level repair escalation for broken channels. |
| `fleet-wake` | Wake sleeping machines via health check, SSH, or WoL. |
| `fleet-tunnel` | SSH tunnel management for restricted networks. |
| `fleet-worker` | tmux-isolated worker pool -- no interactive session disruption. |
| `fleet-watchdog` | Continuous health monitoring with auto-repair triggers. |
| `fleet-idle` | Productive idle -- automatic work discovery when nodes are idle. |
| `fleet-ack` | Delivery confirmation protocol -- ACK tracking, retry, failure alerting. |
| `fleet-security` | HMAC signing, replay prevention, peer validation, session gold sanitization. |
| `productivity-view` | Live fleet-wide dashboard of nodes, agents, and backlog. |
| `fleet-events` | EventBus pub/sub for fleet event streaming. |
| `fleet-liaison` | Background communication handler with escalation levels. |
| `fleet-onboard` | New node onboarding flow. |

## Orchestration Skills

| Skill | Description |
|-------|-------------|
| `fleet-chain` | Chain orchestration -- multi-step task dependencies with automatic sequencing. |
| `fleet-orchestrate` | Parallel scatter-gather, pipeline, fan-out/fan-in across fleet nodes. |
| `fleet-race` | Parallel competing execution across nodes. |

## Consensus Skills

| Skill | Description |
|-------|-------------|
| `fleet-verdict` | Structured verdict packets for cross-machine 3x3x3 consensus. |
| `fleet-rebuttal` | 4-phase cross-machine critique cycle converging on chief decision. |

## Invariance Skills (Hard Gates)

| Skill | Description |
|-------|-------------|
| `fleet-protocol` | Self-healing communication invariant and background healing agents. |
| `fleet-config-gate` | Verify safety and blast radius before changing fleet configuration. |
| `fleet-dispatch-gate` | Verify target readiness and task safety before dispatching work. |
| `fleet-post-verification` | Verify fleet health after completing work before claiming done. |
| `fleet-healer` | Spawns background agents that auto-heal broken channels. |
