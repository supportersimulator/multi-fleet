"""ContextDNA Chief Sync Adapter — pull memory before major work.

Queries ContextDNA memory (professor, query, evidence) and packages
the results into a ChiefMemoryContext that synthesis can consume.

This is the "memory-backed chief" — the holy-shit demo moment where
the chief's decisions visibly reference past learnings and failures.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("fleet-nerve.adapter.chief-sync")


@dataclass
class ChiefMemoryContext:
    """Memory context pulled from ContextDNA for chief synthesis."""

    professor_guidance: str = ""
    prior_failures: list[str] = field(default_factory=list)
    prior_successes: list[str] = field(default_factory=list)
    arch_decisions: list[str] = field(default_factory=list)
    evidence_entries: list[dict] = field(default_factory=list)
    query_count: int = 0
    pull_duration_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "professor_guidance": self.professor_guidance,
            "prior_failures": self.prior_failures,
            "prior_successes": self.prior_successes,
            "arch_decisions": self.arch_decisions,
            "evidence_count": len(self.evidence_entries),
            "query_count": self.query_count,
            "pull_duration_ms": round(self.pull_duration_ms, 1),
        }

    @property
    def has_content(self) -> bool:
        return bool(
            self.professor_guidance
            or self.prior_failures
            or self.prior_successes
            or self.arch_decisions
            or self.evidence_entries
        )

    def summary(self) -> str:
        parts = []
        if self.professor_guidance:
            parts.append(f"professor: {self.professor_guidance[:80]}")
        if self.prior_failures:
            parts.append(f"{len(self.prior_failures)} prior failure(s)")
        if self.prior_successes:
            parts.append(f"{len(self.prior_successes)} prior success(es)")
        if self.arch_decisions:
            parts.append(f"{len(self.arch_decisions)} arch decision(s)")
        if self.evidence_entries:
            parts.append(f"{len(self.evidence_entries)} evidence entries")
        return "; ".join(parts) if parts else "no memory context"


class ChiefSyncAdapter:
    """Pulls ContextDNA memory before chief synthesis.

    Usage:
        adapter = ChiefSyncAdapter()
        ctx = adapter.pull(task_summary="Fix auth token refresh",
                          files_touched=["src/auth.py"])
        # ctx.professor_guidance, ctx.prior_failures, etc.
    """

    def __init__(self):
        self._query_fn = None
        self._professor_fn = None
        self._brain = None
        self._init_memory()

    def _init_memory(self):
        """Try to import ContextDNA memory modules."""
        try:
            from memory.query import query_learnings
            self._query_fn = query_learnings
        except Exception:
            pass

        if self._query_fn is None:
            try:
                from memory.brain import Brain
                self._brain = Brain()
                self._query_fn = lambda q, **kw: self._brain.context(q)
            except Exception:
                pass

        try:
            from memory.professor import consult
            self._professor_fn = consult
        except Exception:
            pass

    @property
    def available(self) -> bool:
        return self._query_fn is not None

    def pull(
        self,
        task_summary: str,
        files_touched: list[str] = None,
        verdicts: dict = None,
    ) -> ChiefMemoryContext:
        """Pull memory context for a synthesis decision.

        Args:
            task_summary: What the task is about.
            files_touched: Files changed (for file-specific memory queries).
            verdicts: Machine verdicts (for extracting additional context).
        """
        t0 = time.monotonic()
        ctx = ChiefMemoryContext()

        if not self.available:
            logger.debug("ContextDNA memory not available — empty context")
            return ctx

        files_touched = files_touched or []

        # 1. Professor guidance
        if self._professor_fn and task_summary:
            try:
                guidance = self._professor_fn(task=task_summary)
                if guidance and isinstance(guidance, str) and len(guidance.strip()) > 10:
                    ctx.professor_guidance = guidance.strip()[:500]
                    ctx.query_count += 1
            except Exception as e:
                logger.debug("Professor consult failed: %s", e)

        # 2. General memory query
        if task_summary:
            try:
                results = self._query_fn(task_summary, limit=5)
                ctx.query_count += 1
                for r in results:
                    text = r if isinstance(r, str) else str(r)
                    text_lower = text.lower()
                    if any(kw in text_lower for kw in (
                        "fail", "broke", "regression", "revert", "bug", "crash", "error",
                    )):
                        ctx.prior_failures.append(text[:200])
                    elif any(kw in text_lower for kw in (
                        "success", "works", "fixed", "solved", "improved", "verified",
                    )):
                        ctx.prior_successes.append(text[:200])
                    elif any(kw in text_lower for kw in (
                        "decision", "architecture", "pattern", "design", "rationale",
                    )):
                        ctx.arch_decisions.append(text[:200])
            except Exception as e:
                logger.debug("Memory query failed: %s", e)

        # 3. File-specific failure check
        if files_touched:
            file_query = " ".join(files_touched[:5]) + " fail error regression"
            try:
                results = self._query_fn(file_query, limit=3)
                ctx.query_count += 1
                for r in results:
                    text = r if isinstance(r, str) else str(r)
                    if any(kw in text.lower() for kw in ("fail", "error", "regression", "bug")):
                        if text[:200] not in ctx.prior_failures:
                            ctx.prior_failures.append(text[:200])
            except Exception as e:
                logger.debug("File-specific query failed: %s", e)

        # 4. Evidence ledger entries
        try:
            from multifleet.evidence_ledger import EvidenceLedger
            ledger = EvidenceLedger()
            recent = ledger.query(event_type="merge_decision", limit=10)
            ctx.evidence_entries = recent
            ctx.query_count += 1
        except Exception as e:
            logger.debug("Evidence ledger query failed: %s", e)

        ctx.pull_duration_ms = (time.monotonic() - t0) * 1000
        logger.info("chief_sync pull: %s (%.0fms)", ctx.summary(), ctx.pull_duration_ms)
        return ctx


# Module-level singleton
_adapter: Optional[ChiefSyncAdapter] = None


def get_chief_sync() -> ChiefSyncAdapter:
    """Get or create the ChiefSyncAdapter singleton."""
    global _adapter
    if _adapter is None:
        _adapter = ChiefSyncAdapter()
    return _adapter
