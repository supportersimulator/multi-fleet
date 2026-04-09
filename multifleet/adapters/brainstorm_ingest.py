"""Superpowers Brainstorm Ingest Adapter — normalize brainstorm output to packet.

Converts Superpowers brainstorming skill output into a BrainstormPacket
that 3-Surgeons can adjudicate. This is the first link in the chain:

  Superpowers brainstorm -> BrainstormPacket -> SurgeonDecisionPacket -> VisionContract

"Superpowers generates the possibility space. 3-Surgeons collapses it
into an operational decision."
"""

import logging
import os
import re
import time
from typing import Optional

from multifleet.vision_packets import BrainstormPacket, BrainstormOption

logger = logging.getLogger("fleet-nerve.adapter.brainstorm-ingest")


class BrainstormIngestAdapter:
    """Converts various brainstorm formats into BrainstormPackets.

    Supports:
    - Structured dict with options list
    - Superpowers brainstorm markdown output
    - Free-form text with A/B/C options

    Usage:
        adapter = BrainstormIngestAdapter()
        packet = adapter.ingest({
            "task_id": "fix-auth",
            "options": [
                {"title": "Option A", "description": "...", "pros": [...], "cons": [...]},
                {"title": "Option B", "description": "...", "pros": [...], "cons": [...]},
            ],
            "recommended": "opt-a-id",
        })
    """

    def __init__(self, node_id: str = ""):
        self._node_id = node_id or os.environ.get("MULTIFLEET_NODE_ID", "unknown")
        self._stats = {"ingested": 0, "from_markdown": 0, "from_dict": 0, "errors": 0}

    def ingest(self, source: dict | str) -> BrainstormPacket:
        """Ingest brainstorm output into a BrainstormPacket.

        Args:
            source: Either a dict with structured options, or a markdown string.

        Returns:
            BrainstormPacket ready for surgeon adjudication.
        """
        if isinstance(source, str):
            return self._from_markdown(source)
        return self._from_dict(source)

    def _from_dict(self, data: dict) -> BrainstormPacket:
        """Convert structured dict to BrainstormPacket."""
        packet = BrainstormPacket(
            task_id=data.get("task_id", ""),
            node_id=self._node_id,
            recommended=data.get("recommended", ""),
        )

        for opt_data in data.get("options", []):
            if isinstance(opt_data, dict):
                packet.add_option(
                    title=opt_data.get("title", "Untitled"),
                    description=opt_data.get("description", ""),
                    pros=opt_data.get("pros", []),
                    cons=opt_data.get("cons", []),
                    confidence=opt_data.get("confidence", 0.5),
                )
            elif isinstance(opt_data, BrainstormOption):
                packet.options.append(opt_data)

        # Auto-set recommended to first option if not specified
        if not packet.recommended and packet.options:
            packet.recommended = packet.options[0].id

        self._stats["ingested"] += 1
        self._stats["from_dict"] += 1
        logger.info(
            "brainstorm ingested from dict: task=%s options=%d",
            packet.task_id[:8], len(packet.options),
        )
        return packet

    def _from_markdown(self, text: str) -> BrainstormPacket:
        """Parse brainstorm markdown output into BrainstormPacket.

        Expected format (flexible):
            ## Option A: Title
            Description text...
            **Pros:** item1, item2
            **Cons:** item1, item2

            ## Option B: Title
            ...
        """
        packet = BrainstormPacket(node_id=self._node_id)

        # Extract task_id from first line or header
        lines = text.strip().splitlines()
        if lines and lines[0].startswith("# "):
            packet.task_id = lines[0].lstrip("# ").strip()[:100]

        # Parse options (## headers)
        option_blocks = re.split(r'\n(?=##\s)', text)
        for block in option_blocks:
            block = block.strip()
            if not block.startswith("##"):
                continue

            # Extract title
            header_match = re.match(r'##\s+(?:Option\s+\w+:?\s*)?(.+)', block)
            if not header_match:
                continue
            title = header_match.group(1).strip()

            # Extract description (text before Pros/Cons)
            desc_match = re.search(
                r'\n(.*?)(?=\*\*Pros|Pros:|$)', block, re.DOTALL,
            )
            description = desc_match.group(1).strip() if desc_match else ""

            # Extract pros
            pros = []
            pros_match = re.search(r'\*?\*?Pros\*?\*?:?\s*(.*?)(?=\*?\*?Cons|$)', block, re.DOTALL)
            if pros_match:
                pros_text = pros_match.group(1).strip()
                pros = [p.strip().lstrip("- ") for p in pros_text.split("\n") if p.strip()]

            # Extract cons
            cons = []
            cons_match = re.search(r'\*?\*?Cons\*?\*?:?\s*(.*?)$', block, re.DOTALL)
            if cons_match:
                cons_text = cons_match.group(1).strip()
                cons = [c.strip().lstrip("- ") for c in cons_text.split("\n") if c.strip()]

            packet.add_option(
                title=title,
                description=description[:500],
                pros=pros[:10],
                cons=cons[:10],
            )

        if packet.options:
            packet.recommended = packet.options[0].id

        self._stats["ingested"] += 1
        self._stats["from_markdown"] += 1
        logger.info(
            "brainstorm ingested from markdown: options=%d",
            len(packet.options),
        )
        return packet

    def to_surgeon_request(self, packet: BrainstormPacket) -> dict:
        """Convert a BrainstormPacket into a request for 3-surgeon adjudication.

        Returns a dict suitable for SurgeonAdapter.check_consensus() or
        3-surgeons cross-examination.
        """
        options_text = []
        for i, opt in enumerate(packet.options):
            pros = ", ".join(opt.pros[:3]) if opt.pros else "none listed"
            cons = ", ".join(opt.cons[:3]) if opt.cons else "none listed"
            options_text.append(
                f"Option {chr(65 + i)}: {opt.title}\n"
                f"  {opt.description[:200]}\n"
                f"  Pros: {pros}\n"
                f"  Cons: {cons}"
            )

        return {
            "brainstorm_id": packet.id,
            "task_id": packet.task_id,
            "claim": f"Which approach should be taken for: {packet.task_id}",
            "options": "\n\n".join(options_text),
            "recommended": packet.recommended,
            "intensity": "standard",
        }

    @property
    def stats(self) -> dict:
        return dict(self._stats)


# Module-level singleton
_adapter: Optional[BrainstormIngestAdapter] = None


def get_brainstorm_ingest() -> BrainstormIngestAdapter:
    global _adapter
    if _adapter is None:
        _adapter = BrainstormIngestAdapter()
    return _adapter
