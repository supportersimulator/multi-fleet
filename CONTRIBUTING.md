# Contributing to Multi-Fleet

Thanks for your interest in Multi-Fleet. This guide covers how to set up,
test, and submit changes.

## Development Setup

```bash
# Clone and enter
git clone https://github.com/supportersimulator/er-simulator-superrepo.git
cd er-simulator-superrepo/multi-fleet

# Create virtualenv
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install nats-py pytest pytest-asyncio ruff

# Set node identity
export MULTIFLEET_NODE_ID=dev1
```

## Running Tests

```bash
# All tests (no network required -- everything is mocked)
cd multi-fleet
python -m pytest tests/ -v

# Single file
python -m pytest tests/test_fleet_send.py -v

# With coverage
pip install pytest-cov
python -m pytest tests/ --cov=bin --cov-report=term-missing
```

## Code Style

- **Formatter/linter:** `ruff check . && ruff format --check .`
- **Type hints** on all public functions
- **Docstrings** on modules, classes, and public functions
- **No `except Exception: pass`** -- always log or count failures
- **Imports:** stdlib, third-party, local (separated by blank lines)

## Pull Request Process

1. **Fork** the repo and create a feature branch from `main`
2. **Write tests** for new functionality (see `tests/` for patterns)
3. **Run the full suite:** `python -m pytest tests/ -v`
4. **Lint:** `ruff check . && ruff format --check .`
5. **Open a PR** against `main` with a clear description of what and why
6. **One approval** required before merge

Keep PRs focused -- one feature or fix per PR. Large changes should be
discussed in an issue first.

## Adding a Communication Channel

The fallback cascade lives in `bin/fleet_nerve_mcp.py`. To add a new channel:

1. Choose a priority slot (P0-P8). Gaps between existing channels are
   intentional for future expansion.
2. Implement `_send_via_<channel>(msg, target, timeout)` following the
   pattern of existing channels. Must return `True` on success, `False`
   on failure.
3. Add channel to the `CHANNEL_ORDER` list at its priority position.
4. Add a channel state entry in the state machine (HEALTHY/DEGRADED/BROKEN).
5. Write tests in `tests/test_fleet_send.py` mocking the transport layer.
6. Document the channel in `COMMS-PROTOCOL.md`.

## Adding an MCP Tool

Tools are defined in `bin/fleet_nerve_mcp.py`:

1. Define a function with the `@tool` decorator pattern used by existing tools.
2. Add input schema (JSON Schema) and a clear description.
3. Register it in the tool dispatch map.
4. Add tests in `tests/test_fleet_mcp.py`.
5. Run `python3 scripts/build_manifests.py` to regenerate IDE manifests.

## Adding a Skill

Skills live in `skills/<skill-name>/SKILL.md`:

1. Create `skills/<your-skill>/SKILL.md` with frontmatter and instructions.
2. Follow the pattern of existing skills (e.g., `fleet-send/SKILL.md`).
3. The skill is auto-discovered by Claude Code from the plugin manifest.

## Questions?

Open an issue. We're happy to help.
