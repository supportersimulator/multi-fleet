"""Tests for multifleet.replay — event replay/debug tool."""
import json
import time
from pathlib import Path

import pytest

from multifleet.replay import EventReplay

SAMPLE_EVENTS = [
    {"type": "fleet.peer.online", "timestamp": "2026-04-05T10:00:00Z",
     "payload": {"peer_id": "mac2"}, "metadata": {"node_id": "mac1"}},
    {"type": "fleet.message.sent", "timestamp": "2026-04-05T10:01:00Z",
     "payload": {"target": "mac2", "channel": "nats"},
     "metadata": {"node_id": "mac1"}},
    {"type": "fleet.health.degraded", "timestamp": "2026-04-05T10:02:00Z",
     "payload": {"peer_id": "mac2", "score": "degraded", "broken": ["nats"]},
     "metadata": {"node_id": "mac2"}},
    {"type": "fleet.lease.acquired", "timestamp": "2026-04-05T10:03:00Z",
     "payload": {"resource_id": "gpu", "holder": "mac1"},
     "metadata": {"node_id": "mac1"}},
]


@pytest.fixture
def log_file(tmp_path):
    p = tmp_path / "events.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in SAMPLE_EVENTS) + "\n")
    return str(p)


def test_tail(log_file):
    r = EventReplay(log_file)
    assert len(r.tail(2)) == 2
    assert r.tail(2)[0]["type"] == "fleet.health.degraded"
    assert len(r.tail(100)) == 4


def test_search(log_file):
    r = EventReplay(log_file)
    assert len(r.search("degraded")) == 1
    assert len(r.search("mac1")) == 3  # node_id=mac1 in 3 events
    assert len(r.search("nonexistent")) == 0


def test_filter_event_type(log_file):
    r = EventReplay(log_file)
    assert len(r.filter(event_type="peer.online")) == 1
    assert r.filter(event_type="peer.online")[0]["payload"]["peer_id"] == "mac2"


def test_filter_node(log_file):
    r = EventReplay(log_file)
    assert len(r.filter(node="mac2")) == 1
    assert r.filter(node="mac2")[0]["type"] == "fleet.health.degraded"


def test_filter_combined(log_file):
    r = EventReplay(log_file)
    assert len(r.filter(event_type="message.sent", node="mac1")) == 1


def test_timeline(log_file):
    r = EventReplay(log_file)
    # All events are old, so timeline with 1 min should be empty
    out = r.timeline(last_minutes=1)
    assert "No events" in out


def test_timeline_with_events(tmp_path):
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    p = tmp_path / "events.jsonl"
    e = {"type": "fleet.test", "timestamp": now,
         "payload": {"x": 1}, "metadata": {"node_id": "mac1"}}
    p.write_text(json.dumps(e) + "\n")
    r = EventReplay(str(p))
    out = r.timeline(last_minutes=5)
    assert "1 events" in out
    assert "fleet.test" in out


def test_empty_log(tmp_path):
    p = tmp_path / "events.jsonl"
    p.write_text("")
    r = EventReplay(str(p))
    assert r.tail() == []
    assert r.search("anything") == []


def test_missing_log(tmp_path):
    r = EventReplay(str(tmp_path / "nope.jsonl"))
    assert r.tail() == []
