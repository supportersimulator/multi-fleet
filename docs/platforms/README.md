# Platform Setup Guide

Multi-Fleet is cross-platform. The core (Python, NATS, HTTP daemon) is identical everywhere. What differs is how you **auto-start services**, **store secrets**, and **configure firewalls**.

## Quick Reference

| Concern | macOS | Linux | Windows |
|---------|-------|-------|---------|
| Auto-start | LaunchAgent plist | systemd unit | Task Scheduler / Windows Service |
| Secret storage | Keychain (`security` CLI) | Env file (chmod 600) | Credential Manager |
| NATS install | `brew install nats-server` | apt / dnf / binary | Binary + `sc.exe` |
| Firewall | System Preferences or `pf` | ufw / iptables | `netsh advfirewall` |
| Menu bar | xbar plugin | N/A (use web dashboard) | N/A (use web dashboard) |
| Sleep prevention | `caffeinate` | `systemd-inhibit` | `powercfg` |

## Platform Docs

- [macOS](macos.md) -- primary platform, fully automated via `fleet-menubar-setup.sh`
- [Linux](linux.md) -- systemd units, env-file secrets, ufw rules
- [Windows](windows.md) -- experimental / community support

## What's Universal (No Platform Config Needed)

- Python multifleet package and `fleet` CLI
- Config file (`~/.multifleet/config.json`)
- Fleet Nerve daemon (port 8855)
- NATS protocol (port 4222)
- All MCP tools, skills, commands, hooks, agents
- SSH key setup between nodes
- Web dashboard (`fleet dashboard --web`)
