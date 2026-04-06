"""Fleet event replay tool — read, filter, and search JSONL event logs.

Usage:
    python3 -m multifleet.replay --tail 20
    python3 -m multifleet.replay --search "race"
    python3 -m multifleet.replay --filter --event-type peer.online --node mac1
    python3 -m multifleet.replay --timeline 60
"""
from __future__ import annotations

import argparse
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

_DEFAULT_LOG = Path.home() / ".fleet-nerve" / "events.jsonl"


class EventReplay:
    def __init__(self, log_path: str = None):
        self.path = Path(log_path) if log_path else _DEFAULT_LOG

    def _read_all(self) -> List[dict]:
        if not self.path.exists():
            return []
        events = []
        for line in self.path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except (json.JSONDecodeError, ValueError):
                continue
        return events

    def tail(self, n: int = 20) -> List[dict]:
        return self._read_all()[-n:]

    def search(self, pattern: str) -> List[dict]:
        regex = re.compile(pattern, re.IGNORECASE)
        return [e for e in self._read_all() if regex.search(json.dumps(e))]

    def filter(self, event_type: str = None, node: str = None,
               since: float = None) -> List[dict]:
        results = []
        for e in self._read_all():
            if event_type and not e.get("type", "").endswith(event_type):
                continue
            if node and e.get("metadata", {}).get("node_id") != node:
                continue
            if since:
                ts = e.get("timestamp", "")
                try:
                    et = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                    if et < since:
                        continue
                except (ValueError, TypeError):
                    continue
            results.append(e)
        return results

    def timeline(self, last_minutes: int = 60) -> str:
        cutoff = time.time() - last_minutes * 60
        events = self.filter(since=cutoff)
        if not events:
            return f"No events in the last {last_minutes} minutes."
        lines = []
        for e in events:
            ts = e.get("timestamp", "?")
            etype = e.get("type", "?")
            node = e.get("metadata", {}).get("node_id", "?")
            summary = e.get("payload", {})
            # Compact payload summary
            brief = ", ".join(f"{k}={v}" for k, v in list(summary.items())[:3])
            lines.append(f"  {ts}  [{node}] {etype}  {brief}")
        header = f"Timeline (last {last_minutes}min, {len(events)} events):"
        return "\n".join([header] + lines)


def main():
    p = argparse.ArgumentParser(description="Fleet event replay/debug tool")
    p.add_argument("--log", default=None, help="Path to events.jsonl")
    p.add_argument("--tail", type=int, metavar="N", help="Show last N events")
    p.add_argument("--search", type=str, metavar="PATTERN", help="Grep-like search")
    p.add_argument("--timeline", type=int, metavar="MINS", help="Human-readable timeline")
    p.add_argument("--filter", action="store_true", help="Filter mode")
    p.add_argument("--event-type", type=str, help="Filter by event type suffix")
    p.add_argument("--node", type=str, help="Filter by node_id")
    p.add_argument("--since-minutes", type=float, help="Filter events newer than N minutes")
    args = p.parse_args()

    r = EventReplay(log_path=args.log)

    if args.tail is not None:
        for e in r.tail(args.tail):
            print(json.dumps(e, indent=2))
    elif args.search:
        for e in r.search(args.search):
            print(json.dumps(e, indent=2))
    elif args.timeline is not None:
        print(r.timeline(args.timeline))
    elif args.filter:
        since = time.time() - args.since_minutes * 60 if args.since_minutes else None
        for e in r.filter(event_type=args.event_type, node=args.node, since=since):
            print(json.dumps(e, indent=2))
    else:
        p.print_help()


if __name__ == "__main__":
    main()
