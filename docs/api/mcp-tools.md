# MCP Tools

Multi-Fleet exposes 34 MCP tools via the `fleet-nerve` MCP server. All tools are available to any IDE that supports the Model Context Protocol.

## Communication

| Tool | Description |
|------|-------------|
| `fleet_send` | Send a message to a fleet node via 9-priority fallback chain (P0 Cloud > NATS > HTTP > Chief > Seed > SSH > WoL > Git > Text). |
| `fleet_send_smart` | Session-aware smart routing. Probes target first, picks best delivery: `--resume` for active sessions, seed file for idle, agent spawn, or relay queue. |
| `fleet_broadcast` | Send a message to ALL fleet nodes (except self) via fallback. Returns per-node results. |
| `fleet_relay` | Queue a message for chief store-and-forward relay. Held on chief, delivered when target comes online. |
| `fleet_inject` | Inject a message into an active Claude session via `--resume`. Falls back to seed file. |

## Fleet Health

| Tool | Description |
|------|-------------|
| `fleet_probe` | Health check all fleet nodes -- per-node status, latency, sessions, peers, surgeon availability. |
| `fleet_status` | Fleet-wide dashboard -- all nodes with roles, connectivity, sessions, git state, surgeon status. |
| `fleet_dashboard` | Visual fleet status with formatted node table and activity summary. |
| `fleet_status_snapshot` | Aggregated fleet status snapshot -- daemon health, peers, liaison, rebuttals, leases, events, health dot. |
| `fleet_doctor` | 6 diagnostic checks: daemon health, NATS, peer reachability, keychain, message store, Python deps. |
| `fleet_discover` | Discover fleet peers from config, peer registry, and heartbeat data. Probes reachability. |
| `fleet_channels` | Channel state for all peers -- which channels are up/down per peer with latency. |
| `fleet_onboard` | 10-check onboarding wizard for new nodes. |

## Task Dispatch

| Tool | Description |
|------|-------------|
| `fleet_worker_dispatch` | Launch a Claude session-driver worker on a target node. Runs in detached tmux. |
| `fleet_tasks` | Active Claude sessions/agents across the entire fleet with session IDs, idle times, branches. |
| `fleet_work` | View work backlog -- active and pending items. Use `node='all'` for fleet-wide. |
| `fleet_work_next` | Get the next unclaimed work item from the backlog. |
| `fleet_wake` | Wake a sleeping node: health check, SSH + start daemon, WoL magic packet. |
| `fleet_historian` | Trigger session historian -- extract gold, analyze, clean dead cache. |
| `fleet_sessions` | Session gold from a node -- active sessions with extracted context. |

## Repair

| Tool | Description |
|------|-------------|
| `fleet_repair` | 4-level repair escalation: notify > guide > assist > remote SSH. |
| `fleet_gains_gate` | Fleet health verification gate. 6-10 checks. Use before merges or phase transitions. |
| `fleet_gate_check` | Run invariance gate on demand: `send`, `repair`, or `merge`. |
| `fleet_invariance` | 7 machine-checkable protocol enforcement rules (GhostScan). |
| `fleet_accept` | Pre-merge acceptance gate: probe findings, surgeon resolution, invariants, evidence. |

## Intelligence

| Tool | Description |
|------|-------------|
| `fleet_evidence` | Tamper-evident evidence ledger -- record, query, verify hash chain, get latest, stats. |
| `fleet_rebuttal` | Structured consensus protocol for proposals: propose, review, accept, rebut, counter, resolve. |
| `fleet_cross_rebuttal` | Cross-machine rebuttal: task_brief > local verdicts > cross-critique > chief synthesis. |
| `fleet_brainstorm` | Vision packets -- brainstorm, 3-surgeon adjudication, vision contracts, execution tracking. |
| `fleet_overlay` | Node overlay -- 5-strip view of what each node "thinks" (epistemic, surgeon, invariant, evidence, probe). |

## Orchestration

| Tool | Description |
|------|-------------|
| `fleet_race` | Competitive Branch Race -- parallel problem-solving across nodes. First success wins. |
| `fleet_race_status` | Check race progress or list all races. |
| `fleet_orchestrate` | Full task lifecycle: truth ladder > probes > race/supervisor > acceptance gate > evidence. |
| `fleet_hierarchy` | Manage fleet hierarchy -- roles, routing, NATS subjects, chief elections. |
| `fleet_lease` | Resource leases -- acquire, release, renew, query ownership to prevent dual-work. |

## Observability

| Tool | Description |
|------|-------------|
| `fleet_metrics` | Fleet-wide metrics: collect, aggregate, trends, productivity score, Prometheus export. |
| `fleet_branches` | Branch convergence visualization -- conflict detection, merge order recommendations. |
| `fleet_liaison_status` | Liaison agent state -- pending reviews, rebuttals, unread count. |
| `fleet_server` | Start/stop/status the fleet web server (dashboard + API + SSE + Prometheus). |
