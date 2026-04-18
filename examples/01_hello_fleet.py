#!/usr/bin/env python3
"""
Example 01 -- Hello Fleet: minimal fleet setup.

Connects to NATS, sends periodic heartbeats, and discovers peers
automatically. This is the simplest possible multifleet program.

Prerequisites:
  - NATS server running: nats-server -p 4222
  - multifleet installed: pip install multifleet

Usage (open two terminals with different node IDs):
  MULTIFLEET_NODE_ID=alice python3 examples/01_hello_fleet.py
  MULTIFLEET_NODE_ID=bob   python3 examples/01_hello_fleet.py

Both nodes will discover each other within seconds. Ctrl-C to quit.

Environment variables:
  NATS_URL              NATS connection URL (default: nats://127.0.0.1:4222)
  MULTIFLEET_NODE_ID    This node's identity (default: demo-node)
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
import uuid

try:
    import nats
except ImportError:
    sys.exit("nats-py is required. Install it: pip install nats-py")

# ── Configuration from environment ──
NODE_ID = os.environ.get("MULTIFLEET_NODE_ID", "demo-node")
NATS_URL = os.environ.get("NATS_URL", "nats://127.0.0.1:4222")

# How often to send a heartbeat (seconds)
HEARTBEAT_INTERVAL = 5.0

# Peers not heard from in this many seconds are considered offline
STALE_THRESHOLD = 30.0


async def main() -> None:
    # Step 1: Connect to NATS with our node identity
    nc = await nats.connect(NATS_URL, name=NODE_ID)
    print(f"[{NODE_ID}] Connected to NATS at {NATS_URL}")

    # Track discovered peers: node_id -> last_seen timestamp
    peers: dict[str, float] = {}

    # Step 2: Subscribe to heartbeat subject to discover peers
    async def on_heartbeat(msg):
        """Called whenever any node publishes a heartbeat."""
        try:
            data = json.loads(msg.data.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        sender = data.get("from", "")
        if sender and sender != NODE_ID:
            is_new = sender not in peers
            peers[sender] = time.time()
            if is_new:
                print(f"[{NODE_ID}] Discovered new peer: {sender}")

    await nc.subscribe("fleet.heartbeat", cb=on_heartbeat)

    # Step 3: Subscribe to direct messages addressed to this node
    async def on_direct_message(msg):
        """Called when another node sends a message directly to us."""
        try:
            data = json.loads(msg.data.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            data = msg.data.decode(errors="replace")
        print(f"[{NODE_ID}] Received on {msg.subject}: {data}")

    await nc.subscribe(f"fleet.{NODE_ID}.>", cb=on_direct_message)

    # Step 4: Subscribe to broadcast messages for all nodes
    await nc.subscribe("fleet.all.>", cb=on_direct_message)

    print(f"[{NODE_ID}] Listening on fleet.{NODE_ID}.> and fleet.all.>")
    print(f"[{NODE_ID}] Sending heartbeats every {HEARTBEAT_INTERVAL}s. Ctrl-C to quit.\n")

    # Graceful shutdown on SIGINT / SIGTERM
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    # Step 5: Main loop -- send heartbeats and print peer status
    while not stop.is_set():
        # Publish heartbeat so other nodes can find us
        heartbeat = {
            "from": NODE_ID,
            "ts": time.time(),
            "id": str(uuid.uuid4()),
        }
        await nc.publish("fleet.heartbeat", json.dumps(heartbeat).encode())

        # Prune stale peers and display who is alive
        now = time.time()
        alive = {
            node: f"{now - last_seen:.1f}s ago"
            for node, last_seen in peers.items()
            if now - last_seen < STALE_THRESHOLD
        }
        if alive:
            print(f"[{NODE_ID}] Peers online: {alive}")
        else:
            print(f"[{NODE_ID}] No peers discovered yet (waiting for heartbeats...)")

        # Wait for next heartbeat interval or shutdown signal
        try:
            await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_INTERVAL)
        except asyncio.TimeoutError:
            pass

    # Step 6: Clean disconnect
    await nc.drain()
    print(f"\n[{NODE_ID}] Disconnected cleanly.")


if __name__ == "__main__":
    asyncio.run(main())
