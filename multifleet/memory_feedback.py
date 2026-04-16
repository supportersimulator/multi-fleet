"""Fleet Memory Feedback -- wire repair outcomes into the memory learning pipeline.

Bridges fleet self-heal + health monitor events into memory modules:
- auto_capture.capture_success/capture_failure for repair outcomes
- bugfix_tracker.record_bugfix_passed/failed for recurring channel fixes
- Tracks channel failure patterns for fleet operational intelligence

All memory calls wrapped in try/except -- never blocks fleet operations.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("fleet_nats.memory_feedback")

# Resolve memory/ path relative to repo root
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_MEMORY_DIR = _REPO_ROOT / "memory"

# Lazy-loaded flags -- avoid import cost at module level
_auto_capture = None
_bugfix_tracker = None
_imports_attempted = False


def _ensure_imports():
    """Lazy-load memory modules. Called once, caches results."""
    global _auto_capture, _bugfix_tracker, _imports_attempted
    if _imports_attempted:
        return
    _imports_attempted = True

    # Add memory/ parent to sys.path if needed
    repo_str = str(_REPO_ROOT)
    if repo_str not in sys.path:
        sys.path.insert(0, repo_str)

    try:
        from memory import auto_capture as ac
        _auto_capture = ac
    except Exception as exc:
        logger.debug("memory.auto_capture unavailable: %s", exc)

    try:
        from memory import bugfix_tracker as bt
        _bugfix_tracker = bt
    except Exception as exc:
        logger.debug("memory.bugfix_tracker unavailable: %s", exc)


class FleetMemoryFeedback:
    """Records fleet repair outcomes and health transitions into memory.

    All public methods are fire-and-forget: they never raise, never block
    fleet operations, and degrade gracefully when memory modules are absent.
    """

    def __init__(self):
        # channel -> {failure_count, last_failure, last_success, consecutive_failures}
        self._channel_patterns: dict[str, dict] = {}

    def record_repair(
        self,
        channel: str,
        action: str,
        success: bool,
        details: str = "",
        target: str = "",
        level: int = 0,
    ) -> Optional[str]:
        """Record a repair outcome into the memory learning pipeline.

        Args:
            channel: Which channel was repaired (e.g. P1_nats, P2_http)
            action: Repair action taken (e.g. restart_nats, restart_tunnel)
            success: Whether the repair succeeded
            details: Additional context (error message on failure, result on success)
            target: Target node that was repaired
            level: Escalation level (1-4) at which repair succeeded/failed

        Returns:
            Session/claim ID from memory if recorded, None otherwise.
        """
        _ensure_imports()

        # Update channel pattern tracking (always, even without memory modules)
        self._update_channel_pattern(channel, success)

        task_desc = f"Fleet repair {channel} on {target}" if target else f"Fleet repair {channel}"
        detail_parts = [f"action={action}"]
        if level:
            detail_parts.append(f"level=L{level}")
        if details:
            detail_parts.append(details)
        detail_str = ", ".join(detail_parts)

        result_id = None

        if success:
            result_id = self._record_success(task_desc, detail_str, channel, action, target)
            self.record_success_pattern(channel, action, target)
        else:
            result_id = self._record_failure(task_desc, detail_str, channel, action, target)

        return result_id

    def record_health_transition(
        self,
        node: str,
        old_status: str,
        new_status: str,
        broken_channels: Optional[list] = None,
    ) -> Optional[str]:
        """Record a node health status transition into memory.

        Args:
            node: Node ID (e.g. mac1, mac2)
            old_status: Previous status (healthy, degraded, offline)
            new_status: New status
            broken_channels: List of broken channel names, if any

        Returns:
            Session/claim ID if recorded, None otherwise.
        """
        _ensure_imports()

        # Recovery transitions are successes; degradation transitions are failures
        is_recovery = new_status == "healthy" and old_status in ("degraded", "offline")
        is_degradation = new_status in ("degraded", "offline") and old_status == "healthy"

        if not (is_recovery or is_degradation):
            # Intermediate transitions (offline->degraded) -- log but don't record
            logger.debug("Health transition %s: %s -> %s (not recorded)", node, old_status, new_status)
            return None

        task = f"Fleet node {node} {old_status} -> {new_status}"
        details = ""
        if broken_channels:
            details = f"broken_channels={broken_channels}"

        try:
            if is_recovery and _auto_capture:
                result = _auto_capture.capture_success(
                    task=task,
                    details=details or f"Node {node} recovered to healthy",
                    area="fleet",
                )
                # Wire 5: auto-learn from recovery commit if available
                try:
                    from memory.auto_learn import process_git_commit
                    process_git_commit()
                except Exception:
                    pass  # best-effort
                return result
            elif is_degradation and _auto_capture:
                return _auto_capture.capture_failure(
                    task=task,
                    error=details or f"Node {node} became {new_status}",
                    area="fleet",
                )
        except Exception as exc:
            logger.debug("Memory record_health_transition failed: %s", exc)

        return None

    def record_channel_pattern(
        self,
        channel: str,
        failure_count: int,
        last_failure: float,
    ) -> None:
        """Track recurring channel failure patterns.

        When a channel crosses the flaky threshold (3+ failures), records
        it as a bugfix entry so the fleet learns which channels are unreliable.

        Args:
            channel: Channel name (e.g. P1_nats)
            failure_count: Total failures observed
            last_failure: Timestamp of most recent failure
        """
        _ensure_imports()

        pattern = self._channel_patterns.setdefault(channel, {
            "failure_count": 0,
            "last_failure": 0.0,
            "last_success": 0.0,
            "consecutive_failures": 0,
        })
        pattern["failure_count"] = failure_count
        pattern["last_failure"] = last_failure

        # Record in bugfix tracker when pattern crosses threshold
        if failure_count >= 3 and _bugfix_tracker:
            try:
                _bugfix_tracker.record_bugfix_failed(
                    title=f"fleet-channel-{channel}-flaky",
                    failure_note=f"Channel {channel} failed {failure_count} times, "
                                 f"last at {last_failure:.0f}",
                    symptom=f"Fleet channel {channel} recurring failures",
                )
            except Exception as exc:
                logger.debug("Bugfix tracker record_channel_pattern failed: %s", exc)

    def get_channel_patterns(self) -> dict[str, dict]:
        """Return current channel failure pattern data (for diagnostics)."""
        return dict(self._channel_patterns)

    def record_success_pattern(
        self,
        channel: str,
        action: str,
        target: str = "",
    ) -> None:
        """Wire 4: Extract success patterns via PatternEvolutionEngine.

        Called after a successful repair to feed into pattern evolution.
        Lazy import, best-effort -- never blocks fleet ops.
        """
        try:
            from memory.pattern_evolution import PatternEvolutionEngine
            engine = PatternEvolutionEngine()
            try:
                engine.evolve()
            finally:
                engine.close()
        except Exception as exc:
            logger.debug("record_success_pattern failed: %s", exc)

    # -- Internal helpers --

    def _update_channel_pattern(self, channel: str, success: bool) -> None:
        """Update internal pattern tracking for a channel."""
        pattern = self._channel_patterns.setdefault(channel, {
            "failure_count": 0,
            "last_failure": 0.0,
            "last_success": 0.0,
            "consecutive_failures": 0,
        })
        now = time.time()
        if success:
            pattern["last_success"] = now
            pattern["consecutive_failures"] = 0
        else:
            pattern["failure_count"] += 1
            pattern["last_failure"] = now
            pattern["consecutive_failures"] += 1

            # Auto-record pattern when crossing threshold
            if pattern["consecutive_failures"] >= 3:
                self.record_channel_pattern(
                    channel, pattern["failure_count"], now
                )

    def _record_success(
        self, task: str, details: str, channel: str, action: str, target: str
    ) -> Optional[str]:
        """Record successful repair into memory modules."""
        result_id = None

        # 1. auto_capture.capture_success
        if _auto_capture:
            try:
                result_id = _auto_capture.capture_success(
                    task=task, details=details, area="fleet"
                )
            except Exception as exc:
                logger.debug("capture_success failed: %s", exc)

        # 2. bugfix_tracker -- mark channel fix as passing
        if _bugfix_tracker:
            try:
                _bugfix_tracker.record_bugfix_passed(
                    title=f"fleet-channel-{channel}-repair",
                    symptom=f"Fleet channel {channel} down on {target}",
                    fix=f"Repair action: {action}",
                )
            except Exception as exc:
                logger.debug("bugfix_tracker record_passed failed: %s", exc)

        return result_id

    def _record_failure(
        self, task: str, details: str, channel: str, action: str, target: str
    ) -> Optional[str]:
        """Record failed repair into memory modules."""
        result_id = None

        # 1. auto_capture.capture_failure
        if _auto_capture:
            try:
                result_id = _auto_capture.capture_failure(
                    task=task,
                    error=details,
                    area="fleet",
                    root_cause=f"Repair action {action} failed on channel {channel}",
                )
            except Exception as exc:
                logger.debug("capture_failure failed: %s", exc)

        # 2. bugfix_tracker -- mark channel fix as failing
        if _bugfix_tracker:
            try:
                _bugfix_tracker.record_bugfix_failed(
                    title=f"fleet-channel-{channel}-repair",
                    failure_note=f"Repair {action} failed on {target}: {details}",
                    symptom=f"Fleet channel {channel} down on {target}",
                )
            except Exception as exc:
                logger.debug("bugfix_tracker record_failed failed: %s", exc)

        return result_id


# Module-level singleton for convenience
_singleton: Optional[FleetMemoryFeedback] = None


def get_feedback() -> FleetMemoryFeedback:
    """Get or create the module-level FleetMemoryFeedback singleton."""
    global _singleton
    if _singleton is None:
        _singleton = FleetMemoryFeedback()
    return _singleton
