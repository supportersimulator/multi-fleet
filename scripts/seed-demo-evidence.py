#!/usr/bin/env python3
"""Seed evidence ledger with realistic demo data for memory-influenced synthesis."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from multifleet.evidence_ledger import EvidenceLedger

ledger = EvidenceLedger()

demo_entries = [
    {
        "event_type": "merge_decision",
        "node_id": "mac1",
        "subject": "fix-auth-middleware: approved — evidence-backed refactor reduced attack surface",
        "payload": {"task_id": "fix-auth", "decision": "approved", "winner_branch": "mac2/fix-auth",
                    "confidence": 0.92, "reasoning": "Better error handling, cleaner middleware chain, all tests pass"},
    },
    {
        "event_type": "merge_decision",
        "node_id": "mac2",
        "subject": "optimize-database-queries: approved — 3x performance gain verified",
        "payload": {"task_id": "optimize-db", "decision": "approved", "winner_branch": "mac1/optimize-db",
                    "confidence": 0.88, "reasoning": "Query plan analysis showed 3x improvement with index changes"},
    },
    {
        "event_type": "merge_decision",
        "node_id": "mac3",
        "subject": "refactor-api-routes: rejected — regression in integration tests",
        "payload": {"task_id": "refactor-api", "decision": "rejected", "winner_branch": None,
                    "confidence": 0.75, "reasoning": "3 integration tests fail, auth middleware incompatible with new route structure"},
    },
    {
        "event_type": "merge_decision",
        "node_id": "mac1",
        "subject": "add-caching-layer: approved — Redis integration with graceful fallback",
        "payload": {"task_id": "add-cache", "decision": "approved", "winner_branch": "mac3/add-cache",
                    "confidence": 0.95, "reasoning": "Clean Redis integration, falls back to in-memory when Redis unavailable"},
    },
    {
        "event_type": "race_completed",
        "node_id": "mac2",
        "subject": "fix-websocket-reconnect: mac1 won — cleaner backoff strategy",
        "payload": {"race_id": "race-001", "winner": "mac1", "participants": ["mac1", "mac2", "mac3"],
                    "confidence": 0.85, "reasoning": "Exponential backoff with jitter, handles edge cases"},
    },
    {
        "event_type": "race_completed",
        "node_id": "mac1",
        "subject": "implement-rate-limiter: mac3 won — token bucket with Redis backing",
        "payload": {"race_id": "race-002", "winner": "mac3", "participants": ["mac1", "mac2", "mac3"],
                    "confidence": 0.91, "reasoning": "Token bucket algorithm, distributed via Redis, per-IP and per-user limits"},
    },
    {
        "event_type": "scan_finding",
        "node_id": "mac3",
        "subject": "stale-context: CLAUDE.md references deprecated fleet-keepalive.sh",
        "payload": {"probe": "stale_context", "severity": "medium",
                    "finding": "CLAUDE.md references fleet-keepalive.sh which was replaced by fleet-keepawake.sh"},
    },
    {
        "event_type": "probe_finding",
        "node_id": "mac2",
        "subject": "hidden-dependency: nats_client.py imports tools.fleet_historian without guard",
        "payload": {"probe": "hidden_dependency", "severity": "high",
                    "finding": "nats_client.py line 62 imports tools.fleet_historian_nats without try/except in standalone mode"},
    },
    {
        "event_type": "consensus_reached",
        "node_id": "mac1",
        "subject": "architecture: evidence ledger should use WAL mode for concurrent access",
        "payload": {"decision": "Use PRAGMA journal_mode=WAL in all SQLite databases",
                    "participants": ["mac1", "mac2", "mac3"], "confidence": 1.0},
    },
    {
        "event_type": "merge_decision",
        "node_id": "mac2",
        "subject": "improve-error-handling: approved — consistent error response format",
        "payload": {"task_id": "error-handling", "decision": "approved", "winner_branch": "mac2/error-handling",
                    "confidence": 0.87, "reasoning": "Standardized error responses, proper HTTP status codes, error tracking"},
    },
    {
        "event_type": "merge_decision",
        "node_id": "mac3",
        "subject": "add-logging-middleware: approved — structured logging with correlation IDs",
        "payload": {"task_id": "logging", "decision": "approved", "winner_branch": "mac1/structured-logging",
                    "confidence": 0.93, "reasoning": "Correlation IDs across services, structured JSON logs, log level filtering"},
    },
    {
        "event_type": "race_completed",
        "node_id": "mac1",
        "subject": "fix-memory-leak: mac2 won — proper cleanup in event listeners",
        "payload": {"race_id": "race-003", "winner": "mac2", "participants": ["mac1", "mac2"],
                    "confidence": 0.79, "reasoning": "WeakRef pattern for event listeners, explicit cleanup on disconnect"},
    },
]

seeded = 0
for entry in demo_entries:
    try:
        ledger.record(**entry)
        seeded += 1
        print(f"  OK: {entry['event_type']} — {entry['subject'][:60]}")
    except Exception as e:
        print(f"  SKIP: {entry['subject'][:40]} ({e})")

print(f"\nSeeded {seeded}/{len(demo_entries)} entries.")
stats = ledger.stats
print(f"Ledger: {stats}")
