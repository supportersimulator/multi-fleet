"""Built-in probes for the ContextDNA Probe Engine.

Registers all priority probes with the engine on import.
All probes unified from code_scanner + probe_engine into a single system.
"""

from multifleet.probe_engine import ProbeCategory, ProbeEngine, ProbeRegistry

from multifleet.probes.mirror_drift import probe_mirror_drift
from multifleet.probes.stale_context import probe_stale_context
from multifleet.probes.fleet_health import probe_fleet_health
from multifleet.probes.architectural_hotspots import probe_architectural_hotspots
from multifleet.probes.branch_intent import probe_branch_intent
from multifleet.probes.dependency_drift import probe_dependency_drift
from multifleet.probes.agent_readiness import probe_agent_readiness
from multifleet.probes.repeated_failure import probe_repeated_failure
from multifleet.probes.hidden_dependency import probe_hidden_dependency
from multifleet.probes.task_diff_mismatch import probe_task_diff_mismatch
from multifleet.probes.worktree_contamination import probe_worktree_contamination
from multifleet.probes.invariant_coverage import probe_invariant_coverage
from multifleet.probes.blast_radius import probe_blast_radius
from multifleet.probes.security_surface import probe_security_surface
from multifleet.probes.injection_prioritizer import probe_injection_prioritizer
from multifleet.probes.repeated_failure_cluster import probe_repeated_failure_cluster
from multifleet.probes.worktree_divergence import probe_worktree_divergence
from multifleet.probes.partition_lease_drift import probe_partition_lease_drift
from multifleet.probes.distributed import DistributedProbeRunner


def register_all(registry: ProbeRegistry) -> None:
    """Register all built-in probes with the given registry."""
    registry.register(
        probe_id="mirror_drift",
        fn=probe_mirror_drift,
        category=ProbeCategory.CONTEXT_QUALITY,
        description="Checks if docs reference files/paths that no longer exist, plus param drift and env drift.",
        triggers=["manual", "commit", "session_end"],
    )
    registry.register(
        probe_id="stale_context",
        fn=probe_stale_context,
        category=ProbeCategory.CONTEXT_QUALITY,
        description="Detects stale CLAUDE.md, deprecated comments, fleet messages, and stale plans.",
        triggers=["manual", "session_start", "session_end"],
    )
    registry.register(
        probe_id="fleet_health",
        fn=probe_fleet_health,
        category=ProbeCategory.RELIABILITY,
        description="Wraps FleetDoctor results as a probe — daemon, NATS, peers, keychain.",
        triggers=["manual", "session_start"],
    )
    registry.register(
        probe_id="architectural_hotspots",
        fn=probe_architectural_hotspots,
        category=ProbeCategory.STRUCTURE,
        description="Finds high-churn, high-blast-radius files (modification_frequency * importers).",
        triggers=["manual", "session_start", "commit"],
    )
    registry.register(
        probe_id="branch_intent",
        fn=probe_branch_intent,
        category=ProbeCategory.CHANGE,
        description="Detects scope creep and branch name vs actual changes mismatches.",
        triggers=["manual", "commit"],
    )
    registry.register(
        probe_id="dependency_drift",
        fn=probe_dependency_drift,
        category=ProbeCategory.CHANGE,
        description="Flags dependency file changes without co-occurring test modifications.",
        triggers=["manual", "commit", "session_end"],
    )
    registry.register(
        probe_id="agent_readiness",
        fn=probe_agent_readiness,
        category=ProbeCategory.AGENT_READINESS,
        description="Checks Python version, imports, daemon, Keychain keys, Superset DB.",
        triggers=["manual", "session_start"],
    )
    registry.register(
        probe_id="repeated_failure",
        fn=probe_repeated_failure,
        category=ProbeCategory.RELIABILITY,
        description="Detects files with 3+ failures in 24h — the 'stop circling' probe.",
        triggers=["manual", "session_start", "session_end"],
    )
    registry.register(
        probe_id="hidden_dependency",
        fn=probe_hidden_dependency,
        category=ProbeCategory.STRUCTURE,
        description="Scans Python imports for undeclared dependencies, subprocess binaries, and cross-module coupling.",
        triggers=["manual", "commit"],
    )
    registry.register(
        probe_id="task_diff_mismatch",
        fn=probe_task_diff_mismatch,
        category=ProbeCategory.CHANGE,
        description="Flags when git diff touches areas unrelated to the task intent.",
        triggers=["manual", "diff_ready"],
    )
    registry.register(
        probe_id="worktree_contamination",
        fn=probe_worktree_contamination,
        category=ProbeCategory.CHANGE,
        description="Detects uncommitted changes in inactive git worktrees.",
        triggers=["manual", "session_start"],
    )
    registry.register(
        probe_id="invariant_coverage",
        fn=probe_invariant_coverage,
        category=ProbeCategory.RELIABILITY,
        description="Checks that gains-gate, evidence ledger, and hooks are active.",
        triggers=["manual", "session_start", "session_end"],
    )
    registry.register(
        probe_id="blast_radius",
        fn=probe_blast_radius,
        category=ProbeCategory.STRUCTURE,
        description="Estimates change impact — importers, tests, churn, public API surface.",
        triggers=["manual", "commit"],
    )
    registry.register(
        probe_id="security_surface",
        fn=probe_security_surface,
        category=ProbeCategory.SECURITY,
        description="Detects hardcoded secrets, shell injection, .env in git, private keys.",
        triggers=["manual", "commit", "session_start"],
    )
    registry.register(
        probe_id="injection_prioritizer",
        fn=probe_injection_prioritizer,
        category=ProbeCategory.AGENT_READINESS,
        description="Ranks context snippets by relevance for next prompt injection.",
        triggers=["manual", "session_start"],
    )
    registry.register(
        probe_id="repeated_failure_cluster",
        fn=probe_repeated_failure_cluster,
        category=ProbeCategory.RELIABILITY,
        description="Detects recurring error patterns across sessions — systemic failures that need root-cause fixes.",
        triggers=["manual", "session_start", "session_end"],
    )
    registry.register(
        probe_id="worktree_divergence",
        fn=probe_worktree_divergence,
        category=ProbeCategory.CHANGE,
        description="Detects git worktrees that have diverged from their tracking branch.",
        triggers=["manual", "session_start"],
    )
    registry.register(
        probe_id="partition_lease_drift",
        fn=probe_partition_lease_drift,
        category=ProbeCategory.RELIABILITY,
        description="Detects provisional lease accumulation during network partitions — the split-brain early warning.",
        triggers=["manual", "session_start", "synthesis"],
    )


def create_engine(wire_evidence_bridge: bool = True) -> ProbeEngine:
    """Create a ProbeEngine with all built-in probes registered.

    Args:
        wire_evidence_bridge: If True, connect the EvidenceBridge
            so probe findings also feed into the evidence ledger and
            EventBus.
    """
    registry = ProbeRegistry()
    register_all(registry)

    engine = ProbeEngine(registry=registry)

    # Note: Evidence recording is handled by ProbeEngine._record_high_findings()
    # which records HIGH+ findings directly to the evidence ledger.
    # The old code_scanner.evidence_bridge has been removed.

    return engine
