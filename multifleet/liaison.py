"""Fleet Liaison Agent — background comms handler per machine.

Runs as a separate lightweight process/session. Monitors all fleet channels,
processes incoming messages, and only interrupts the main coding agent when
something crosses an escalation threshold.

Design principles:
- Schema-bound, low-token (NOT allowed to edit code)
- Only relays, summarizes, compares, and escalates
- Publishes fleet.* events to EventBus for IDE consumption
- Maintains a small state: pending reviews, active rebuttals, lease status
"""

import json
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("fleet-nerve.liaison")


class EscalationLevel(Enum):
    """When to interrupt the main coding agent."""
    SILENT = "silent"           # Log only, don't interrupt
    INFO = "info"               # Add to next systemMessage batch
    URGENT = "urgent"           # Interrupt via --resume injection
    CRITICAL = "critical"       # Interrupt + audible alert


# Message type -> default escalation level
ESCALATION_MAP: Dict[str, EscalationLevel] = {
    "heartbeat": EscalationLevel.SILENT,
    "ack": EscalationLevel.SILENT,
    "context": EscalationLevel.INFO,
    "sync": EscalationLevel.SILENT,
    "task_brief": EscalationLevel.URGENT,       # Chief assigned work
    "critique": EscalationLevel.URGENT,          # Peer reviewing our branch
    "chief_decision": EscalationLevel.URGENT,    # Chief made a decision
    "repair": EscalationLevel.INFO,
    "alert": EscalationLevel.CRITICAL,
    "local_verdict": EscalationLevel.INFO,       # Another machine's verdict
    "merge_candidate": EscalationLevel.INFO,
    "branch_status": EscalationLevel.SILENT,
}

# Maximum number of unread messages before oldest are dropped
_MAX_UNREAD = 200


@dataclass
class LiaisonState:
    """Lightweight state maintained by the liaison."""
    node_id: str
    pending_reviews: List[Dict[str, Any]] = field(default_factory=list)
    active_rebuttals: List[Dict[str, Any]] = field(default_factory=list)
    unread_messages: List[Dict[str, Any]] = field(default_factory=list)
    last_heartbeat: float = 0.0
    coding_agent_session: Optional[str] = None

    def summary(self) -> str:
        """One-line status for systemMessage injection."""
        parts: List[str] = []
        if self.pending_reviews:
            parts.append(f"{len(self.pending_reviews)} review(s) pending")
        if self.active_rebuttals:
            parts.append(f"{len(self.active_rebuttals)} rebuttal(s) active")
        if self.unread_messages:
            parts.append(f"{len(self.unread_messages)} unread")
        return " | ".join(parts) if parts else "Fleet quiet"


class FleetLiaison:
    """Main liaison class — processes messages and decides escalation."""

    def __init__(self, node_id: str, daemon_url: str = "http://127.0.0.1:8855"):
        self.node_id = node_id
        self.daemon_url = daemon_url
        self.state = LiaisonState(node_id=node_id)
        self._escalation_callbacks: Dict[EscalationLevel, Callable] = {}
        self._event_bus: Any = None

    def register_callback(self, level: EscalationLevel, fn: Callable) -> None:
        """Register callback for escalation level."""
        self._escalation_callbacks[level] = fn

    def process_message(self, msg: dict) -> EscalationLevel:
        """Process an incoming fleet message. Returns escalation level.

        Steps:
        1. Classify message type -> default escalation
        2. Apply override rules (blocking critique -> CRITICAL)
        3. Update state (pending reviews, rebuttals, unread queue)
        4. Fire registered callback for the escalation level
        5. Publish event to EventBus
        """
        msg_type = msg.get("type", "context")
        level = ESCALATION_MAP.get(msg_type, EscalationLevel.INFO)

        # Override: blocking critique escalates to CRITICAL
        if msg_type == "critique":
            payload = msg.get("payload", {})
            if payload.get("blocking_concerns"):
                level = EscalationLevel.CRITICAL

        # Update state based on message type
        if msg_type in ("critique", "chief_decision", "task_brief"):
            self.state.pending_reviews.append(msg)

        if msg_type == "local_verdict":
            task_id = msg.get("payload", {}).get("task_id")
            if task_id:
                existing_ids = [r.get("task_id") for r in self.state.active_rebuttals]
                if task_id not in existing_ids:
                    self.state.active_rebuttals.append({
                        "task_id": task_id,
                        "received_at": time.time(),
                    })

        # Track heartbeats
        if msg_type == "heartbeat":
            self.state.last_heartbeat = time.time()

        # Add to unread queue
        self.state.unread_messages.append({
            "type": msg_type,
            "from": msg.get("from", "unknown"),
            "summary": self._extract_summary(msg),
            "escalation": level.value,
            "received_at": time.time(),
        })

        # Cap unread queue to prevent unbounded growth
        if len(self.state.unread_messages) > _MAX_UNREAD:
            self.state.unread_messages = self.state.unread_messages[-_MAX_UNREAD:]

        # Fire callback if registered
        cb = self._escalation_callbacks.get(level)
        if cb:
            try:
                cb(msg, level)
            except Exception as e:
                logger.warning("Escalation callback failed: %s", e)

        # Publish to EventBus
        self._publish_event(msg_type, msg)

        return level

    def get_batch_for_injection(self) -> Optional[str]:
        """Get accumulated messages for next systemMessage injection.

        Returns formatted markdown string or None if nothing to report.
        Clears the unread queue after generating the batch.
        """
        if not self.state.unread_messages:
            return None

        lines: List[str] = ["## Fleet Update"]

        # Group by escalation level for readability
        by_level: Dict[str, List[Dict[str, Any]]] = {}
        for msg in self.state.unread_messages:
            lvl = msg.get("escalation", "info")
            by_level.setdefault(lvl, []).append(msg)

        # Render in priority order: critical > urgent > info > silent
        for lvl_value in ("critical", "urgent", "info", "silent"):
            msgs = by_level.get(lvl_value, [])
            if not msgs:
                continue
            lines.append(f"\n### [{lvl_value.upper()}]")
            for m in msgs:
                sender = m.get("from", "?")
                summary = m.get("summary", "no summary")
                lines.append(f"- **{sender}** ({m.get('type', '?')}): {summary}")

        # Append state summary
        state_summary = self.state.summary()
        if state_summary != "Fleet quiet":
            lines.append(f"\n**State:** {state_summary}")

        # Clear the queue
        self.state.unread_messages.clear()

        return "\n".join(lines)

    def mark_read(self, count: Optional[int] = None) -> None:
        """Mark messages as read (clear from unread queue).

        If count is None, clears all. Otherwise clears the oldest `count` messages.
        """
        if count is None:
            self.state.unread_messages.clear()
        else:
            self.state.unread_messages = self.state.unread_messages[count:]

    def _extract_summary(self, msg: dict) -> str:
        """Extract a one-line summary from a message, truncated to 100 chars."""
        payload = msg.get("payload", {})

        # Try common summary fields in order
        for key in ("subject", "summary", "body", "message"):
            val = payload.get(key)
            if val and isinstance(val, str):
                return val[:100]

        # For dicts without string summary, try to extract from nested body
        if isinstance(payload.get("body"), dict):
            nested = payload["body"]
            for key in ("summary", "subject", "title"):
                val = nested.get(key)
                if val and isinstance(val, str):
                    return val[:100]

        # Fallback: message type
        msg_type = msg.get("type", "unknown")
        sender = msg.get("from", "unknown")
        return f"{msg_type} from {sender}"[:100]

    def _publish_event(self, event_type: str, msg: dict) -> None:
        """Publish to EventBus if available. Graceful no-op if unavailable."""
        if self._event_bus is not None:
            try:
                self._event_bus.emit(
                    f"liaison.message.{event_type}",
                    {
                        "from": msg.get("from", "unknown"),
                        "type": event_type,
                        "summary": self._extract_summary(msg),
                    },
                )
            except Exception as e:
                logger.debug("EventBus publish failed (non-fatal): %s", e)
            return

        # Try lazy import of FleetEventBus
        try:
            from multifleet.eventbus import FleetEventBus
            self._event_bus = FleetEventBus(node_id=self.node_id)
            self._event_bus.emit(
                f"liaison.message.{event_type}",
                {
                    "from": msg.get("from", "unknown"),
                    "type": event_type,
                    "summary": self._extract_summary(msg),
                },
            )
        except Exception:
            # EventBus not available — silently degrade
            self._event_bus = _NullEventBus()

    def to_dict(self) -> dict:
        """Serialize state for persistence / MCP tool response."""
        return {
            "node_id": self.state.node_id,
            "pending_reviews": len(self.state.pending_reviews),
            "active_rebuttals": len(self.state.active_rebuttals),
            "unread_count": len(self.state.unread_messages),
            "last_heartbeat": self.state.last_heartbeat,
            "coding_agent_session": self.state.coding_agent_session,
            "summary": self.state.summary(),
        }


class _NullEventBus:
    """Sentinel that silently drops all emit() calls."""

    def emit(self, *args: Any, **kwargs: Any) -> bool:
        return False


# ── Factory ──

def create_liaison(node_id: Optional[str] = None) -> FleetLiaison:
    """Factory function — auto-detects node ID from environment."""
    if node_id is None:
        node_id = os.environ.get("MULTIFLEET_NODE_ID", "unknown")
    return FleetLiaison(node_id=node_id)
