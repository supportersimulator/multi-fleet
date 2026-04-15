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

__all__ = [
    "CoordinatorAgent",
    "EvidencePipeline",
    "HealthChecker",
    "LocalHealthSnapshot",
    "ServiceHealth",
    "SurgeonProxy",
    "SurgicalOutcome",
    "SurgeonAvailability",
]
