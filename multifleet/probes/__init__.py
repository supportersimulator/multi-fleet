"""Built-in probes for the ContextDNA Probe Engine.

Registers all priority probes with the engine on import.
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


def register_all(registry: ProbeRegistry) -> None:
    """Register all built-in probes with the given registry."""
    registry.register(
        probe_id="mirror_drift",
        fn=probe_mirror_drift,
        category=ProbeCategory.CONTEXT_QUALITY,
        description="Checks if CLAUDE.md and README reference files/paths that no longer exist.",
        triggers=["manual", "commit", "session_end"],
    )
    registry.register(
        probe_id="stale_context",
        fn=probe_stale_context,
        category=ProbeCategory.CONTEXT_QUALITY,
        description="Detects stale fleet messages (>7d) and stale plan docs (>30d, NOT STARTED).",
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
        description="Scans Python imports for undeclared dependencies not in requirements/pyproject.",
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


def create_engine() -> ProbeEngine:
    """Create a ProbeEngine with all built-in probes registered."""
    registry = ProbeRegistry()
    register_all(registry)
    return ProbeEngine(registry=registry)
