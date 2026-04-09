"""Ghost Corroborate -- cross-references probe findings with memory.

Strengthens or weakens evidence confidence based on historical patterns.
If a finding recurs, confidence is boosted. If novel, it stays low until
corroborated. All corroboration decisions are recorded to the evidence ledger.
"""

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("fleet-nerve.ghost-corroborate")


@dataclass
class CorroborationItem:
    """A single corroboration assessment."""

    finding_title: str
    original_confidence: float
    adjusted_confidence: float
    prior_occurrences: int
    verdict: str  # "strengthened", "weakened", "novel", "unchanged"
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "finding_title": self.finding_title,
            "original_confidence": round(self.original_confidence, 3),
            "adjusted_confidence": round(self.adjusted_confidence, 3),
            "prior_occurrences": self.prior_occurrences,
            "verdict": self.verdict,
            "reason": self.reason,
        }


@dataclass
class CorroborateResult:
    """Result of corroboration analysis."""

    items: list[CorroborationItem] = field(default_factory=list)
    strengthened: int = 0
    weakened: int = 0
    novel: int = 0
    duration_ms: float = 0.0
    timestamp: str = ""

    def to_dict(self) -> dict:
        return {
            "items": [i.to_dict() for i in self.items],
            "strengthened": self.strengthened,
            "weakened": self.weakened,
            "novel": self.novel,
            "duration_ms": round(self.duration_ms, 1),
            "timestamp": self.timestamp,
        }

    def summary(self) -> str:
        parts = []
        if self.strengthened:
            parts.append(f"{self.strengthened} strengthened")
        if self.weakened:
            parts.append(f"{self.weakened} weakened")
        if self.novel:
            parts.append(f"{self.novel} novel")
        return " | ".join(parts) if parts else "no findings to corroborate"


class GhostCorroborate:
    """Cross-references probe findings with the evidence ledger.

    Philosophy: recurring findings gain credibility. Novel findings
    start with lower trust. Contradicted findings lose confidence.
    This prevents one-off probe noise from becoming assumed truth.
    """

    # Confidence adjustments
    RECURRENCE_BOOST = 0.1   # Per prior occurrence (capped)
    NOVELTY_PENALTY = 0.05   # Applied to truly novel findings
    MAX_BOOST = 0.25         # Cap total boost
    CONFIDENCE_FLOOR = 0.1   # Never go below this
    CONFIDENCE_CEILING = 0.95  # Never go above this

    def __init__(self, node_id: str = "unknown", evidence_db_path: Optional[str] = None):
        self._node_id = node_id
        self._evidence_db_path = evidence_db_path

    def corroborate(self, findings: list[dict]) -> CorroborateResult:
        """Cross-reference a list of probe findings with historical evidence.

        Args:
            findings: List of dicts with at minimum "title" and "confidence" keys.
                      Typically from ProbeResult.findings after to_dict().
        """
        t0 = time.monotonic()
        result = CorroborateResult(
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )

        # Load historical evidence
        history = self._load_history()

        for finding in findings:
            title = finding.get("title", "")
            if not title:
                continue

            original_conf = finding.get("confidence", 0.5)
            prior_count = self._count_prior_occurrences(title, history)

            item = self._assess(title, original_conf, prior_count)
            result.items.append(item)

            if item.verdict == "strengthened":
                result.strengthened += 1
            elif item.verdict == "weakened":
                result.weakened += 1
            elif item.verdict == "novel":
                result.novel += 1

        # Record corroboration to ledger
        if result.items:
            self._record_corroboration(result)

        # Emit event
        self._emit_event(result)

        result.duration_ms = (time.monotonic() - t0) * 1000
        logger.info("ghost_corroborate: %s (%.0fms)", result.summary(), result.duration_ms)
        return result

    def _assess(
        self, title: str, original_confidence: float, prior_occurrences: int
    ) -> CorroborationItem:
        """Assess a single finding against its history."""
        if prior_occurrences >= 3:
            boost = min(prior_occurrences * self.RECURRENCE_BOOST, self.MAX_BOOST)
            adjusted = min(original_confidence + boost, self.CONFIDENCE_CEILING)
            return CorroborationItem(
                finding_title=title,
                original_confidence=original_confidence,
                adjusted_confidence=adjusted,
                prior_occurrences=prior_occurrences,
                verdict="strengthened",
                reason=f"Seen {prior_occurrences} times before — pattern confirmed",
            )
        elif prior_occurrences >= 1:
            # Seen before but not enough to strengthen significantly
            boost = prior_occurrences * (self.RECURRENCE_BOOST / 2)
            adjusted = min(original_confidence + boost, self.CONFIDENCE_CEILING)
            return CorroborationItem(
                finding_title=title,
                original_confidence=original_confidence,
                adjusted_confidence=adjusted,
                prior_occurrences=prior_occurrences,
                verdict="unchanged",
                reason=f"Seen {prior_occurrences} time(s) — accumulating evidence",
            )
        else:
            # Novel finding — apply small penalty
            adjusted = max(
                original_confidence - self.NOVELTY_PENALTY,
                self.CONFIDENCE_FLOOR,
            )
            return CorroborationItem(
                finding_title=title,
                original_confidence=original_confidence,
                adjusted_confidence=adjusted,
                prior_occurrences=0,
                verdict="novel",
                reason="First occurrence — needs corroboration",
            )

    def _load_history(self) -> list[dict]:
        """Load recent evidence from the ledger."""
        try:
            from multifleet.evidence_ledger import EvidenceLedger

            kwargs = {}
            if self._evidence_db_path:
                kwargs["db_path"] = self._evidence_db_path
            ledger = EvidenceLedger(**kwargs)
            return ledger.query(event_type="probe_finding", limit=100)
        except Exception as e:
            logger.debug("Evidence ledger read failed: %s", e)
            return []

    def _count_prior_occurrences(self, title: str, history: list[dict]) -> int:
        """Count how many times a similar finding appeared in history."""
        # Normalize for comparison — match on key words
        title_lower = title.lower()
        key_words = set(title_lower.split())
        if len(key_words) < 2:
            return 0

        count = 0
        for entry in history:
            payload = entry.get("payload", {})
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except (json.JSONDecodeError, TypeError):
                    continue
            hist_title = payload.get("title", payload.get("subject", "")).lower()
            hist_words = set(hist_title.split())
            # Consider a match if >50% of key words overlap
            overlap = key_words & hist_words
            if len(overlap) >= max(2, len(key_words) // 2):
                count += 1

        return count

    def _record_corroboration(self, result: CorroborateResult) -> None:
        """Record corroboration decisions to the evidence ledger."""
        try:
            from multifleet.evidence_ledger import EvidenceLedger

            kwargs = {}
            if self._evidence_db_path:
                kwargs["db_path"] = self._evidence_db_path
            ledger = EvidenceLedger(**kwargs)
            ledger.record(
                event_type="ghost.corroboration",
                node_id=self._node_id,
                subject=result.summary(),
                payload={
                    "strengthened": result.strengthened,
                    "weakened": result.weakened,
                    "novel": result.novel,
                    "items": [i.to_dict() for i in result.items[:10]],
                },
            )
        except Exception as e:
            logger.debug("Corroboration ledger write failed: %s", e)

    def _emit_event(self, result: CorroborateResult) -> None:
        """Emit ghost corroborate event."""
        try:
            from multifleet.event_emitter import emit_typed
            from multifleet.event_schema import EventSource

            emit_typed(
                event_type="ghost.corroborate.completed",
                source=EventSource.GHOST,
                payload={
                    "strengthened": result.strengthened,
                    "weakened": result.weakened,
                    "novel": result.novel,
                    "total": len(result.items),
                },
            )
        except (ImportError, Exception):
            pass


def run_corroborate(findings: list[dict], **kwargs) -> dict:
    """Convenience function to run corroboration on findings."""
    ghost = GhostCorroborate(**kwargs)
    return ghost.corroborate(findings).to_dict()
