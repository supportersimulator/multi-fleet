"""Wisdom Consumer — closes the feedback loop.

Reads promoted wisdom (evidence entries with applied_to_wisdom status,
wisdom files in ~/.context-dna/fleet-wisdom/) and feeds proven patterns
back into synthesis decisions.

Pipeline position:
  claim → quarantine → trusted → flagged_for_review → applied_to_wisdom
  → **WisdomConsumer reads here** → feeds back into synthesis

Without this: the moat accumulates evidence but never opens it.
With this: every synthesis decision is informed by proven fleet wisdom.
"""

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("fleet-nerve.wisdom-consumer")

# Keywords that indicate a contradiction between wisdom and a decision
_CONTRADICTION_SIGNALS = {
    "reject": {"merge", "approved", "clean"},
    "merge": {"reject", "broke", "failed", "needs_rebuttal"},
    "needs_rebuttal": {"approved", "clean", "merge"},
    "cherry_pick": {"reject", "failed"},
}


class WisdomEntry:
    """A single wisdom entry from any source."""

    __slots__ = ("source", "event_type", "area", "subject", "content",
                 "timestamp", "node_id", "entry_id", "confidence")

    def __init__(self, *, source: str, event_type: str = "", area: str = "",
                 subject: str = "", content: str = "", timestamp: float = 0.0,
                 node_id: str = "", entry_id: str = "", confidence: float = 0.0):
        self.source = source
        self.event_type = event_type
        self.area = area
        self.subject = subject
        self.content = content
        self.timestamp = timestamp
        self.node_id = node_id
        self.entry_id = entry_id
        self.confidence = confidence

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "event_type": self.event_type,
            "area": self.area,
            "subject": self.subject,
            "content": self.content[:300],
            "timestamp": self.timestamp,
            "node_id": self.node_id,
            "entry_id": self.entry_id,
            "confidence": self.confidence,
        }

    def matches_keywords(self, keywords: list[str], min_hits: int = 1) -> bool:
        """Check if this entry matches task keywords."""
        if not keywords:
            return False
        searchable = f"{self.subject} {self.content} {self.area} {self.event_type}".lower()
        hits = sum(1 for kw in keywords if kw.lower() in searchable)
        return hits >= min_hits


class WisdomAccuracyTracker:
    """Tracks false-positive rates for wisdom contradiction flags.

    When wisdom flags a synthesis decision as contradictory but the decision
    turns out correct, that's a false positive. Entries with high false-positive
    rates get suppressed to stop noisy wisdom from degrading confidence.

    Storage: JSON sidecar at ~/.context-dna/fleet-wisdom/.accuracy.json
    Schema: {entry_id: {flagged: N, confirmed: N, false_positive: N}}
    """

    def __init__(self, wisdom_path: Optional[str] = None):
        base = wisdom_path or str(Path.home() / ".context-dna" / "fleet-wisdom")
        self._path = Path(base) / ".accuracy.json"
        self._data: Optional[dict[str, dict]] = None

    def _load(self) -> dict[str, dict]:
        if self._data is not None:
            return self._data
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text())
            except (json.JSONDecodeError, OSError):
                self._data = {}
        else:
            self._data = {}
        return self._data

    def _save(self):
        data = self._load()
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(data, indent=2))
        except OSError as e:
            logger.warning("Failed to save accuracy tracker: %s", e)

    def _ensure_entry(self, entry_id: str) -> dict:
        data = self._load()
        if entry_id not in data:
            data[entry_id] = {"flagged": 0, "confirmed": 0, "false_positive": 0}
        return data[entry_id]

    def record_flag(self, entry_id: str):
        """Record that a wisdom entry was flagged as contradicting a decision."""
        rec = self._ensure_entry(entry_id)
        rec["flagged"] += 1
        self._save()

    def record_confirmed(self, entry_id: str):
        """Record that the contradiction was real (decision failed)."""
        rec = self._ensure_entry(entry_id)
        rec["confirmed"] += 1
        self._save()

    def record_false_positive(self, entry_id: str):
        """Record that the contradiction was wrong (decision succeeded)."""
        rec = self._ensure_entry(entry_id)
        rec["false_positive"] += 1
        self._save()

    def resolve_outcome(self, entry_ids: list[str], decision_succeeded: bool):
        """Resolve outcome for a batch of flagged entries after decision result is known."""
        for eid in entry_ids:
            if decision_succeeded:
                self.record_false_positive(eid)
            else:
                self.record_confirmed(eid)

    def get_stats(self, entry_id: str) -> dict:
        """Return {flagged, confirmed, false_positive} for an entry."""
        data = self._load()
        return dict(data.get(entry_id, {"flagged": 0, "confirmed": 0, "false_positive": 0}))

    def false_positive_rate(self, entry_id: str) -> float:
        """Return false positive rate (0.0-1.0). Returns 0.0 if no data."""
        stats = self.get_stats(entry_id)
        total = stats["confirmed"] + stats["false_positive"]
        if total == 0:
            return 0.0
        return stats["false_positive"] / total

    def get_unreliable_entries(self, threshold: float = 0.5) -> list[str]:
        """Return entry IDs with false positive rate above threshold."""
        data = self._load()
        unreliable = []
        for entry_id, stats in data.items():
            total = stats.get("confirmed", 0) + stats.get("false_positive", 0)
            if total == 0:
                continue
            fpr = stats.get("false_positive", 0) / total
            if fpr > threshold:
                unreliable.append(entry_id)
        return unreliable

    def should_suppress(self, entry_id: str) -> bool:
        """Return True if entry has been a false positive >3 times."""
        stats = self.get_stats(entry_id)
        return stats["false_positive"] > 3


class WisdomConsumer:
    """Reads promoted wisdom and feeds it into synthesis decisions.

    Two sources:
      1. Evidence ledger entries with event_type containing 'wisdom' or 'promoted'
      2. Wisdom JSON files in ~/.context-dna/fleet-wisdom/ (written by MemoryBridge)
    """

    def __init__(
        self,
        evidence_ledger=None,
        wisdom_path: Optional[str] = None,
        accuracy_tracker: Optional[WisdomAccuracyTracker] = None,
    ):
        self._ledger = evidence_ledger
        self._wisdom_path = wisdom_path or str(
            Path.home() / ".context-dna" / "fleet-wisdom"
        )
        self._accuracy = accuracy_tracker or WisdomAccuracyTracker(self._wisdom_path)
        self._cache: Optional[list[WisdomEntry]] = None
        self._cache_ts: float = 0.0
        self._cache_ttl: float = 60.0  # seconds

    # ── Loading ──

    def _load_from_ledger(self) -> list[WisdomEntry]:
        """Load wisdom from evidence ledger (promoted/wisdom events)."""
        if not self._ledger:
            return []

        entries = []
        try:
            # Query entries with wisdom-related event types
            for event_type_filter in (None,):  # single broad query
                rows = self._ledger.query(limit=100)
                for row in rows:
                    et = row.get("event_type", "")
                    subject = row.get("subject", "")
                    # Match entries that are wisdom/promoted or high-confidence decisions
                    if not _is_wisdom_event(et, subject):
                        continue

                    payload = row.get("payload", {})
                    if isinstance(payload, str):
                        try:
                            payload = json.loads(payload)
                        except (json.JSONDecodeError, TypeError):
                            payload = {}

                    content = payload.get("details", "") or payload.get("reasoning", "")
                    if not content:
                        content = json.dumps(payload, default=str)

                    entries.append(WisdomEntry(
                        source="evidence_ledger",
                        event_type=et,
                        area=payload.get("area", ""),
                        subject=subject,
                        content=content,
                        timestamp=row.get("timestamp", 0),
                        node_id=row.get("node_id", ""),
                        entry_id=row.get("entry_id", ""),
                        confidence=float(payload.get("confidence", 0.0)),
                    ))
        except Exception as e:
            logger.warning("Failed to load wisdom from ledger: %s", e)

        return entries

    def _load_from_files(self) -> list[WisdomEntry]:
        """Load wisdom from JSON files in fleet-wisdom directory."""
        wisdom_dir = Path(self._wisdom_path)
        if not wisdom_dir.exists():
            return []

        entries = []
        for filepath in wisdom_dir.glob("*.json"):
            if filepath.name.startswith("."):
                continue  # skip tracker files
            try:
                data = json.loads(filepath.read_text())
                content_field = data.get("content", {})
                if isinstance(content_field, dict):
                    content_str = content_field.get("details", "") or json.dumps(content_field, default=str)
                else:
                    content_str = str(content_field)

                entries.append(WisdomEntry(
                    source="wisdom_file",
                    event_type=data.get("event_type", ""),
                    area=data.get("area", ""),
                    subject=data.get("subject", ""),
                    content=content_str,
                    timestamp=data.get("original_timestamp", data.get("promoted_at", 0)),
                    node_id=data.get("node_id", ""),
                    entry_id=data.get("entry_id", ""),
                    confidence=float(data.get("content", {}).get("confidence", 0.0)
                                     if isinstance(data.get("content"), dict) else 0.0),
                ))
            except Exception as e:
                logger.debug("Failed to read wisdom file %s: %s", filepath, e)

        return entries

    def _load_all(self) -> list[WisdomEntry]:
        """Load and deduplicate wisdom from all sources, with caching."""
        now = time.time()
        if self._cache is not None and (now - self._cache_ts) < self._cache_ttl:
            return self._cache

        ledger_entries = self._load_from_ledger()
        file_entries = self._load_from_files()

        # Deduplicate by entry_id
        seen_ids: set[str] = set()
        combined: list[WisdomEntry] = []
        for entry in ledger_entries + file_entries:
            if entry.entry_id and entry.entry_id in seen_ids:
                continue
            if entry.entry_id:
                seen_ids.add(entry.entry_id)
            combined.append(entry)

        # Sort by timestamp descending (newest first)
        combined.sort(key=lambda e: e.timestamp, reverse=True)

        self._cache = combined
        self._cache_ts = now
        return combined

    # ── Public API ──

    def get_wisdom_for_task(self, task_keywords: list[str]) -> list[dict]:
        """Return wisdom entries matching task keywords.

        Args:
            task_keywords: list of keywords describing the current task.

        Returns:
            List of matching wisdom dicts, newest first.
        """
        if not task_keywords:
            return []

        all_wisdom = self._load_all()
        # Require at least 1 keyword match for broad recall
        matches = [
            e.to_dict() for e in all_wisdom
            if e.matches_keywords(task_keywords, min_hits=1)
        ]
        return matches[:20]  # cap to avoid context bloat

    def feed_synthesis(self, synthesis_result: dict) -> dict:
        """Post-synthesis check: does the decision contradict proven wisdom?

        Args:
            synthesis_result: dict from SynthesisResult.to_chief_decision()
                or similar with keys: task_id, decision, reasoning, confidence.

        Returns:
            dict with:
              - warnings: list of contradiction warnings
              - supporting: list of supporting wisdom entries
              - wisdom_checked: int count of entries checked
        """
        task_id = synthesis_result.get("task_id", "")
        decision = synthesis_result.get("decision", "")
        reasoning = synthesis_result.get("reasoning", "")
        confidence = float(synthesis_result.get("confidence", 1.0))

        # Extract keywords from task context
        keywords = _extract_keywords(task_id, reasoning)
        if not keywords:
            return {"warnings": [], "supporting": [], "wisdom_checked": 0}

        relevant = [e for e in self._load_all() if e.matches_keywords(keywords)]

        warnings: list[dict] = []
        supporting: list[dict] = []
        flagged_entry_ids: list[str] = []

        for entry in relevant:
            # Skip entries that have proven unreliable
            if entry.entry_id and self._accuracy.should_suppress(entry.entry_id):
                logger.debug(
                    "Suppressing unreliable wisdom entry %s (>3 false positives)",
                    entry.entry_id,
                )
                continue

            contradiction = _detect_contradiction(decision, entry)
            if contradiction:
                warnings.append({
                    "type": "contradiction",
                    "message": contradiction,
                    "wisdom_entry": entry.to_dict(),
                })
                # Track the flag for later outcome resolution
                if entry.entry_id:
                    self._accuracy.record_flag(entry.entry_id)
                    flagged_entry_ids.append(entry.entry_id)
            else:
                supporting.append(entry.to_dict())

        if warnings:
            logger.warning(
                "Wisdom contradictions for task %s: %d warning(s)",
                task_id, len(warnings),
            )

        return {
            "warnings": warnings,
            "supporting": supporting[:10],
            "wisdom_checked": len(relevant),
            "flagged_entry_ids": flagged_entry_ids,
        }

    def invalidate_cache(self):
        """Force reload on next access."""
        self._cache = None
        self._cache_ts = 0.0


# ── Helpers ──

def _is_wisdom_event(event_type: str, subject: str) -> bool:
    """Check if an evidence entry represents wisdom/promoted content."""
    combined = f"{event_type} {subject}".lower()
    return any(signal in combined for signal in (
        "wisdom", "promoted", "consensus_reached", "applied_to_wisdom",
    ))


def _extract_keywords(task_id: str, reasoning: str) -> list[str]:
    """Extract searchable keywords from task context."""
    text = f"{task_id} {reasoning}"
    words = set(re.findall(r"[a-z]{3,}", text.lower()))
    noise = {"the", "and", "for", "this", "that", "with", "from", "into",
             "has", "was", "are", "not", "but", "all", "can", "will",
             "original", "reasoning", "scores", "winner"}
    return [w for w in words if w not in noise][:20]


def _detect_contradiction(decision: str, wisdom_entry: WisdomEntry) -> str:
    """Detect if a synthesis decision contradicts a wisdom entry.

    Returns a warning message if contradiction found, empty string otherwise.
    """
    signals = _CONTRADICTION_SIGNALS.get(decision, set())
    if not signals:
        return ""

    content_lower = f"{wisdom_entry.subject} {wisdom_entry.content}".lower()

    # Check if the wisdom entry's outcome contradicts the current decision
    matched_signals = [s for s in signals if s in content_lower]
    if not matched_signals:
        return ""

    # Build warning message
    return (
        f"Decision '{decision}' may contradict past wisdom: "
        f"'{wisdom_entry.subject[:80]}' (area: {wisdom_entry.area}, "
        f"event: {wisdom_entry.event_type}). "
        f"Contradiction signals: {', '.join(matched_signals)}"
    )
