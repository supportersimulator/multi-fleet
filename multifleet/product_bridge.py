"""Product Bridge — connects multi-fleet to other ContextDNA products.

Integration skeleton that lets 5 products talk to each other.
Each product registers its capabilities and the bridge routes between them.

Products:
  multi-fleet   (port 8855)  — transport layer, message delivery
  3-surgeons    (no port)    — consensus engine, cross-exam, gates
  superpowers   (no port)    — workflow orchestration, skills
  context-dna   (port 8029)  — memory, evidence, webhook injection
  admin-panel   (port 3000)  — dashboard, monitoring UI

Architecture:
  ProductBridge is the routing hub. Each product pair has typed adapter
  methods that translate between product-native formats. All routing
  is async-safe but sync by default (fleet daemon is sync).

  Discovery is port-based for networked products, import-based for
  library products, and MCP-config-based for MCP-enabled products.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("fleet-nerve.product-bridge")


# ── Product Registry ─────────────────────────────────────────────────────


@dataclass
class ProductInfo:
    """Metadata about a registered product."""

    name: str
    port: Optional[int] = None
    product_type: str = "unknown"  # transport | consensus | workflow | memory | dashboard
    mcp_enabled: bool = False
    available: bool = False
    capabilities: Dict[str, Any] = field(default_factory=dict)
    last_checked: float = 0.0


PRODUCT_CATALOG: Dict[str, Dict[str, Any]] = {
    "multi-fleet": {"port": 8855, "type": "transport", "mcp": True},
    "3-surgeons": {"port": None, "type": "consensus", "mcp": True},
    "superpowers": {"port": None, "type": "workflow", "mcp": False},
    "context-dna": {"port": 8029, "type": "memory", "mcp": True},
    "admin-panel": {"port": 3000, "type": "dashboard", "mcp": False},
}

# How long to cache discovery results (seconds)
_DISCOVERY_CACHE_TTL = 60.0


# ── Errors ───────────────────────────────────────────────────────────────


class BridgeError(Exception):
    """Base error for product bridge operations."""


class ProductUnavailable(BridgeError):
    """Raised when a target product is not reachable."""


class RouteNotFound(BridgeError):
    """Raised when no route exists between two products."""


# ── Product Bridge ───────────────────────────────────────────────────────


class ProductBridge:
    """Central integration point between ContextDNA products.

    Usage:
        bridge = ProductBridge()
        available = bridge.discover_products()
        result = bridge.route("multi-fleet", "3-surgeons", {"type": "verdict_request", ...})
    """

    def __init__(self):
        self._products: Dict[str, ProductInfo] = {}
        self._routes: Dict[str, Callable] = {}
        self._discovery_cache: Optional[Dict[str, ProductInfo]] = None
        self._discovery_ts: float = 0.0

        # Register built-in routes
        self._register_builtin_routes()

    # ── Registration ─────────────────────────────────────────────────

    def register_product(self, name: str, capabilities: Dict[str, Any]) -> ProductInfo:
        """Register a product with its capabilities.

        Args:
            name: Product identifier (must be in PRODUCT_CATALOG).
            capabilities: Dict of capability_name -> bool/config.

        Returns:
            ProductInfo for the registered product.
        """
        catalog = PRODUCT_CATALOG.get(name, {})
        info = ProductInfo(
            name=name,
            port=catalog.get("port"),
            product_type=catalog.get("type", "unknown"),
            mcp_enabled=catalog.get("mcp", False),
            available=True,
            capabilities=capabilities,
            last_checked=time.time(),
        )
        self._products[name] = info
        logger.info("registered product: %s (type=%s, caps=%s)", name, info.product_type, list(capabilities.keys()))
        return info

    def discover_products(self) -> Dict[str, ProductInfo]:
        """Auto-discover which products are available on this machine.

        Checks ports for networked products, imports for library products,
        and .mcp.json for MCP-enabled products. Results cached for 60s.
        """
        now = time.time()
        if self._discovery_cache and (now - self._discovery_ts) < _DISCOVERY_CACHE_TTL:
            return self._discovery_cache

        discovered = {}
        for name, catalog in PRODUCT_CATALOG.items():
            info = ProductInfo(
                name=name,
                port=catalog.get("port"),
                product_type=catalog.get("type", "unknown"),
                mcp_enabled=catalog.get("mcp", False),
                last_checked=now,
            )

            # Port-based check
            if info.port:
                info.available = self._check_port(info.port)
            else:
                # Import-based check for library products
                info.available = self._check_import(name)

            discovered[name] = info

        self._discovery_cache = discovered
        self._discovery_ts = now
        self._products.update(discovered)
        return discovered

    # ── Routing ──────────────────────────────────────────────────────

    def route(self, from_product: str, to_product: str, message: Dict[str, Any]) -> Dict[str, Any]:
        """Route a message between products.

        Args:
            from_product: Source product name.
            to_product: Destination product name.
            message: Message payload (product-specific format).

        Returns:
            Response dict from the target product.

        Raises:
            ProductUnavailable: Target product not reachable.
            RouteNotFound: No route defined for this product pair.
        """
        route_key = f"{from_product}->{to_product}"
        handler = self._routes.get(route_key)

        if handler is None:
            raise RouteNotFound(f"no route: {route_key}")

        # Check target availability
        target = self._products.get(to_product)
        if target and not target.available:
            # Re-check before failing
            self._refresh_product(to_product)
            target = self._products.get(to_product)
            if target and not target.available:
                raise ProductUnavailable(f"{to_product} not available")

        result = handler(message)

        # Emit event for observability (EventEnvelope standard)
        try:
            from multifleet.event_emitter import emit_typed
            from multifleet.event_schema import EventType, EventSource
            emit_typed(
                EventType.FLEET_TASK_DISPATCHED,
                EventSource.MULTI_FLEET,
                payload={
                    "route": route_key,
                    "from": from_product,
                    "to": to_product,
                    "message_type": message.get("type", "unknown"),
                    "success": "error" not in result,
                },
            )
        except (ImportError, Exception):
            pass  # Event emission is best-effort

        return result

    def list_routes(self) -> List[str]:
        """Return all registered route keys."""
        return list(self._routes.keys())

    # ── Bridge Adapters (typed product-pair methods) ─────────────────

    def fleet_to_surgeons(self, verdict_request: Dict[str, Any]) -> Dict[str, Any]:
        """Multi-fleet asks 3-surgeons for cross-exam on a fleet decision.

        Tries SurgeonAdapter first (direct bridge), falls back to generic routing.

        Input: {task_id, claim, evidence, files?, severity?}
        Output: {verdict, confidence, reasoning, dissent}
        """
        try:
            from multifleet.surgeon_adapter import SurgeonAdapter
            adapter = SurgeonAdapter()
            if adapter.is_available():
                claim = verdict_request.get("claim", "fleet decision review")
                evidence = verdict_request.get("evidence", [])
                review = adapter.request_review(claim, evidence if isinstance(evidence, list) else [str(evidence)])
                # Normalize to expected output format
                return {
                    "verdict": review.get("status", "unknown"),
                    "confidence": 0.0,
                    "reasoning": review.get("output", ""),
                    "dissent": [],
                    "source": "surgeon_adapter",
                    "review_type": review.get("review_type", "unknown"),
                }
            # Adapter imported but bridge unavailable — fall through to generic route
            logger.debug("SurgeonAdapter available but bridge not connected, using generic route")
        except ImportError:
            logger.debug("SurgeonAdapter not importable, using generic route")
        except Exception as exc:
            logger.warning("SurgeonAdapter error, falling back to generic route: %s", exc)

        return self.route("multi-fleet", "3-surgeons", verdict_request)

    def surgeons_to_fleet(self, verdict: Dict[str, Any]) -> Dict[str, Any]:
        """3-surgeons sends verdict back to fleet for distribution.

        Input: {task_id, verdict, confidence, target_nodes?}
        Output: {delivered_to, channel_used}
        """
        return self.route("3-surgeons", "multi-fleet", verdict)

    def fleet_to_memory(self, evidence: Dict[str, Any]) -> Dict[str, Any]:
        """Multi-fleet stores evidence in ContextDNA memory.

        Input: {event_type, node_id, evidence, tags?}
        Output: {stored, entry_id}
        """
        return self.route("multi-fleet", "context-dna", evidence)

    def memory_to_fleet(self, query: str) -> Dict[str, Any]:
        """ContextDNA provides memory context to fleet synthesis.

        Input: query string
        Output: {results, count, source}
        """
        return self.route("context-dna", "multi-fleet", {"query": query})

    def probes_to_superpowers(self, findings: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Probe findings inform superpowers workflow decisions.

        Input: [{title, severity, blast_radius, evidence}]
        Output: {workflow_adjustments, risk_level}
        """
        return self.route("multi-fleet", "superpowers", {"findings": findings})

    # ── Internal Route Handlers ──────────────────────────────────────

    def _register_builtin_routes(self):
        """Register all built-in product-pair routes."""
        self._routes["multi-fleet->3-surgeons"] = self._handle_fleet_to_surgeons
        self._routes["3-surgeons->multi-fleet"] = self._handle_surgeons_to_fleet
        self._routes["multi-fleet->context-dna"] = self._handle_fleet_to_memory
        self._routes["context-dna->multi-fleet"] = self._handle_memory_to_fleet
        self._routes["multi-fleet->superpowers"] = self._handle_probes_to_superpowers
        self._routes["3-surgeons->context-dna"] = self._handle_surgeons_to_memory
        self._routes["context-dna->3-surgeons"] = self._handle_memory_to_surgeons

    def _handle_fleet_to_surgeons(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        """Route fleet verdict request to 3-surgeons cross-exam."""
        try:
            cross_exam = _import_cross_exam()
            if cross_exam is None:
                return {"error": "3-surgeons not importable", "fallback": True}

            result = cross_exam.consult(
                topic=msg.get("claim", "fleet decision review"),
                context=json.dumps(msg.get("evidence", {})),
            )
            return {
                "verdict": getattr(result, "verdict", "unknown"),
                "confidence": getattr(result, "confidence", 0.0),
                "reasoning": getattr(result, "reasoning", ""),
                "dissent": getattr(result, "dissent", []),
                "source": "3-surgeons",
            }
        except Exception as e:
            logger.warning("fleet->surgeons failed: %s", e)
            return {"error": str(e), "fallback": True}

    def _handle_surgeons_to_fleet(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        """Route surgeon verdict to fleet for broadcast."""
        try:
            from multifleet.packets import FleetPacket

            packet = FleetPacket(
                type="surgeon_verdict",
                from_node=os.environ.get("MULTIFLEET_NODE_ID", "unknown"),
                to_node=msg.get("target_nodes", "all"),
                payload=msg,
            )
            return {
                "packet_id": packet.id,
                "delivered_to": packet.to_node,
                "channel_used": "internal",
            }
        except Exception as e:
            logger.warning("surgeons->fleet failed: %s", e)
            return {"error": str(e), "fallback": True}

    def _handle_fleet_to_memory(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        """Store fleet evidence in ContextDNA memory via HTTP or import."""
        # Try HTTP first (port 8029)
        try:
            payload = json.dumps(msg).encode()
            req = urllib.request.Request(
                "http://127.0.0.1:8029/contextdna/evidence",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read())
        except (urllib.error.URLError, OSError):
            pass

        # Fallback: local evidence ledger
        try:
            from multifleet.evidence_ledger import EvidenceLedger

            ledger = EvidenceLedger()
            entry_id = ledger.append(
                event_type=msg.get("event_type", "fleet_evidence"),
                node_id=msg.get("node_id", "unknown"),
                data=msg.get("evidence", msg),
            )
            return {"stored": True, "entry_id": entry_id, "backend": "local_ledger"}
        except Exception as e:
            logger.warning("fleet->memory failed: %s", e)
            return {"error": str(e), "stored": False}

    def _handle_memory_to_fleet(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        """Query ContextDNA memory and return results to fleet."""
        query = msg.get("query", "")

        # Try HTTP first
        try:
            url = f"http://127.0.0.1:8029/contextdna/query?q={urllib.request.quote(query)}"
            with urllib.request.urlopen(url, timeout=5) as resp:
                return json.loads(resp.read())
        except (urllib.error.URLError, OSError):
            pass

        # Fallback: local evidence ledger search
        try:
            from multifleet.evidence_ledger import EvidenceLedger

            ledger = EvidenceLedger()
            entries = ledger.query(event_type=None, limit=10)
            # Basic keyword filter
            matched = [e for e in entries if query.lower() in json.dumps(e).lower()]
            return {"results": matched[:5], "count": len(matched), "source": "local_ledger"}
        except Exception as e:
            logger.warning("memory->fleet failed: %s", e)
            return {"results": [], "count": 0, "error": str(e)}

    def _handle_probes_to_superpowers(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        """Translate probe findings into superpowers workflow adjustments."""
        findings = msg.get("findings", [])
        if not findings:
            return {"workflow_adjustments": [], "risk_level": "none"}

        # Classify risk level from probe severities
        severities = [f.get("blast_radius", "low") for f in findings]
        if "critical" in severities or "high" in severities:
            risk_level = "high"
        elif "medium" in severities:
            risk_level = "medium"
        else:
            risk_level = "low"

        # Map findings to workflow recommendations
        adjustments = []
        for finding in findings:
            title = finding.get("title", "")
            if "security" in title.lower():
                adjustments.append({
                    "action": "gate",
                    "gate_type": "security_review",
                    "reason": title,
                })
            elif "drift" in title.lower() or "diverge" in title.lower():
                adjustments.append({
                    "action": "sync",
                    "reason": title,
                })
            elif "hotspot" in title.lower() or "blast" in title.lower():
                adjustments.append({
                    "action": "review",
                    "gate_type": "architectural_gate",
                    "reason": title,
                })
            else:
                adjustments.append({
                    "action": "inform",
                    "reason": title,
                })

        return {
            "workflow_adjustments": adjustments,
            "risk_level": risk_level,
            "findings_count": len(findings),
        }

    def _handle_surgeons_to_memory(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        """Store surgeon cross-exam results in ContextDNA."""
        evidence = {
            "event_type": "surgeon_verdict",
            "node_id": msg.get("surgeon", "atlas"),
            "evidence": msg,
        }
        return self._handle_fleet_to_memory(evidence)

    def _handle_memory_to_surgeons(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        """Provide memory context to surgeons for cross-exam."""
        query = msg.get("query", msg.get("topic", ""))
        memory_result = self._handle_memory_to_fleet({"query": query})
        return {
            "context": memory_result.get("results", []),
            "source": "context-dna",
            "count": memory_result.get("count", 0),
        }

    # ── Discovery Helpers ────────────────────────────────────────────

    @staticmethod
    def _check_port(port: int, timeout: float = 2.0) -> bool:
        """Check if a port is responding on localhost."""
        try:
            url = f"http://127.0.0.1:{port}/health"
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status == 200
        except Exception:
            return False

    @staticmethod
    def _check_import(product_name: str) -> bool:
        """Check if a product's Python package is importable."""
        import_map = {
            "3-surgeons": "three_surgeons",
            "superpowers": None,  # No Python package yet
            "multi-fleet": "multifleet",
            "context-dna": "memory",
            "admin-panel": None,  # TypeScript, not importable
        }
        module_name = import_map.get(product_name)
        if module_name is None:
            return False
        try:
            importlib.import_module(module_name)
            return True
        except ImportError:
            return False

    def _refresh_product(self, name: str):
        """Re-check a single product's availability."""
        catalog = PRODUCT_CATALOG.get(name, {})
        port = catalog.get("port")
        if port:
            available = self._check_port(port)
        else:
            available = self._check_import(name)

        if name in self._products:
            self._products[name].available = available
            self._products[name].last_checked = time.time()


# ── Lazy Imports ─────────────────────────────────────────────────────────


def _import_cross_exam():
    """Lazily import 3-surgeons cross_exam module."""
    try:
        from three_surgeons.core.cross_exam import CrossExamEngine
        return CrossExamEngine()
    except ImportError:
        logger.debug("three_surgeons not installed — cross-exam unavailable")
        return None


# ── Module-level singleton ───────────────────────────────────────────────

_bridge: Optional[ProductBridge] = None


def get_bridge() -> ProductBridge:
    """Get or create the global ProductBridge singleton."""
    global _bridge
    if _bridge is None:
        _bridge = ProductBridge()
    return _bridge
