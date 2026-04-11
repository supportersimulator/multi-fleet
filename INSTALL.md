# Multi-Fleet v5.0.0 -- Installation Guide

Cross-machine AI collaboration: real-time peer-to-peer messaging, session-aware
task agents, and fleet-wide productivity visibility.

## Quick Install

```bash
pip install multifleet          # from PyPI (when published)
# or from source:
pip install ./multi-fleet       # from the superrepo
```

After install, the `fleet` command is available:

```bash
fleet --version                 # show version
fleet status                    # fleet health overview
fleet doctor                    # run diagnostics
fleet dashboard                 # terminal dashboard
fleet dashboard --web           # web dashboard in browser
```

## Prerequisites

- Python 3.10+
- Network access between fleet nodes (port 8855)
- NATS server on chief node (`brew install nats-server` or `apt install nats-server`)

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MULTIFLEET_NODE_ID` | `<your-node-id>` | Machine identifier |
| `FLEET_NERVE_PORT` | `8855` | MCP server port |

Set `MULTIFLEET_NODE_ID` to a unique identifier for this machine (e.g., `mac1`, `mac2`).

---

## Claude Code

Multi-Fleet is a native Claude Code plugin. No extra config needed.

```bash
# From the superrepo root:
cd multi-fleet
# Plugin is auto-discovered from .claude-plugin/plugin.json
```

The MCP server, skills, commands, hooks, and agents are all registered
automatically via `plugin.json`.

---

## Cursor

Cursor supports MCP servers natively.

1. Copy the generated manifest:

   ```bash
   cp multi-fleet/.cursor-plugin/plugin.json ~/.cursor/mcp.json
   ```

   Or add the `mcpServers` block from `.cursor-plugin/plugin.json` into your
   existing `~/.cursor/mcp.json`.

2. Set your node ID in the env block:

   ```json
   "MULTIFLEET_NODE_ID": "mac2"
   ```

3. Restart Cursor. The `fleet-nerve` tools appear in Cursor's tool list.

---

## VS Code

VS Code supports MCP via the `mcp.json` workspace config.

1. Copy the example config:

   ```bash
   mkdir -p .vscode
   cp multi-fleet/.vscode/mcp.json.example .vscode/mcp.json
   ```

2. Edit `.vscode/mcp.json` and set `MULTIFLEET_NODE_ID`.

3. Reload VS Code. MCP tools are available to any VS Code AI extension that
   reads `.vscode/mcp.json` (e.g., GitHub Copilot, Continue).

---

## Codex CLI

OpenAI Codex CLI reads `codex-config.toml` for MCP server definitions.

1. Copy the example config:

   ```bash
   cp multi-fleet/codex-config.toml.example codex-config.toml
   ```

2. Edit `codex-config.toml` and set `MULTIFLEET_NODE_ID`.

3. Run Codex. Fleet tools are available via MCP.

---

## Gemini (Google AI Studio / Gemini CLI)

Gemini supports extensions via JSON manifests.

1. Reference the extension manifest:

   ```bash
   # Point Gemini to the extension file:
   multi-fleet/gemini-extension.json
   ```

2. Set `MULTIFLEET_NODE_ID` in the transport env block.

3. Fleet tools are available as Gemini function calls.

---

## Available Capabilities

### Skills (slash commands)
- `/using-multi-fleet`
- `/fleet-send`
- `/fleet-status`
- `/fleet-task`
- `/fleet-wake`
- `/fleet-repair`
- `/fleet-tunnel`
- `/fleet-watchdog`
- `/fleet-check`
- `/fleet-worker`
- `/productivity-view`

### Commands
- `/fleet-check`
- `/fleet-dashboard`
- `/fleet-repair`
- `/fleet-send`
- `/fleet-status`
- `/fleet-task`
- `/fleet-tunnel`
- `/fleet-wake`
- `/fleet-watchdog`
- `/fleet-worker`

### Agents
- `fleet-coordinator`
- `fleet-worker`

### MCP Tools

All tools are exposed via the `fleet-nerve` MCP server.
The exact tool list depends on the server implementation. Run the server
with `--list-tools` to see all available tools.

---

## Manifest Regeneration

All host-specific manifests are generated from the canonical source
(`plugin.json` + `.mcp.json`). To regenerate after changes:

```bash
python3 multi-fleet/scripts/build_manifests.py
```

To verify all manifests are in sync without writing:

```bash
python3 multi-fleet/scripts/build_manifests.py --check
```

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| MCP server not found | Verify python3 path; check `fleet_nerve_mcp.py` exists |
| Connection refused | Ensure Redis is running; check FLEET_NERVE_PORT |
| Node not visible | Verify MULTIFLEET_NODE_ID is set and unique |
| Manifests out of sync | Run `python3 multi-fleet/scripts/build_manifests.py` |
