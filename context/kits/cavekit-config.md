---
domain: config-onboarding-adapters
version: 1.0.0
status: brownfield
modules:
  - config.py
  - onboarding.py
  - cli.py
  - billing.py
  - stripe_integration.py
  - backup.py
  - benchmark.py
  - migration.py
  - security.py
  - sanitize.py
  - _paths.py
  - adapters/chief_sync.py
  - adapters/brainstorm_ingest.py
  - adapters/surgeon_escalation.py
  - adapters/surgeon_events.py
  - adapters/overlay_stream.py
  - adapters/packet_delivery.py
  - product_bridge.py
  - superpowers_adapter.py
  - superpowers_bridge.py
  - superset_bridge.py
  - surgeon_adapter.py
  - professor_bridge.py
  - synaptic_bridge.py
  - memory_bridge.py
  - discord_bridge.py
  - contextdna_evidence_bridge.py
depends_on:
  - cavekit-transport
  - cavekit-evidence
  - cavekit-synthesis
---

# Cavekit: Configuration, Onboarding, and Adapters

## Scope

System-wide configuration, new-node onboarding, CLI entry point, billing/payments, backup/restore, schema migration, message security, input sanitization, path resolution, and cross-product adapter/bridge modules that connect multi-fleet to external systems (ContextDNA, 3-Surgeons, Superpowers, Superset, Discord, Professor, Synaptic). This domain owns the operational surface area -- how users interact with the system and how the system connects to its ecosystem.

## Requirements

### R1: CLI with 20+ Subcommands

**Description:** A unified CLI (`python3 -m multifleet.cli` or `fleet` entry point) exposing all fleet operations as subcommands. Uses Rich for formatted output when available, plain text fallback otherwise.

**Acceptance Criteria:**
- [ ] CLI exposes at minimum these 21 subcommands: `status`, `peers`, `channels`, `send`, `broadcast`, `race`, `doctor`, `gains-gate`, `dashboard`, `evidence`, `lease`, `rebuttal`, `truth`, `supervisor`, `overlay`, `theater`, `arbiter`, `probe`, `orchestrate`, `discord`, `nats-cluster`
- [ ] `fleet status` returns JSON from daemon `/health` endpoint or error message when daemon unreachable
- [ ] `fleet send <target> <message>` POSTs to daemon `/message` with target and message body
- [ ] `fleet broadcast <message>` POSTs to daemon `/broadcast`
- [ ] `fleet race "<task>"` POSTs to daemon `/race` with task description and optional `--nodes` flag
- [ ] `fleet dashboard` launches Rich terminal TUI; `fleet dashboard --web` starts web server
- [ ] `fleet evidence verify` runs evidence chain verification; `fleet evidence list` shows recent entries
- [ ] `fleet gains-gate` runs gains gate checks and reports pass/fail with optional `--json` flag
- [ ] `fleet arbiter` launches arbiter view (standalone or web)
- [ ] `fleet probe run` triggers probe scan; `fleet probe list` shows latest results
- [ ] `fleet theater` launches 5-zone theatrical terminal dashboard
- [ ] All subcommands that query the daemon print `ERROR: Daemon unreachable` on connection failure and exit with nonzero status
- [ ] Rich-formatted output (tables, colors) used when `rich` is importable; plain text otherwise
- [ ] [GAP] No `fleet onboard` subcommand wired -- OnboardingWizard exists but is not exposed via CLI argparse

**Dependencies:** Daemon HTTP API (all subcommands proxy to daemon)

### R2: Node Onboarding

**Description:** A wizard that checks prerequisites for a new machine joining the fleet and renders a step-by-step setup guide. Sanitizes node IDs to prevent injection. Checks: python, NATS, daemon, keychain HMAC, superset, config.json, SSH keys, probes, surgeons, skills.

**Acceptance Criteria:**
- [ ] `OnboardingWizard(node_id)` sanitizes node_id to match `^[a-z][a-z0-9_-]{0,31}$`; unsafe characters are stripped; empty result falls back to hostname
- [ ] `check_all()` returns dict with `node_id`, `completed` (list), `remaining` (list), `ready` (bool), `progress_pct` (0-100 float)
- [ ] `ready` is True only when ALL steps with `required == True` have `passed == True`
- [ ] Required checks: `python`, `nats`, `daemon`, `keychain`, `config`
- [ ] Optional checks: `superset`, `ssh`, `probes`, `surgeons`, `skills`
- [ ] Each check returns dict with keys: `name`, `passed` (bool), `required` (bool), `message` (str), `fix` (str, may be empty)
- [ ] `render_guide()` returns markdown string with completed steps as checkboxes and remaining steps with bash fix commands
- [ ] `--json` flag on CLI entry point outputs `check_all()` as JSON
- [ ] NATS check connects to `127.0.0.1:4222` with 2s timeout
- [ ] Daemon check hits `http://127.0.0.1:8855/health` with 3s timeout
- [ ] Keychain check uses macOS `security find-generic-password -s fleet-nerve-hmac`; provides env var fallback for non-macOS

**Dependencies:** _paths.py (REPO_ROOT)

### R3: Billing Tiers

**Description:** Four billing tiers with feature gating: Community (free, unlimited nodes), Pro ($29/node/mo, <=10 nodes), Team ($49/node/mo, <=50 nodes), Enterprise (custom, unlimited). MCP tools are mapped to required features; ungated tools default to Community.

**Acceptance Criteria:**
- [ ] `BillingTier` enum has exactly 4 values: `COMMUNITY`, `PRO`, `TEAM`, `ENTERPRISE`
- [ ] `TIER_LIMITS[BillingTier.COMMUNITY]` has `max_nodes >= 999`, `price_per_node == 0`
- [ ] `TIER_LIMITS[BillingTier.PRO]` has `max_nodes == 10`, `price_per_node == 29`
- [ ] `TIER_LIMITS[BillingTier.TEAM]` has `max_nodes == 50`, `price_per_node == 49`
- [ ] `TIER_LIMITS[BillingTier.ENTERPRISE]` has features list containing `"all"`
- [ ] `BillingManager.check_feature(feature)` returns True if feature is in current tier's feature list or tier has `"all"`
- [ ] `BillingManager.require_feature(feature)` raises `BillingError` with `current_tier`, `required_tier`, `feature` attrs when feature not available
- [ ] `BillingManager.require_tool(tool_name)` resolves tool to feature via `TOOL_FEATURE_MAP`; tools not in map are always allowed
- [ ] `BillingManager.check_node_limit(current_nodes)` returns False when current_nodes exceeds tier max
- [ ] `BillingManager.record_usage(feature, node_id)` appends `UsageEvent` with timestamp
- [ ] `BillingManager.usage_report()` returns dict with `tier`, `max_nodes`, `price_per_node`, `features_available`, `usage_counts`, `total_events`
- [ ] `BillingManager.upgrade_tier(new_tier)` returns dict with `direction` in (`upgrade`, `downgrade`, `no_change`), `features_gained`, `features_lost`
- [ ] `get_billing()` returns module-level singleton; `reset_billing()` clears it
- [ ] `BillingManager.sync_from_stripe(subscription_id)` queries Stripe and updates tier; keeps current tier unchanged when Stripe unavailable

**Dependencies:** stripe_integration.py (optional, for sync_from_stripe)

### R4: Stripe Integration

**Description:** Connects billing tiers to Stripe subscriptions. Handles checkout session creation, subscription verification, webhook event processing, and node count tracking. Gracefully degrades when `stripe` package not installed or API key not set.

**Acceptance Criteria:**
- [ ] `StripeIntegration` defines 3 products: `pro` ($29/mo, 10 nodes), `team` ($49/mo, 50 nodes), `enterprise` (custom)
- [ ] `StripeIntegration.available` property returns False when stripe package missing or API key not set
- [ ] All public methods return error dict (not raise) when Stripe unavailable: `{"error": "..."}`
- [ ] `create_checkout_session(tier, success_url, cancel_url)` returns dict with `session_id` and `checkout_url`
- [ ] `verify_subscription(subscription_id)` returns dict with `active` (bool), `tier` (str), `node_count` (int)
- [ ] `handle_webhook(payload, sig_header)` validates webhook signature and processes 6 event types: `checkout.session.completed`, `customer.subscription.created`, `customer.subscription.updated`, `customer.subscription.deleted`, `invoice.paid`, `invoice.payment_failed`
- [ ] API key loaded from env var `MULTIFLEET_STRIPE_SECRET_KEY` or `STRIPE_SECRET_KEY`
- [ ] Webhook secret loaded from env var `MULTIFLEET_STRIPE_WEBHOOK_SECRET`
- [ ] `get_stripe()` returns module-level singleton

**Dependencies:** `stripe` Python package (optional)

### R5: Cross-Product Adapters

**Description:** Bridge/adapter modules that connect multi-fleet to external products: ContextDNA (chief_sync, evidence_bridge, memory_bridge), 3-Surgeons (surgeon_adapter, surgeon_escalation, surgeon_events), Superpowers (superpowers_adapter, superpowers_bridge, brainstorm_ingest), Superset (superset_bridge), Discord (discord_bridge), Professor (professor_bridge), Synaptic (synaptic_bridge), and the unified product_bridge.

**Acceptance Criteria:**
- [ ] `ChiefSyncAdapter.pull_context(task)` returns `ChiefMemoryContext` with `professor_guidance`, `prior_failures`, `prior_successes`, `arch_decisions`, `evidence_entries`, `pull_duration_ms`
- [ ] `BrainstormIngestAdapter.ingest(data)` accepts structured dict, markdown, or free-form text and returns `BrainstormPacket`
- [ ] `SurgeonEscalationAdapter.escalate_no_consensus(surgeon_decision, task_id)` returns `ChiefEscalationPacket`
- [ ] `SurgeonAdapter.is_available()` returns bool; `get_state()` returns current adapter state; `get_counters()` returns usage counters
- [ ] `ProfessorBridge.query(topic)` returns dict with professor wisdom for the topic
- [ ] `ProfessorBridge.get_panel_data()` returns dict with `recent_wisdom` and `active_gotchas` lists
- [ ] `SynapticBridge.get_active_insights()` returns list of insight dicts with `severity` field
- [ ] `SynapticBridge.should_alert_aaron()` returns bool
- [ ] `MemoryBridge` provides access to ContextDNA memory store from within multi-fleet
- [ ] `ContextDNAEvidenceBridge` syncs evidence entries between multi-fleet evidence ledger and ContextDNA
- [ ] `SupersetBridge` proxies queries to Superset execution runtime
- [ ] `DiscordBridge` enables fleet notifications via Discord webhooks
- [ ] `ProductBridge` provides unified interface across all cross-product integrations with stats tracking
- [ ] All adapters/bridges include `_stats` dict tracking call counts and errors
- [ ] All adapters/bridges degrade gracefully (return error dicts or defaults) when target product is unavailable

**Dependencies:** External products (ContextDNA, 3-Surgeons, Superpowers, Superset, Discord) -- all optional

### R6: Configuration Centralization

**Description:** Unified peer discovery and path resolution. Config priority: `.multifleet/config.json` (JSON, scales to 100+) > `3s-network.local.conf` (legacy shell) > discovered peers (mDNS/NATS cache). `_paths.py` resolves REPO_ROOT, GIT_ROOT, SUPERREPO_ROOT without hardcoded absolute paths.

**Acceptance Criteria:**
- [ ] `_paths.REPO_ROOT` resolves to multi-fleet package root; overridable via `MULTIFLEET_REPO_ROOT` env var
- [ ] `_paths.get_git_root()` returns git repo root or REPO_ROOT if not in a git repo
- [ ] `_paths.get_superrepo_root()` returns superrepo root; overridable via `MULTIFLEET_SUPERREPO_ROOT` env var
- [ ] `config.detect_node_id()` priority: `MULTIFLEET_NODE_ID` env var > IP match in config.json > hostname fallback
- [ ] `config.load_peers()` returns `(peers_dict, chief_node_id)` where peers_dict maps node_id to `{user, ip, port, role}`
- [ ] `load_peers()` loads from JSON config first, falls back to shell conf, then discovered peers only
- [ ] Discovered peers are merged with config peers; config takes priority on conflicts
- [ ] Discovered peer cache evicts entries older than 30 minutes
- [ ] `save_discovered_peers()` writes to `.multifleet/discovered_peers.json`, filtering out config-sourced peers
- [ ] `is_tailscale_available()` returns True only when `tailscale status --json` shows `BackendState == "Running"`
- [ ] `resolve_tailscale_ip()` returns static config IP first, dynamic tailscale lookup second, None if unavailable

**Dependencies:** None (foundational module)

### R7: Backup and Restore

**Description:** Snapshot and restore fleet state as compressed tarballs. Backs up: messages.db, leases.db, evidence.db (with WAL/SHM), audit trail, decisions.jsonl, config.json, version.json.

**Acceptance Criteria:**
- [ ] `FleetBackup.create_snapshot()` produces a `.tar.gz` file containing all `BACKUP_TARGETS` that exist
- [ ] Snapshot tarball includes `snapshot-meta.json` with `timestamp`, `fleet_dir`, `version`, `files` list
- [ ] `create_snapshot(output_path)` writes to specified path; without it, writes to `{fleet_dir}/snapshots/`
- [ ] `FleetBackup.restore_snapshot(path)` extracts tarball into fleet_dir and returns `{restored_entries, fleet_dir, warnings}`
- [ ] Restore with `force=False` (default) creates pre-restore backup of existing state before overwriting
- [ ] Restore rejects tarballs containing path traversal (`..` or absolute paths)
- [ ] `list_snapshots()` returns list of snapshot metadata dicts sorted by file glob
- [ ] `snapshot_info(path)` returns dict with `path`, `size_bytes`, `entries`, and optional `metadata` from embedded JSON

**Dependencies:** None (file-system only)

### R8: Schema Migration

**Description:** Version-tracked fleet state migrations. Current version: 5.0.0. Migrations run in order (3.0.0->4.0.0->5.0.0). Version stored in `version.json`.

**Acceptance Criteria:**
- [ ] `FleetMigration.CURRENT_VERSION == "5.0.0"`
- [ ] `check_version()` returns dict with `current_version`, `expected_version`, `up_to_date` (bool), `needs_migration` (bool)
- [ ] `migrate()` auto-detects from_version and runs all applicable migration steps in order
- [ ] `migrate()` returns dict with `status` in (`ok`, `error`, `already_current`), `steps` list, `errors` list
- [ ] v3->v4 migration creates `audit/` directory and `config.json` if missing
- [ ] v4->v5 migration creates evidence.db with `evidence` table (9 columns) and 3 indexes; adds `probe_status` column to leases table if it exists
- [ ] Version file updated only when all migration steps succeed (no partial version update on error)
- [ ] Migration stops on first error and reports the failing step

**Dependencies:** SQLite (evidence.db, messages.db)

### R9: Security and Sanitization

**Description:** HMAC-SHA256 message signing/verification, replay prevention (5-min window), peer identity validation, prompt injection defense, credential leak prevention, and message size limits.

**Acceptance Criteria:**
- [ ] `FleetSecurity` loads HMAC key with priority: `FLEET_HMAC_KEY` env var > macOS Keychain (`fleet_nerve_hmac_key` service) > auto-generate
- [ ] `sign_message(msg)` adds `_hmac` and `_signed_ts` fields to message dict
- [ ] `verify_message(msg)` returns True only when HMAC matches and timestamp is within `REPLAY_WINDOW_S` (300s)
- [ ] `verify_message()` rejects messages with timestamps outside the 5-minute replay window
- [ ] `FleetSecurity._stats` tracks counters: `signed`, `verified`, `rejected_bad_sig`, `rejected_replay`, `rejected_unknown_peer`, `accepted_unsigned`
- [ ] When no HMAC key is available, unsigned messages are accepted with `accepted_unsigned` counter incremented (graceful degradation)
- [ ] `sanitize_message(text)` applies all 3 defense layers in sequence: strip LLM markers, strip prompt injections, limit size
- [ ] `strip_llm_markers(text)` removes ChatML, Llama/Mistral, and Anthropic role markers (11 patterns)
- [ ] `strip_prompt_injections(text)` removes known prompt injection patterns with word boundaries
- [ ] `limit_message_size(text)` enforces `DEFAULT_MAX_MESSAGE_BYTES` (10KB) configurable via `FLEET_MAX_MESSAGE_BYTES` env var
- [ ] `SanitizingLogFilter` redacts API keys from log output
- [ ] HMAC key value is never written to logs (only signature pass/fail status)

**Dependencies:** macOS Keychain (optional, for key storage)

## Out of Scope

- NATS transport layer (see cavekit-transport)
- Evidence ledger storage (see cavekit-evidence)
- Probe engine logic (see cavekit-probes)
- Synthesis/race orchestration (see cavekit-synthesis)
- Event bus internals (see cavekit-events)
- IDE panel rendering (see cavekit-ide)
- Webhook signature verification for non-Stripe webhooks
- Multi-tenant isolation (Enterprise tier mentioned but not implemented)
- SSO integration (Enterprise tier placeholder only)

## Cross-References

- See also: cavekit-ide.md (consumes professor_bridge, synaptic_bridge, surgeon_adapter for panel data)
- See also: cavekit-transport.md (config.py peer registry feeds NATS connection targets)
- See also: cavekit-evidence.md (migration.py creates evidence schema; contextdna_evidence_bridge syncs entries)
- See also: cavekit-synthesis.md (surgeon_escalation routes no-consensus to chief; chief_sync pulls memory context)
- See also: cavekit-events.md (adapters emit events through the event bus)
