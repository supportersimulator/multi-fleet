<div align="center">

<img src="docs/assets/multi-fleet-banner.png" alt="Multi-Fleet — cross-machine AI collaboration" width="100%" />

# Multi-Fleet

### One developer. One fleet. Limitless throughput.

**Multi-Fleet turns every computer you own into a coordinated AI teammate.** Start with one laptop. Scale to three Macs. Scale to a hundred machines in a datacenter. Every node runs Claude Code (or Cursor, Codex, Gemini, VS Code) — and they all *talk to each other*.

Stop context-switching between machines. Stop SSHing in to check on a build. Stop letting your second computer sit idle while the first one chokes. **Your fleet codes together** — racing, reviewing, refactoring, deploying — in real time, with HMAC-signed packets and a **9-priority self-healing fallback chain** that delivers messages even when the network is on fire.

**This is what 10× productivity actually looks like.** Not a faster typist. A swarm.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![NATS](https://img.shields.io/badge/transport-NATS_JetStream-27aae1.svg)](https://nats.io/)
[![Claude Code](https://img.shields.io/badge/Claude_Code-plugin-7c3aed.svg)](https://claude.com/claude-code)
[![ZSF](https://img.shields.io/badge/Zero_Silent_Failures-invariant-success.svg)](#zero-silent-failures)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

[**Quick Start**](#quick-start) · [**Architecture**](ARCHITECTURE.md) · [**Demo**](DEMO.md) · [**Install**](INSTALL.md) · [**Docs**](docs/index.md)

</div>

---

## The Vision: Orchestrated Coding at Any Scale

> A single developer with a coordinated fleet of AI agents will out-ship a team of ten with isolated IDEs.

Multi-Fleet is built on one belief: **the bottleneck in software is no longer typing — it's coordination.** When you have one AI assistant in one IDE, you're driving a car. When you have a hundred AI assistants running on a hundred machines, all talking to each other, all aware of what the others just shipped — **you're not driving anymore. You're conducting an orchestra.**

| Scale | What it unlocks |
|-------|-----------------|
| **1 machine** | Persistent context across sessions, never re-explain your codebase |
| **2 machines** | Background agent on machine #2 reviews every PR you push from #1 — instant second opinion |
| **3–5 machines** | A team of AI agents you own. One races to fix the bug, one writes the test, one updates the docs. Best result wins. |
| **10+ machines** | A swarm. Refactor your entire monorepo overnight. Each agent owns a directory. Failures auto-redistribute. |
| **100+ machines** | A coding datacenter. Continuous fleet-wide refactor. AI agents propose changes 24/7. You wake up to a stack of evidence-backed PRs. |

The protocol scales linearly. The architecture scales horizontally. **The only ceiling is your imagination.**

## Why Multi-Fleet?

You have three Macs. Two of them are working on your codebase right now. The third is sleeping. One has the database. One has the GPU. One has your IDE open.

**Without Multi-Fleet:** You manually `ssh` into each machine, copy-paste commands, lose context, forget which session knows what. When one machine drops off Wi-Fi, your workflow stops.

**With Multi-Fleet:** Your AI assistant on mac1 sends a task to mac2, mac2 picks it up in its own Claude Code session, mac3 wakes from sleep to run the GPU job. If NATS goes down mid-conversation, the message reroutes through HTTP. If HTTP fails, it falls through SSH. **The message gets there.**

```
   "@mac3 train the model overnight"
            │
            ▼
   ┌──────────────────────────────────────────────────┐
   │  mac1 (you)  ◀── 9-priority cascade ──▶  mac3   │
   │                                                  │
   │  P0  Discord/Cloud                               │
   │  P1  NATS pub/sub (clustered, primary)           │
   │  P2  HTTP direct (both daemons up)               │
   │  P3  Chief relay (one peer reachable)            │
   │  P4  Seed file (SSH-write to inbox)              │
   │  P5  SSH direct (keys configured)                │
   │  P6  Wake-on-LAN (target asleep)                 │
   │  P7  Git push (last resort, always works)        │
   │  P8  Local IPC (Superset terminal-host.sock)     │
   └──────────────────────────────────────────────────┘
```

---

## Features

| | |
|---|---|
| 🛰️ **9-priority cascade** | NATS → HTTP → relay → seed → SSH → WoL → git → IPC. First channel that works, wins. |
| 🔄 **Self-healing** | Channels that come back online are automatically re-prioritized. Broken paths trigger repair via working ones. |
| 🤖 **Session-aware agents** | Tasks remember which VS Code window / Claude Code session they came from. Reply routing is automatic. |
| 🔐 **HMAC-signed** | Every packet is signed. Replay-protected. Optional E2E encryption via age-keys. |
| 🩺 **Health observable** | `/health` endpoint, per-channel counters, JetStream stream stats, per-peer last-seen. |
| 🧬 **LLM-native** | Built for Claude Code, Cursor, Codex, Gemini, VS Code. Plugin packages included. |
| 📊 **Fleet visibility** | Productivity races, leaderboards, evidence streams. See what every node is doing in real time. |
| 🛡️ **Zero Silent Failures** | Every error path bumps a named counter. No `except: pass`. Ever. |

---

## Quick Start

### Install

```bash
# pip install (single node)
pip install multi-fleet

# Or clone for development
git clone https://github.com/supportersimulator/multi-fleet
cd multi-fleet && pip install -e .
```

### Run a node

```bash
export MULTIFLEET_NODE_ID=mac1
export NATS_URL=nats://127.0.0.1:4222
python3 -m multifleet.daemon serve
```

### Send a message

```bash
curl -X POST http://127.0.0.1:8855/message \
  -H "Content-Type: application/json" \
  -d '{
    "type":    "context",
    "to":      "mac2",
    "payload": {"subject":"deploy","body":"push the landing page"}
  }'
```

### Watch fleet health

```bash
curl -s http://127.0.0.1:8855/health | jq
bash scripts/fleet-check.sh    # full dashboard
bash scripts/fleet-summary.sh  # one-line status bar
```

That's it. Two nodes with `MULTIFLEET_NODE_ID` set to different values, both connected to the same NATS (or both reachable via any of P2–P7), and you have a fleet.

---

## Architecture

```
    ┌─────────────────────────────────────────────────────────────┐
    │                      Your Machine (mac1)                    │
    │  ┌────────────┐    ┌──────────────┐    ┌─────────────────┐  │
    │  │ Claude Code├───▶│  HTTP :8855  │───▶│ ChannelProtocol │  │
    │  │ /Cursor/etc│    │   /message   │    │  (9 channels)   │  │
    │  └────────────┘    └──────────────┘    └────────┬────────┘  │
    │                                                  │           │
    │  ┌──────────────────────────────────────────────▼────────┐  │
    │  │   NATS  │  HTTP  │  SSH  │  Git  │  WoL  │  IPC  │ ...│  │
    │  └────────┬─────────────────────────────────────────────┘  │
    └───────────┼──────────────────────────────────────────────────┘
                │
                ▼  (whichever channel is healthy)
    ┌─────────────────────────────────────────────────────────────┐
    │                      Peer Machine (mac2)                    │
    │                                                             │
    │   Inbound packet  ▶  inbox  ▶  hook  ▶  Claude Code session │
    └─────────────────────────────────────────────────────────────┘
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full deep dive: JetStream replication, MFINV invariants C01–C07, fleet-state KV, plist drift detection, and the self-heal loop.

---

## The Three Invariants

These cannot be relaxed:

1. **ZSF — Zero Silent Failures.** Every exception path bumps an observable counter. `except Exception: pass` is forbidden.
2. **No Single Point of Failure.** No channel, daemon, or peer is required. The cascade always has a fallback.
3. **No Polling.** Event-driven everywhere. Polling loops fail the `test_no_polling_invariant.py` gate.

---

## Fleet-State KV

Multi-Fleet ships with a JetStream-backed KV store (`fleet_roster`) that every node reads and writes. It tracks who's alive, who's chief, who has which capability, and which channels are working between which peers.

```bash
nats kv get fleet_roster mac2 --raw | jq
```

```json
{
  "node_id":        "mac2",
  "last_seen":      "2026-05-13T21:14:02Z",
  "capabilities":   ["nats","http","ssh","git"],
  "peers_seen":     ["mac1","mac3"],
  "git_head":       "865f4a8...",
  "claude_session": "fab27887..."
}
```

This is what makes the cascade smart: each node knows *which channels work to which peer right now*, and picks the cheapest healthy one first.

---

## Use Cases — What "Orchestrated Coding" Actually Means

### 🏁 Race Mode — Multiple AI agents, one task, best result wins
Three machines, three Claude Code sessions, same prompt. They race. They watch each other. They learn from each other's commits. The first to ship a passing solution wins; the others diff against the winner and learn. **You get the best of three minds without paying for three.**

### 🛠️ Swarm Refactor — Carve up a monorepo and ship it overnight
"Update every component in the codebase to use the new logging interface." With one machine, that's a weekend. With Multi-Fleet, each agent claims a directory from a shared task pool, ships their patches, and the fleet auto-merges. **Wake up to 200 PRs. All tested. All evidence-logged.**

### 🔁 Continuous Background Review
Every commit you push from your laptop triggers a fleet-wide review. mac2 runs the test suite. mac3 audits for security. cloud-node runs the 3-Surgeons consensus check. **You ship; the fleet validates.** No more "I'll review this tomorrow."

### 🧠 Always-On Context Memory
Your IDE on the laptop tells the desktop "remember this for tomorrow." The desktop persists it. The laptop closes. You open a new session anywhere on the fleet and the context is there. **Your codebase has a memory now.**

### 💾 Distributed AI Development
The Mac with the GPU runs the model. The Mac with the database runs the migrations. The Mac with your IDE has zero latency on autocomplete. **Each machine does what it's best at — all coordinated through one protocol.**

### 🌐 Geographically Distributed Teams
One Multi-Fleet cluster spans your home office, your cabin laptop, your AWS cloud node, and your collaborator's desktop on another continent. **All four code on the same project as if they were on the same Wi-Fi.**

### 🛡️ Auto-Recovering Pipelines
CI/CD steps that don't fail when one box loses Wi-Fi for 90 seconds. The cascade reroutes. The work finishes. **Pipelines that survive what real networks do.**

### 🚀 Solo-Founder Force Multiplier
You are one person. Your fleet is twenty. You don't hire — you provision. **The next generation of unicorns will be built by one founder and one fleet.**

---

## Integrations

| Tool | Plugin | Status |
|------|--------|--------|
| Claude Code | [plugin (this repo)](skills/) | ✅ Stable |
| Cursor | MCP server (`tools/multifleet_mcp.py`) | ✅ Stable |
| VS Code | extension host bridge | ✅ Stable |
| Codex | `codex-config.toml.example` | ✅ Stable |
| Gemini | `gemini-extension.json` | ✅ Stable |
| Superset terminal-host | P8 IPC | 🧪 Experimental |

---

## Zero Silent Failures

Every failure path in Multi-Fleet bumps a named counter. You can grep the codebase: there is no `except: pass`. The CI gate enforces this.

```bash
curl -s http://127.0.0.1:8855/health | jq '.counters'
```

```json
{
  "nats_publish_errors_total":          0,
  "http_send_errors_total":             2,
  "ssh_seed_write_errors_total":        0,
  "channel_cascade_fallback_total":     14,
  "self_heal_repair_attempts_total":    3,
  "self_heal_repair_success_total":     3,
  "webhook_offsite_nats_publish_total": 1024
}
```

If you see a counter climbing, you have a real bug. If you see all zeros, your fleet is healthy.

---

## Roadmap

- [x] 9-priority channel cascade
- [x] JetStream replication (R=3 across cluster)
- [x] Fleet-state KV with auto-sync
- [x] Self-heal loop with cardinal-direction repair
- [x] HMAC signing
- [x] Leaf-node mode for off-network reliability
- [x] Discord P0 channel for emergency relay
- [ ] WireGuard auto-mesh for zero-config private networking
- [ ] Native Windows daemon (currently macOS/Linux first-class)
- [ ] Browser extension for in-tab fleet visibility

---

## Documentation

- [**ARCHITECTURE.md**](ARCHITECTURE.md) — How it works, the deep dive
- [**INSTALL.md**](INSTALL.md) — macOS / Linux / Windows install
- [**DEMO.md**](DEMO.md) — Try it in 5 minutes
- [**COMMS-PROTOCOL.md**](COMMS-PROTOCOL.md) — Packet format, signing, replay protection
- [**SCALING.md**](SCALING.md) — Beyond 3 nodes
- [**CONTRIBUTING.md**](CONTRIBUTING.md) — How to help
- [**CODE_OF_CONDUCT.md**](CODE_OF_CONDUCT.md) — Be kind

---

## Status

Multi-Fleet powers a working 4-node fleet (mac1, mac2, mac3, cloud) running 24/7 across home network, cellular, and AWS. It has shipped through real network partitions, sleep/wake cycles, NATS server restarts, and intermittent Wi-Fi without dropping a message.

It is **production-tested at small scale**. We invite you to test it at yours.

---

## License

MIT — do anything you want, just keep the copyright notice. See [LICENSE](LICENSE).

---

<div align="center">

### The future is not faster developers. The future is coordinated fleets.

Every machine you own is a teammate. Every spare CPU cycle is potential throughput. Every idle laptop is an AI agent waiting for a task.

**Build the fleet. Conduct the orchestra. Ship at the speed of imagination.**

⭐ Star this repo if you've ever had three machines and wished they talked to each other.

**Built by humans who got tired of `ssh user@mac2 'pkill -9 daemon'`.**

</div>
