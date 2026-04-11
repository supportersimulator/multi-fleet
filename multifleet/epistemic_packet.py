"""EpistemicPacket — the knowledge payload that travels with every fleet dispatch.

This is the bridge between the Probe Engine and fleet task dispatch.
Instead of sending raw task descriptions, the fleet sends evidence-bearing
packets that include probe findings, known risks, active invariants,
and required surgeon intensity.

"Multi-Fleet is not just message passing. It becomes evidence-bearing dispatch."

Usage:
    from multifleet.epistemic_packet import EpistemicPacketBuilder
    builder = EpistemicPacketBuilder(repo_root=".")
    packet = builder.build(
        task_intent="Fix auth token refresh",
        changed_files=["src/auth.py"],
    )
    # packet.to_dict() → ready for fleet dispatch
"""

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from multifleet.probe_engine import ProbeResult

logger = logging.getLogger("fleet-nerve.epistemic-packet")


@dataclass
class EpistemicPacket:
    """Knowledge payload sent with every meaningful fleet dispatch.

    This is NOT just a task description. It carries:
    - What probes found about the code being changed
    - What invariants are active
    - What risks are known from evidence
    - How intense the surgeon review should be
    - What the system remembers about this area
    """

    packet_id: str
    task_intent: str
    branch_name: Optional[str] = None
    changed_files: list[str] = field(default_factory=list)

    # Probe-derived intelligence
    probe_results: list[dict] = field(default_factory=list)
    blocking_findings: list[dict] = field(default_factory=list)
    required_surgeon_intensity: str = "light"  # light/standard/full

    # Evidence context
    active_invariants: list[str] = field(default_factory=list)
    known_risks: list[str] = field(default_factory=list)
    mirror_facts: list[str] = field(default_factory=list)
    stale_context_warnings: list[str] = field(default_factory=list)

    # Acceptance criteria
    required_probes: list[str] = field(default_factory=list)
    acceptance_policy: str = "standard"  # permissive/standard/strict

    # Metadata
    built_by: str = ""
    built_at: str = ""

    def to_dict(self) -> dict:
        return {
            "packet_id": self.packet_id,
            "task_intent": self.task_intent,
            "branch_name": self.branch_name,
            "changed_files": self.changed_files,
            "probe_results": self.probe_results,
            "blocking_findings": self.blocking_findings,
            "required_surgeon_intensity": self.required_surgeon_intensity,
            "active_invariants": self.active_invariants,
            "known_risks": self.known_risks,
            "mirror_facts": self.mirror_facts,
            "stale_context_warnings": self.stale_context_warnings,
            "required_probes": self.required_probes,
            "acceptance_policy": self.acceptance_policy,
            "built_by": self.built_by,
            "built_at": self.built_at,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_dict(cls, data: dict) -> "EpistemicPacket":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def summary(self) -> str:
        """One-line summary for logging."""
        return (
            f"Packet {self.packet_id[:8]}: "
            f"{len(self.probe_results)} probes, "
            f"{len(self.blocking_findings)} blocking, "
            f"surgeon={self.required_surgeon_intensity}, "
            f"{len(self.known_risks)} risks"
        )


class EpistemicPacketBuilder:
    """Builds EpistemicPackets by running probes and gathering context.

    This is the "ghost packet builder" — it assembles the smallest
    high-value context payload before any task dispatch.
    """

    def __init__(self, repo_root: str = ".", node_id: str = ""):
        self._repo_root = repo_root
        self._node_id = node_id or os.environ.get("MULTIFLEET_NODE_ID", "unknown")

    def build(
        self,
        task_intent: str,
        changed_files: list[str] = None,
        branch_name: str = None,
        run_probes: bool = True,
    ) -> EpistemicPacket:
        """Build an EpistemicPacket for a task dispatch.

        Args:
            task_intent: What the task is about.
            changed_files: Files that will be changed (for probe context).
            branch_name: Target branch.
            run_probes: Whether to run probes (False for speed-critical paths).
        """
        packet_id = str(uuid.uuid4())[:12]
        probe_results = []
        blocking = []
        intensity = "light"

        if run_probes:
            probe_results, blocking, intensity = self._run_probes(
                task_intent, changed_files or []
            )

        # Gather context from evidence ledger
        known_risks = self._gather_known_risks(changed_files or [])
        mirror_facts = self._gather_mirror_facts(changed_files or [])
        stale_warnings = self._check_staleness()
        active_invariants = self._get_active_invariants()

        # Determine acceptance policy based on risk
        policy = "strict" if blocking else "standard" if intensity != "light" else "permissive"

        packet = EpistemicPacket(
            packet_id=packet_id,
            task_intent=task_intent,
            branch_name=branch_name,
            changed_files=changed_files or [],
            probe_results=[r.to_dict() if isinstance(r, ProbeResult) else r for r in probe_results],
            blocking_findings=[f.to_dict() if hasattr(f, 'to_dict') else f for f in blocking],
            required_surgeon_intensity=intensity,
            active_invariants=active_invariants,
            known_risks=known_risks,
            mirror_facts=mirror_facts,
            stale_context_warnings=stale_warnings,
            required_probes=["mirror_drift", "hidden_dependency"],
            acceptance_policy=policy,
            built_by=self._node_id,
            built_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )

        logger.info("epistemic_packet_built %s", packet.summary())
        return packet

    def _run_probes(self, task_intent: str, changed_files: list[str]):
        """Run probes and extract results."""
        try:
            from multifleet.probes import create_engine
            engine = create_engine()
            results = engine.scan("all", {
                "repo_root": self._repo_root,
                "task_intent": task_intent,
                "changed_files": changed_files,
            })
            blocking = engine.blocking_findings()
            intensity = engine.determine_surgeon_intensity()
            return results, blocking, intensity
        except Exception as e:
            logger.warning("Probe scan failed: %s", e)
            return [], [], "standard"

    def _gather_known_risks(self, changed_files: list[str]) -> list[str]:
        """Check evidence ledger for known risks about changed files."""
        risks = []
        try:
            ledger_path = Path.home() / ".fleet-nerve" / "decisions.jsonl"
            if ledger_path.exists():
                lines = ledger_path.read_text().strip().splitlines()[-20:]
                for line in lines:
                    try:
                        entry = json.loads(line)
                        if entry.get("decision") == "needs_rebuttal":
                            risks.append(f"Prior rebuttal: {entry.get('reasoning', '')[:100]}")
                        if entry.get("dissent_count", 0) > 0:
                            risks.append(f"Prior dissent ({entry.get('dissent_count')}): task {entry.get('task_id', '?')[:8]}")
                    except json.JSONDecodeError:
                        continue
        except Exception:
            pass
        return risks[:5]  # Top 5 risks

    def _gather_mirror_facts(self, changed_files: list[str]) -> list[str]:
        """Extract relevant facts from architecture twin."""
        facts = []
        try:
            arch_map = Path(self._repo_root) / "architecture.map.json"
            if arch_map.exists():
                data = json.loads(arch_map.read_text())
                modules = data.get("modules", {})
                for f in changed_files[:5]:
                    mod_name = Path(f).stem
                    if mod_name in modules:
                        mod = modules[mod_name]
                        facts.append(f"{mod_name}: {mod.get('type', '?')} ({mod.get('imports_count', '?')} imports)")
        except Exception:
            pass
        return facts[:5]

    def _check_staleness(self) -> list[str]:
        """Check for stale context indicators."""
        warnings = []
        try:
            # Check if CLAUDE.md was recently modified
            claude_md = Path(self._repo_root) / "CLAUDE.md"
            if claude_md.exists():
                age_days = (time.time() - claude_md.stat().st_mtime) / 86400
                if age_days > 7:
                    warnings.append(f"CLAUDE.md is {int(age_days)} days old")
        except Exception:
            pass
        return warnings

    def _get_active_invariants(self) -> list[str]:
        """Get list of currently active invariant gates."""
        invariants = ["zero_silent_failures", "webhook_health_priority"]
        try:
            gates_path = Path(self._repo_root) / "multi-fleet" / "multifleet" / "invariance_gates.py"
            if gates_path.exists():
                invariants.append("fleet_invariance_gates")
        except Exception:
            pass
        return invariants
