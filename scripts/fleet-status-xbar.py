#!/usr/bin/env python3
# <xbar.title>Fleet Status</xbar.title>
# <xbar.version>v1.0.0</xbar.version>
# <xbar.author>Context DNA Fleet</xbar.author>
# <xbar.desc>Multi-fleet daemon status — nodes, channels, tasks</xbar.desc>
# <xbar.dependencies>python3</xbar.dependencies>
# <xbar.var>string(DAEMON_URL="http://127.0.0.1:8855"): Fleet daemon URL</xbar.var>
# <xbar.var>string(REPO=""): Path to your multi-fleet repo (leave blank to auto-detect from MULTIFLEET_REPO env var)</xbar.var>
import json
import os
from urllib.request import urlopen, Request

DAEMON_URL = "http://127.0.0.1:8855"
TIMEOUT = 4
REPO = os.environ.get("MULTIFLEET_REPO") or os.path.expanduser("~/multi-fleet")

def fetch_health():
    try:
        with urlopen(Request(f"{DAEMON_URL}/health", headers={"Accept": "application/json"}), timeout=TIMEOUT) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None

def fmt_secs(s):
    try: s = int(s)
    except (TypeError, ValueError): return "?"
    return f"{s}s ago" if s < 60 else f"{s//60}m ago" if s < 3600 else f"{s//3600}h ago"

def fmt_uptime(s):
    try: s = int(s)
    except (TypeError, ValueError): return "?"
    h, rem = divmod(s, 3600); m, sec = divmod(rem, 60)
    return f"{h}h {m}m {sec}s"

def peer_dot(p):
    try: ls = int(p.get("lastSeen", 9999))
    except (TypeError, ValueError): return ":red_circle:"
    return ":white_check_mark:" if ls < 30 else ":large_orange_diamond:" if ls < 300 else ":red_circle:"

def main():
    data = fetch_health()
    if data is None:
        print("F:? | color=red")
        print("---")
        print("Fleet daemon unreachable | color=red")
        print(f"URL: {DAEMON_URL}/health | color=gray size=11")
        print("---")
        print(f"Start Daemon | bash=/usr/bin/env param1=bash param2=-c param3='cd {REPO} && python3 tools/fleet_nerve_nats.py serve' terminal=true")
        return

    node_id = data.get("nodeId", data.get("node_id", "?"))
    peers = data.get("peers", {})
    uptime = data.get("uptime_s")
    status = data.get("status", "unknown")
    active = data.get("activeSessions", 0)
    transport = data.get("transport", "?")
    stats = data.get("stats", {})
    git_info = data.get("git", {})

    online = sum(1 for p in peers.values() if isinstance(p, dict) and int(p.get("lastSeen", 9999)) < 30)
    total = 1 + online

    if status in ("ok", "healthy", "running") and online == len(peers):
        color = "green"
    elif online > 0:
        color = "orange"
    else:
        color = "red" if peers else "green"
    print(f"F:{total} | color={color}")

    print("---")
    print(f"Fleet Daemon — {node_id} | size=14")
    ok = "green" if status in ("ok", "healthy", "running") else "red"
    print(f"Status: {status} ({transport}) | color={ok}")
    if uptime is not None:
        print(f"Uptime: {fmt_uptime(uptime)} | color=gray size=11")
    print(f"Active sessions: {active}")
    if git_info:
        print(f"Branch: {git_info.get('branch', '?')} | color=gray size=11")

    print("---")
    if peers:
        for name, p in sorted(peers.items()):
            if not isinstance(p, dict): continue
            print(f"{peer_dot(p)} {name} — {p.get('sessions',0)} sessions, seen {fmt_secs(p.get('lastSeen'))} | size=13")
    else:
        print("No peers discovered | color=gray")

    if stats:
        print("---")
        print(f"Msgs: {stats.get('sent',0)} sent / {stats.get('received',0)} recv | size=12")
        print(f"Broadcasts: {stats.get('broadcasts',0)} | size=12")
        errs = stats.get("errors", 0)
        print(f"Errors: {errs} | color={'red' if errs else 'gray'} size=12")

    print("---")
    print(f"Run Fleet Check | bash=/usr/bin/env param1=bash param2=-c param3='cd {REPO} && bash scripts/fleet-check.sh' terminal=true")
    print(f"Test Fallback | bash=/usr/bin/env param1=bash param2=-c param3='cd {REPO} && bash scripts/fleet-test-fallback.sh all' terminal=true")
    print("---")
    print("Refresh | refresh=true")

if __name__ == "__main__":
    main()
