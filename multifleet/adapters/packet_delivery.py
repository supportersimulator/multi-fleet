"""ContextDNA Packet Delivery Adapter — attach memory packets to routed tasks.

Before a task is dispatched to a fleet node, this adapter enriches
the task payload with an EpistemicPacket built from ContextDNA memory
and probe findings. The receiving node gets not just instructions,
but the system's accumulated knowledge about the area being changed.

"Multi-Fleet is not just message passing. It becomes evidence-bearing dispatch."
"""

import logging
import os
import time
from typing import Optional

logger = logging.getLogger("fleet-nerve.adapter.packet-delivery")


class PacketDeliveryAdapter:
    """Enriches fleet task dispatches with epistemic packets.

    Usage:
        adapter = PacketDeliveryAdapter()
        enriched = adapter.enrich(task_payload={
            "task_id": "fix-auth",
            "summary": "Fix auth token refresh",
            "files": ["src/auth.py"],
        })
        # enriched now has "epistemic_packet" key with probe/memory context
    """

    def __init__(self, repo_root: str = ".", node_id: str = ""):
        self._repo_root = repo_root
        self._node_id = node_id or os.environ.get("MULTIFLEET_NODE_ID", "unknown")

    def enrich(
        self,
        task_payload: dict,
        run_probes: bool = True,
    ) -> dict:
        """Enrich a task payload with epistemic packet.

        Args:
            task_payload: The raw task dict with task_id, summary, files, etc.
            run_probes: Whether to run probes (False for speed-critical paths).

        Returns:
            The task payload with an added "epistemic_packet" key.
        """
        t0 = time.monotonic()
        task_id = task_payload.get("task_id", "")
        summary = task_payload.get("summary", task_payload.get("subject", ""))
        files = task_payload.get("files", task_payload.get("changed_files", []))

        try:
            from multifleet.epistemic_packet import EpistemicPacketBuilder
            builder = EpistemicPacketBuilder(
                repo_root=self._repo_root,
                node_id=self._node_id,
            )
            packet = builder.build(
                task_intent=summary,
                changed_files=files,
                branch_name=task_payload.get("branch"),
                run_probes=run_probes,
            )
            task_payload["epistemic_packet"] = packet.to_dict()
            elapsed = (time.monotonic() - t0) * 1000
            logger.info(
                "packet_delivery enriched task=%s: %s (%.0fms)",
                task_id[:8], packet.summary(), elapsed,
            )
        except Exception as e:
            logger.warning("Packet enrichment failed (non-blocking): %s", e)
            task_payload["epistemic_packet"] = None

        return task_payload

    def strip_for_size(self, task_payload: dict, max_bytes: int = 65536) -> dict:
        """Trim epistemic packet if payload exceeds max size.

        NATS messages have size limits. This strips probe details
        while preserving the summary.
        """
        import json
        raw = json.dumps(task_payload)
        if len(raw.encode()) <= max_bytes:
            return task_payload

        packet = task_payload.get("epistemic_packet")
        if not packet:
            return task_payload

        # Progressive stripping: probe_results first, then mirror_facts
        if isinstance(packet, dict):
            packet["probe_results"] = [
                {"probe_id": r.get("probe_id", "?"), "status": r.get("status", "?")}
                for r in packet.get("probe_results", [])
            ]
            packet.pop("mirror_facts", None)
            packet.pop("stale_context_warnings", None)

        return task_payload


# Module-level singleton
_adapter: Optional[PacketDeliveryAdapter] = None


def get_packet_delivery() -> PacketDeliveryAdapter:
    global _adapter
    if _adapter is None:
        _adapter = PacketDeliveryAdapter()
    return _adapter
