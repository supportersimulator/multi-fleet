"""
Example 03 — Request/reply between nodes (same pattern as node-ID collision detect)

Demonstrates the NATS request/reply primitive: one side publishes a probe
with a reply inbox, the other side responds, the requester gets the answer
or times out.

This is the same mechanism multifleet uses for node-ID collision detection
on fleet.presence.{node_id} — see multifleet/nats_client.py.

Prereqs:
  - NATS server running
  - Two terminals

Terminal 1 (responder):
  MULTIFLEET_ROLE=responder python 03_request_reply.py

Terminal 2 (requester):
  MULTIFLEET_ROLE=requester python 03_request_reply.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

try:
    import nats
except ImportError:
    sys.exit("pip install nats-py first")


NATS_URL = os.environ.get("NATS_URL", "nats://127.0.0.1:4222")
ROLE = os.environ.get("MULTIFLEET_ROLE", "requester")
SUBJECT = "fleet.demo.ping"


async def run_responder() -> None:
    nc = await nats.connect(NATS_URL, name="demo-responder")

    async def on_ping(msg):
        try:
            body = json.loads(msg.data.decode())
        except Exception:
            body = {}
        print(f"[responder] ping: {body}")
        reply = {"pong": True, "ts": time.time(), "echo": body}
        if msg.reply:
            await nc.publish(msg.reply, json.dumps(reply).encode())

    await nc.subscribe(SUBJECT, cb=on_ping)
    print(f"[responder] listening on {SUBJECT}. Ctrl-C to quit.")
    stop = asyncio.Event()
    try:
        await stop.wait()
    finally:
        await nc.drain()


async def run_requester() -> None:
    nc = await nats.connect(NATS_URL, name="demo-requester")
    probe = json.dumps({"from": "demo-requester", "ts": time.time()}).encode()
    try:
        reply = await nc.request(SUBJECT, probe, timeout=2.0)
        print(f"[requester] got: {json.loads(reply.data.decode())}")
    except asyncio.TimeoutError:
        print("[requester] no responder within 2s (start a responder first)")
    await nc.drain()


async def main() -> None:
    if ROLE == "responder":
        await run_responder()
    else:
        await run_requester()


if __name__ == "__main__":
    asyncio.run(main())
