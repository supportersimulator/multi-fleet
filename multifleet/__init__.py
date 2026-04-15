"""
multifleet — self-contained Python package for Multi-Fleet coordination.

Provides NATS-based real-time communication, 7-priority fallback messaging,
peer discovery, HMAC security, worker dispatch, and visual rendering for
coordinating Claude Code sessions across multiple machines.
"""

__version__ = "5.1.0"

from .coordinator_agent import CoordinatorAgent
from .coordinator_evidence import EvidencePipeline
from .coordinator_health import HealthChecker, LocalHealthSnapshot, ServiceHealth
from .coordinator_surgeon_proxy import SurgeonProxy, SurgicalOutcome, SurgeonAvailability
from .chief_orchestrator import ChiefOrchestrator, SurgicalPhase, SurgicalProtocolResult
from .workspace_roles import WorkspaceRegistry, WorkspaceRole, ApproachStrategy
from .surgeon_lan_relay import SurgeonLANRelay, SurgeonPacket

__all__ = [
    "CoordinatorAgent",
    "EvidencePipeline",
    "HealthChecker",
    "LocalHealthSnapshot",
    "ServiceHealth",
    "SurgeonProxy",
    "SurgicalOutcome",
    "SurgeonAvailability",
    "ChiefOrchestrator",
    "SurgicalPhase",
    "SurgicalProtocolResult",
    "WorkspaceRegistry",
    "WorkspaceRole",
    "ApproachStrategy",
    "SurgeonLANRelay",
    "SurgeonPacket",
]
