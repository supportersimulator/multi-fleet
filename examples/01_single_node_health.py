"""Example 1: Single-node fleet health check.

Spins up an in-process fleet daemon, checks health, shuts down.
Demonstrates: daemon lifecycle, health endpoint, channel cascade.

Run: python3 examples/01_single_node_health.py
"""
from __future__ import annotations

import json
import time
import urllib.request
import subprocess
import sys


def main() -> int:
    # Start daemon in background
    proc = subprocess.Popen(
        [sys.executable, "-m", "multifleet.daemon"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # Wait for /health to respond
        for _ in range(20):
            try:
                with urllib.request.urlopen("http://127.0.0.1:8855/health", timeout=1) as r:
                    data = json.load(r)
                    break
            except Exception:
                time.sleep(0.5)
        else:
            print("daemon did not start", file=sys.stderr)
            return 1

        print(f"Node ID:    {data.get('nodeId')}")
        print(f"Status:     {data.get('status')}")
        print(f"Transport:  {data.get('transport')}")
        print(f"Peers:      {list(data.get('peers', {}).keys()) or '[none — single-node mode]'}")
        print(f"Uptime (s): {data.get('uptime_s', 0):.1f}")
        return 0
    finally:
        proc.terminate()
        proc.wait(timeout=5)


if __name__ == "__main__":
    sys.exit(main())
