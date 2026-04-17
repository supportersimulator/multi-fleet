"""Panel Engine Bridge -- translates Python panel data to TS engine format.

Bridges the gap between Python fleet panels (IDEPanelServer, PanelBusBridge)
and the TypeScript Context DNA engine panel system (PanelDescriptor, PanelManifest,
PanelEvent, IContextBus BusEvent).

TS engine types mapped:
  PanelManifest  -> generate_manifests()
  PanelDescriptor -> generate_descriptors()
  PanelEvent     -> translate_event()
  BusEvent       -> translate_bus_event()

Usage:
    from multifleet.panel_engine_bridge import PanelEngineBridge
    bridge = PanelEngineBridge()
    manifests = bridge.generate_manifests()
    descriptors = bridge.generate_descriptors()

Or via web_server:
    GET /api/panels/engine-format
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Optional

from multifleet.memory_scope import MemoryScope, map_python_source

logger = logging.getLogger("multifleet.panel_engine_bridge")


# ---------------------------------------------------------------------------
# Panel ID -> TS manifest mapping (static config)
# ---------------------------------------------------------------------------

# Maps Python panel IDs to TS PanelManifest-compatible metadata.
# source: "mcp" because these panels originate from a remote Python service,
# not built into the TS IDE.

_PANEL_REGISTRY: dict[str, dict[str, Any]] = {
    "fleet-status": {
        "panelId": "fleet.status",
        "version": "1.0.0",
        "displayName": "Fleet Status",
        "description": "Node health, channels, sessions across the multi-fleet",
        "source": "mcp",
        "entry": "/api/panels/fleet-status",
        "memoryScope": MemoryScope.SESSION.value,
        "capabilities": ["bus_subscribe", "evidence_read"],
        "layout": {
            "defaultMode": "docked",
            "defaultPosition": "bottom",
            "minWidth": 300,
            "minHeight": 150,
            "resizable": True,
        },
        "icon": "codicon:server",
        "activationEvents": ["onStartup"],
    },
    "evidence-timeline": {
        "panelId": "fleet.evidence-timeline",
        "version": "1.0.0",
        "displayName": "Evidence Timeline",
        "description": "Hash-chained evidence ledger entries with verification",
        "source": "mcp",
        "entry": "/api/panels/evidence-timeline",
        "memoryScope": MemoryScope.CORE.value,
        "capabilities": ["evidence_read", "bus_subscribe"],
        "layout": {
            "defaultMode": "docked",
            "defaultPosition": "bottom",
            "minWidth": 400,
            "minHeight": 200,
            "resizable": True,
        },
        "icon": "codicon:timeline-open",
        "activationEvents": ["onStartup"],
    },
    "synthesis-decisions": {
        "panelId": "fleet.synthesis-decisions",
        "version": "1.0.0",
        "displayName": "Synthesis Decisions",
        "description": "ChiefSynthesis decisions with confidence and winners",
        "source": "mcp",
        "entry": "/api/panels/synthesis-decisions",
        "memoryScope": MemoryScope.WORKSPACE.value,
        "capabilities": ["evidence_read", "bus_subscribe"],
        "layout": {
            "defaultMode": "docked",
            "defaultPosition": "bottom",
            "minWidth": 350,
            "minHeight": 180,
            "resizable": True,
        },
        "icon": "codicon:merge",
        "activationEvents": ["onCommand:openSynthesis"],
    },
    "probe-findings": {
        "panelId": "fleet.probe-findings",
        "version": "1.0.0",
        "displayName": "Probe Findings",
        "description": "Latest probe scan results with severity and findings",
        "source": "mcp",
        "entry": "/api/panels/probe-findings",
        "memoryScope": MemoryScope.TASK.value,
        "capabilities": ["evidence_read", "bus_subscribe"],
        "layout": {
            "defaultMode": "docked",
            "defaultPosition": "bottom",
            "minWidth": 350,
            "minHeight": 180,
            "resizable": True,
        },
        "icon": "codicon:search-fuzzy",
        "activationEvents": ["onStartup"],
    },
    "wisdom-feedback": {
        "panelId": "fleet.wisdom-feedback",
        "version": "1.0.0",
        "displayName": "Wisdom Feedback",
        "description": "Professor wisdom, gotchas, and evidence-backed feedback",
        "source": "mcp",
        "entry": "/api/panels/wisdom-feedback",
        "memoryScope": MemoryScope.CORE.value,
        "capabilities": ["evidence_read"],
        "layout": {
            "defaultMode": "docked",
            "defaultPosition": "right",
            "minWidth": 300,
            "minHeight": 200,
            "resizable": True,
        },
        "icon": "codicon:lightbulb",
        "activationEvents": ["onCommand:openWisdom"],
    },
    "quorum-pulse": {
        "panelId": "fleet.quorum-pulse",
        "version": "1.0.0",
        "displayName": "Quorum Pulse",
        "description": "Real-time quorum voting and consensus state",
        "source": "mcp",
        "entry": "/api/panels/poll",
        "memoryScope": MemoryScope.SESSION.value,
        "capabilities": ["bus_subscribe"],
        "layout": {
            "defaultMode": "docked",
            "defaultPosition": "bottom",
            "minWidth": 250,
            "minHeight": 120,
            "resizable": True,
        },
        "icon": "codicon:pulse",
        "activationEvents": ["onStartup"],
    },
    "surgeon-feed": {
        "panelId": "fleet.surgeon-feed",
        "version": "1.0.0",
        "displayName": "Surgeon Feed",
        "description": "3-surgeon cross-examination results and consensus",
        "source": "mcp",
        "entry": "/api/panels/poll",
        "memoryScope": MemoryScope.TASK.value,
        "capabilities": ["bus_subscribe", "evidence_read"],
        "layout": {
            "defaultMode": "docked",
            "defaultPosition": "bottom",
            "minWidth": 400,
            "minHeight": 200,
            "resizable": True,
        },
        "icon": "codicon:beaker",
        "activationEvents": ["onCommand:openSurgeonFeed"],
    },
}

# Maps Python event bus namespaces to TS PanelEventTypes
_NAMESPACE_TO_PANEL_EVENT: dict[str, str] = {
    "fleet": "panel.activated",
    "probe": "panel.activated",
    "surgeon": "panel.activated",
    "evidence": "panel.activated",
    "quorum": "panel.activated",
    "panel.updated": "panel.activated",
}

# Maps Python event bus namespaces to target panel IDs (TS format)
_NAMESPACE_TO_PANEL_ID: dict[str, str] = {
    "fleet": "fleet.status",
    "probe": "fleet.probe-findings",
    "surgeon": "fleet.surgeon-feed",
    "evidence": "fleet.evidence-timeline",
    "quorum": "fleet.quorum-pulse",
}


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class PanelEngineBridge:
    """Translates Python panel data into TypeScript engine-compatible format.

    Reads from IDEPanelServer (structured JSON panels) and PanelBusBridge
    (event-driven caches) and outputs PanelManifest, PanelDescriptor, and
    PanelEvent dicts matching the TS type contracts.
    """

    _SENTINEL = object()

    def __init__(
        self,
        ide_panel_server=_SENTINEL,
        panel_bus_bridge=_SENTINEL,
    ):
        # _SENTINEL means "lazy-init on first use"; explicit None means "disabled"
        self._ide_panel_server = ide_panel_server
        self._panel_bus_bridge = panel_bus_bridge

    # -- Lazy accessors --

    def _get_ide_panel_server(self):
        if self._ide_panel_server is not self._SENTINEL:
            return self._ide_panel_server
        try:
            from multifleet.ide_panels import IDEPanelServer
            self._ide_panel_server = IDEPanelServer()
        except Exception as e:
            logger.debug("IDEPanelServer unavailable: %s", e)
            self._ide_panel_server = None
        return self._ide_panel_server

    def _get_panel_bus_bridge(self):
        if self._panel_bus_bridge is not self._SENTINEL:
            return self._panel_bus_bridge
        try:
            from multifleet.panel_bus_bridge import PanelBusBridge
            self._panel_bus_bridge = PanelBusBridge.get()
        except Exception as e:
            logger.debug("PanelBusBridge unavailable: %s", e)
            self._panel_bus_bridge = None
        return self._panel_bus_bridge

    # -- Manifest generation --

    def generate_manifests(self) -> list[dict]:
        """Generate PanelManifest-compatible dicts for all Python panels.

        Each manifest matches the TS PanelManifest interface:
          panelId, version, displayName, description, source, entry,
          capabilities, layout, icon, activationEvents
        """
        return [dict(reg) for reg in _PANEL_REGISTRY.values()]

    def generate_manifest(self, python_panel_id: str) -> Optional[dict]:
        """Generate a single PanelManifest for a Python panel ID."""
        reg = _PANEL_REGISTRY.get(python_panel_id)
        if reg is None:
            return None
        return dict(reg)

    # -- Descriptor generation --

    def generate_descriptors(self) -> list[dict]:
        """Generate PanelDescriptor-compatible dicts for all Python panels.

        Each descriptor matches the TS PanelDescriptor interface:
          manifest, state, layoutMode, permissions, registeredAt,
          lastStateChange, activatedAt, errorInfo
        """
        now = _now_iso()
        bus_bridge = self._get_panel_bus_bridge()
        bus_timestamps = {}
        if bus_bridge:
            ts_data = bus_bridge.get_timestamps()
            bus_timestamps = ts_data.get("panels", {})

        descriptors = []
        for py_id, manifest in _PANEL_REGISTRY.items():
            # Determine state from bus bridge activity
            bus_info = bus_timestamps.get(py_id, {})
            event_count = bus_info.get("event_count", 0)
            updated_at = bus_info.get("updated_at", 0)

            if event_count > 0:
                state = "active"
                activated_at = time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(updated_at)
                ) if updated_at else now
            else:
                state = "registered"
                activated_at = None

            descriptors.append({
                "manifest": dict(manifest),
                "state": state,
                "layoutMode": manifest["layout"]["defaultMode"],
                "permissions": {
                    "panelId": manifest["panelId"],
                    "granted": list(manifest["capabilities"]),
                    "denied": [],
                    "evaluatedAt": now,
                    "policyVersion": 1,
                },
                "registeredAt": now,
                "lastStateChange": now,
                "activatedAt": activated_at,
                "errorInfo": None,
            })

        return descriptors

    # -- Event translation --

    def translate_event(self, event_type: str, payload: dict,
                        source: str = "python") -> Optional[dict]:
        """Translate a Python IDEEventBus event to TS PanelEvent format.

        Args:
            event_type: Python event type (e.g. "fleet.node_update")
            payload: Python event payload dict
            source: Emitter name

        Returns:
            PanelEvent-compatible dict, or None if unmappable.
        """
        namespace = event_type.split(".")[0]
        panel_id = _NAMESPACE_TO_PANEL_ID.get(namespace)
        if panel_id is None:
            return None

        return {
            "type": "panel.activated",
            "panelId": panel_id,
            "source": source,
            "timestamp": _now_iso(),
            "payload": payload,
            "correlationId": str(uuid.uuid4()),
        }

    def translate_bus_event(self, event_type: str, payload: dict,
                            source: str = "python") -> Optional[dict]:
        """Translate a Python event to TS IContextBus BusEvent format.

        Args:
            event_type: Python event type (e.g. "fleet.node_update")
            payload: Python event payload dict
            source: Emitter name

        Returns:
            BusEvent-compatible dict: {channel, key, value, timestamp}
            or None if unmappable.
        """
        namespace = event_type.split(".")[0]
        panel_id = _NAMESPACE_TO_PANEL_ID.get(namespace)
        if panel_id is None:
            return None

        # TS bus-bridge uses channel "panel.events" with key "{panelId}:{eventType}"
        return {
            "channel": "panel.events",
            "key": f"{panel_id}:panel.activated",
            "value": {
                "type": "panel.activated",
                "panelId": panel_id,
                "source": source,
                "timestamp": _now_iso(),
                "payload": payload,
                "correlationId": str(uuid.uuid4()),
            },
            "timestamp": _now_iso(),
        }

    # -- Full engine-format endpoint --

    def engine_format(self) -> dict:
        """Return all panel data in TS engine-compatible format.

        This is the payload for GET /api/panels/engine-format.
        Contains manifests, descriptors, and panel data merged from
        both IDEPanelServer and PanelBusBridge.
        """
        manifests = self.generate_manifests()
        descriptors = self.generate_descriptors()

        # Merge live panel data from IDEPanelServer
        panel_data = {}
        server = self._get_ide_panel_server()
        if server:
            try:
                all_panels = server.all_panels()
                for panel in all_panels.get("panels", []):
                    pid = panel.get("panel_id", "")
                    panel_data[pid] = panel
            except Exception as e:
                logger.debug("IDEPanelServer.all_panels() failed: %s", e)

        # Merge event-bus cache data from PanelBusBridge
        bus_data = {}
        bus_bridge = self._get_panel_bus_bridge()
        if bus_bridge:
            try:
                all_bus = bus_bridge.get_all_panels()
                bus_data = all_bus.get("panels", {})
            except Exception as e:
                logger.debug("PanelBusBridge.get_all_panels() failed: %s", e)

        return {
            "manifests": manifests,
            "descriptors": descriptors,
            "panelData": panel_data,
            "busCache": bus_data,
            "generatedAt": _now_iso(),
        }

    # -- Health --

    def health(self) -> dict:
        server_ok = self._get_ide_panel_server() is not None
        bus_ok = self._get_panel_bus_bridge() is not None
        return {
            "status": "ok" if (server_ok or bus_ok) else "degraded",
            "ide_panel_server": server_ok,
            "panel_bus_bridge": bus_ok,
            "registered_panels": len(_PANEL_REGISTRY),
        }
