"""
MCP entry point — loads the fleet-nerve MCP server.

When pip-installed, the MCP server module ships as `multifleet/_mcp_server.py`
(force-included from bin/fleet_nerve_mcp.py by hatch).
In a dev checkout where _mcp_server.py is absent, falls back to loading
bin/fleet_nerve_mcp.py from the repo root via explicit file path.
"""

import importlib.util
import sys
from pathlib import Path


def main():
    # Preferred path: packaged _mcp_server module (available after pip install)
    try:
        from . import _mcp_server  # type: ignore[attr-defined]
        _mcp_server.main()
        return
    except ImportError:
        pass

    # Dev fallback: load bin/fleet_nerve_mcp.py directly from repo root
    from ._paths import REPO_ROOT
    bin_file = REPO_ROOT / "bin" / "fleet_nerve_mcp.py"
    if not bin_file.is_file():
        print(
            "ERROR: Could not locate fleet-nerve MCP server. "
            "Expected multifleet._mcp_server or bin/fleet_nerve_mcp.py.",
            file=sys.stderr,
        )
        sys.exit(1)
    spec = importlib.util.spec_from_file_location("fleet_nerve_mcp", str(bin_file))
    if spec and spec.loader:
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.main()
    else:
        print("ERROR: Failed to load bin/fleet_nerve_mcp.py", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
