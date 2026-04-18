#!/usr/bin/env python3
"""
Example 03 -- Health monitor dashboard.

Polls the local fleet daemon's /health endpoint every few seconds and
prints a formatted status table showing peers, channels, and uptime.

Prerequisites:
  - Fleet daemon running: python3 -m multifleet.daemon

Usage:
  python3 examples/03_health_monitor.py
  python3 examples/03_health_monitor.py --interval 10

Environment variables:
  FLEET_DAEMON_URL    Daemon HTTP base URL (default: http://127.0.0.1:8855)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

DAEMON_URL = os.environ.get("FLEET_DAEMON_URL", "http://127.0.0.1:8855")


def fetch_health() -> dict | None:
    """Fetch health data from the fleet daemon."""
    try:
        with urllib.request.urlopen(f"{DAEMON_URL}/health", timeout=5) as resp:
            return json.load(resp)
    except Exception:
        return None


def fetch_peers() -> dict | None:
    """Fetch peer registry from the fleet daemon."""
    try:
        with urllib.request.urlopen(f"{DAEMON_URL}/peers", timeout=5) as resp:
            return json.load(resp)
    except Exception:
        return None


def format_uptime(seconds: float) -> str:
    """Format seconds into a human-readable uptime string."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m {seconds % 60:.0f}s"
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    return f"{hours}h {minutes}m"


def format_ago(timestamp: float) -> str:
    """Format a timestamp as 'X ago'."""
    if not timestamp:
        return "never"
    ago = time.time() - timestamp
    if ago < 0:
        return "just now"
    if ago < 60:
        return f"{ago:.0f}s ago"
    if ago < 3600:
        return f"{ago / 60:.0f}m ago"
    return f"{ago / 3600:.1f}h ago"


def clear_screen() -> None:
    """Clear terminal screen (works on macOS, Linux, Windows)."""
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()


def print_dashboard(health: dict, peers: dict | None) -> None:
    """Print a formatted health dashboard to the terminal."""
    now = datetime.now().strftime("%H:%M:%S")
    node_id = health.get("nodeId", "unknown")
    status = health.get("status", "unknown")
    uptime = health.get("uptime_s", 0)
    transport = health.get("transport", "unknown")

    # Status indicator
    status_icon = "OK" if status == "ok" else "DEGRADED" if status == "degraded" else "DOWN"

    clear_screen()
    print("=" * 60)
    print(f"  Multi-Fleet Health Monitor        {now}")
    print("=" * 60)
    print()

    # Node info
    print(f"  Node:       {node_id}")
    print(f"  Status:     [{status_icon}]")
    print(f"  Transport:  {transport}")
    print(f"  Uptime:     {format_uptime(uptime)}")
    print()

    # Peers section
    peer_data = health.get("peers", {})
    if peers and isinstance(peers, dict):
        # Merge with detailed peer info if available
        for pid, pinfo in peers.items():
            if pid not in peer_data:
                peer_data[pid] = pinfo

    if peer_data:
        print("  Peers")
        print("  " + "-" * 50)
        print(f"  {'Node':<15} {'Status':<10} {'Last Seen':<15} {'IP':<15}")
        print("  " + "-" * 50)
        for pid, pinfo in sorted(peer_data.items()):
            if isinstance(pinfo, dict):
                last_ts = pinfo.get("last_seen", pinfo.get("ts", 0))
                ip = pinfo.get("ip", "")
                ago = time.time() - last_ts if last_ts else float("inf")
                p_status = "online" if ago < 30 else "stale" if ago < 120 else "offline"
                print(f"  {pid:<15} {p_status:<10} {format_ago(last_ts):<15} {ip:<15}")
            else:
                print(f"  {pid:<15} {'?':<10}")
        print()
    else:
        print("  Peers: none discovered")
        print()

    # Surgeon availability (if present in health response)
    surgeons = health.get("surgeons", {})
    if surgeons:
        print("  3-Surgeon Availability")
        print("  " + "-" * 40)
        for name, available in sorted(surgeons.items()):
            avail_str = "available" if available else "unavailable"
            print(f"  {name:<25} [{avail_str}]")
        print()

    # Channel health (if present)
    channels = health.get("channels", health.get("channel_health", {}))
    if channels:
        print("  Channel Health")
        print("  " + "-" * 40)
        for ch_name, ch_status in sorted(channels.items()):
            if isinstance(ch_status, dict):
                state = ch_status.get("state", "unknown")
            else:
                state = str(ch_status)
            print(f"  {ch_name:<20} {state}")
        print()

    # Stats (if present)
    stats = health.get("stats", {})
    if stats:
        sent = stats.get("sent", 0)
        received = stats.get("received", 0)
        errors = stats.get("errors", 0)
        print(f"  Messages: {sent} sent, {received} received, {errors} errors")
        print()

    print("-" * 60)
    print("  Polling every few seconds. Ctrl-C to quit.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Poll fleet daemon health and display a live dashboard."
    )
    parser.add_argument(
        "--interval", "-i",
        type=float,
        default=5.0,
        help="Polling interval in seconds (default: 5)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Print once and exit (no loop)",
    )
    args = parser.parse_args()

    # Initial check
    print(f"Connecting to fleet daemon at {DAEMON_URL}...")
    health = fetch_health()
    if not health:
        print(
            f"ERROR: Fleet daemon not reachable at {DAEMON_URL}.\n"
            f"Start it with:\n"
            f"  export MULTIFLEET_NODE_ID=yournode\n"
            f"  python3 -m multifleet.daemon",
            file=sys.stderr,
        )
        return 1

    if args.once:
        peers = fetch_peers()
        print_dashboard(health, peers)
        return 0

    # Continuous polling loop
    try:
        while True:
            health = fetch_health()
            peers = fetch_peers()
            if health:
                print_dashboard(health, peers)
            else:
                clear_screen()
                print(f"[{datetime.now().strftime('%H:%M:%S')}] "
                      f"Daemon unreachable at {DAEMON_URL}. Retrying...")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nMonitor stopped.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
