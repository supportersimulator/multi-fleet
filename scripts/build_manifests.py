#!/usr/bin/env python3
"""
build_manifests.py — ONE canonical source, all host-specific manifests.

Reads multi-fleet/.claude-plugin/plugin.json + .mcp.json as the canonical
source and generates manifests for: Claude Code, Cursor, VS Code, Codex, Gemini.

Usage:
    python3 multi-fleet/scripts/build_manifests.py          # Generate all manifests
    python3 multi-fleet/scripts/build_manifests.py --check  # Verify sync, no writes
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
CANONICAL_PLUGIN = PLUGIN_ROOT / ".claude-plugin" / "plugin.json"
CANONICAL_MCP = PLUGIN_ROOT / ".mcp.json"
MARKETPLACE_JSON = PLUGIN_ROOT / ".claude-plugin" / "marketplace.json"
HOOKS_JSON = PLUGIN_ROOT / "hooks" / "hooks.json"

# Output targets
OUTPUTS: dict[str, Path] = {
    "claude": PLUGIN_ROOT / ".claude-plugin" / "plugin.json",
    "cursor": PLUGIN_ROOT / ".cursor-plugin" / "plugin.json",
    "vscode": PLUGIN_ROOT / ".vscode" / "mcp.json.example",
    "codex": PLUGIN_ROOT / "codex-config.toml.example",
    "gemini": PLUGIN_ROOT / "gemini-extension.json",
    "install": PLUGIN_ROOT / "INSTALL.md",
}


# ---------------------------------------------------------------------------
# Canonical source loader
# ---------------------------------------------------------------------------
def load_canonical() -> dict[str, Any]:
    """Load and merge canonical plugin.json + .mcp.json into a single dict."""
    with open(CANONICAL_PLUGIN) as f:
        plugin = json.load(f)
    with open(CANONICAL_MCP) as f:
        mcp = json.load(f)

    # Discover skills from marketplace.json (authoritative list)
    skills: list[str] = []
    if MARKETPLACE_JSON.exists():
        with open(MARKETPLACE_JSON) as f:
            mp = json.load(f)
        for p in mp.get("plugins", []):
            for s in p.get("skills", []):
                # ./skills/fleet-send -> fleet-send
                skills.append(s.rsplit("/", 1)[-1])

    # Discover commands from commands/ directory
    commands: list[str] = []
    cmd_dir = PLUGIN_ROOT / "commands"
    if cmd_dir.is_dir():
        commands = sorted(
            p.stem for p in cmd_dir.glob("*.md")
        )

    # Discover agents from agents/ directory
    agents: list[str] = []
    agent_dir = PLUGIN_ROOT / "agents"
    if agent_dir.is_dir():
        agents = sorted(
            p.stem for p in agent_dir.glob("*.md")
        )

    # Hooks
    hooks: dict = {}
    if HOOKS_JSON.exists():
        with open(HOOKS_JSON) as f:
            hooks = json.load(f)

    # MCP server config
    mcp_servers = mcp.get("mcpServers", {})
    server_name = list(mcp_servers.keys())[0] if mcp_servers else "fleet-nerve"
    server_cfg = mcp_servers.get(server_name, {})

    return {
        "name": plugin["name"],
        "displayName": plugin.get("displayName", plugin["name"]),
        "description": plugin["description"],
        "version": plugin["version"],
        "author": plugin["author"],
        "homepage": plugin.get("homepage", ""),
        "repository": plugin.get("repository", ""),
        "license": plugin.get("license", "MIT"),
        "keywords": plugin.get("keywords", []),
        "mcp_server_name": server_name,
        "mcp_server": server_cfg,
        "skills": skills,
        "commands": commands,
        "agents": agents,
        "hooks": hooks,
    }


# ---------------------------------------------------------------------------
# Manifest generators
# ---------------------------------------------------------------------------

def gen_claude(canon: dict[str, Any]) -> str:
    """Claude Code plugin.json — validate/rewrite canonical."""
    doc = {
        "name": canon["name"],
        "displayName": canon["displayName"],
        "description": canon["description"],
        "version": canon["version"],
        "author": canon["author"],
        "homepage": canon["homepage"],
        "repository": canon["repository"],
        "license": canon["license"],
        "keywords": canon["keywords"],
        "skills": "./skills/",
        "agents": "./agents/",
        "commands": "./commands/",
        "hooks": "./hooks/hooks.json",
        "mcp": "./.mcp.json",
    }
    return json.dumps(doc, indent=2) + "\n"


def gen_cursor(canon: dict[str, Any]) -> str:
    """Cursor plugin.json — same structure but with Cursor-specific MCP path."""
    server = canon["mcp_server"]
    # Cursor uses ${workspaceFolder} instead of ${CLAUDE_PLUGIN_ROOT}
    args = []
    for a in server.get("args", []):
        args.append(a.replace("${CLAUDE_PLUGIN_ROOT}/../", "${workspaceFolder}/multi-fleet/"))

    doc = {
        "name": canon["name"],
        "displayName": canon["displayName"],
        "description": canon["description"],
        "version": canon["version"],
        "ide": "cursor",
        "mcpServers": {
            canon["mcp_server_name"]: {
                "command": server.get("command", "python3"),
                "args": args,
                "env": server.get("env", {}),
            }
        },
        "tools": _tool_list(canon),
        "skills": canon["skills"],
        "commands": canon["commands"],
    }
    return json.dumps(doc, indent=2) + "\n"


def gen_vscode(canon: dict[str, Any]) -> str:
    """VS Code .vscode/mcp.json.example — pure MCP server config."""
    server = canon["mcp_server"]
    args = []
    for a in server.get("args", []):
        args.append(a.replace("${CLAUDE_PLUGIN_ROOT}/../", "${workspaceFolder}/multi-fleet/"))

    doc = {
        "_comment": f"Multi-Fleet v{canon['version']} — MCP server config for VS Code",
        "_setup": "Copy this file to .vscode/mcp.json in your workspace root. Set MULTIFLEET_NODE_ID.",
        "mcpServers": {
            canon["mcp_server_name"]: {
                "command": server.get("command", "python3"),
                "args": args,
                "env": server.get("env", {}),
            }
        },
    }
    return json.dumps(doc, indent=2) + "\n"


def gen_codex(canon: dict[str, Any]) -> str:
    """Codex CLI codex-config.toml.example."""
    server = canon["mcp_server"]
    args_raw = server.get("args", [])
    # Codex uses relative paths from project root
    args = []
    for a in args_raw:
        args.append(a.replace("${CLAUDE_PLUGIN_ROOT}/../", "multi-fleet/"))

    env = server.get("env", {})
    env_lines = "\n".join(f'{k} = "{v}"' for k, v in env.items())

    tools = _tool_list(canon)
    tools_str = ", ".join(f'"{t}"' for t in tools)

    lines = [
        f"# Multi-Fleet v{canon['version']} -- Codex CLI configuration",
        "# Copy to your project root as codex-config.toml and set MULTIFLEET_NODE_ID.",
        "",
        f"[mcp_servers.{canon['mcp_server_name']}]",
        f'command = "{server.get("command", "python3")}"',
        f"args = {json.dumps(args)}",
        "",
        f"[mcp_servers.{canon['mcp_server_name']}.env]",
        env_lines,
        "",
        "# Available tools (exposed via MCP)",
        f"# tools = [{tools_str}]",
        "",
        f"# Skills (slash commands): {', '.join(f'/{s}' for s in canon['skills'])}",
        f"# Commands: {', '.join(f'/{c}' for c in canon['commands'])}",
        "",
    ]
    return "\n".join(lines)


def gen_gemini(canon: dict[str, Any]) -> str:
    """Gemini extension manifest."""
    server = canon["mcp_server"]
    args = []
    for a in server.get("args", []):
        args.append(a.replace("${CLAUDE_PLUGIN_ROOT}/../", "multi-fleet/"))

    doc = {
        "_comment": f"Multi-Fleet v{canon['version']} — Gemini extension manifest",
        "name": canon["name"],
        "displayName": canon["displayName"],
        "description": canon["description"],
        "version": canon["version"],
        "author": canon["author"]["name"],
        "license": canon["license"],
        "transport": {
            "type": "stdio",
            "command": server.get("command", "python3"),
            "args": args,
            "env": server.get("env", {}),
        },
        "tools": _tool_list(canon),
        "capabilities": {
            "skills": canon["skills"],
            "commands": canon["commands"],
            "agents": canon["agents"],
        },
    }
    return json.dumps(doc, indent=2) + "\n"


def gen_install(canon: dict[str, Any]) -> str:
    """INSTALL.md -- per-host setup instructions."""
    server = canon["mcp_server"]
    env = server.get("env", {})
    env_rows = []
    for k, v in env.items():
        desc = "Machine identifier" if "NODE" in k else "MCP server port"
        env_rows.append(f"| `{k}` | `{v or '<your-node-id>'}` | {desc} |")
    env_table = "\n".join(env_rows)

    skills_list = "\n".join(f"- `/{s}`" for s in canon["skills"])
    commands_list = "\n".join(f"- `/{c}`" for c in canon["commands"])
    agents_list = "\n".join(f"- `{a}`" for a in canon["agents"])
    port = env.get("FLEET_NERVE_PORT", "8855")
    srv = canon["mcp_server_name"]

    return f"""# Multi-Fleet v{canon["version"]} -- Installation Guide

Cross-machine AI collaboration: real-time peer-to-peer messaging, session-aware
task agents, and fleet-wide productivity visibility.

## Prerequisites

- Python 3.10+
- Redis (local or remote)
- Network access between fleet nodes (port {port})

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
{env_table}

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

3. Restart Cursor. The `{srv}` tools appear in Cursor's tool list.

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
{skills_list}

### Commands
{commands_list}

### Agents
{agents_list}

### MCP Tools

All tools are exposed via the `{srv}` MCP server.
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
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tool_list(canon: dict[str, Any]) -> list[str]:
    """Derive tool names from skills + commands (MCP-exposed surface)."""
    tools = []
    for s in canon["skills"]:
        tools.append(s.replace("-", "_"))
    for c in canon["commands"]:
        tools.append(f"cmd_{c.replace('-', '_')}")
    return tools


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def generate_all(check: bool = False) -> bool:
    """Generate or check all manifests. Returns True if all in sync."""
    canon = load_canonical()

    generators: dict[str, callable] = {
        "claude": gen_claude,
        "cursor": gen_cursor,
        "vscode": gen_vscode,
        "codex": gen_codex,
        "gemini": gen_gemini,
        "install": gen_install,
    }

    all_ok = True

    for key, gen_fn in generators.items():
        target = OUTPUTS[key]
        expected = gen_fn(canon)

        if check:
            if not target.exists():
                print(f"MISSING  {target.relative_to(PLUGIN_ROOT)}")
                all_ok = False
            else:
                actual = target.read_text()
                if actual != expected:
                    print(f"STALE    {target.relative_to(PLUGIN_ROOT)}")
                    all_ok = False
                else:
                    print(f"OK       {target.relative_to(PLUGIN_ROOT)}")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(expected)
            print(f"WROTE    {target.relative_to(PLUGIN_ROOT)}")

    if check:
        if all_ok:
            print("\nAll manifests in sync.")
        else:
            print("\nManifests out of sync. Run without --check to regenerate.")

    return all_ok


def main():
    parser = argparse.ArgumentParser(
        description="Generate host-specific manifests from canonical Multi-Fleet plugin source."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify all manifests are in sync without writing files.",
    )
    args = parser.parse_args()

    ok = generate_all(check=args.check)
    sys.exit(0 if ok or not args.check else 1)


if __name__ == "__main__":
    main()
