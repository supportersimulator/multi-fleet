# Changelog

All notable changes to Multi-Fleet are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [4.0.0] - 2026-04-06

### Added
- Competitive Branch Racing (`fleet_race`, `race_orchestrator`) — 3 machines race same problem
- Chief synthesis engine (`synthesis.py`) — pluggable analyzers, confidence scoring
- Cross-machine rebuttal state machine (`cross_rebuttal.py`) — 7-phase protocol
- Superset integration — bridge, MCP, presets, setup/teardown hooks
- Fleet Doctor — 14 structured diagnostic checks
- Rich TUI Dashboard — live peer/race/stats display
- Durable task state machine (`task_state.py`) — SQLite-backed lifecycle
- Generic packet envelope (`packets.py`) — fleet/v1 protocol, opaque payload
- Pluggable packet registry — JSON Schema validation, 9 types
- Evidence ledger + gains gate (Phase 4)
- EventBus WebSocket server for IDE integration
- xbar menubar widget for glanceable fleet status
- VS Code extension fleet wiring (fleet.* events)
- Fleet Liaison agent architecture
- Pre-demo checklist script
- Hourly maintenance loop (WAL checkpoint, stale peer eviction)
- 5 branch coordination packet schemas

### Fixed
- All silent `except: pass` eliminated in production code
- All 12 failing tests fixed (cascade, MCP tool count, hook output, seed path)
- WebSocket reconnect re-subscribes (was silently dropping events)
- NATS connect_timeout bumped to 10s (was 2s — too tight for WiFi)
- Mac3 IP updated in config (was stale)
- Superset MCP Accept header (requires both application/json + text/event-stream)

### Changed
- Architecture: multi-fleet = standalone transport with deep integration surface
- Hybrid evidence store: transport owns delivery, IDE owns meaning
- Packet payload is opaque — consumers own their schemas
- Monolith decomposed: nats_client.py → channels, sessions, self_heal, discovery
- Tests: 160 → 900+ (all passing)

---

## [3.0.0] - 2026-04-05

Major release: chain orchestration, dispatch gates, security hardening, and 5 new skills.

### Added
- 5 new skills: fleet-config-gate, fleet-dispatch-gate, fleet-healer, fleet-post-verification, fleet-protocol (16 total)
- 2 new commands: fleet-chain (multi-step task chains), fleet-dispatch (gated task dispatch)
- Chain orchestration — sequential multi-node task pipelines with automatic step tracking
- Dispatch gate — pre-flight safety checks before sending work to remote nodes
- Config gate — blast radius verification before fleet configuration changes
- Post-verification gate — health and connectivity checks before claiming fleet work complete
- Fleet protocol skill — self-healing communication invariant with channel priority and repair escalation
- Fleet healer skill — background agents that automatically heal broken communication channels
- Seed file security validation (ownership, size limits, symlink attack prevention)
- Content sanitization (ANSI escape stripping) for seed file ingestion
- API key management via macOS Keychain with AWS Secrets Manager fallback

### Changed
- Bumped version to 3.0.0 across plugin.json, package.json, marketplace.json
- Fixed duplicate SessionStart key in hooks.json (JSON only keeps last occurrence)
- Marketplace skills list now includes all 16 skills (was 11)

### Fixed
- hooks.json had duplicate SessionStart entries — consolidated to single entry with empty matcher

## [2.0.0] - 2026-04-05

Major release: production-ready cross-machine AI collaboration with self-healing communication, session-aware task dispatch, and HMAC security. 31 agents contributed to this version in a single session.

### Communication

- 9-priority fallback delivery chain (P0 Cloud, P1 NATS, P2 HTTP, P3 Chief relay, P4 Seed file, P5 SSH, P6 Wake-on-LAN, P7 Git push, P8 Direct text)
- Self-healing communication protocol -- when P3+ channels deliver, broken P1/P2 channels auto-repair through 4-level escalation (notify, guide, background agent, SSH remote)
- Channel state machine with 4 states (HEALTHY, DEGRADED, BROKEN, HEALING) and automatic 15-minute stale reset
- HMAC-SHA256 message signing on all NATS messages with peer identity verification and 5-minute replay prevention
- Message type routing -- 7 types (alert, task, reply, context, broadcast, sync, repair) with type-aware channel selection and timeout budgets
- ACK protocol with SQLite write-ahead log for zero message loss, exponential backoff retry, and cross-device WAL replication
- 30-second max total cascade time with per-channel timeouts

### Task Dispatch

- Session-aware autonomous task agents (send_smart) -- agents inherit live context from target's active session via session historian gold extraction
- Work coordination endpoints -- fleet-wide task tracking with claim/release/status to prevent duplicate work across nodes
- Productive idle detection -- sessions idle >5 minutes auto-pick up fleet backlog; channel repair takes priority over plan items
- True idle detection using both fleet message activity AND Claude Code JSONL mtime

### Discovery and Monitoring

- UDP gossip heartbeat every 10 seconds with git branch/commit context; negligible bandwidth at 100+ nodes
- VS Code session detection via 3 methods (PID files, JSONL mtime, process scan) -- active vs idle vs closed
- Proactive watchdog with continuous health monitoring, threshold alerts, and automatic repair triggers
- Live productivity dashboard showing all nodes, agents, tasks, and backlog in real-time
- Structured observability logs -- every send attempt produces JSON with channel, priority, latency, and state (no message body content)

### Security

- HMAC-SHA256 signing with constant-time comparison on all NATS messages
- Peer identity verification -- unknown senders rejected at ingest
- Replay prevention with 5-minute timestamp window
- macOS Keychain key storage (`fleet_nerve_hmac_key`) with env var override for CI
- Session gold sanitization -- only safe metadata (node_id, topic_keywords, idle_s) published on NATS
- Log invariant enforced -- no message bodies, API keys, tokens, or SSH material in logs
- Graceful degradation -- unsigned messages accepted during key rollout with log warnings

### Skills (16)

- `using-multi-fleet` -- bootstrap skill: architecture overview, role guide, skill index
- `fleet-send` -- send messages with 9-priority fallback delivery
- `fleet-task` -- dispatch autonomous session-aware work to remote machines
- `fleet-status` -- quick health check: who's online, idle, working
- `fleet-check` -- full 7-channel communication test to a target node
- `fleet-repair` -- 4-level repair escalation for broken channels
- `fleet-wake` -- wake sleeping machines via health check, SSH, or WoL
- `fleet-tunnel` -- SSH tunnel management for restricted networks
- `fleet-worker` -- tmux-isolated worker pool without interactive session disruption
- `fleet-watchdog` -- continuous health monitoring with auto-repair triggers
- `productivity-view` -- live fleet-wide dashboard of nodes, agents, and backlog
- `fleet-protocol` -- self-healing communication invariant and background healing agents
- `fleet-config-gate` -- HARD GATE: verify safety before changing fleet configuration
- `fleet-dispatch-gate` -- HARD GATE: verify target readiness before dispatching work
- `fleet-post-verification` -- HARD GATE: verify fleet health before claiming done
- `fleet-healer` -- spawns background agents that auto-heal broken channels

### Commands (10)

- `fleet-send`, `fleet-status`, `fleet-task`, `fleet-check`, `fleet-repair`
- `fleet-wake`, `fleet-tunnel`, `fleet-watchdog`, `fleet-worker`, `fleet-dashboard`

### Agents (2)

- `fleet-coordinator` -- orchestrates multi-node work: task decomposition, dispatch, result synthesis
- `fleet-worker` -- executes dispatched tasks autonomously with session context awareness

### Hooks (4)

- `SessionStart` -- ingest pending fleet messages from inbox
- `UserPromptSubmit` -- relay fleet awareness into active session
- `TeammateIdle` -- async rewake for queued work when session goes idle
- `Stop` -- flush outbound message queue

### Platform

- Cross-IDE support: Claude Code (native), Cursor, VS Code, Codex CLI, Gemini
- Generated manifests from canonical source with `build_manifests.py` and `--check` verification
- Per-session seed files (`/tmp/fleet-seed-*.md`) injected on next prompt via hook
- Template config with no hardcoded IPs or secrets -- open-source ready
- LaunchAgent installer for daemon auto-start on login and crash recovery
- SSH tunnel support (autossh + LaunchAgent) for restricted networks

### Testing

- 71 test functions across 7 test files (1,039 lines)
- Coverage: config loading (15), MCP protocol (16), self-healing repair (8), fallback cascade (11), watchdog/heartbeat (9), worker dispatch (12)
- Shared test fixtures via conftest.py with mock daemon, NATS client, and config

### Changed

- Replaced basic HTTP-only transport with NATS pub/sub as primary channel
- Rewrote daemon with full 9-priority fallback chain instead of single-channel delivery
- Upgraded from 11 skills to 16 with invariance gates and hard gates

---

## [1.0.0] - 2026-03-30

Initial release.

### Added

- Basic skill scaffold (fleet-send, fleet-status, fleet-task)
- HTTP direct transport (single channel, no fallback)
- Simple peer-to-peer messaging
- Initial plugin.json and package.json
