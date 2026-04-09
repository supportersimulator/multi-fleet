"""Bidirectional evidence sync between fleet and Context DNA memory.

Wire 2 of the fleet<->ContextDNA integration. Evidence flows both ways:

Direction A (fleet -> contextdna):
  Race verdicts, probe findings, synthesis decisions pushed to ContextDNA
  memory via MemoryBridge.promote_entry() and ProductBridge.fleet_to_memory().

Direction B (contextdna -> fleet):
  Memory learnings pulled back into evidence ledger as 'memory_learning'
  entries via ProductBridge.memory_to_fleet(). Deduped by content hash.

Sync watermarks tracked in ~/.fleet-nerve/sync_state.json to avoid
re-processing entries on each cycle.
"""

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("fleet-nerve.contextdna-bridge")

# Map fleet event_types to memory categories
_EVENT_TO_MEMORY = {
    "merge_decision": "architecture",
    "race_completed": "win",
    "race_started": "fleet-race",
    "race_node_completed": "fleet-race",
    "scan_finding": "fix",
    "acceptance_decision": "architecture",
    "consensus_reached": "consensus",
    "rebuttal_resolved": "design-decision",
    "gains_gate_result": "gains-gate",
    "channel_repair": "channel-repair",
    "lease_granted": "lease",
}


class ContextDNAEvidenceBridge:
    """Bridges fleet evidence ledger with Context DNA memory store.

    Direction A (fleet -> contextdna): Race verdicts, probe findings,
        synthesis decisions pushed to memory.
    Direction B (contextdna -> fleet): Memory learnings fed back into
        evidence ledger as 'memory_learning' entries.
    """

    def __init__(self, ledger=None, product_bridge=None,
                 state_path: Optional[str] = None):
        self._ledger = ledger
        self._product_bridge = product_bridge
        self._node_id = os.environ.get("MULTIFLEET_NODE_ID", "unknown")
        self._state_path = state_path or str(
            Path.home() / ".fleet-nerve" / "sync_state.json"
        )
        self._state = self._load_state()
        self._memory_bridge = None
        self._stats = {
            "pushed": 0,
            "pulled": 0,
            "push_errors": 0,
            "pull_errors": 0,
            "dedup_skipped": 0,
        }

        # Lazy-init memory bridge
        try:
            from multifleet.memory_bridge import MemoryBridge
            self._memory_bridge = MemoryBridge(evidence_ledger=self._ledger)
        except (ImportError, Exception) as e:
            logger.debug("MemoryBridge not available: %s", e)

        # Lazy-init product bridge
        if self._product_bridge is None:
            try:
                from multifleet.product_bridge import get_bridge
                self._product_bridge = get_bridge()
            except (ImportError, Exception) as e:
                logger.debug("ProductBridge not available: %s", e)

    # ── State Management ─────────────────────────────────────────────

    def _load_state(self) -> dict:
        """Load sync watermarks from disk."""
        try:
            if os.path.exists(self._state_path):
                return json.loads(Path(self._state_path).read_text())
        except Exception as e:
            logger.warning("Failed to load sync state: %s", e)
        return {
            "last_push_ts": 0.0,
            "last_pull_ts": 0.0,
            "pushed_hashes": [],
            "pulled_hashes": [],
        }

    def _save_state(self):
        """Persist sync watermarks to disk."""
        try:
            Path(self._state_path).parent.mkdir(parents=True, exist_ok=True)
            # Keep hash lists bounded (last 500)
            self._state["pushed_hashes"] = self._state.get("pushed_hashes", [])[-500:]
            self._state["pulled_hashes"] = self._state.get("pulled_hashes", [])[-500:]
            Path(self._state_path).write_text(
                json.dumps(self._state, indent=2)
            )
        except Exception as e:
            logger.warning("Failed to save sync state: %s", e)

    @staticmethod
    def _content_hash(content: str) -> str:
        """SHA-256 hash for dedup."""
        return hashlib.sha256(content.encode()).hexdigest()

    # ── Direction A: Fleet -> ContextDNA ──────────────────────────────

    def push_to_contextdna(self, entry_ids: Optional[list] = None,
                           since_ts: Optional[float] = None) -> dict:
        """Push unpromoted fleet evidence to Context DNA memory.

        Uses MemoryBridge.promote_entry() for each entry.
        Also tries HTTP POST via ProductBridge.fleet_to_memory().

        Args:
            entry_ids: Specific entry IDs to push (overrides since_ts).
            since_ts: Push entries newer than this timestamp.
                      Defaults to last_push_ts watermark.

        Returns:
            {pushed: int, errors: int, skipped: int}
        """
        if not self._ledger:
            return {"pushed": 0, "errors": 0, "skipped": 0,
                    "reason": "no ledger configured"}

        since = since_ts or self._state.get("last_push_ts", 0.0)
        # Limit to 10 entries per push to avoid blocking synthesis
        entries = self._ledger.query(since=since, limit=10)

        # Filter to specific IDs if provided
        if entry_ids:
            id_set = set(entry_ids)
            entries = [e for e in entries if e.get("entry_id") in id_set]

        pushed = 0
        errors = 0
        skipped = 0
        pushed_hashes = set(self._state.get("pushed_hashes", []))

        for entry in entries:
            entry_hash = entry.get("entry_hash", "")
            if entry_hash in pushed_hashes:
                skipped += 1
                self._stats["dedup_skipped"] += 1
                continue

            ok = self._push_single_entry(entry)
            if ok:
                pushed += 1
                self._stats["pushed"] += 1
                pushed_hashes.add(entry_hash)
            else:
                errors += 1
                self._stats["push_errors"] += 1

        # Update watermarks
        if entries:
            latest_ts = max(e.get("timestamp", 0) for e in entries)
            self._state["last_push_ts"] = latest_ts
        self._state["pushed_hashes"] = list(pushed_hashes)
        self._save_state()

        logger.info("push_to_contextdna: pushed=%d errors=%d skipped=%d",
                     pushed, errors, skipped)
        return {"pushed": pushed, "errors": errors, "skipped": skipped}

    def _push_single_entry(self, entry: dict) -> bool:
        """Push a single evidence entry to ContextDNA. Returns True on success."""
        entry_id = entry.get("entry_id", "")
        event_type = entry.get("event_type", "unknown")

        # Strategy 1: MemoryBridge.promote_entry() (local, reliable)
        if self._memory_bridge:
            try:
                promoted = self._memory_bridge.promote_entry(entry)
                if promoted:
                    logger.debug("Pushed %s via MemoryBridge", entry_id[:8])
                    return True
            except Exception as e:
                logger.debug("MemoryBridge push failed for %s: %s",
                             entry_id[:8], e)

        # Strategy 2: ProductBridge.fleet_to_memory() (HTTP, fallback)
        if self._product_bridge:
            try:
                memory_type = _EVENT_TO_MEMORY.get(event_type, "fleet")
                payload = entry.get("payload", {})
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except (json.JSONDecodeError, TypeError):
                        payload = {}

                result = self._product_bridge.fleet_to_memory({
                    "event_type": event_type,
                    "node_id": entry.get("node_id", self._node_id),
                    "evidence": payload,
                    "tags": [memory_type, "fleet-evidence"],
                    "entry_hash": entry.get("entry_hash", ""),
                })
                if result.get("stored") or result.get("entry_id"):
                    logger.debug("Pushed %s via ProductBridge", entry_id[:8])
                    return True
            except Exception as e:
                logger.debug("ProductBridge push failed for %s: %s",
                             entry_id[:8], e)

        return False

    # ── Direction B: ContextDNA -> Fleet ──────────────────────────────

    def pull_from_contextdna(self, keywords: Optional[list] = None,
                             since_ts: Optional[float] = None) -> dict:
        """Pull recent Context DNA learnings into fleet evidence ledger.

        Queries memory via ProductBridge.memory_to_fleet().
        Records as evidence_ledger entries with event_type='memory_learning'.
        Dedup by content hash.

        Args:
            keywords: Search terms for memory query.
            since_ts: Pull learnings newer than this timestamp.
                      Defaults to last_pull_ts watermark.

        Returns:
            {pulled: int, errors: int, skipped: int}
        """
        if not self._ledger:
            return {"pulled": 0, "errors": 0, "skipped": 0,
                    "reason": "no ledger configured"}

        if not self._product_bridge:
            return {"pulled": 0, "errors": 0, "skipped": 0,
                    "reason": "no product bridge configured"}

        query_terms = keywords or ["fleet", "race", "synthesis", "evidence"]
        pulled = 0
        errors = 0
        skipped = 0
        pulled_hashes = set(self._state.get("pulled_hashes", []))

        for term in query_terms:
            try:
                result = self._product_bridge.memory_to_fleet(term)
                results = result.get("results", [])

                for item in results:
                    content_str = json.dumps(item, sort_keys=True, default=str)
                    content_hash = self._content_hash(content_str)

                    if content_hash in pulled_hashes:
                        skipped += 1
                        self._stats["dedup_skipped"] += 1
                        continue

                    # Check timestamp filter
                    item_ts = item.get("timestamp", item.get("original_timestamp", 0))
                    since = since_ts or self._state.get("last_pull_ts", 0.0)
                    if since and item_ts and item_ts < since:
                        skipped += 1
                        continue

                    # Record as memory_learning in evidence ledger
                    try:
                        self._ledger.record(
                            event_type="memory_learning",
                            node_id=self._node_id,
                            subject=f"[contextdna] {item.get('subject', item.get('event_type', term))}",
                            payload={
                                "source": "contextdna_pull",
                                "query_term": term,
                                "content_hash": content_hash,
                                "original": item,
                            },
                        )
                        pulled += 1
                        self._stats["pulled"] += 1
                        pulled_hashes.add(content_hash)
                    except Exception as e:
                        logger.warning("Failed to record pulled learning: %s", e)
                        errors += 1
                        self._stats["pull_errors"] += 1

            except Exception as e:
                logger.debug("memory_to_fleet query failed for '%s': %s", term, e)
                errors += 1

        # Update watermarks
        self._state["last_pull_ts"] = time.time()
        self._state["pulled_hashes"] = list(pulled_hashes)
        self._save_state()

        logger.info("pull_from_contextdna: pulled=%d errors=%d skipped=%d",
                     pulled, errors, skipped)
        return {"pulled": pulled, "errors": errors, "skipped": skipped}

    # ── Full Sync ────────────────────────────────────────────────────

    def sync_cycle(self) -> dict:
        """Run full bidirectional sync. Called periodically or on-demand.

        Returns:
            {pushed: {...}, pulled: {...}, elapsed_s: float}
        """
        t0 = time.time()
        push_result = self.push_to_contextdna()
        pull_result = self.pull_from_contextdna()
        elapsed = time.time() - t0

        result = {
            "pushed": push_result,
            "pulled": pull_result,
            "elapsed_s": round(elapsed, 3),
            "node_id": self._node_id,
            "timestamp": time.time(),
        }

        logger.info(
            "sync_cycle: pushed=%d pulled=%d in %.1fs",
            push_result.get("pushed", 0),
            pull_result.get("pulled", 0),
            elapsed,
        )
        return result

    # ── Event Hooks ──────────────────────────────────────────────────

    def on_race_completed(self, task_id: str, result: dict):
        """Hook: called after fleet_race._finalize() or synthesis.

        Pushes the race completion evidence to ContextDNA immediately
        rather than waiting for the next sync cycle.
        """
        if not self._ledger:
            return

        # Find the race_completed entry in recent evidence
        try:
            recent = self._ledger.query(event_type="race_completed", limit=5)
            race_entries = [
                e for e in recent
                if (e.get("payload", {}) or {}).get("race_id") == task_id
                or task_id in (e.get("subject") or "")
            ]
            if race_entries:
                for entry in race_entries:
                    self._push_single_entry(entry)
                    logger.info("on_race_completed: pushed evidence for %s",
                                task_id)
        except Exception as e:
            logger.warning("on_race_completed hook failed: %s", e)

    def on_probe_finding(self, finding: dict):
        """Hook: called after probe records a finding.

        Records the probe finding and pushes to ContextDNA immediately.
        """
        if not self._ledger:
            return

        try:
            entry = self._ledger.record(
                event_type="scan_finding",
                node_id=self._node_id,
                subject=f"[probe] {finding.get('title', finding.get('probe', 'unknown'))}",
                payload={
                    "source": "probe_hook",
                    "finding": finding,
                    "severity": finding.get("severity", "medium"),
                },
            )
            self._push_single_entry(entry)
            logger.info("on_probe_finding: recorded and pushed %s",
                        entry.get("entry_id", "?")[:8])
        except Exception as e:
            logger.warning("on_probe_finding hook failed: %s", e)

    # ── Stats ────────────────────────────────────────────────────────

    @property
    def stats(self) -> dict:
        """Return bridge statistics."""
        return {
            **self._stats,
            "last_push_ts": self._state.get("last_push_ts", 0.0),
            "last_pull_ts": self._state.get("last_pull_ts", 0.0),
            "pushed_hashes_count": len(self._state.get("pushed_hashes", [])),
            "pulled_hashes_count": len(self._state.get("pulled_hashes", [])),
            "memory_bridge_available": self._memory_bridge is not None,
            "product_bridge_available": self._product_bridge is not None,
        }
