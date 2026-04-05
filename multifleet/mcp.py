"""
MCP entry point — delegates to bin/fleet_nerve_mcp.py.

This module exists so that `fleet-nerve-mcp` console_script works
when the package is pip-installed. The actual implementation lives
in bin/fleet_nerve_mcp.py (which also works standalone).
"""

import importlib
import sys
from pathlib import Path

def main():
    # Import the real MCP server module from bin/
    bin_dir = Path(__file__).parent.parent / "bin"
    if bin_dir.is_dir():
        sys.path.insert(0, str(bin_dir))
    # The bin module sets up its own sys.path for multifleet package imports
    spec = importlib.util.spec_from_file_location(
        "fleet_nerve_mcp", str(bin_dir / "fleet_nerve_mcp.py")
    )
    if spec and spec.loader:
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.main()
    else:
        print("ERROR: Could not locate bin/fleet_nerve_mcp.py", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
