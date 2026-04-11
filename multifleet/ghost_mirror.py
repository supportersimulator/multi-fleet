"""Ghost Mirror -- keeps documentation truthful by detecting code<->docs drift.

Wraps the mirror_drift probe with auto-fix suggestions. Runs periodically
to ensure docs (CLAUDE.md, README.md, ARCHITECTURE.md) reflect actual code.
Drift is recorded to the evidence ledger so it accumulates signal over time.
"""

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("fleet-nerve.ghost-mirror")


@dataclass
class MirrorResult:
    """Result of a mirror drift scan."""

    docs_checked: int = 0
    drift_items: list[dict] = field(default_factory=list)
    fix_suggestions: list[str] = field(default_factory=list)
    duration_ms: float = 0.0
    timestamp: str = ""

    def to_dict(self) -> dict:
        return {
            "docs_checked": self.docs_checked,
            "drift_items": self.drift_items,
            "fix_suggestions": self.fix_suggestions,
            "duration_ms": round(self.duration_ms, 1),
            "timestamp": self.timestamp,
        }

    def summary(self) -> str:
        if not self.drift_items:
            return "no drift"
        return f"{len(self.drift_items)} drift items, {len(self.fix_suggestions)} suggestions"


class GhostMirror:
    """Detects documentation drift and generates fix suggestions.

    Uses the mirror_drift probe under the hood, then enriches findings
    with actionable suggestions and records to the evidence ledger.
    """

    def __init__(self, repo_root: str = ".", node_id: str = "unknown"):
        self._repo_root = Path(repo_root).resolve()
        self._node_id = node_id

    def scan(self) -> MirrorResult:
        """Run mirror drift detection and generate fix suggestions."""
        t0 = time.monotonic()
        result = MirrorResult(
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )

        # Run the mirror_drift probe
        probe_result = self._run_probe()
        if probe_result is None:
            result.duration_ms = (time.monotonic() - t0) * 1000
            return result

        result.docs_checked = len(self._get_doc_paths())

        # Extract drift items from probe findings
        for finding in probe_result.get("findings", []):
            title = finding.get("title", "")
            evidence = finding.get("evidence", [])
            file_paths = finding.get("file_paths", [])

            for item in evidence:
                drift_entry = {
                    "doc": title,
                    "missing_ref": str(item),
                    "file_paths": file_paths[:3],
                }
                result.drift_items.append(drift_entry)
                result.fix_suggestions.append(
                    self._suggest_fix(str(item), file_paths)
                )

        # Record to evidence ledger
        if result.drift_items:
            self._record_drift(result)

        # Emit event
        self._emit_event(result)

        result.duration_ms = (time.monotonic() - t0) * 1000
        logger.info("ghost_mirror: %s (%.0fms)", result.summary(), result.duration_ms)
        return result

    def _run_probe(self) -> Optional[dict]:
        """Run mirror_drift probe and return its result as dict."""
        try:
            from multifleet.probes.mirror_drift import probe_mirror_drift

            probe_result = probe_mirror_drift({"repo_root": str(self._repo_root)})
            return {
                "status": probe_result.status,
                "findings": [
                    {
                        "title": f.title,
                        "evidence": f.evidence,
                        "file_paths": f.file_paths,
                    }
                    for f in probe_result.findings
                ],
            }
        except Exception as e:
            logger.warning("mirror_drift probe failed: %s", e)
            return None

    def _get_doc_paths(self) -> list[Path]:
        """Return doc files to check."""
        candidates = ["CLAUDE.md", "README.md", "ARCHITECTURE.md"]
        return [self._repo_root / c for c in candidates if (self._repo_root / c).exists()]

    def _suggest_fix(self, missing_ref: str, file_paths: list[str]) -> str:
        """Generate a fix suggestion for a missing reference."""
        ref = missing_ref.replace("Missing: ", "")
        doc = file_paths[0] if file_paths else "unknown doc"
        doc_name = Path(doc).name if doc != "unknown doc" else doc
        return f"Update {doc_name}: remove or fix reference to '{ref}'"

    def _record_drift(self, result: MirrorResult) -> None:
        """Record drift findings to the evidence ledger."""
        try:
            from multifleet.evidence_ledger import EvidenceLedger

            ledger = EvidenceLedger()
            ledger.record(
                event_type="ghost.mirror.drift_detected",
                node_id=self._node_id,
                subject=f"{len(result.drift_items)} doc drift items",
                payload={
                    "drift_count": len(result.drift_items),
                    "drift_items": result.drift_items[:10],
                    "suggestions": result.fix_suggestions[:10],
                },
            )
        except Exception as e:
            logger.debug("Evidence ledger write failed: %s", e)

    def _emit_event(self, result: MirrorResult) -> None:
        """Emit ghost mirror event."""
        try:
            from multifleet.event_emitter import emit_typed
            from multifleet.event_schema import EventSource

            emit_typed(
                event_type="ghost.mirror.scanned",
                source=EventSource.GHOST,
                payload={
                    "docs_checked": result.docs_checked,
                    "drift_items": len(result.drift_items),
                    "suggestions": len(result.fix_suggestions),
                },
            )
        except (ImportError, Exception):
            pass


def run_mirror(repo_root: str = ".", **kwargs) -> dict:
    """Convenience function to run a mirror scan."""
    mirror = GhostMirror(repo_root=repo_root, **kwargs)
    return mirror.scan().to_dict()
