"""Example 3: Send a fleet message via the 10-priority channel cascade.

Sends "hello" to target node. Cascade tries P0 Discord → P1 NATS → P2 HTTP → ... → P7 git.
First success wins. Requires fleet daemon running.

Run: python3 examples/03_send_fleet_message.py <target_node>
"""
from __future__ import annotations

import json
import sys
import urllib.request


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: 03_send_fleet_message.py <target_node>", file=sys.stderr)
        return 2

    target = sys.argv[1]
    body = json.dumps({
        "type": "context",
        "to": target,
        "payload": {"subject": "hello", "body": f"test from example at {__import__('time').time():.0f}"},
    }).encode()

    req = urllib.request.Request(
        "http://127.0.0.1:8855/message",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            resp = json.load(r)
            print(f"delivered via: {resp.get('channel', '?')}")
            print(f"latency_ms:    {resp.get('latency_ms', 0):.0f}")
            return 0
    except Exception as exc:
        print(f"send failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
