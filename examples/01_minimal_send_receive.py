"""
Example 01 — Minimal fleet send + receive

Prereqs:
  - NATS server running (nats-server -p 4222)
  - multifleet installed: pip install multifleet

What it shows:
  - Connect to NATS as a named node
  - Send a message to 'all' peers (broadcast)
  - Subscribe + receive messages on your node subject
  - Graceful shutdown

Run in two terminals with different node IDs to see cross-node flow:
  term1:  MULTIFLEET_NODE_ID=alice python 01_minimal_send_receive.py
  term2:  MULTIFLEET_NODE_ID=bob   python 01_minimal_send_receive.py

Both see each other's broadcasts. Ctrl-C to quit.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import uuid

try:
    import nats
except ImportError:
    sys.exit("pip install nats-py first")


NODE_ID = os.environ.get("MULTIFLEET_NODE_ID", "demo-node")
NATS_URL = os.environ.get("NATS_URL", "nats://127.0.0.1:4222")


async def main() -> None:
    nc = await nats.connect(NATS_URL, name=NODE_ID)
    print(f"[{NODE_ID}] connected to {NATS_URL}")

    async def on_message(msg):
        try:
            body = json.loads(msg.data.decode())
        except Exception:
            body = msg.data.decode(errors="replace")
        print(f"[{NODE_ID}] <-- {msg.subject}: {body}")

    await nc.subscribe(f"fleet.{NODE_ID}.>", cb=on_message)
    await nc.subscribe("fleet.all.>", cb=on_message)
    print(f"[{NODE_ID}] subscribed: fleet.{NODE_ID}.> + fleet.all.>")

    # Broadcast a hello every 3s. Stop on SIGINT.
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    counter = 0
    while not stop.is_set():
        counter += 1
        payload = {"from": NODE_ID, "seq": counter, "id": str(uuid.uuid4())}
        await nc.publish("fleet.all.hello", json.dumps(payload).encode())
        print(f"[{NODE_ID}] --> fleet.all.hello seq={counter}")
        try:
            await asyncio.wait_for(stop.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            pass

    await nc.drain()
    print(f"[{NODE_ID}] disconnected cleanly")


if __name__ == "__main__":
    asyncio.run(main())
