"""Probe Evidence Bridge -- connects probe findings to evidence ledger + EventBus.

Translates ProbeResult findings into:
1. Evidence ledger entries (tamper-evident hash chain)
2. EventBus events (fleet.scan.finding namespace)
3. Summary statistics for health endpoints
"""

import logging
import time
from typing import Optional

from multifleet.code_scanner.probe_contract import ProbeResult

logger = logging.getLogger("fleet-nerve.code_scanner.evidence_bridge")


class ProbeEvidenceBridge:
    """Bridges probe scan results into the fleet evidence + event systems."""

    def __init__(self, evidence_ledger=None, eventbus=None):
        """Initialize bridge with optional evidence ledger and eventbus.

        Args:
            evidence_ledger: EvidenceLedger instance (or None for dry-run).
            eventbus: FleetEventBus instance (or None for no event emission).
        """
        self._ledger = evidence_ledger
        self._eventbus = eventbus
        self._stats = {
            "results_processed": 0,
            "findings_recorded": 0,
            "events_emitted": 0,
            "errors": 0,
        }

    def record_results(self, results: list[ProbeResult], node_id: str = "") -> dict:
        """Record probe results in evidence ledger + emit EventBus events.

        Only records medium/high severity findings to avoid noise.
        Returns summary dict of what was recorded.
        """
        recorded = []
        emitted = []

        for result in results:
            self._stats["results_processed"] += 1

            for finding in result.findings:
                if finding.severity not in ("medium", "high"):
                    continue

                # Record in evidence ledger
                if self._ledger:
                    try:
                        entry = self._ledger.record(
                            event_type="scan_finding",
                            node_id=node_id or "unknown",
                            subject=f"[{result.probe_id}] {finding.title}",
                            payload={
                                "probe_id": result.probe_id,
                                "title": finding.title,
                                "summary": finding.summary,
                                "severity": finding.severity,
                                "confidence": finding.confidence,
                                "blast_radius": finding.blast_radius,
                                "evidence_refs": finding.evidence_refs[:5],
                                "suggested_actions": finding.suggested_actions[:3],
                                "file_path": finding.file_path,
                                "line_number": finding.line_number,
                            },
                        )
                        recorded.append(entry.get("entry_id", "unknown"))
                        self._stats["findings_recorded"] += 1
                    except Exception as e:
                        logger.error("evidence_record_error probe=%s: %s", result.probe_id, e)
                        self._stats["errors"] += 1

                # Emit EventBus event
                if self._eventbus:
                    try:
                        self._eventbus.emit(
                            event_type="scan.finding",
                            payload={
                                "probe_id": result.probe_id,
                                "title": finding.title,
                                "severity": finding.severity,
                                "confidence": finding.confidence,
                                "blast_radius": finding.blast_radius,
                                "file_path": finding.file_path,
                                "summary": finding.summary[:200],
                            },
                        )
                        emitted.append(result.probe_id)
                        self._stats["events_emitted"] += 1
                    except Exception as e:
                        logger.error("eventbus_emit_error probe=%s: %s", result.probe_id, e)
                        self._stats["errors"] += 1

        return {
            "results_count": len(results),
            "findings_recorded": len(recorded),
            "events_emitted": len(emitted),
            "node_id": node_id,
            "timestamp": time.time(),
        }

    @property
    def stats(self) -> dict:
        return dict(self._stats)
