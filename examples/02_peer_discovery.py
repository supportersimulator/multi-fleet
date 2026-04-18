"""
Example 02 — Peer discovery via heartbeat

Each node publishes a heartbeat every 5s on fleet.heartbeat. Other nodes
listen + build a live peer map. Stale peers (>30s no heartbeat) drop off.

Prereqs:
  - NATS server running
  - Run with 2+ different MULTIFLEET_NODE_ID values

Run:
  MULTIFLEET_NODE_ID=alpha python 02_peer_discovery.py
  MULTIFLEET_NODE_ID=beta  python 02_peer_discovery.py
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
from collections import defaultdict

try:
    import nats
except ImportError:
    sys.exit("pip install nats-py first")


NODE_ID = os.environ.get("MULTIFLEET_NODE_ID", "demo-node")
NATS_URL = os.environ.get("NATS_URL", "nats://127.0.0.1:4222")
STALE_AFTER = 30.0


async def main() -> None:
    nc = await nats.connect(NATS_URL, name=NODE_ID)
    peers: dict[str, float] = defaultdict(float)

    async def on_heartbeat(msg):
        try:
            d = json.loads(msg.data.decode())
            src = d.get("from")
            if src and src != NODE_ID:
                peers[src] = time.time()
        except Exception:
            pass

    await nc.subscribe("fleet.heartbeat", cb=on_heartbeat)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    print(f"[{NODE_ID}] heartbeating every 5s, listening for peers...")
    while not stop.is_set():
        await nc.publish("fleet.heartbeat", json.dumps({"from": NODE_ID, "ts": time.time()}).encode())
        now = time.time()
        alive = {p: round(now - t, 1) for p, t in peers.items() if now - t < STALE_AFTER}
        print(f"[{NODE_ID}] peers alive: {alive or 'none yet'}")
        try:
            await asyncio.wait_for(stop.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass

    await nc.drain()


if __name__ == "__main__":
    asyncio.run(main())
