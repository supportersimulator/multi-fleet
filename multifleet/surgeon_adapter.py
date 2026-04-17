"""Surgeon Adapter — live bridge between multi-fleet and the 3-surgeon protocol.

Wraps memory/surgery_bridge.py to provide fleet-compatible surgeon state
queries. Replaces the trust-the-caller pattern where acceptance gate
accepted `surgeon_resolved=True` without ever calling 3-surgeons.

Key methods:
  check_availability()   — per-surgeon liveness (atlas/cardiologist/neurologist)
  request_cross_exam()   — 3-surgeon cross-examination on a fleet decision
  validate_acceptance()  — validate task result through surgeon protocol
  check_consensus()      — confidence-weighted consensus on a claim
  request_review()       — cardio review with fallback to consensus
  get_state()            — overlay-compatible surgeon state dict
  get_surgeon_state()    — detailed per-surgeon status for dashboard

Zero Silent Failures: every bridge error is logged + counted.
"""

from __future__ import annotations

import logging
import os
import time
import urllib.request
from typing import Any, Dict, List, Optional

logger = logging.getLogger("fleet-nerve.surgeon-adapter")

# Counters for observability (Zero Silent Failures)
_COUNTERS: Dict[str, int] = {
    "queries": 0,
    "reviews": 0,
    "cross_exams": 0,
    "validations": 0,
    "availability_checks": 0,
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

    def check_availability(self) -> Dict[str, Any]:
        """Check which surgeons are available right now.

        Returns per-surgeon status:
          atlas:        Always True (we ARE Atlas)
          cardiologist: True if CONTEXT_DNA_OPENAI env var set (GPT-4.1-mini access)
          neurologist:  True if local LLM on port 5044 responds

        Returns:
            {atlas: {available, model, reason},
             cardiologist: {available, model, reason},
             neurologist: {available, model, reason},
             any_available: bool, all_available: bool}
        """
        _COUNTERS["availability_checks"] += 1

        result: Dict[str, Any] = {
            "atlas": {"available": True, "model": "claude-opus", "reason": "self"},
            "cardiologist": {"available": False, "model": "gpt-4.1-mini", "reason": "unchecked"},
            "neurologist": {"available": False, "model": "qwen3-4b", "reason": "unchecked"},
        }

        # Cardiologist: needs OpenAI API key
        openai_key = os.environ.get("CONTEXT_DNA_OPENAI") or os.environ.get("OPENAI_API_KEY")
        if openai_key:
            result["cardiologist"]["available"] = True
            result["cardiologist"]["reason"] = "api_key_present"
        else:
            result["cardiologist"]["reason"] = "no CONTEXT_DNA_OPENAI or OPENAI_API_KEY env var"

        # Neurologist: probe local LLM port
        try:
            req = urllib.request.Request("http://127.0.0.1:5044/v1/models", method="GET")
            resp = urllib.request.urlopen(req, timeout=2)
            if resp.status == 200:
                result["neurologist"]["available"] = True
                result["neurologist"]["reason"] = "port_5044_responding"
        except Exception as exc:
            result["neurologist"]["reason"] = f"port 5044 unreachable: {exc}"

        # Bridge status overlay
        if self._bridge_available and self._bridge_module:
            try:
                bridge_st = self._bridge_module.bridge_status()
                result["bridge"] = {
                    "available": True,
                    "mode": bridge_st.get("mode", "unknown"),
                    "effective_route": bridge_st.get("effective_route", "unknown"),
                }
            except Exception as exc:
                _COUNTERS["bridge_errors"] += 1
                result["bridge"] = {"available": False, "error": str(exc)}
        else:
            result["bridge"] = {"available": False, "error": "bridge not imported"}

        avail_list = [result[s]["available"] for s in ("atlas", "cardiologist", "neurologist")]
        result["any_available"] = any(avail_list)
        result["all_available"] = all(avail_list)

        return result

    def request_cross_exam(self, topic: str, evidence: Dict[str, Any]) -> Dict[str, Any]:
        """Request 3-surgeon cross-examination on a fleet decision.

        Routes through surgery_bridge.cardio_review (which triggers the
        cardiologist cross-exam). If cardio unavailable, falls back to
        consensus check. If all fail, returns degraded result.

        Records corrigibility data: the topic, consensus result, and
        whether surgeon input resulted in a different outcome than
        the caller's proposed action.

        Args:
            topic: Description of the decision to examine.
            evidence: Dict of evidence supporting/opposing the decision.

        Returns:
            {surgeons_consulted: list, consensus: bool, dissents: list,
             confidence: float, output: str, source: str}
        """
        _COUNTERS["cross_exams"] += 1

        if not self._bridge_available or self._bridge_module is None:
            _COUNTERS["degraded_fallbacks"] += 1
            return {
                "surgeons_consulted": ["atlas"],
                "consensus": False,
                "dissents": [],
                "confidence": 0.0,
                "output": "bridge unavailable — cross-exam degraded to atlas-only",
                "source": "degraded",
            }

        # Enrich with evidence ledger entries (evidence_ledger <-> surgeon bridge)
        try:
            from multifleet.evidence_ledger import EvidenceLedger
            ledger = EvidenceLedger()
            ledger_evidence = ledger.evidence_for_surgeon(topic, limit=5)
            if ledger_evidence:
                evidence = dict(evidence) if evidence else {}
                evidence["ledger_entries"] = "; ".join(
                    f"[{e['event_type']}] {e['subject']}" for e in ledger_evidence
                )
        except Exception as exc:
            logger.debug("Evidence ledger enrichment failed (non-blocking): %s", exc)

        # Build prompt with evidence
        prompt = topic
        if evidence:
            evidence_lines = []
            for k, v in evidence.items():
                evidence_lines.append(f"  {k}: {v}")
            prompt += "\n\nEvidence:\n" + "\n".join(evidence_lines)

        # Try cardio_review (cross-exam path)
        surgeons_consulted = ["atlas"]
        exam_result: Optional[Dict[str, Any]] = None
        try:
            review = self._bridge_module.cardio_review(prompt)
            if review.get("status") == "ok":
                surgeons_consulted.append("cardiologist")
                output = review.get("output", "")
                # Parse consensus from review output
                resolved, confidence, dissent = self._parse_consensus(review)
                exam_result = {
                    "surgeons_consulted": surgeons_consulted,
                    "consensus": resolved,
                    "dissents": dissent,
                    "confidence": confidence,
                    "output": output if isinstance(output, str) else str(output),
                    "source": review.get("path", "unknown"),
                }
        except Exception as exc:
            _COUNTERS["bridge_errors"] += 1
            logger.warning("Cross-exam cardio_review failed: %s", exc)

        # Fallback: neurologist challenge
        if exam_result is None:
            try:
                challenge = self._bridge_module.neurologist_challenge(prompt)
                if challenge.get("status") == "ok":
                    surgeons_consulted.append("neurologist")
                    exam_result = {
                        "surgeons_consulted": surgeons_consulted,
                        "consensus": False,  # challenge != consensus
                        "dissents": [str(challenge.get("output", ""))[:200]],
                        "confidence": 0.4,
                        "output": str(challenge.get("output", "")),
                        "source": challenge.get("path", "unknown"),
                    }
            except Exception as exc:
                _COUNTERS["bridge_errors"] += 1
                logger.warning("Cross-exam neurologist_challenge failed: %s", exc)

        # Fallback: consensus only
        if exam_result is None:
            try:
                cons = self._bridge_module.consensus(prompt)
                resolved, confidence, dissent = self._parse_consensus(cons)
                exam_result = {
                    "surgeons_consulted": surgeons_consulted,
                    "consensus": resolved,
                    "dissents": dissent,
                    "confidence": confidence,
                    "output": str(cons.get("output", "")),
                    "source": cons.get("path", "unknown"),
                }
            except Exception as exc:
                _COUNTERS["bridge_errors"] += 1
                logger.error("All cross-exam paths failed: %s", exc)
                exam_result = {
                    "surgeons_consulted": surgeons_consulted,
                    "consensus": False,
                    "dissents": [],
                    "confidence": 0.0,
                    "output": f"all paths failed: {exc}",
                    "source": "error",
                }

        # Record to corrigibility ledger (best-effort)
        self._record_corrigibility(
            context=topic,
            proposed="proceed",
            surgeon_input=str(exam_result.get("output", ""))[:500],
            consensus=exam_result.get("consensus", False),
            dissents=exam_result.get("dissents", []),
            source="cross_exam",
        )

        # INTERLOCK 6: Record surgeon cross-exam result to EvidenceLedger
        # Surgeon decisions become queryable evidence for future cross-exams.
        # This closes the loop: evidence feeds surgeons (enrichment above),
        # and surgeon results feed back into the evidence chain.
        try:
            from multifleet.evidence_ledger import EvidenceLedger
            ledger = EvidenceLedger()
            ledger.record(
                "surgeon_cross_exam",
                os.environ.get("MULTIFLEET_NODE_ID", "unknown"),
                topic[:200],
                {
                    "consensus": exam_result.get("consensus", False),
                    "confidence": exam_result.get("confidence", 0.0),
                    "surgeons_consulted": exam_result.get("surgeons_consulted", []),
                    "dissent_count": len(exam_result.get("dissents", [])),
                    "source": exam_result.get("source", "unknown"),
                },
            )
        except Exception as exc:
            logger.debug("Evidence ledger recording for cross-exam failed: %s", exc)

        # INTERLOCK 4: Propagate dissent to QuorumRegistry as strategic proposal
        if not exam_result.get("consensus", True) and exam_result.get("dissents"):
            self._propagate_dissent_to_quorum(topic, exam_result)

        return exam_result

    def validate_acceptance(self, task_id: str, result: Dict[str, Any]) -> Dict[str, Any]:
        """Validate a task result through surgeon protocol before acceptance.

        Replaces the broken caller-provided surgeon_resolved=True pattern.
        Actually calls surgeons instead of trusting caller.

        Args:
            task_id: Identifier for the task being validated.
            result: Task result dict (should contain 'claim' or 'description').

        Returns:
            {validated: bool, confidence: float, surgeon_resolved: bool,
             dissent_count: int, dissents: list, source: str, task_id: str}
        """
        _COUNTERS["validations"] += 1

        claim = result.get("claim", result.get("description", f"Task {task_id} acceptance"))
        evidence = result.get("evidence", {})

        # If bridge unavailable, return explicit degraded state (never fake True)
        if not self._bridge_available or self._bridge_module is None:
            _COUNTERS["degraded_fallbacks"] += 1
            return {
                "validated": False,
                "confidence": 0.0,
                "surgeon_resolved": False,
                "dissent_count": 0,
                "dissents": [],
                "source": "degraded",
                "task_id": task_id,
                "reason": "surgeon bridge unavailable — cannot validate",
            }

        # Route through cross-exam for validation
        cross_exam = self.request_cross_exam(
            topic=f"Acceptance validation for task '{task_id}': {claim}",
            evidence=evidence if isinstance(evidence, dict) else {"raw": str(evidence)},
        )

        validated = cross_exam.get("consensus", False)
        confidence = cross_exam.get("confidence", 0.0)
        dissents = cross_exam.get("dissents", [])

        # Caller assumed acceptance (proposed=accept). Did surgeons change that?
        surgeon_resolved = validated and confidence >= 0.6
        changed = not surgeon_resolved  # surgeon blocked what caller wanted to accept
        self._record_corrigibility(
            context=f"acceptance:{task_id}:{claim[:100]}",
            proposed="accept",
            surgeon_input=str(cross_exam.get("output", ""))[:500],
            consensus=validated,
            dissents=dissents,
            source="validate_acceptance",
        )

        return {
            "validated": validated,
            "confidence": confidence,
            "surgeon_resolved": surgeon_resolved,
            "dissent_count": len(dissents),
            "dissents": dissents,
            "source": cross_exam.get("source", "unknown"),
            "surgeons_consulted": cross_exam.get("surgeons_consulted", []),
            "task_id": task_id,
        }

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

    def _record_corrigibility(
        self,
        context: str,
        proposed: str,
        surgeon_input: str,
        consensus: bool,
        dissents: List,
        source: str,
    ) -> None:
        """Best-effort recording to corrigibility ledger."""
        try:
            from multifleet.corrigibility_ledger import CorrigibilityLedger
            ledger = CorrigibilityLedger()
            changed = not consensus or len(dissents) > 0
            final = "blocked" if not consensus else "approved"
            ledger.record_decision_point(
                context=context,
                proposed_action=proposed,
                surgeon_input=surgeon_input,
                final_action=final,
                changed=changed,
                source=source,
            )
            if dissents:
                ledger.record_disagreement(
                    topic=context,
                    surgeon_a_position=proposed,
                    surgeon_b_position="; ".join(str(d)[:200] for d in dissents),
                    resolution=final,
                )
        except Exception as exc:
            logger.debug("Corrigibility ledger recording failed: %s", exc)

    def _propagate_dissent_to_quorum(
        self, topic: str, exam_result: Dict[str, Any]
    ) -> None:
        """INTERLOCK 4: Propagate surgeon dissent to QuorumRegistry.

        When cross-exam produces a dissent, it becomes a strategic proposal
        in the quorum registry. Other fleet nodes see the dissent via quorum
        signals, enabling fleet-wide architectural concerns to propagate.
        """
        try:
            from multifleet.quorum.quorum_registry import QuorumRegistry
            node_id = os.environ.get("MULTIFLEET_NODE_ID", "unknown")
            registry = QuorumRegistry()
            registry.record_surgeon_dissent(topic, exam_result, node_id)
            logger.info(
                "INTERLOCK 4: Surgeon dissent propagated to quorum registry "
                "(topic=%s, confidence=%.2f)",
                topic[:80], exam_result.get("confidence", 0.0),
            )
        except Exception as exc:
            logger.debug("Dissent propagation to quorum failed: %s", exc)

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

    def get_surgeon_state(self) -> Dict[str, Any]:
        """Get current state of all 3 surgeons for overlay/dashboard.

        Combines availability check with last consensus state for a
        complete picture.

        Returns:
            {atlas: {status, model, last_action},
             cardiologist: {status, model, last_action},
             neurologist: {status, model, last_action},
             bridge: {available, mode},
             counters: {...}}
        """
        avail = self.check_availability()
        overlay_state = self.get_state()

        state: Dict[str, Any] = {}
        for surgeon in ("atlas", "cardiologist", "neurologist"):
            info = avail.get(surgeon, {})
            status = "online" if info.get("available") else "offline"
            state[surgeon] = {
                "status": status,
                "model": info.get("model", "unknown"),
                "last_action": overlay_state.get("phase", "idle"),
            }

        state["bridge"] = avail.get("bridge", {"available": False})
        state["counters"] = self.get_counters()
        state["overlay"] = overlay_state
        return state

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
