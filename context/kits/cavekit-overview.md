---
created: "2026-04-12T15:30:00Z"
last_edited: "2026-04-12T15:30:00Z"
---

# Cavekit Overview

## Project
multifleet v5.0.0 — Cross-machine AI collaboration for Claude Code, Cursor, VS Code, Codex, and Gemini. Published to PyPI. 157 modules, 3,193 tests, 8 logical domains.

## Domain Index
| Domain | Cavekit File | Requirements | GAPs | Status | Description |
|--------|-------------|-------------|------|--------|-------------|
| transport | cavekit-transport.md | 5 | 3 | DRAFT | 7-priority self-healing channel cascade, NATS, peer discovery |
| events | cavekit-events.md | 4 | 6 | DRAFT | Typed events, pub/sub, legacy/new bus consolidation |
| synthesis | cavekit-synthesis.md | 11 | 2 | DRAFT | Chief synthesis, review flow, merge adjudicator, gates |
| evidence | cavekit-evidence.md | 9 | 1 | DRAFT | Hash-chained ledger, wisdom consumer, ghost layer, memory bridge |
| probes | cavekit-probes.md | 13 | 2 | DRAFT | Probe engine, 17 typed probes, scheduling, self-heal |
| coordination | cavekit-coordination.md | 13 | 3 | DRAFT | Fleet race, auto-coordinator, supervisor, captain, liaison |
| ide | cavekit-ide.md | 7 | 1 | DRAFT | IDE panels, dashboards, arbiter view, theatrical components |
| config | cavekit-config.md | 9 | 1 | DRAFT | CLI, onboarding, billing, Stripe, adapters, backup |

## Cross-Reference Map
| Domain A | Interacts With | Interaction Type |
|----------|---------------|-----------------|
| transport | events | Events emitted on channel state changes |
| transport | probes | Probes monitor channel health |
| synthesis | evidence | Synthesis reads/writes evidence ledger |
| synthesis | probes | Pre-synthesis probe scans |
| synthesis | events | Synthesis decisions emitted as events |
| evidence | transport | Evidence sync via fleet messages |
| evidence | events | Evidence promotion emits events |
| probes | evidence | Probe findings feed evidence pipeline |
| probes | transport | Self-heal uses channel cascade |
| coordination | synthesis | Race results feed synthesis |
| coordination | transport | Task dispatch via fleet messages |
| ide | synthesis | Panels display synthesis decisions |
| ide | evidence | Panels display evidence timeline |
| ide | probes | Panels display probe findings |
| ide | events | Panel-bus bridge for live updates |
| config | transport | CLI manages fleet operations |
| config | coordination | Adapters bridge external products |

## Dependency Graph
```
Tier 0 (no deps):     config, events
Tier 1 (foundation):  transport (depends: config, events)
Tier 2 (data):        evidence (depends: transport, events)
Tier 3 (analysis):    probes (depends: evidence, transport)
Tier 4 (decisions):   synthesis (depends: evidence, probes, events)
Tier 5 (orchestrate): coordination (depends: synthesis, transport)
Tier 6 (visibility):  ide (depends: all above)
```

## GAP Summary (19 total)
| Priority | Kit | GAP | Impact |
|----------|-----|-----|--------|
| HIGH | events | Legacy/new event bus split (6 modules on old bus) | Events invisible across buses |
| HIGH | events | Two EventType registries, two create_event factories | Duplication risk |
| HIGH | evidence | CrossNodeSynthesizer has no remote evidence collection | Cross-node synthesis incomplete |
| MEDIUM | transport | No test for NATS resubscribe after disconnect | Silent message loss |
| MEDIUM | synthesis | Wisdom feedback has no false-positive tracking | Bad wisdom persists |
| MEDIUM | synthesis | HumanArbiter uses in-memory dict (no persistence) | Decisions lost on restart |
| MEDIUM | coordination | Captain relay method unverified | May not actually relay |
| MEDIUM | coordination | Second CHIEF registration behavior unknown | Potential split-brain |
| LOW | transport | No mechanism to remove decommissioned peers | Stale peer accumulation |
| LOW | probes | 17 vs 18 probe registration count | Minor count mismatch |
| LOW | ide | Arbiter view polling-only (no WebSocket push) | Higher latency for decisions |
| LOW | config | OnboardingWizard not wired to CLI | Manual-only onboarding |

## Methodology
- Brownfield extraction via /ck:sketch --from-code
- 4 parallel ck:drafter agents (opus model)
- Reviewed by ck:cavekit-reviewer: APPROVED
- 3-surgeon cross-examination evaluated cavekit adoption: PARTIAL (spec layer yes, execution layer no)
