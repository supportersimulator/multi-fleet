# Installation

## Quick Install

```bash
pip install multifleet
```

Or from source (inside the superrepo):

```bash
pip install ./multi-fleet
```

After install, the `fleet` CLI is available:

```bash
fleet --version
fleet status
fleet doctor
fleet dashboard
fleet dashboard --web
```

## Prerequisites

- Python 3.10+
- Network access between fleet nodes (port 8855)
- NATS server on chief node (`brew install nats-server` or `apt install nats-server`)

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MULTIFLEET_NODE_ID` | *(required)* | Unique machine identifier (e.g. `mac1`, `mac2`) |
| `FLEET_NERVE_PORT` | `8855` | Fleet daemon HTTP port |

## IDE Setup

### Claude Code

Native plugin -- no extra config. Auto-discovered from `.claude-plugin/plugin.json`.

```bash
cd multi-fleet
# Plugin is auto-discovered
```

### Cursor

```bash
cp multi-fleet/.cursor-plugin/plugin.json ~/.cursor/mcp.json
```

Edit `~/.cursor/mcp.json` and set `MULTIFLEET_NODE_ID`. Restart Cursor.

### VS Code

```bash
mkdir -p .vscode
cp multi-fleet/.vscode/mcp.json.example .vscode/mcp.json
```

Edit `.vscode/mcp.json` and set `MULTIFLEET_NODE_ID`. Reload VS Code.

### Codex CLI

```bash
cp multi-fleet/codex-config.toml.example codex-config.toml
```

Edit and set `MULTIFLEET_NODE_ID`.

### Gemini

Reference the extension manifest at `multi-fleet/gemini-extension.json`. Set `MULTIFLEET_NODE_ID` in the transport env block.

## Manifest Regeneration

All IDE manifests are generated from the canonical `plugin.json`:

```bash
python3 multi-fleet/scripts/build_manifests.py        # regenerate
python3 multi-fleet/scripts/build_manifests.py --check # verify sync
```

## Troubleshooting

| Problem | Fix |
|---------|-----|
| MCP server not found | Verify python3 path; check `fleet_nerve_mcp.py` exists |
| Connection refused | Ensure daemon is running; check `FLEET_NERVE_PORT` |
| Node not visible | Verify `MULTIFLEET_NODE_ID` is set and unique |
| Manifests out of sync | Run `build_manifests.py` |
