# Changelog

All notable changes to Multi-Fleet are documented here.

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

Major release: production-ready cross-machine AI collaboration.

### Added
- 11 skills: using-multi-fleet, fleet-send, fleet-status, fleet-task, fleet-wake, fleet-repair, fleet-tunnel, fleet-watchdog, fleet-check, fleet-worker, productivity-view
- 2 agents: fleet-coordinator, fleet-worker
- 4 hooks: UserPromptSubmit, SessionStart, TeammateIdle, Stop
- 6 MCP tools via fleet-nerve server
- 7-priority fallback delivery (NATS, HTTP, Chief relay, Seed file, SSH, Wake-on-LAN, Git push)
- 4-level self-healing repair (detect degraded channel, diagnose, repair, verify)
- Proactive watchdog with automatic health monitoring
- Session-driver workers for autonomous task dispatch
- VS Code session detection (active, idle, closed)
- SSH tunnel support for restricted networks (autossh + LaunchAgent)
- Template config with no hardcoded values — open-source ready

### Changed
- Replaced basic HTTP transport with NATS pub/sub as primary channel
- Rewrote daemon with full fallback chain instead of single-channel delivery

## [1.0.0] - 2026-03-30

Initial release.

### Added
- Basic skill scaffold (fleet-send, fleet-status, fleet-task)
- HTTP direct transport
- Simple peer-to-peer messaging
- Initial plugin.json and package.json
