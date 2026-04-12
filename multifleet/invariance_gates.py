"""Fleet Invariance Gates — structured checks before major operations.

fleet_send_gate: validates packet before send (schema, auth, rate limit)
fleet_repair_gate: verifies repair action is warranted (not stale diagnostic)
fleet_merge_gate: pre-merge check (no lease conflicts, all nodes acked, gains pass)
"""

import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("fleet-nerve.gates")

# Valid repair actions (allowlist)
VALID_REPAIR_ACTIONS = frozenset({
    "restart_nats", "restart_daemon", "full_restart",
    "reconnect_nats", "restart_eventbus", "clear_dedup",
})

# Required envelope fields
REQUIRED_ENVELOPE_FIELDS = ("from_node", "to_node", "type", "id")

# Cooldown between repairs on the same target (seconds)
REPAIR_COOLDOWN_S = 300  # 5 minutes


@dataclass
class GateCheck:
    """Result of a single gate check."""
    name: str
    passed: bool
    message: str
    critical: bool = True

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "passed": self.passed,
            "message": self.message,
            "critical": self.critical,
        }


@dataclass
class GateResult:
    """Aggregate outcome of running a gate."""
    gate: str
    passed: bool
    checks: list = field(default_factory=list)  # list of GateCheck
    duration_ms: float = 0.0

    @property
    def summary(self) -> str:
        total = len(self.checks)
        passed_count = sum(1 for c in self.checks if c.passed)
        failed_critical = [c for c in self.checks if not c.passed and c.critical]
        if self.passed:
            failed_non = [c for c in self.checks if not c.passed and not c.critical]
            if failed_non:
                names = ", ".join(c.name for c in failed_non)
                return (f"{self.gate} PASSED ({passed_count}/{total}). "
                        f"Non-critical issues: {names}")
            return f"{self.gate} PASSED ({passed_count}/{total})"
        names = ", ".join(c.name for c in failed_critical)
        return f"{self.gate} FAILED: {names}"

    def to_dict(self) -> dict:
        return {
            "gate": self.gate,
            "passed": self.passed,
            "checks": [c.to_dict() if isinstance(c, GateCheck) else c
                        for c in self.checks],
            "duration_ms": round(self.duration_ms, 1),
            "summary": self.summary,
        }


def _record_audit(gate_name: str, result: "GateResult"):
    """Best-effort audit trail recording."""
    try:
        from .evidence_ledger import EvidenceLedger
        ledger = EvidenceLedger()
        ledger.record(
            f"gate_{gate_name}",
            os.environ.get("MULTIFLEET_NODE_ID", "unknown"),
            result.summary,
            result.to_dict(),
        )
    except Exception:
        pass  # Audit must never break gate operation


class FleetSendGate:
    """Validates a packet before sending.

    Quorum sensing integration (P. aeruginosa-inspired):
    - Strategic (Las-like) messages require corroboration metadata
    - Under critical stress (IQS-like), irreversible tactical actions are blocked
    - Stress signals always pass (they ARE the environmental override)
    """

    def check(self, packet: dict, registry=None, rate_limiter=None,
              stress_score: float = 0.0, quorum_registry=None) -> GateResult:
        """Run pre-send checks.

        1. Packet has required envelope fields (from_node, to_node, type, id)
        2. Payload validates against registered schema (if type registered)
        3. Rate limit not exceeded for this message type
        4. HMAC key available for signing
        5. Target node is known (in config or discovered)
        6. Quorum sensing: strategic corroboration + stress policy
        """
        t0 = time.monotonic()
        checks: list[GateCheck] = []

        # 1. Required envelope fields
        missing = [f for f in REQUIRED_ENVELOPE_FIELDS if not packet.get(f)]
        # Also accept "from"/"to" as aliases
        if "from_node" not in packet and "from" in packet:
            missing = [f for f in missing if f != "from_node"]
        if "to_node" not in packet and "to" in packet:
            missing = [f for f in missing if f != "to_node"]
        if missing:
            checks.append(GateCheck(
                "envelope_fields", False,
                f"Missing required fields: {', '.join(missing)}",
                critical=True,
            ))
        else:
            checks.append(GateCheck(
                "envelope_fields", True,
                "All required envelope fields present",
                critical=True,
            ))

        # 2. Schema validation (if registry provided and type is registered)
        msg_type = packet.get("type", "")
        if registry is not None:
            try:
                schema = registry.get_schema(msg_type) if hasattr(registry, "get_schema") else None
                if schema is not None:
                    errors = registry.validate(packet) if hasattr(registry, "validate") else None
                    if errors:
                        checks.append(GateCheck(
                            "schema_validation", False,
                            f"Schema validation failed for type '{msg_type}': {errors}",
                            critical=True,
                        ))
                    else:
                        checks.append(GateCheck(
                            "schema_validation", True,
                            f"Payload valid for type '{msg_type}'",
                            critical=True,
                        ))
                else:
                    checks.append(GateCheck(
                        "schema_validation", True,
                        f"No schema registered for type '{msg_type}' (skipped)",
                        critical=False,
                    ))
            except Exception as e:
                checks.append(GateCheck(
                    "schema_validation", False,
                    f"Schema validation error: {e}",
                    critical=False,
                ))
        else:
            checks.append(GateCheck(
                "schema_validation", True,
                "No registry provided (schema check skipped)",
                critical=False,
            ))

        # 3. Rate limit check
        if rate_limiter is not None:
            allowed, reason = rate_limiter.check("fleet_send")
            if not allowed:
                checks.append(GateCheck(
                    "rate_limit", False,
                    f"Rate limit exceeded: {reason}",
                    critical=True,
                ))
            else:
                checks.append(GateCheck(
                    "rate_limit", True,
                    "Rate limit OK",
                    critical=True,
                ))
        else:
            checks.append(GateCheck(
                "rate_limit", True,
                "No rate limiter provided (skipped)",
                critical=False,
            ))

        # 4. HMAC key availability
        try:
            from .security import FleetSecurity
            sec = FleetSecurity()
            if sec.has_hmac_key:
                checks.append(GateCheck(
                    "hmac_available", True,
                    "HMAC key available for signing",
                    critical=False,
                ))
            else:
                checks.append(GateCheck(
                    "hmac_available", False,
                    "No HMAC key — messages will be unsigned",
                    critical=False,
                ))
        except Exception:
            checks.append(GateCheck(
                "hmac_available", False,
                "Could not check HMAC key availability",
                critical=False,
            ))

        # 5. Target node known
        target = packet.get("to_node") or packet.get("to", "")
        if target and target != "all":
            try:
                from .config import load_config
                config = load_config()
                known_nodes = set(config.get("nodes", {}).keys())
                if target in known_nodes:
                    checks.append(GateCheck(
                        "target_known", True,
                        f"Target '{target}' is a known node",
                        critical=True,
                    ))
                else:
                    checks.append(GateCheck(
                        "target_known", False,
                        f"Target '{target}' not in known nodes: {known_nodes}",
                        critical=True,
                    ))
            except Exception:
                checks.append(GateCheck(
                    "target_known", True,
                    "Could not load config to verify target (skipped)",
                    critical=False,
                ))
        else:
            checks.append(GateCheck(
                "target_known", True,
                f"Target '{target}' accepted (broadcast or empty)",
                critical=False,
            ))

        # 6. Quorum sensing checks (Las/Rhl/PQS/IQS-inspired)
        signal_class = packet.get("signal_class")
        if signal_class is None:
            # Auto-classify if not present
            try:
                from .quorum.signal_class import classify_message
                signal_class = classify_message(
                    msg_type, packet.get("payload", {})
                ).value
            except Exception:
                signal_class = "tactical"

        # 6a. Strategic messages require corroboration (Las-like: quorum threshold)
        if signal_class == "strategic":
            meta = packet.get("meta", {})
            corroboration = meta.get("corroboration_count", 0)
            if corroboration < 1:
                # Check quorum registry if provided
                proposal_id = packet.get("id", "")
                has_quorum = False
                if quorum_registry is not None:
                    try:
                        has_quorum = quorum_registry.has_quorum(proposal_id)
                    except Exception:
                        pass
                if not has_quorum:
                    checks.append(GateCheck(
                        "strategic_corroboration", False,
                        "Strategic signal lacks corroboration — requires quorum "
                        "(2+ nodes + chief/post-verification) before fleet-wide authority",
                        critical=True,
                    ))
                else:
                    checks.append(GateCheck(
                        "strategic_corroboration", True,
                        "Strategic signal has quorum in registry",
                        critical=True,
                    ))
            else:
                checks.append(GateCheck(
                    "strategic_corroboration", True,
                    f"Strategic signal has {corroboration} corroboration(s)",
                    critical=True,
                ))
        else:
            checks.append(GateCheck(
                "strategic_corroboration", True,
                f"Signal class '{signal_class}' does not require corroboration",
                critical=False,
            ))

        # 6b. Stress policy enforcement (IQS-like: hierarchy shift under pressure)
        if stress_score > 0.0:
            try:
                from .quorum.stress_score import apply_stress_policy, get_stress_level
                policy = apply_stress_policy(stress_score)
                level = get_stress_level(stress_score)

                if signal_class == "tactical" and not policy["allow_irreversible_actions"]:
                    is_irreversible = packet.get("meta", {}).get("irreversible", False)
                    if is_irreversible:
                        checks.append(GateCheck(
                            "stress_policy", False,
                            f"Stress level '{level}' (score={stress_score:.2f}): "
                            f"irreversible tactical actions blocked",
                            critical=True,
                        ))
                    else:
                        checks.append(GateCheck(
                            "stress_policy", True,
                            f"Stress level '{level}': tactical action is reversible (allowed)",
                            critical=False,
                        ))
                elif signal_class == "stress":
                    # Stress signals always pass — they ARE the override
                    checks.append(GateCheck(
                        "stress_policy", True,
                        "Stress signal: IQS-like override passes immediately",
                        critical=False,
                    ))
                else:
                    checks.append(GateCheck(
                        "stress_policy", True,
                        f"Stress level '{level}' (score={stress_score:.2f}): "
                        f"signal class '{signal_class}' allowed",
                        critical=False,
                    ))
            except Exception:
                checks.append(GateCheck(
                    "stress_policy", True,
                    "Stress policy check error (skipped)",
                    critical=False,
                ))
        else:
            checks.append(GateCheck(
                "stress_policy", True,
                "No stress (normal operations)",
                critical=False,
            ))

        # Build result
        critical_failures = [c for c in checks if not c.passed and c.critical]
        passed = len(critical_failures) == 0
        duration_ms = (time.monotonic() - t0) * 1000

        result = GateResult(
            gate="FleetSendGate",
            passed=passed,
            checks=checks,
            duration_ms=duration_ms,
        )

        _record_audit("send", result)
        return result


class FleetRepairGate:
    """Verifies a repair action is warranted before executing."""

    # Track recent repairs: {target: last_repair_timestamp}
    _repair_history: dict[str, float] = {}
    # Track in-progress repairs: {target: start_timestamp}
    _repair_in_progress: dict[str, float] = {}

    def check(self, target: str, action: str,
              channel_health: dict = None,
              node_id: str = None,
              stress_score: float = 0.0) -> GateResult:
        """Run pre-repair checks.

        1. Target node is actually unhealthy (not stale diagnostic)
        2. No repair already in progress for this target
        3. Repair action is valid (in allowlist)
        4. L4 SSH repairs only from chief node
        5. Cooldown period respected (5min per target)
        6. Stress policy: under critical stress, prefer DAO guidance for repairs
        """
        t0 = time.monotonic()
        checks: list[GateCheck] = []
        now = time.time()

        # 1. Target actually unhealthy
        if channel_health is not None:
            target_health = channel_health.get(target, {})
            if isinstance(target_health, dict):
                score = target_health.get("score", 0)
                last_seen = target_health.get("last_seen", 0)
                age_s = now - last_seen if last_seen > 0 else float("inf")

                if age_s > 300:  # diagnostic older than 5 minutes
                    checks.append(GateCheck(
                        "target_unhealthy", False,
                        f"Stale diagnostic for '{target}': last seen {age_s:.0f}s ago",
                        critical=True,
                    ))
                elif score > 50:
                    checks.append(GateCheck(
                        "target_unhealthy", False,
                        f"Target '{target}' appears healthy (score={score})",
                        critical=True,
                    ))
                else:
                    checks.append(GateCheck(
                        "target_unhealthy", True,
                        f"Target '{target}' confirmed unhealthy (score={score})",
                        critical=True,
                    ))
            else:
                checks.append(GateCheck(
                    "target_unhealthy", True,
                    f"No structured health data for '{target}' (proceeding)",
                    critical=False,
                ))
        else:
            checks.append(GateCheck(
                "target_unhealthy", True,
                "No channel health data provided (skipped)",
                critical=False,
            ))

        # 2. No repair already in progress
        in_progress_ts = self._repair_in_progress.get(target, 0)
        if in_progress_ts > 0 and (now - in_progress_ts) < REPAIR_COOLDOWN_S:
            checks.append(GateCheck(
                "no_concurrent_repair", False,
                f"Repair already in progress for '{target}' "
                f"(started {now - in_progress_ts:.0f}s ago)",
                critical=True,
            ))
        else:
            # Clean up stale in-progress entries
            if target in self._repair_in_progress:
                del self._repair_in_progress[target]
            checks.append(GateCheck(
                "no_concurrent_repair", True,
                f"No active repair for '{target}'",
                critical=True,
            ))

        # 3. Valid repair action
        if action in VALID_REPAIR_ACTIONS:
            checks.append(GateCheck(
                "valid_action", True,
                f"Action '{action}' is in allowlist",
                critical=True,
            ))
        else:
            checks.append(GateCheck(
                "valid_action", False,
                f"Action '{action}' not in allowlist: {sorted(VALID_REPAIR_ACTIONS)}",
                critical=True,
            ))

        # 4. L4 SSH repairs only from chief
        is_ssh_repair = action in ("full_restart",)
        current_node = node_id or os.environ.get("MULTIFLEET_NODE_ID", "")
        if is_ssh_repair:
            try:
                from .config import load_config
                config = load_config()
                chief_node = config.get("chief", {}).get("nodeId", "mac1")
            except Exception:
                chief_node = "mac1"

            if current_node == chief_node:
                checks.append(GateCheck(
                    "l4_chief_only", True,
                    f"L4 SSH repair allowed (running on chief '{chief_node}')",
                    critical=True,
                ))
            else:
                checks.append(GateCheck(
                    "l4_chief_only", False,
                    f"L4 SSH repairs only allowed from chief node "
                    f"(current='{current_node}', chief='{chief_node}')",
                    critical=True,
                ))
        else:
            checks.append(GateCheck(
                "l4_chief_only", True,
                f"Action '{action}' does not require chief node",
                critical=False,
            ))

        # 5. Cooldown period
        last_repair = self._repair_history.get(target, 0)
        if last_repair > 0:
            elapsed = now - last_repair
            if elapsed < REPAIR_COOLDOWN_S:
                remaining = REPAIR_COOLDOWN_S - elapsed
                checks.append(GateCheck(
                    "cooldown", False,
                    f"Cooldown active for '{target}': {remaining:.0f}s remaining",
                    critical=True,
                ))
            else:
                checks.append(GateCheck(
                    "cooldown", True,
                    f"Cooldown OK for '{target}' (last repair {elapsed:.0f}s ago)",
                    critical=True,
                ))
        else:
            checks.append(GateCheck(
                "cooldown", True,
                f"No prior repairs recorded for '{target}'",
                critical=True,
            ))

        # 6. Stress policy for repairs (IQS-like: under critical stress,
        #    only allow non-destructive repairs unless chief approves)
        if stress_score >= 0.75:
            is_destructive = action in ("full_restart",)
            if is_destructive:
                checks.append(GateCheck(
                    "stress_repair_policy", False,
                    f"Critical stress (score={stress_score:.2f}): destructive repair "
                    f"'{action}' blocked — requires chief review first",
                    critical=True,
                ))
            else:
                checks.append(GateCheck(
                    "stress_repair_policy", True,
                    f"Critical stress: non-destructive repair '{action}' allowed",
                    critical=False,
                ))
        elif stress_score >= 0.50:
            checks.append(GateCheck(
                "stress_repair_policy", True,
                f"Degraded stress (score={stress_score:.2f}): repair allowed "
                f"with DAO guidance preferred",
                critical=False,
            ))
        else:
            checks.append(GateCheck(
                "stress_repair_policy", True,
                f"Stress level OK (score={stress_score:.2f})",
                critical=False,
            ))

        # Build result
        critical_failures = [c for c in checks if not c.passed and c.critical]
        passed = len(critical_failures) == 0
        duration_ms = (time.monotonic() - t0) * 1000

        result = GateResult(
            gate="FleetRepairGate",
            passed=passed,
            checks=checks,
            duration_ms=duration_ms,
        )

        # If passed, record the repair start
        if passed:
            self._repair_in_progress[target] = now
            self._repair_history[target] = now

        _record_audit("repair", result)
        return result

    def clear_repair(self, target: str):
        """Mark a repair as completed (clears in-progress lock)."""
        self._repair_in_progress.pop(target, None)


class FleetMergeGate:
    """Pre-merge verification before integrating branches."""

    def check(self, branch: str, lease_mgr=None, gains_gate=None,
              rebuttal_store=None, repo_path: str = None) -> GateResult:
        """Run pre-merge checks.

        1. No active leases on conflicting resources
        2. All participating nodes have acked (if rebuttal in progress)
        3. Gains gate passes (infrastructure healthy)
        4. No unresolved critical dissent
        5. Branch is up-to-date with main (no conflicts)
        """
        t0 = time.monotonic()
        checks: list[GateCheck] = []

        # 1. No active lease conflicts
        if lease_mgr is not None:
            try:
                active = lease_mgr.list_active() if hasattr(lease_mgr, "list_active") else []
                conflicts = [
                    l for l in active
                    if isinstance(l, dict) and (
                        l.get("resource", "").startswith(branch)
                        or l.get("resource", "") == "merge"
                    )
                ]
                if conflicts:
                    resources = [l.get("resource", "?") for l in conflicts]
                    checks.append(GateCheck(
                        "no_lease_conflicts", False,
                        f"Active leases on conflicting resources: {resources}",
                        critical=True,
                    ))
                else:
                    checks.append(GateCheck(
                        "no_lease_conflicts", True,
                        f"No conflicting leases ({len(active)} active total)",
                        critical=True,
                    ))
            except Exception as e:
                checks.append(GateCheck(
                    "no_lease_conflicts", True,
                    f"Lease check error (skipped): {e}",
                    critical=False,
                ))
        else:
            checks.append(GateCheck(
                "no_lease_conflicts", True,
                "No lease manager provided (skipped)",
                critical=False,
            ))

        # 2. All nodes acked (if rebuttal in progress)
        if rebuttal_store is not None:
            try:
                proposals = (rebuttal_store.list_proposals()
                             if hasattr(rebuttal_store, "list_proposals") else [])
                open_proposals = [
                    p for p in proposals
                    if isinstance(p, dict) and p.get("state") in ("open", "rebutted")
                    and p.get("branch", "") == branch
                ]
                if open_proposals:
                    checks.append(GateCheck(
                        "all_nodes_acked", False,
                        f"{len(open_proposals)} unresolved proposal(s) for branch '{branch}'",
                        critical=True,
                    ))
                else:
                    checks.append(GateCheck(
                        "all_nodes_acked", True,
                        "All proposals resolved or no proposals pending",
                        critical=True,
                    ))
            except Exception as e:
                checks.append(GateCheck(
                    "all_nodes_acked", True,
                    f"Rebuttal check error (skipped): {e}",
                    critical=False,
                ))
        else:
            checks.append(GateCheck(
                "all_nodes_acked", True,
                "No rebuttal store provided (skipped)",
                critical=False,
            ))

        # 3. Gains gate passes
        if gains_gate is not None:
            try:
                gate_result = gains_gate.run(quick=True)
                if gate_result.passed:
                    checks.append(GateCheck(
                        "gains_gate", True,
                        f"Gains gate passed ({gate_result.passed_count}/{gate_result.total})",
                        critical=True,
                    ))
                else:
                    checks.append(GateCheck(
                        "gains_gate", False,
                        f"Gains gate failed: {gate_result.recommendation}",
                        critical=True,
                    ))
            except Exception as e:
                checks.append(GateCheck(
                    "gains_gate", False,
                    f"Gains gate error: {e}",
                    critical=True,
                ))
        else:
            checks.append(GateCheck(
                "gains_gate", True,
                "No gains gate provided (skipped)",
                critical=False,
            ))

        # 4. No unresolved critical dissent
        if rebuttal_store is not None:
            try:
                proposals = (rebuttal_store.list_proposals()
                             if hasattr(rebuttal_store, "list_proposals") else [])
                critical_dissent = [
                    p for p in proposals
                    if isinstance(p, dict)
                    and p.get("severity") == "critical"
                    and p.get("state") != "resolved"
                ]
                if critical_dissent:
                    checks.append(GateCheck(
                        "no_critical_dissent", False,
                        f"{len(critical_dissent)} unresolved critical dissent(s)",
                        critical=True,
                    ))
                else:
                    checks.append(GateCheck(
                        "no_critical_dissent", True,
                        "No unresolved critical dissent",
                        critical=True,
                    ))
            except Exception as e:
                checks.append(GateCheck(
                    "no_critical_dissent", True,
                    f"Dissent check error (skipped): {e}",
                    critical=False,
                ))
        else:
            checks.append(GateCheck(
                "no_critical_dissent", True,
                "No rebuttal store provided (skipped)",
                critical=False,
            ))

        # 5. Branch up-to-date with main (no conflicts)
        from ._paths import get_git_root
        rpath = repo_path or str(get_git_root())
        try:
            # Check if branch exists and is mergeable
            result = subprocess.run(
                ["git", "merge-tree", "--write-tree", "main", branch],
                capture_output=True, text=True, timeout=10,
                cwd=rpath,
            )
            if result.returncode == 0:
                checks.append(GateCheck(
                    "branch_mergeable", True,
                    f"Branch '{branch}' merges cleanly with main",
                    critical=True,
                ))
            else:
                stderr = result.stderr.strip()[:200] if result.stderr else "unknown"
                checks.append(GateCheck(
                    "branch_mergeable", False,
                    f"Branch '{branch}' has conflicts with main: {stderr}",
                    critical=True,
                ))
        except FileNotFoundError:
            checks.append(GateCheck(
                "branch_mergeable", True,
                "git not available (skipped)",
                critical=False,
            ))
        except subprocess.TimeoutExpired:
            checks.append(GateCheck(
                "branch_mergeable", False,
                "git merge-tree timed out",
                critical=False,
            ))
        except Exception as e:
            checks.append(GateCheck(
                "branch_mergeable", True,
                f"Merge check error (skipped): {e}",
                critical=False,
            ))

        # Build result
        critical_failures = [c for c in checks if not c.passed and c.critical]
        passed = len(critical_failures) == 0
        duration_ms = (time.monotonic() - t0) * 1000

        result = GateResult(
            gate="FleetMergeGate",
            passed=passed,
            checks=checks,
            duration_ms=duration_ms,
        )

        _record_audit("merge", result)
        return result
