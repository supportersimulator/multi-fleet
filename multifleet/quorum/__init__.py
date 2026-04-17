"""Quorum Sensing — bacterial communication intelligence for Multi-Fleet.

Inspired by P. aeruginosa's 4 interlocking signaling systems:

    Las  -> SignalClass.STRATEGIC  : rare, authoritative, requires corroboration
    Rhl  -> SignalClass.TACTICAL   : frequent, local, execution flow
    PQS  -> SignalClass.STATE      : contextual broadcast, modulates behavior
    IQS  -> SignalClass.STRESS     : environmental override, shifts hierarchy

Key biological principles honored:
    1. Hierarchy is real but NOT rigid — stress re-weights which layer dominates
    2. Threshold-based — quorum means population density, not one cell shouting
    3. Coordination without centralization — coherent colony, no single cell sees the whole map
    4. Anti-biofilm — too much consensus is dangerous (quorum quenching)

The invariant Multi-Fleet already has: messages always deliver.
The invariant quorum sensing adds: not every delivered message deserves equal authority.
"""

from .signal_class import SignalClass, classify_message
from .stress_score import (
    STRESS_CRITICAL,
    STRESS_DEGRADED,
    STRESS_GUARDED,
    STRESS_NORMAL,
    apply_stress_policy,
    compute_stress_score,
    emit_stress_event,
    get_stress_level,
)
from .quorum_registry import QuorumRegistry
from .node_reputation import compute_node_reputation

__all__ = [
    "SignalClass",
    "classify_message",
    "compute_stress_score",
    "apply_stress_policy",
    "get_stress_level",
    "STRESS_NORMAL",
    "STRESS_GUARDED",
    "STRESS_DEGRADED",
    "STRESS_CRITICAL",
    "emit_stress_event",
    "QuorumRegistry",
    "compute_node_reputation",
]
