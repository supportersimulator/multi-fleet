"""Invariance Rules Engine — machine-checkable protocol enforcement.

8 rules from the GhostScan vision that prevent protocol violations.
Each rule is a pure function: (state) -> InvarianceResult.

Rules:
  INV-001  No execution without BrainstormPacket or explicit bypass
  INV-002  No VisionContract locks without SurgeonDecisionPacket
  INV-003  Missing surgeon consensus on standard/full intensity -> mandatory fleet escalation
  INV-004  No COMPLETE without locked Vision + satisfied done definition + Snapshot + chief acceptance
  INV-005  DAO promotion cannot occur directly from Vision; must flow through Snapshot
  INV-006  Transport fallback to P3+ triggers auto-repair request
  INV-007  Overlay state must always reflect last known surgeon phase, execution phase, chief sync age
  INV-008  Channel health: broken channels auto-trigger repair
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("fleet-nerve.invariance-rules")


@dataclass
class InvarianceResult:
    """Outcome of a single invariance rule check."""
    rule_id: str
    passed: bool
    violation: str = ""
    severity: str = "warning"  # warning, error, critical
    hard_gate: bool = False  # True = blocking failure, not advisory

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "passed": self.passed,
            "violation": self.violation,
            "severity": self.severity,
            "hard_gate": self.hard_gate,
        }


class InvarianceEngine:
    """Run the 7 GhostScan invariance rules against protocol state.

    State dict keys (all optional — missing keys degrade gracefully):
      supervisor_phase: str          — current SupervisorPhase value
      brainstorm_id: str             — active BrainstormPacket ID
      bypass_reason: str             — explicit bypass justification
      vision_locked: bool            — whether VisionContract is locked
      surgeon_decision_id: str       — SurgeonDecisionPacket ID used for lock
      surgeon_consensus: bool        — whether surgeons reached consensus
      surgeon_intensity: str         — "light", "standard", "full"
      escalation_sent: bool          — whether chief escalation was sent
      done_criteria: list[str]       — VisionContract done criteria
      criteria_met: list[str]        — criteria satisfied so far
      has_snapshot: bool             — SnapshotRecord exists for this task
      chief_accepted: bool           — chief acceptance gate passed
      truth_ladder_path: list[str]   — promotion path through truth layers
      transport_channel: str         — channel used (e.g. "P1_nats", "P3_chief")
      repair_requested: bool         — whether auto-repair was triggered
      overlay_surgeon_phase: str     — overlay's surgeon strip phase
      overlay_execution_phase: str   — overlay's execution phase
      overlay_chief_sync_age_s: float — seconds since last chief sync
      actual_surgeon_phase: str      — real surgeon phase
      actual_execution_phase: str    — real execution phase
      actual_chief_sync_age_s: float — real chief sync age
    """

    # Rules promoted from advisory to blocking when hard_gate=True
    HARD_GATE_RULES = frozenset({"INV-001", "INV-004"})

    def __init__(self):
        self._rules = [
            ("INV-001", "no_execution_without_brainstorm", self._rule_001),
            ("INV-002", "no_vision_lock_without_surgeon", self._rule_002),
            ("INV-003", "missing_consensus_escalation", self._rule_003),
            ("INV-004", "no_complete_without_full_chain", self._rule_004),
            ("INV-005", "no_dao_skip_snapshot", self._rule_005),
            ("INV-006", "transport_fallback_repair", self._rule_006),
            ("INV-007", "overlay_reflects_state", self._rule_007),
        ]

    def check_all(self, state: dict, hard_gate: bool = False) -> list[InvarianceResult]:
        """Run all 7 invariance checks against current state.

        Args:
            state: Protocol state dict (see class docstring for keys).
            hard_gate: When True, INV-001 and INV-004 failures are marked as
                hard-gate blocking (critical + hard_gate=True) instead of
                advisory. Other rules are unaffected.
        """
        results = []
        for rule_id, name, fn in self._rules:
            try:
                result = fn(state)
            except Exception as e:
                logger.warning("invariance rule %s raised: %s", rule_id, e)
                result = InvarianceResult(
                    rule_id=rule_id, passed=False,
                    violation=f"Rule check error: {e}",
                    severity="error",
                )
            # Promote to hard-gate when enabled and rule supports it
            if hard_gate and not result.passed and rule_id in self.HARD_GATE_RULES:
                result.hard_gate = True
                result.severity = "critical"
            results.append(result)
        return results

    def check_one(self, rule_id: str, state: dict, hard_gate: bool = False) -> InvarianceResult:
        """Run a specific invariance check by rule ID.

        Args:
            rule_id: Rule identifier (e.g. "INV-001").
            state: Protocol state dict.
            hard_gate: When True and rule_id is in HARD_GATE_RULES, a failure
                is marked as hard-gate blocking.
        """
        for rid, name, fn in self._rules:
            if rid == rule_id:
                try:
                    result = fn(state)
                except Exception as e:
                    return InvarianceResult(
                        rule_id=rule_id, passed=False,
                        violation=f"Rule check error: {e}",
                        severity="error",
                    )
                if hard_gate and not result.passed and rid in self.HARD_GATE_RULES:
                    result.hard_gate = True
                    result.severity = "critical"
                return result
        return InvarianceResult(
            rule_id=rule_id, passed=False,
            violation=f"Unknown rule: {rule_id}",
            severity="error",
        )

    @property
    def rule_ids(self) -> list[str]:
        """List all rule IDs."""
        return [r[0] for r in self._rules]

    @property
    def rule_names(self) -> dict[str, str]:
        """Map rule IDs to human-readable names."""
        return {r[0]: r[1] for r in self._rules}

    # ── Rule implementations ──

    def _rule_001(self, state: dict) -> InvarianceResult:
        """INV-001: No execution without BrainstormPacket or explicit bypass.

        If supervisor is in EXECUTION phase (or beyond), there must be
        either a brainstorm_id or a bypass_reason present.
        """
        EXECUTION_PHASES = {
            "execution", "review", "fix_loop", "snapshot_record",
            "dao_candidate", "chief_acceptance", "complete",
        }
        phase = state.get("supervisor_phase", "")
        if phase not in EXECUTION_PHASES:
            return InvarianceResult("INV-001", True)

        brainstorm_id = state.get("brainstorm_id", "")
        bypass_reason = state.get("bypass_reason", "")

        if brainstorm_id or bypass_reason:
            return InvarianceResult("INV-001", True)

        return InvarianceResult(
            "INV-001", False,
            violation=f"In {phase} without BrainstormPacket or bypass_reason",
            severity="critical",
        )

    def _rule_002(self, state: dict) -> InvarianceResult:
        """INV-002: No VisionContract locks without SurgeonDecisionPacket.

        A VisionContract can only be locked if a surgeon_decision_id exists,
        proving that surgical adjudication occurred.
        """
        vision_locked = state.get("vision_locked", False)
        if not vision_locked:
            return InvarianceResult("INV-002", True)

        surgeon_decision_id = state.get("surgeon_decision_id", "")
        if surgeon_decision_id:
            return InvarianceResult("INV-002", True)

        return InvarianceResult(
            "INV-002", False,
            violation="VisionContract locked without SurgeonDecisionPacket",
            severity="critical",
        )

    def _rule_003(self, state: dict) -> InvarianceResult:
        """INV-003: Missing surgeon consensus on standard/full -> mandatory fleet escalation.

        When intensity is standard or full and consensus was not reached,
        an escalation to chief must have been sent.
        """
        intensity = state.get("surgeon_intensity", "light")
        consensus = state.get("surgeon_consensus", True)

        if intensity not in ("standard", "full"):
            return InvarianceResult("INV-003", True)
        if consensus:
            return InvarianceResult("INV-003", True)

        escalation_sent = state.get("escalation_sent", False)
        if escalation_sent:
            return InvarianceResult("INV-003", True)

        return InvarianceResult(
            "INV-003", False,
            violation=f"No consensus at {intensity} intensity without fleet escalation",
            severity="error",
        )

    def _rule_004(self, state: dict) -> InvarianceResult:
        """INV-004: No COMPLETE without locked Vision + satisfied done criteria + Snapshot + chief acceptance.

        All four conditions must be true before reaching COMPLETE phase.
        """
        phase = state.get("supervisor_phase", "")
        if phase != "complete":
            return InvarianceResult("INV-004", True)

        missing = []
        if not state.get("vision_locked", False):
            missing.append("locked VisionContract")
        done_criteria = state.get("done_criteria", [])
        criteria_met = state.get("criteria_met", [])
        if done_criteria and not all(c in criteria_met for c in done_criteria):
            unmet = [c for c in done_criteria if c not in criteria_met]
            missing.append(f"unsatisfied criteria: {unmet}")
        if not state.get("has_snapshot", False):
            missing.append("SnapshotRecord")
        if not state.get("chief_accepted", False):
            missing.append("chief acceptance")

        if not missing:
            return InvarianceResult("INV-004", True)

        return InvarianceResult(
            "INV-004", False,
            violation=f"COMPLETE without: {', '.join(missing)}",
            severity="critical",
        )

    def _rule_005(self, state: dict) -> InvarianceResult:
        """INV-005: DAO promotion cannot occur directly from Vision; must flow through Snapshot.

        The truth_ladder_path must include 'snapshot' before 'dao'.
        """
        path = state.get("truth_ladder_path", [])
        if not path:
            return InvarianceResult("INV-005", True)

        if "dao" not in path:
            return InvarianceResult("INV-005", True)

        # DAO is in the path — verify snapshot comes before it
        try:
            dao_idx = path.index("dao")
            snapshot_idx = path.index("snapshot") if "snapshot" in path else -1
        except ValueError:
            snapshot_idx = -1

        if snapshot_idx >= 0 and snapshot_idx < dao_idx:
            return InvarianceResult("INV-005", True)

        return InvarianceResult(
            "INV-005", False,
            violation=f"DAO promotion skipped Snapshot layer (path: {path})",
            severity="critical",
        )

    def _rule_006(self, state: dict) -> InvarianceResult:
        """INV-006: Transport fallback to P3+ triggers auto-repair request.

        If the transport channel used is P3 (chief relay) or worse,
        an auto-repair request must have been issued for the broken channels.
        """
        channel = state.get("transport_channel", "")
        if not channel:
            return InvarianceResult("INV-006", True)

        # P1=nats, P2=http are healthy channels. P3+=fallback territory.
        FALLBACK_PREFIXES = ("P3", "P4", "P5", "P6", "P7")
        is_fallback = any(channel.startswith(p) for p in FALLBACK_PREFIXES)
        if not is_fallback:
            return InvarianceResult("INV-006", True)

        repair_requested = state.get("repair_requested", False)
        if repair_requested:
            return InvarianceResult("INV-006", True)

        return InvarianceResult(
            "INV-006", False,
            violation=f"Transport fell back to {channel} without auto-repair request",
            severity="warning",
        )

    def _rule_007(self, state: dict) -> InvarianceResult:
        """INV-007: Overlay state must reflect last known surgeon phase, execution phase, chief sync age.

        The overlay strips must match the actual state within tolerance.
        """
        mismatches = []

        overlay_surgeon = state.get("overlay_surgeon_phase", "")
        actual_surgeon = state.get("actual_surgeon_phase", "")
        if overlay_surgeon and actual_surgeon and overlay_surgeon != actual_surgeon:
            mismatches.append(f"surgeon phase: overlay={overlay_surgeon} actual={actual_surgeon}")

        overlay_exec = state.get("overlay_execution_phase", "")
        actual_exec = state.get("actual_execution_phase", "")
        if overlay_exec and actual_exec and overlay_exec != actual_exec:
            mismatches.append(f"execution phase: overlay={overlay_exec} actual={actual_exec}")

        overlay_sync = state.get("overlay_chief_sync_age_s", None)
        actual_sync = state.get("actual_chief_sync_age_s", None)
        if overlay_sync is not None and actual_sync is not None:
            drift = abs(overlay_sync - actual_sync)
            if drift > 60:  # >60s drift is a mismatch
                mismatches.append(
                    f"chief sync age drift: {drift:.0f}s "
                    f"(overlay={overlay_sync:.0f}s actual={actual_sync:.0f}s)"
                )

        if not mismatches:
            return InvarianceResult("INV-007", True)

        return InvarianceResult(
            "INV-007", False,
            violation=f"Overlay state mismatch: {'; '.join(mismatches)}",
            severity="warning",
        )


class ChannelHealthInvariant:
    """INV-008: If any peer's channels drop below threshold, trigger repair.

    Checks channel health from daemon /health endpoint data.
    If channel score drops below half → violation with auto-repair action.
    Supports escalation levels L1 (notify) → L2 (guide) → L3 (assist) → L4 (remote fix).
    """

    # Track escalation level per peer: peer_id -> current level (1-4)
    _escalation_levels: dict[str, int] = {}
    # Track last repair attempt per peer: peer_id -> timestamp
    _last_repair_attempt: dict[str, float] = {}
    # Cooldown between escalation bumps
    ESCALATION_COOLDOWN_S = 300  # 5 min

    def __init__(self):
        self._escalation_levels = {}
        self._last_repair_attempt = {}

    def check(self, channel_health: dict) -> list[dict]:
        """Check all peer channel health. Returns list of violations.

        Args:
            channel_health: {peer_id: {"score": "5/7", "broken": [...]}} or
                            {peer_id: "5/7"} (string shorthand).
        """
        violations = []
        for peer, health in channel_health.items():
            if isinstance(health, dict):
                score_str = health.get("score", "0/7")
                broken = health.get("broken", [])
            else:
                score_str = str(health)
                broken = []

            try:
                parts = score_str.split("/")
                up = int(parts[0])
                total = int(parts[1])
            except (ValueError, IndexError):
                logger.debug("Could not parse score '%s' for peer %s", score_str, peer)
                continue

            if total == 0:
                continue

            threshold = total // 2
            if up < threshold:
                level = self._escalation_levels.get(peer, 1)
                severity = "critical" if up == 0 else "high"
                violations.append({
                    "peer": peer,
                    "score": score_str,
                    "up": up,
                    "total": total,
                    "broken": broken,
                    "severity": severity,
                    "action": "repair",
                    "escalation_level": level,
                })

        # INTERLOCK 3: Record channel health degradation to CorrigibilityLedger
        # When channels degrade, the system is making an implicit decision to
        # continue operating with reduced connectivity. This must be tracked
        # for corrigibility — surgeon input could change repair strategy.
        if violations:
            try:
                from .corrigibility_ledger import CorrigibilityLedger
                cl = CorrigibilityLedger()
                for v in violations:
                    cl.record_decision_point(
                        context=f"channel_health_degraded:{v['peer']}",
                        proposed_action=f"repair ({v['score']})",
                        surgeon_input=f"severity={v['severity']}, broken={v.get('broken', [])}",
                        final_action=f"escalation_L{v.get('escalation_level', 1)}",
                        changed=False,
                        source="channel_health_invariant",
                    )
            except Exception as exc:
                logger.debug("CorrigibilityLedger recording failed for channel health: %s", exc)

        return violations

    def auto_repair(self, violation: dict, health_monitor=None) -> dict:
        """Trigger repair for a channel violation.

        Uses health_monitor.auto_repair() if available, otherwise returns
        the repair intent for the caller to execute.

        Escalation: L1 notify → L2 guide → L3 assist → L4 remote fix.
        Each level tried after ESCALATION_COOLDOWN_S if previous level failed.
        """
        peer = violation["peer"]
        score = violation["score"]
        level = violation.get("escalation_level", 1)
        broken = violation.get("broken", [])
        now = time.time()

        # Check if we should escalate
        last_attempt = self._last_repair_attempt.get(peer, 0)
        if last_attempt > 0 and (now - last_attempt) > self.ESCALATION_COOLDOWN_S:
            # Previous attempt didn't resolve — escalate
            level = min(level + 1, 4)
            self._escalation_levels[peer] = level

        self._last_repair_attempt[peer] = now

        level_names = {1: "notify", 2: "guide", 3: "assist", 4: "remote_fix"}
        issue = (
            f"Channel degraded: {score} (L{level} {level_names.get(level, 'unknown')}), "
            f"broken: {broken}"
        )

        result = {
            "peer": peer,
            "issue": issue,
            "level": level,
            "level_name": level_names.get(level, "unknown"),
            "score": score,
            "broken": broken,
            "timestamp": now,
            "status": "pending",
        }

        # INTERLOCK 3: Surgeon corrigibility check before L4 remote_fix
        # L4 is the most invasive repair level — validate against surgeon
        # corrigibility gate before auto-executing
        if level >= 4:
            try:
                from .surgeon_adapter import SurgeonAdapter
                adapter = SurgeonAdapter()
                review = adapter.request_review(
                    subject=f"L4 remote_fix repair on peer '{peer}' — corrigibility check",
                    evidence=[
                        f"peer: {peer}",
                        f"score: {score}",
                        f"broken channels: {broken}",
                        f"escalation level: L{level}",
                    ],
                )
                if review.get("status") == "ok":
                    output = str(review.get("output", "")).lower()
                    if "vision lock" in output or "block" in output or "deny" in output:
                        result["status"] = "blocked_by_surgeon"
                        result["surgeon_reason"] = "Surgeon corrigibility gate blocked L4 repair — escalate to chief"
                        logger.warning(
                            "L4 repair on %s blocked by surgeon corrigibility gate", peer
                        )
                        return result
                    else:
                        result["surgeon_approved"] = True
                else:
                    result["surgeon_review_status"] = review.get("status", "unknown")
            except Exception as e:
                # Graceful degradation: if surgeon unavailable, proceed with repair
                logger.warning("Surgeon corrigibility check unavailable for L4 repair on %s: %s", peer, e)
                result["surgeon_check_error"] = str(e)

        if health_monitor is not None:
            try:
                repair_result = health_monitor.auto_repair(peer, issue)
                result["status"] = repair_result.get("status", "unknown")
                result["repair_detail"] = repair_result
            except Exception as e:
                result["status"] = "error"
                result["error"] = str(e)
                logger.warning("ChannelHealthInvariant repair failed for %s: %s", peer, e)
        else:
            result["status"] = "no_monitor"

        return result

    def clear_escalation(self, peer: str):
        """Reset escalation level for a peer (channel restored)."""
        self._escalation_levels.pop(peer, None)
        self._last_repair_attempt.pop(peer, None)

    def get_escalation_level(self, peer: str) -> int:
        """Get current escalation level for a peer (1-4, default 1)."""
        return self._escalation_levels.get(peer, 1)
