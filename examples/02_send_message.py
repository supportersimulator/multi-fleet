#!/usr/bin/env python3
"""
Example 02 -- Send messages via the priority channel cascade.

Demonstrates how multifleet delivers messages through its priority fallback
chain. The local fleet daemon tries each channel in order (P0 Cloud through
P7 Git) and returns as soon as one succeeds.

This example shows three message types:
  - context: passive information sharing (uses all channels)
  - task:    work assignment requiring an active session (P1-P3 only)
  - alert:   critical notification with 3x retry (P1 only)

Prerequisites:
  - Fleet daemon running: python3 -m multifleet.daemon
    (or: python3 bin/fleet_nerve_mcp.py)
  - Target peer must be configured or previously discovered

Usage:
  python3 examples/02_send_message.py <target_node>
  python3 examples/02_send_message.py mac2
  python3 examples/02_send_message.py mac1 --type alert

Environment variables:
  MULTIFLEET_NODE_ID    This node's identity (default: demo-node)
  FLEET_DAEMON_URL      Daemon HTTP base URL (default: http://127.0.0.1:8855)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

# ── Configuration ──

import os

DAEMON_URL = os.environ.get("FLEET_DAEMON_URL", "http://127.0.0.1:8855")

# ── Message type definitions ──
# Each type maps to different channel subsets and retry behavior.
# See COMMS-PROTOCOL.md for the full specification.

MESSAGE_TYPES = {
    "context": {
        "description": "Passive enrichment -- any channel works",
        "channels": "P1 NATS -> P2 HTTP -> P3 Chief -> P4 Seed -> P5 SSH -> P7 Git",
    },
    "task": {
        "description": "Work assignment -- needs active session on target",
        "channels": "P1 NATS -> P2 HTTP -> P3 Chief",
    },
    "alert": {
        "description": "Critical notification -- P1 only, 3x retry",
        "channels": "P1 NATS (3 retries)",
    },
}


def send_message(target: str, msg_type: str, subject: str, body: str) -> dict:
    """Send a message to a target node via the local fleet daemon.

    The daemon handles the full priority cascade internally. We just POST
    the message and get back which channel delivered it.

    Args:
        target:   Destination node ID (e.g. "mac2")
        msg_type: One of "context", "task", "alert", "reply", "broadcast", "sync", "repair"
        subject:  Short description of the message
        body:     Full message content

    Returns:
        Response dict with delivery details (channel used, latency, etc.)

    Raises:
        ConnectionError: If the local fleet daemon is not running
    """
    payload = json.dumps({
        "type": msg_type,
        "to": target,
        "payload": {
            "subject": subject,
            "body": body,
        },
    }).encode()

    req = urllib.request.Request(
        f"{DAEMON_URL}/message",
        data=payload,
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.URLError as exc:
        raise ConnectionError(
            f"Cannot reach fleet daemon at {DAEMON_URL}. "
            f"Start it with: python3 -m multifleet.daemon\n"
            f"Error: {exc}"
        ) from exc


def check_daemon_health() -> dict | None:
    """Check if the local fleet daemon is running and return health data."""
    try:
        with urllib.request.urlopen(f"{DAEMON_URL}/health", timeout=5) as resp:
            return json.load(resp)
    except Exception:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Send a fleet message via the priority channel cascade."
    )
    parser.add_argument("target", help="Target node ID (e.g. mac2)")
    parser.add_argument(
        "--type", "-t",
        choices=list(MESSAGE_TYPES.keys()),
        default="context",
        help="Message type (default: context)",
    )
    parser.add_argument(
        "--subject", "-s",
        default="hello from example",
        help="Message subject line",
    )
    parser.add_argument(
        "--body", "-b",
        default=None,
        help="Message body (default: auto-generated)",
    )
    args = parser.parse_args()

    # Step 1: Verify the daemon is running
    print(f"Checking fleet daemon at {DAEMON_URL}...")
    health = check_daemon_health()
    if not health:
        print(
            f"ERROR: Fleet daemon not reachable at {DAEMON_URL}.\n"
            f"Start it with:\n"
            f"  export MULTIFLEET_NODE_ID=yournode\n"
            f"  python3 -m multifleet.daemon",
            file=sys.stderr,
        )
        return 1

    node_id = health.get("nodeId", "unknown")
    print(f"Daemon running as node '{node_id}'\n")

    # Step 2: Show what channels this message type uses
    type_info = MESSAGE_TYPES.get(args.type, {})
    print(f"Message type: {args.type}")
    print(f"  {type_info.get('description', '')}")
    print(f"  Channels: {type_info.get('channels', 'all')}\n")

    # Step 3: Send the message
    body = args.body or f"Test {args.type} message from {node_id} at {time.time():.0f}"

    print(f"Sending {args.type} to '{args.target}'...")
    print(f"  Subject: {args.subject}")
    print(f"  Body:    {body}\n")

    try:
        result = send_message(args.target, args.type, args.subject, body)
    except ConnectionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    # Step 4: Report which channel delivered
    delivered = result.get("delivered", False)
    channel = result.get("channel", "unknown")
    latency = result.get("latency_ms", 0)

    if delivered:
        print(f"Delivered successfully!")
        print(f"  Channel:    {channel}")
        print(f"  Latency:    {latency:.0f}ms")
    else:
        print(f"Delivery failed.")
        print(f"  All channels exhausted. Target '{args.target}' may be offline.")
        print(f"  The message has been queued in the outbox for retry.")
        print(f"\nTroubleshooting:")
        print(f"  1. Is the target node running a fleet daemon?")
        print(f"  2. Check peer status: curl {DAEMON_URL}/peers")
        print(f"  3. Run diagnostics: python3 -m multifleet.doctor")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
