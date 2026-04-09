"""Evidence → ContextDNA Memory Bridge — THE MOAT.

Promotes fleet evidence into ContextDNA's persistent memory system.
This is what makes the fleet's intelligence compound over time.
Without this: disposable parallel execution (commodity).
With this: institutional knowledge that improves every decision.

Promotion flow:
  evidence_ledger.db → filter(severity, time, already-promoted)
    → format as wisdom artifact
    → write to ~/.context-dna/fleet-wisdom/
    → call brain.py success/fix (if available)
    → emit evidence.promoted event via EventBus
    → mark promoted in local tracker
"""

import hashlib
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("fleet-nerve.memory-bridge")

# Severity ranking for filtering
_SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}

# Map evidence event_type → memory action
_EVENT_TYPE_MAP = {
    "merge_decision": ("success", "architecture"),
    "race_completed": ("success", "fleet-race"),
    "scan_finding": ("fix", "scan-finding"),
    "rebuttal_resolved": ("success", "design-decision"),
    "gains_gate_result": ("fix", "gains-gate"),
    "channel_repair": ("fix", "channel-repair"),
    "consensus_reached": ("success", "consensus"),
    "lease_granted": ("success", "lease"),
}


class MemoryBridge:
    """Promotes fleet evidence entries into ContextDNA memory."""

    def __init__(self, evidence_ledger=None, memory_path: str = None,
                 eventbus=None):
        self._ledger = evidence_ledger
        self._memory_path = memory_path or str(
            Path.home() / ".context-dna" / "fleet-wisdom"
        )
        Path(self._memory_path).mkdir(parents=True, exist_ok=True)
        self._eventbus = eventbus
        self._promoted_count = 0
        self._last_promotion = 0.0
        self._tracker_path = os.path.join(self._memory_path, ".promoted.json")
        self._promoted_ids: set = self._load_promoted_ids()

    # ── Tracking (which entries have been promoted) ──

    def _load_promoted_ids(self) -> set:
        """Load set of already-promoted entry IDs from disk."""
        try:
            if os.path.exists(self._tracker_path):
                data = json.loads(Path(self._tracker_path).read_text())
                return set(data.get("ids", []))
        except Exception as e:
            logger.warning("Failed to load promoted tracker: %s", e)
        return set()

    def _save_promoted_ids(self):
        """Persist promoted IDs to disk."""
        try:
            Path(self._tracker_path).write_text(json.dumps({
                "ids": list(self._promoted_ids),
                "count": len(self._promoted_ids),
                "last_saved": time.time(),
            }, indent=2))
        except Exception as e:
            logger.warning("Failed to save promoted tracker: %s", e)

    # ── Core promotion ──

    def promote_recent(self, hours: int = 24, min_severity: str = "medium") -> list:
        """Promote recent evidence entries to ContextDNA memory.

        Filters by: time window, severity, and whether already promoted.
        Returns list of promoted entries.
        """
        if not self._ledger:
            logger.warning("No evidence ledger configured — skipping promotion")
            return []

        since_ts = time.time() - (hours * 3600)
        entries = self._ledger.query(since=since_ts, limit=200)
        min_rank = _SEVERITY_RANK.get(min_severity, 1)

        promoted = []
        for entry in entries:
            entry_id = entry.get("entry_id", "")
            if entry_id in self._promoted_ids:
                continue

            # Extract severity from payload
            payload = entry.get("payload", {})
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except (json.JSONDecodeError, TypeError):
                    payload = {}

            severity = payload.get("severity", "medium")
            if _SEVERITY_RANK.get(severity, 1) < min_rank:
                continue

            if self.promote_entry(entry):
                promoted.append(entry)

        if promoted:
            self._save_promoted_ids()
            logger.info("Promoted %d evidence entries to memory", len(promoted))

        return promoted

    def promote_entry(self, entry: dict) -> bool:
        """Promote a single evidence entry to memory.

        Returns True if successfully promoted.
        """
        entry_id = entry.get("entry_id", "")
        if entry_id in self._promoted_ids:
            return False

        event_type = entry.get("event_type", "unknown")
        action, area = _EVENT_TYPE_MAP.get(event_type, ("success", "fleet"))

        payload = entry.get("payload", {})
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (json.JSONDecodeError, TypeError):
                payload = {}

        subject = entry.get("subject", "") or payload.get("subject", event_type)
        details = payload.get("details", "") or payload.get("body", "")
        if not details:
            details = json.dumps(payload, indent=2, default=str)

        # 1. Write wisdom file
        wisdom_path = self._write_wisdom_file(entry, action, area)

        # 2. Try brain.py integration
        self._call_brain(action, f"[fleet/{area}] {subject}", details)

        # 3. Try auto_capture for successes
        if action == "success":
            self._call_auto_capture(
                f"[fleet/{area}] {subject}", details, area
            )

        # 4. Emit EventBus event
        if self._eventbus:
            try:
                self._eventbus.emit("evidence.promoted", {
                    "entry_id": entry_id,
                    "event_type": event_type,
                    "subject": subject,
                    "area": area,
                    "action": action,
                    "wisdom_path": wisdom_path,
                    "node_id": entry.get("node_id", ""),
                })
            except Exception as e:
                logger.warning("EventBus emit failed: %s", e)

        # 5. Mark as promoted
        self._promoted_ids.add(entry_id)
        self._promoted_count += 1
        self._last_promotion = time.time()

        logger.info("Promoted [%s] %s → %s (%s)",
                     event_type, entry_id[:8], action, area)
        return True

    def _write_wisdom_file(self, entry: dict, action: str, area: str) -> str:
        """Write a wisdom file to the fleet-wisdom directory.

        Returns the file path.
        """
        entry_id = entry.get("entry_id", "unknown")
        ts = entry.get("timestamp", time.time())
        ts_str = time.strftime("%Y%m%d-%H%M%S", time.gmtime(ts))
        filename = f"{ts_str}_{entry.get('event_type', 'unknown')}_{entry_id[:8]}.json"
        filepath = os.path.join(self._memory_path, filename)

        payload = entry.get("payload", {})
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (json.JSONDecodeError, TypeError):
                payload = {}

        wisdom = {
            "source": "fleet-evidence",
            "entry_id": entry_id,
            "event_type": entry.get("event_type", ""),
            "node_id": entry.get("node_id", ""),
            "subject": entry.get("subject", ""),
            "action": action,
            "area": area,
            "evidence_hash": entry.get("entry_hash", ""),
            "promoted_at": time.time(),
            "original_timestamp": ts,
            "content": payload,
        }

        try:
            Path(filepath).write_text(json.dumps(wisdom, indent=2, default=str))
        except Exception as e:
            logger.error("Failed to write wisdom file %s: %s", filepath, e)
            return ""

        return filepath

    def _call_brain(self, action: str, task: str, details: str) -> bool:
        """Call ContextDNA brain.py to record the learning.

        Returns True on success.
        """
        # Find brain.py — check env override, then relative to repo, then home
        _superrepo = os.environ.get("MULTIFLEET_SUPERREPO_ROOT", "")
        brain_candidates = [
            *([] if not _superrepo else [Path(_superrepo) / "memory" / "brain.py"]),
            Path(__file__).resolve().parent.parent.parent / "memory" / "brain.py",
        ]
        brain_path = None
        for candidate in brain_candidates:
            if candidate.exists():
                brain_path = candidate
                break

        if not brain_path:
            return False

        try:
            subprocess.run(
                ["python3", str(brain_path), action, task, details],
                capture_output=True, timeout=5, check=False,
            )
            return True
        except Exception as e:
            logger.debug("brain.py call failed: %s", e)
            return False

    def _call_auto_capture(self, task: str, details: str, area: str) -> bool:
        """Call ContextDNA auto_capture to record success via subprocess.

        Uses subprocess to avoid importing heavy memory dependencies
        into the fleet process. Returns True on success.
        """
        repo_root = Path(__file__).resolve().parent.parent.parent
        script = repo_root / "memory" / "auto_capture.py"
        if not script.exists():
            return False

        try:
            code = (
                f"import sys; sys.path.insert(0, {str(repo_root)!r}); "
                f"from memory.auto_capture import capture_success; "
                f"capture_success(task={task!r}, details={details!r}, area={area!r})"
            )
            subprocess.run(
                ["python3", "-c", code],
                capture_output=True, timeout=5, check=False,
            )
            return True
        except Exception as e:
            logger.debug("auto_capture call failed: %s", e)
            return False

    # ── Stats ──

    def get_wisdom_stats(self) -> dict:
        """Stats: total promoted, by type, last promotion time."""
        wisdom_dir = Path(self._memory_path)
        files = list(wisdom_dir.glob("*.json"))
        # Exclude the tracker file
        files = [f for f in files if f.name != ".promoted.json"]

        by_type: dict = {}
        for f in files:
            try:
                data = json.loads(f.read_text())
                et = data.get("event_type", "unknown")
                by_type[et] = by_type.get(et, 0) + 1
            except Exception:
                pass

        return {
            "total_promoted": len(self._promoted_ids),
            "wisdom_files": len(files),
            "by_event_type": by_type,
            "last_promotion": self._last_promotion,
            "session_promoted": self._promoted_count,
            "memory_path": self._memory_path,
        }

    def run_promotion_cycle(self) -> dict:
        """Run a full promotion cycle — called periodically by daemon.

        Returns summary dict.
        """
        t0 = time.time()
        promoted = self.promote_recent(hours=24, min_severity="medium")
        elapsed = time.time() - t0

        result = {
            "promoted_count": len(promoted),
            "promoted_ids": [e.get("entry_id", "")[:8] for e in promoted],
            "elapsed_s": round(elapsed, 3),
            "total_promoted": len(self._promoted_ids),
        }

        if promoted:
            logger.info("Promotion cycle: %d new entries in %.1fs",
                         len(promoted), elapsed)
        return result
