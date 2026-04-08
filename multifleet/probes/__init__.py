"""Built-in probes for the ContextDNA Probe Engine.

Registers all priority probes with the engine on import.
"""

from multifleet.probe_engine import ProbeCategory, ProbeEngine, ProbeRegistry

from multifleet.probes.mirror_drift import probe_mirror_drift
from multifleet.probes.stale_context import probe_stale_context
from multifleet.probes.fleet_health import probe_fleet_health


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


def create_engine() -> ProbeEngine:
    """Create a ProbeEngine with all built-in probes registered."""
    registry = ProbeRegistry()
    register_all(registry)
    return ProbeEngine(registry=registry)
