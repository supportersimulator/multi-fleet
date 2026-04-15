"""Surgeon LAN Relay — 3s-LAN/v1 structured packet exchange between machines.

Bridges the gap between local 3-Surgeon protocol and cross-machine coordination.
Only HEAD SURGEONS and the CHIEF are allowed to communicate cross-machine.
Local surgeons talk only inside their machine.

Protocol: "3s-lan/v1"
Transport: NATS (P1), HTTP (P2), or P7 git (.fleet-messages/)

Packet types:
  task_brief      — Chief -> machine: work assignment with approach
  branch_status   — Machine -> all: current branch/workspace state
  local_verdict   — Machine -> chief: synthesized local verdict
  review_request  — Chief -> machine: ask to critique another machine
  critique        — Machine -> target: cross-machine critique
  rebuttal_request — Chief -> machine: ask for rebuttal to critique
  chief_decision  — Chief -> all: final merge/cherry-pick/reject decision
  heartbeat       — Machine -> all: periodic health pulse

Design principle: "3 machines, 3 surgeon cells, but only heads + chief
talk cross-machine. 27 agents chattering freely = chaos."

Integrates with:
- nats_client.py for P1 delivery
- liaison.py for escalation routing
- coordinator_agent.py for local dispatch
- packets.py FleetPacket for envelope wrapping
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("fleet-nerve.surgeon-relay")

PROTOCOL_VERSION = "3s-lan/v1"

# Maximum age (seconds) before a packet is considered stale
PACKET_MAX_AGE_S = 3600  # 1 hour


# ── Packet schema ───────────────────────────────────────────────────────────


@dataclass
class SurgeonPacket:
    """Structured packet for cross-machine surgeon communication.

    The payload is intentionally simple and schema-bound.
    No freeform chat — only structured data that the receiving
    head surgeon or chief can act on deterministically.
    """
    protocol: str = PROTOCOL_VERSION
    type: str = ""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    from_node: str = ""
    to_node: str = "all"
    timestamp: float = field(default_factory=time.time)
    task_id: str = ""
    payload: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "protocol": self.protocol,
            "type": self.type,
            "id": self.id,
            "from_node": self.from_node,
            "to_node": self.to_node,
            "timestamp": self.timestamp,
            "task_id": self.task_id,
            "payload": self.payload,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: dict) -> SurgeonPacket:
        return cls(
            protocol=data.get("protocol", PROTOCOL_VERSION),
            type=data.get("type", ""),
            id=data.get("id", str(uuid.uuid4())),
            from_node=data.get("from_node", ""),
            to_node=data.get("to_node", "all"),
            timestamp=data.get("timestamp", time.time()),
            task_id=data.get("task_id", ""),
            payload=data.get("payload", {}),
        )

    @classmethod
    def from_json(cls, raw: str | bytes) -> SurgeonPacket:
        data = json.loads(raw)
        return cls.from_dict(data)

    @property
    def is_stale(self) -> bool:
        return (time.time() - self.timestamp) > PACKET_MAX_AGE_S

    def validate(self) -> list[str]:
        """Validate packet. Returns list of errors (empty = valid)."""
        errors = []
        if self.protocol != PROTOCOL_VERSION:
            errors.append(f"wrong protocol: {self.protocol}")
        if not self.type:
            errors.append("missing type")
        if not self.from_node:
            errors.append("missing from_node")
        if self.type not in VALID_PACKET_TYPES:
            errors.append(f"unknown type: {self.type}")
        return errors


VALID_PACKET_TYPES = {
    "task_brief",
    "branch_status",
    "local_verdict",
    "review_request",
    "critique",
    "rebuttal_request",
    "chief_decision",
    "heartbeat",
    "node_capabilities",
}


# ── Packet builders ─────────────────────────────────────────────────────────


def build_task_brief(
    from_node: str,
    to_node: str,
    task_id: str,
    task: str,
    approach: str,
    machines: list[str],
) -> SurgeonPacket:
    """Chief -> machine: assign work with approach strategy."""
    return SurgeonPacket(
        type="task_brief",
        from_node=from_node,
        to_node=to_node,
        task_id=task_id,
        payload={
            "task": task,
            "approach": approach,
            "machines": machines,
        },
    )


def build_branch_status(
    from_node: str,
    branch: str,
    summary: str,
    files_changed: list[str],
    state: str = "in_progress",  # in_progress | ready_for_review | merged
    task_id: str = "",
) -> SurgeonPacket:
    """Machine -> all: current branch/workspace state."""
    return SurgeonPacket(
        type="branch_status",
        from_node=from_node,
        task_id=task_id,
        payload={
            "branch": branch,
            "summary": summary,
            "files_changed": files_changed,
            "state": state,
        },
    )


def build_local_verdict(
    from_node: str,
    task_id: str,
    summary: str,
    branch: str,
    confidence: float,
    files_touched: list[str] = None,
    risks: list[str] = None,
    dissent: list[dict] = None,
) -> SurgeonPacket:
    """Machine -> chief: synthesized local verdict."""
    return SurgeonPacket(
        type="local_verdict",
        from_node=from_node,
        to_node="chief",
        task_id=task_id,
        payload={
            "summary": summary,
            "branch": branch,
            "confidence": round(confidence, 4),
            "files_touched": files_touched or [],
            "risks": risks or [],
            "dissent": dissent or [],
            "phase": "close",
        },
    )


def build_review_request(
    from_node: str,
    reviewer_node: str,
    task_id: str,
    target_node: str,
    target_branch: str,
    focus: str = "",
) -> SurgeonPacket:
    """Chief -> machine: request critique of another machine's work."""
    return SurgeonPacket(
        type="review_request",
        from_node=from_node,
        to_node=reviewer_node,
        task_id=task_id,
        payload={
            "target_node": target_node,
            "target_branch": target_branch,
            "focus": focus,
        },
    )


def build_critique(
    from_node: str,
    target_node: str,
    task_id: str,
    summary: str,
    confidence: float,
    blocking_concerns: list[str] = None,
    suggested_action: str = "",
) -> SurgeonPacket:
    """Machine -> target: cross-machine critique."""
    return SurgeonPacket(
        type="critique",
        from_node=from_node,
        to_node=target_node,
        task_id=task_id,
        payload={
            "summary": summary,
            "confidence": round(confidence, 4),
            "blocking_concerns": blocking_concerns or [],
            "suggested_action": suggested_action,
            "target_node": target_node,
        },
    )


def build_chief_decision(
    from_node: str,
    task_id: str,
    decision: str,
    winner_branch: str = "",
    confidence: float = 0.0,
    reasoning: str = "",
    cherry_pick_from: list[dict] = None,
    followups: list[str] = None,
    dissent_preserved: list[dict] = None,
    memory_reasons: list[str] = None,
) -> SurgeonPacket:
    """Chief -> all: final merge/cherry-pick/reject decision."""
    return SurgeonPacket(
        type="chief_decision",
        from_node=from_node,
        task_id=task_id,
        payload={
            "decision": decision,
            "winner_branch": winner_branch,
            "confidence": round(confidence, 4),
            "reasoning": reasoning,
            "cherry_pick_from": cherry_pick_from or [],
            "followups": followups or [],
            "dissent_preserved": dissent_preserved or [],
            "memory_reasons": memory_reasons or [],
        },
    )


# ── Relay ───────────────────────────────────────────────────────────────────


class SurgeonLANRelay:
    """Relay for cross-machine surgeon packets.

    Routes packets through available transports:
    1. NATS (P1) — instant, best option
    2. HTTP daemon (P2) — direct delivery
    3. P7 git (.fleet-messages/) — guaranteed async

    Also maintains a dedup cache and delivery log for audit.
    """

    def __init__(
        self,
        node_id: str = "",
        nats_client: Any = None,
        daemon_url: str = "http://127.0.0.1:8855",
        fleet_messages_root: Optional[Path] = None,
    ):
        self.node_id = node_id or os.environ.get("MULTIFLEET_NODE_ID", "unknown")
        self._nats = nats_client
        self._daemon_url = daemon_url
        self._fleet_msg_root = fleet_messages_root

        # Dedup: set of packet IDs we've already processed
        self._seen: set[str] = set()
        self._max_seen = 10000

        # Delivery log
        self._sent: list[dict] = []
        self._received: list[dict] = []
        self._max_log = 500

        # Handlers for incoming packets
        self._handlers: dict[str, list[Callable]] = {}

        # Stats
        self._stats = {
            "sent": 0,
            "received": 0,
            "deduped": 0,
            "stale_dropped": 0,
            "errors": 0,
        }

    # ── Send ────────────────────────────────────────────────────────────

    def send(self, packet: SurgeonPacket) -> bool:
        """Send a surgeon packet to its destination.

        Tries transports in priority order: NATS -> HTTP -> P7 git.
        Returns True if delivery succeeded on any channel.
        """
        errors = packet.validate()
        if errors:
            logger.warning("Invalid packet: %s", errors)
            self._stats["errors"] += 1
            return False

        # Try NATS (P1)
        if self._try_nats(packet):
            self._log_sent(packet, "nats")
            return True

        # Try HTTP daemon (P2)
        if self._try_http(packet):
            self._log_sent(packet, "http")
            return True

        # Fallback to P7 git
        if self._try_p7(packet):
            self._log_sent(packet, "p7-git")
            return True

        logger.warning("All transports failed for packet %s", packet.id[:8])
        self._stats["errors"] += 1
        return False

    def _try_nats(self, packet: SurgeonPacket) -> bool:
        """Attempt NATS delivery."""
        if self._nats is None:
            return False
        try:
            subject = f"fleet.surgeon.{packet.to_node}.{packet.type}"
            # nats_client expects publish(subject, data_bytes)
            if hasattr(self._nats, "publish_sync"):
                self._nats.publish_sync(subject, packet.to_json().encode())
            else:
                # Async client — best-effort sync attempt
                import asyncio
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(
                        self._nats.publish(subject, packet.to_json().encode())
                    )
                except RuntimeError:
                    # No running loop — skip
                    return False
            return True
        except Exception as e:
            logger.debug("NATS send failed: %s", e)
            return False

    def _try_http(self, packet: SurgeonPacket) -> bool:
        """Attempt HTTP daemon delivery."""
        import urllib.request
        import urllib.error

        try:
            data = json.dumps({
                "type": "surgeon_relay",
                "to": packet.to_node,
                "payload": packet.to_dict(),
            }).encode()
            req = urllib.request.Request(
                f"{self._daemon_url}/message",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status == 200
        except Exception as e:
            logger.debug("HTTP send failed: %s", e)
            return False

    def _try_p7(self, packet: SurgeonPacket) -> bool:
        """Attempt P7 git (.fleet-messages/) delivery."""
        if self._fleet_msg_root is None:
            return False
        try:
            target = packet.to_node if packet.to_node != "all" else "all"
            msg_dir = self._fleet_msg_root / target
            msg_dir.mkdir(parents=True, exist_ok=True)

            ts = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
            filename = f"{ts}-surgeon-{packet.type}-{packet.id[:8]}.json"
            (msg_dir / filename).write_text(
                json.dumps(packet.to_dict(), indent=2)
            )
            return True
        except Exception as e:
            logger.debug("P7 write failed: %s", e)
            return False

    def _log_sent(self, packet: SurgeonPacket, transport: str) -> None:
        self._stats["sent"] += 1
        entry = {
            "id": packet.id[:8],
            "type": packet.type,
            "to": packet.to_node,
            "transport": transport,
            "timestamp": time.time(),
        }
        self._sent.append(entry)
        if len(self._sent) > self._max_log:
            self._sent = self._sent[-self._max_log:]

    # ── Receive ─────────────────────────────────────────────────────────

    def receive(self, raw: dict | str | bytes) -> Optional[SurgeonPacket]:
        """Process an incoming surgeon packet.

        Returns the parsed packet if valid and new, None if deduped/stale/invalid.
        Also dispatches to registered handlers.
        """
        # Parse
        if isinstance(raw, (str, bytes)):
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                self._stats["errors"] += 1
                return None
        else:
            data = raw

        packet = SurgeonPacket.from_dict(data)

        # Validate
        errors = packet.validate()
        if errors:
            logger.debug("Invalid incoming packet: %s", errors)
            self._stats["errors"] += 1
            return None

        # Dedup
        if packet.id in self._seen:
            self._stats["deduped"] += 1
            return None
        self._seen.add(packet.id)
        if len(self._seen) > self._max_seen:
            # Trim oldest half
            excess = list(self._seen)[:self._max_seen // 2]
            self._seen -= set(excess)

        # Stale check
        if packet.is_stale:
            self._stats["stale_dropped"] += 1
            logger.debug("Dropping stale packet %s (age=%.0fs)", packet.id[:8],
                         time.time() - packet.timestamp)
            return None

        # Log
        self._stats["received"] += 1
        self._received.append({
            "id": packet.id[:8],
            "type": packet.type,
            "from": packet.from_node,
            "timestamp": time.time(),
        })
        if len(self._received) > self._max_log:
            self._received = self._received[-self._max_log:]

        # Dispatch to handlers
        handlers = self._handlers.get(packet.type, [])
        for handler in handlers:
            try:
                handler(packet)
            except Exception as e:
                logger.error("Handler for %s failed: %s", packet.type, e)

        return packet

    def on(self, packet_type: str, handler: Callable) -> None:
        """Register a handler for a specific packet type."""
        self._handlers.setdefault(packet_type, []).append(handler)

    # ── Status ──────────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        return dict(self._stats)

    def get_recent_sent(self, limit: int = 20) -> list[dict]:
        return self._sent[-limit:]

    def get_recent_received(self, limit: int = 20) -> list[dict]:
        return self._received[-limit:]


# ── Convenience: wire relay into coordinator agent ──────────────────────────


def create_relay_for_node(
    node_id: str,
    nats_client: Any = None,
    superrepo_root: Optional[Path] = None,
) -> SurgeonLANRelay:
    """Create a SurgeonLANRelay wired for a specific node.

    Automatically discovers fleet-messages directory and daemon URL.
    """
    fleet_msg_root = None
    if superrepo_root:
        fleet_msg_root = superrepo_root / ".fleet-messages"
    else:
        # Try to find superrepo root
        try:
            from multifleet._paths import get_superrepo_root
            sr = get_superrepo_root()
            fleet_msg_root = sr / ".fleet-messages"
        except (ImportError, Exception):
            pass

    return SurgeonLANRelay(
        node_id=node_id,
        nats_client=nats_client,
        fleet_messages_root=fleet_msg_root,
    )
