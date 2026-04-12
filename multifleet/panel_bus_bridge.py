"""Panel-Bus Bridge -- connects IDEEventBus to IDE panels and dashboard components.

Subscribes to event bus namespaces, maintains a cache of latest data for each
panel, and emits panel.updated events when cache changes. Dashboard components
and panel endpoints query the bridge for fresh, pre-computed data instead of
recomputing on every request.

Event -> Panel mapping:
    fleet.*    -> fleet_status_panel
    probe.*    -> probe_findings_panel
    surgeon.*  -> SurgeonFeed component
    evidence.* -> evidence_timeline_panel + EvidenceTimeline component
    quorum.*   -> QuorumPulse component

Usage:
    from multifleet.panel_bus_bridge import PanelBusBridge
    from multifleet.event_bus import IDEEventBus

    bus = IDEEventBus.get()
    bridge = PanelBusBridge(bus)
    # Events now auto-populate panel caches
    data = bridge.get_panel("fleet-status")
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any, Optional

from .event_bus import (
    IDEEventBus,
    NS_FLEET,
    NS_PROBE,
    NS_SURGEON,
    NS_EVIDENCE,
    NS_QUORUM,
)

logger = logging.getLogger("multifleet.panel_bus_bridge")

# Panel IDs (match ide_panels.py envelope panel_id values)
PANEL_FLEET_STATUS = "fleet-status"
PANEL_PROBE_FINDINGS = "probe-findings"
PANEL_SURGEON_FEED = "surgeon-feed"
PANEL_EVIDENCE_TIMELINE = "evidence-timeline"
PANEL_QUORUM_PULSE = "quorum-pulse"

ALL_PANEL_IDS = frozenset({
    PANEL_FLEET_STATUS,
    PANEL_PROBE_FINDINGS,
    PANEL_SURGEON_FEED,
    PANEL_EVIDENCE_TIMELINE,
    PANEL_QUORUM_PULSE,
})

# Max items kept per panel cache
DEFAULT_CACHE_SIZE = 100


class PanelBusBridge:
    """Bridge between IDEEventBus events and panel data caches.

    On init, subscribes to fleet.*, probe.*, surgeon.*, evidence.*, quorum.*
    namespaces. Incoming events update per-panel caches. When cache changes,
    emits panel.updated so dashboard components can auto-refresh.
    """

    _instance: Optional[PanelBusBridge] = None
    _lock = threading.Lock()

    def __init__(
        self,
        bus: IDEEventBus,
        cache_size: int = DEFAULT_CACHE_SIZE,
    ):
        self._bus = bus
        self._cache_size = cache_size
        self._data_lock = threading.Lock()

        # Per-panel caches: panel_id -> deque of event payloads (newest first on read)
        self._caches: dict[str, deque] = {
            pid: deque(maxlen=cache_size) for pid in ALL_PANEL_IDS
        }

        # Per-panel metadata
        self._updated_at: dict[str, float] = {pid: 0.0 for pid in ALL_PANEL_IDS}
        self._event_counts: dict[str, int] = {pid: 0 for pid in ALL_PANEL_IDS}

        # Subscribe to namespaces
        self._bus.on("fleet.*", self._on_fleet)
        self._bus.on("probe.*", self._on_probe)
        self._bus.on("surgeon.*", self._on_surgeon)
        self._bus.on("evidence.*", self._on_evidence)
        self._bus.on("quorum.*", self._on_quorum)

    # -- Singleton access --

    @classmethod
    def get(cls, bus: Optional[IDEEventBus] = None, **kwargs) -> PanelBusBridge:
        """Get or create singleton bridge.

        If bus is None, uses IDEEventBus.get().
        """
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    if bus is None:
                        bus = IDEEventBus.get()
                    cls._instance = cls(bus, **kwargs)
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset singleton (for testing)."""
        cls._instance = None

    # -- Event handlers --

    def _on_fleet(self, event: dict) -> None:
        self._cache_event(PANEL_FLEET_STATUS, event)

    def _on_probe(self, event: dict) -> None:
        self._cache_event(PANEL_PROBE_FINDINGS, event)

    def _on_surgeon(self, event: dict) -> None:
        self._cache_event(PANEL_SURGEON_FEED, event)

    def _on_evidence(self, event: dict) -> None:
        self._cache_event(PANEL_EVIDENCE_TIMELINE, event)

    def _on_quorum(self, event: dict) -> None:
        self._cache_event(PANEL_QUORUM_PULSE, event)

    def _cache_event(self, panel_id: str, event: dict) -> None:
        """Add event to panel cache and emit panel.updated."""
        with self._data_lock:
            self._caches[panel_id].append(event)
            self._updated_at[panel_id] = time.time()
            self._event_counts[panel_id] += 1

        self.emit_panel_update(panel_id)

    # -- Panel update emission --

    def emit_panel_update(self, panel_id: str, extra: Optional[dict] = None) -> None:
        """Publish panel.updated event so dashboard components auto-refresh."""
        payload = {
            "panel_id": panel_id,
            "updated_at": self._updated_at.get(panel_id, 0),
            "event_count": self._event_counts.get(panel_id, 0),
        }
        if extra:
            payload.update(extra)
        self._bus.emit("panel.updated", payload, source="panel-bus-bridge")

    # -- Data access --

    def get_panel(self, panel_id: str) -> dict:
        """Get cached panel data.

        Returns:
            {panel_id, updated_at, event_count, events: [...newest first]}
        """
        with self._data_lock:
            events = list(self._caches.get(panel_id, []))
            events.reverse()  # newest first
            return {
                "panel_id": panel_id,
                "updated_at": self._updated_at.get(panel_id, 0),
                "event_count": self._event_counts.get(panel_id, 0),
                "events": events,
            }

    def get_all_panels(self) -> dict:
        """Get all panel caches with timestamps for efficient polling.

        Returns:
            {panels: {panel_id: {updated_at, event_count, events}}, generated_at}
        """
        panels = {}
        for pid in ALL_PANEL_IDS:
            panels[pid] = self.get_panel(pid)
        return {
            "panels": panels,
            "generated_at": time.time(),
        }

    def get_timestamps(self) -> dict:
        """Return just panel timestamps for cheap client-side freshness check.

        Client polls this, compares to local cache, fetches only stale panels.
        """
        with self._data_lock:
            return {
                "panels": {
                    pid: {
                        "updated_at": self._updated_at[pid],
                        "event_count": self._event_counts[pid],
                    }
                    for pid in ALL_PANEL_IDS
                },
            }

    def get_panel_for_component(self, component_name: str) -> dict:
        """Map dashboard component name to panel data.

        Component names: vitals, surgeon-feed, quorum-pulse, evidence-timeline
        """
        mapping = {
            "vitals": PANEL_FLEET_STATUS,
            "surgeon-feed": PANEL_SURGEON_FEED,
            "quorum-pulse": PANEL_QUORUM_PULSE,
            "evidence-timeline": PANEL_EVIDENCE_TIMELINE,
        }
        panel_id = mapping.get(component_name)
        if panel_id is None:
            return {"error": f"Unknown component: {component_name}", "events": []}
        return self.get_panel(panel_id)

    # -- Stats --

    @property
    def stats(self) -> dict:
        with self._data_lock:
            return {
                "panels": {
                    pid: {
                        "cached_events": len(self._caches[pid]),
                        "total_events": self._event_counts[pid],
                        "updated_at": self._updated_at[pid],
                    }
                    for pid in ALL_PANEL_IDS
                },
                "cache_size": self._cache_size,
            }

    def health(self) -> dict:
        active = [
            pid for pid in ALL_PANEL_IDS
            if self._event_counts.get(pid, 0) > 0
        ]
        return {
            "status": "ok",
            "active_panels": active,
            "total_events": sum(self._event_counts.values()),
        }
