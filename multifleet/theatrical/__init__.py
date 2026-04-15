"""Theatrical Dashboard Components — Superset-inspired visualization system.

9 components that make fleet intelligence VISIBLE:
1. Vital Signs Bar — real-time fleet health
2. Surgeon Feed — live cross-examination stream
3. Fleet Constellation — node topology
4. Evidence Timeline — hash-chain decisions
5. Quorum Pulse — bacterial signal flow
6. Memory Heat Map — knowledge density
7. Probe Status Grid — 18 probes with timestamps
8. Corrigibility Gauge — corrigibility score + trend
9. Session Gold Stream — live gold mining output
"""

from .vital_signs import VitalSignsBar
from .surgeon_feed import SurgeonFeed
from .fleet_constellation import FleetConstellation
from .evidence_timeline import EvidenceTimeline
from .quorum_pulse import QuorumPulse
from .memory_heat_map import MemoryHeatMap
from .probe_status_grid import ProbeStatusGrid
from .corrigibility_gauge import CorrigibilityGauge
from .session_gold_stream import SessionGoldStream

__all__ = [
    "VitalSignsBar",
    "SurgeonFeed",
    "FleetConstellation",
    "EvidenceTimeline",
    "QuorumPulse",
    "MemoryHeatMap",
    "ProbeStatusGrid",
    "CorrigibilityGauge",
    "SessionGoldStream",
]
