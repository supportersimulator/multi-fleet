# Changelog

All notable changes to Multi-Fleet are documented here.

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
