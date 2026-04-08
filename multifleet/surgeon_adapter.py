"""Surgeon Adapter — bridges multi-fleet to the 3-surgeon protocol.

Wraps memory/surgery_bridge.py to provide fleet-compatible surgeon state
queries. Replaces the trust-the-caller pattern where acceptance gate
accepted `surgeon_resolved=True` without ever calling 3-surgeons.

Zero Silent Failures: every bridge error is logged + counted.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger("fleet-nerve.surgeon-adapter")

# Counters for observability (Zero Silent Failures)
_COUNTERS: Dict[str, int] = {
    "queries": 0,
    "reviews": 0,
    "bridge_errors": 0,
    "degraded_fallbacks": 0,
}


class SurgeonAdapter:
    """Fleet-compatible interface to the 3-surgeon protocol.

    Wraps memory.surgery_bridge functions so acceptance_gate and
    node_overlay can query real surgeon state instead of trusting
    caller-provided booleans.
    """

    def __init__(self) -> None:
        self._bridge_module: Optional[Any] = None
        self._bridge_available: Optional[bool] = None
        self._last_consensus: Optional[Dict[str, Any]] = None
        self._last_consensus_ts: float = 0.0
        self._CONSENSUS_CACHE_TTL_S = 30.0
        self._try_import_bridge()

    def _try_import_bridge(self) -> None:
        """Try to import the real surgery bridge. Graceful degradation on failure."""
        try:
            import memory.surgery_bridge as sb
            self._bridge_module = sb
            self._bridge_available = True
            logger.info("Surgery bridge imported successfully")
        except ImportError as exc:
            self._bridge_module = None
            self._bridge_available = False
            _COUNTERS["degraded_fallbacks"] += 1
            logger.warning("Surgery bridge unavailable (import failed): %s", exc)

    def is_available(self) -> bool:
        """Check if 3-surgeons protocol is reachable.

        Tries bridge_status() to confirm the bridge can actually
        reach surgeons, not just that the module imported.
        """
        if not self._bridge_available or self._bridge_module is None:
            return False
        try:
            status = self._bridge_module.bridge_status()
            return status.get("status", "error") != "error"
        except Exception as exc:
            _COUNTERS["bridge_errors"] += 1
            logger.warning("Bridge status check failed: %s", exc)
            return False

    def check_consensus(self, claim: str) -> Dict[str, Any]:
        """Check if surgeons have reached consensus on a claim.

        Args:
            claim: The claim or task description to check consensus on.

        Returns:
            {resolved: bool, confidence: float, dissent: list[str],
             source: str, cached: bool}
        """
        _COUNTERS["queries"] += 1

        # Return cached result if fresh
        now = time.time()
        if (
            self._last_consensus is not None
            and (now - self._last_consensus_ts) < self._CONSENSUS_CACHE_TTL_S
            and self._last_consensus.get("_claim") == claim
        ):
            result = dict(self._last_consensus)
            result["cached"] = True
            result.pop("_claim", None)
            return result

        if not self._bridge_available or self._bridge_module is None:
            _COUNTERS["degraded_fallbacks"] += 1
            return {
                "resolved": False,
                "confidence": 0.0,
                "dissent": [],
                "source": "degraded",
                "cached": False,
                "error": "bridge unavailable",
            }

        try:
            raw = self._bridge_module.consensus(claim)
            resolved, confidence, dissent = self._parse_consensus(raw)
            result = {
                "resolved": resolved,
                "confidence": confidence,
                "dissent": dissent,
                "source": raw.get("path", "unknown"),
                "cached": False,
            }
            # Cache
            self._last_consensus = {**result, "_claim": claim}
            self._last_consensus_ts = now
            return result
        except Exception as exc:
            _COUNTERS["bridge_errors"] += 1
            logger.error("Consensus check failed: %s", exc)
            return {
                "resolved": False,
                "confidence": 0.0,
                "dissent": [],
                "source": "error",
                "cached": False,
                "error": str(exc),
            }

    def request_review(
        self, subject: str, evidence: Optional[list] = None
    ) -> Dict[str, Any]:
        """Request a 3-surgeon review of a decision.

        Uses cardio_review for cross-examination of the subject.
        Falls back to consensus if cardio_review is unavailable.

        Args:
            subject: What to review (description of decision/change).
            evidence: Optional list of evidence strings to include.

        Returns:
            {status: str, output: str|dict, path: str, review_type: str}
        """
        _COUNTERS["reviews"] += 1

        if not self._bridge_available or self._bridge_module is None:
            _COUNTERS["degraded_fallbacks"] += 1
            return {
                "status": "degraded",
                "output": "bridge unavailable — surgeon review skipped",
                "path": "none",
                "review_type": "none",
            }

        prompt = subject
        if evidence:
            prompt += "\n\nEvidence:\n" + "\n".join(f"- {e}" for e in evidence)

        try:
            result = self._bridge_module.cardio_review(prompt)
            result["review_type"] = "cardio_review"
            return result
        except Exception as exc:
            _COUNTERS["bridge_errors"] += 1
            logger.warning("cardio_review failed, trying consensus: %s", exc)
            try:
                result = self._bridge_module.consensus(prompt)
                result["review_type"] = "consensus_fallback"
                return result
            except Exception as exc2:
                _COUNTERS["bridge_errors"] += 1
                logger.error("All surgeon review paths failed: %s", exc2)
                return {
                    "status": "error",
                    "output": str(exc2),
                    "path": "none",
                    "review_type": "none",
                }

    def get_state(self) -> Dict[str, Any]:
        """Get current surgeon state for node overlay SurgeonStrip.

        Maps bridge status + last consensus into overlay-compatible dict.

        Returns:
            {phase: str, disagreement_score: float, correction_loops: int,
             abstention: bool}
        """
        base = {
            "phase": "idle",
            "disagreement_score": 0.0,
            "correction_loops": 0,
            "abstention": False,
        }

        if not self._bridge_available:
            base["phase"] = "unavailable"
            base["abstention"] = True
            return base

        # Derive phase from last consensus state
        if self._last_consensus is not None:
            c = self._last_consensus
            if c.get("resolved"):
                base["phase"] = "close"
            elif c.get("confidence", 0.0) > 0:
                base["phase"] = "review"
            base["disagreement_score"] = 1.0 - c.get("confidence", 0.5)
            base["correction_loops"] = len(c.get("dissent", []))
        else:
            base["phase"] = "idle"

        return base

    def get_counters(self) -> Dict[str, int]:
        """Return observability counters."""
        return dict(_COUNTERS)

    @staticmethod
    def _parse_consensus(raw: Dict[str, Any]) -> tuple:
        """Parse raw bridge consensus response into (resolved, confidence, dissent).

        The bridge returns {status, output, path}. Output may be a string
        or dict depending on the route taken. We extract what we can.
        """
        if raw.get("status") == "error":
            return False, 0.0, [raw.get("output", "unknown error")]

        output = raw.get("output", "")

        # If output is a dict (direct import path), extract structured fields
        if isinstance(output, dict):
            resolved = output.get("resolved", output.get("consensus", False))
            confidence = float(output.get("confidence", 0.5))
            dissent = output.get("dissent", output.get("dissenting_opinions", []))
            if isinstance(dissent, str):
                dissent = [dissent] if dissent else []
            return bool(resolved), confidence, list(dissent)

        # String output (subprocess path) — conservative parse
        output_lower = str(output).lower()
        if "consensus" in output_lower or "agreed" in output_lower:
            return True, 0.8, []
        if "dissent" in output_lower or "disagree" in output_lower:
            return False, 0.4, [str(output)[:200]]

        # Can't determine — treat as unresolved
        return False, 0.5, []
