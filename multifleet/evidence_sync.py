"""Cross-Node Evidence Sync — shares evidence ledger entries across fleet nodes.

The evidence ledger records locally. This module exports recent entries as
portable packets and imports received packets, enabling cross-node evidence
synthesis without requiring shared storage.

Transport: uses the fleet daemon's /message endpoint (P2) or EventBus broadcast.
Falls back to P7 git channel via .fleet-messages/ for async delivery.

Usage:
    from multifleet.evidence_sync import EvidenceSync
    sync = EvidenceSync(ledger, node_id="mac2")
    # Export recent entries for other nodes
    packet = sync.export_recent(since_hours=1)
    # Import entries received from another node
    imported = sync.import_packet(packet)
"""

import json
import logging
import os
import time
import urllib.request
from pathlib import Path
from typing import Optional

from multifleet.evidence_ledger import EvidenceLedger

logger = logging.getLogger("fleet-nerve.evidence_sync")

DAEMON_PORT = int(os.environ.get("FLEET_DAEMON_PORT", "8855"))
FLEET_MESSAGES_DIR = Path(os.environ.get(
    "FLEET_MESSAGES_DIR",
    str(Path(os.environ.get("MULTIFLEET_REPO_ROOT",
        str(Path(__file__).resolve().parent.parent))) / ".fleet-messages"),
))


class EvidenceSync:
    """Synchronizes evidence ledger entries across fleet nodes."""

    def __init__(self, ledger: EvidenceLedger = None, node_id: str = ""):
        self._ledger = ledger or EvidenceLedger()
        self._node_id = node_id or os.environ.get("MULTIFLEET_NODE_ID", "unknown")
        self._stats = {
            "exported": 0,
            "imported": 0,
            "duplicates_skipped": 0,
            "send_successes": 0,
            "send_failures": 0,
        }

    def export_recent(self, since_hours: float = 1, limit: int = 50) -> dict:
        """Export recent evidence entries as a portable packet.

        Returns a dict that can be JSON-serialized and sent to other nodes.
        """
        since_ts = time.time() - (since_hours * 3600)
        entries = self._ledger.query(since=since_ts, limit=limit)

        # Strip internal fields, keep evidence content
        portable = []
        for e in entries:
            portable.append({
                "entry_id": e["entry_id"],
                "timestamp": e["timestamp"],
                "event_type": e["event_type"],
                "node_id": e["node_id"],
                "subject": e.get("subject", ""),
                "payload": e["payload"],
                "entry_hash": e["entry_hash"],
            })

        self._stats["exported"] += len(portable)
        return {
            "type": "evidence_sync",
            "from_node": self._node_id,
            "exported_at": time.time(),
            "since": since_ts,
            "entries": portable,
        }

    def import_packet(self, packet: dict) -> dict:
        """Import evidence entries from another node's export packet.

        Entries are recorded as remote evidence with the original node_id.
        Duplicates (same entry_id) are skipped.

        Returns summary of what was imported.
        """
        entries = packet.get("entries", [])
        from_node = packet.get("from_node", "unknown")
        imported = 0
        skipped = 0

        for entry in entries:
            entry_id = entry.get("entry_id", "")

            # Check for duplicate by querying existing entries
            existing = self._ledger.query(
                event_type=entry.get("event_type"),
                node_id=entry.get("node_id"),
                limit=100,
            )
            is_dup = any(e["entry_id"] == entry_id for e in existing)
            if is_dup:
                skipped += 1
                self._stats["duplicates_skipped"] += 1
                continue

            # Record as remote evidence
            try:
                self._ledger.record(
                    event_type=f"remote_{entry.get('event_type', 'unknown')}",
                    node_id=entry.get("node_id", from_node),
                    subject=f"[sync:{from_node}] {entry.get('subject', '')}",
                    payload={
                        "original_entry_id": entry_id,
                        "original_hash": entry.get("entry_hash", ""),
                        "synced_from": from_node,
                        "original_timestamp": entry.get("timestamp"),
                        **(entry.get("payload", {}) if isinstance(entry.get("payload"), dict) else {}),
                    },
                )
                imported += 1
                self._stats["imported"] += 1
            except Exception as e:
                logger.warning("evidence_import_error entry=%s: %s", entry_id[:8], e)

        logger.info(
            "evidence_sync imported=%d skipped=%d from=%s",
            imported, skipped, from_node,
        )
        return {
            "imported": imported,
            "skipped": skipped,
            "from_node": from_node,
            "total_in_packet": len(entries),
        }

    def send_to_node(self, target_node: str, since_hours: float = 1) -> bool:
        """Export and send evidence to a specific node via daemon HTTP.

        Falls back to P7 git channel if daemon is unreachable.
        Returns True if sent successfully via any channel.
        """
        packet = self.export_recent(since_hours=since_hours)
        if not packet["entries"]:
            logger.debug("evidence_sync no entries to send to %s", target_node)
            return True  # Nothing to send is success

        # Try P2: HTTP direct to daemon
        try:
            body = json.dumps({
                "type": "evidence_sync",
                "to": target_node,
                "payload": packet,
            }).encode("utf-8")
            req = urllib.request.Request(
                f"http://127.0.0.1:{DAEMON_PORT}/message",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            resp = urllib.request.urlopen(req, timeout=5)
            if 200 <= resp.status < 300:
                self._stats["send_successes"] += 1
                logger.info("evidence_sync sent %d entries to %s via P2",
                            len(packet["entries"]), target_node)
                return True
        except Exception as e:
            logger.debug("evidence_sync P2 failed for %s: %s", target_node, e)

        # Fallback P7: write to .fleet-messages/
        return self._send_via_git(target_node, packet)

    def _send_via_git(self, target_node: str, packet: dict) -> bool:
        """Send evidence packet via P7 git channel."""
        target_dir = FLEET_MESSAGES_DIR / target_node
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            filename = f"evidence-sync-{self._node_id}-{int(time.time())}.json"
            filepath = target_dir / filename
            filepath.write_text(json.dumps(packet, indent=2))
            self._stats["send_successes"] += 1
            logger.info("evidence_sync wrote %d entries to P7: %s",
                        len(packet["entries"]), filepath)
            return True
        except Exception as e:
            logger.warning("evidence_sync P7 failed for %s: %s", target_node, e)
            self._stats["send_failures"] += 1
            return False

    def broadcast(self, since_hours: float = 1, targets: list[str] = None) -> dict:
        """Send evidence to all fleet nodes (or specified targets).

        Returns dict of {node: success_bool}.
        """
        targets = targets or ["mac1", "mac2", "mac3"]
        targets = [t for t in targets if t != self._node_id]
        results = {}
        for target in targets:
            results[target] = self.send_to_node(target, since_hours=since_hours)
        return results

    @property
    def stats(self) -> dict:
        return dict(self._stats)
