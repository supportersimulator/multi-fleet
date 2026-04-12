"""Ghost Anticipate -- predicts what the session will need next.

Pre-stages context, pre-warms caches, pre-fetches relevant memory.
Analyzes current branch + recent commits to predict likely next files
to edit and feeds predictions to the productive_wait backlog.
"""

import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("fleet-nerve.ghost-anticipate")


@dataclass
class Prediction:
    """A single prediction about what the session will need."""

    kind: str  # "file_edit", "memory_query", "probe_run", "context_load"
    target: str  # file path, query string, probe id, etc.
    confidence: float  # 0.0-1.0
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "target": self.target,
            "confidence": round(self.confidence, 3),
            "reason": self.reason,
        }


@dataclass
class AnticipateResult:
    """Result of an anticipation cycle."""

    predictions: list[Prediction] = field(default_factory=list)
    backlog_items_added: int = 0
    duration_ms: float = 0.0
    timestamp: str = ""

    def to_dict(self) -> dict:
        return {
            "predictions": [p.to_dict() for p in self.predictions],
            "backlog_items_added": self.backlog_items_added,
            "duration_ms": round(self.duration_ms, 1),
            "timestamp": self.timestamp,
        }

    def summary(self) -> str:
        if not self.predictions:
            return "no predictions"
        kinds = {}
        for p in self.predictions:
            kinds[p.kind] = kinds.get(p.kind, 0) + 1
        parts = [f"{v} {k}" for k, v in kinds.items()]
        return ", ".join(parts)


class GhostAnticipate:
    """Predicts session needs from branch activity, recent commits, and git diff.

    Predictions are ranked by confidence and fed to productive_wait backlog
    so the system can pre-fetch context while waiting for fleet replies.
    """

    def __init__(self, repo_root: str = ".", node_id: str = "unknown"):
        self._repo_root = Path(repo_root).resolve()
        self._node_id = node_id

    def anticipate(self) -> AnticipateResult:
        """Run anticipation: analyze branch state and predict next needs."""
        t0 = time.monotonic()
        result = AnticipateResult(
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )

        # Gather signals
        recent_files = self._get_recently_changed_files()
        branch_name = self._get_current_branch()
        staged_files = self._get_staged_files()

        # Predict from recently changed files (likely to be edited again)
        for fpath in recent_files[:5]:
            result.predictions.append(Prediction(
                kind="file_edit",
                target=fpath,
                confidence=0.7,
                reason="Recently modified — likely to need further edits",
            ))

        # Predict from staged files (work in progress)
        for fpath in staged_files[:3]:
            if fpath not in recent_files[:5]:
                result.predictions.append(Prediction(
                    kind="file_edit",
                    target=fpath,
                    confidence=0.8,
                    reason="Currently staged — active work in progress",
                ))

        # Predict memory queries from branch name
        if branch_name and branch_name not in ("main", "master"):
            result.predictions.append(Prediction(
                kind="memory_query",
                target=branch_name.replace("/", " ").replace("-", " "),
                confidence=0.6,
                reason=f"Branch '{branch_name}' suggests relevant past work",
            ))

        # Predict probe needs from file patterns
        probe_predictions = self._predict_probes(recent_files + staged_files)
        result.predictions.extend(probe_predictions)

        # Feed to productive_wait backlog
        result.backlog_items_added = self._feed_backlog(result.predictions)

        # Emit event
        self._emit_event(result)

        result.duration_ms = (time.monotonic() - t0) * 1000
        logger.info("ghost_anticipate: %s (%.0fms)", result.summary(), result.duration_ms)
        return result

    def _get_recently_changed_files(self, max_count: int = 10) -> list[str]:
        """Get files changed in recent commits."""
        try:
            out = subprocess.run(
                ["git", "log", "--oneline", "--name-only", "-5", "--no-merges"],
                capture_output=True, text=True, timeout=5,
                cwd=str(self._repo_root),
            )
            if out.returncode != 0:
                return []
            files = []
            for line in out.stdout.strip().splitlines():
                # Skip commit message lines (they have a hash prefix)
                if " " in line[:12] or not line.strip():
                    continue
                if (self._repo_root / line.strip()).exists():
                    files.append(line.strip())
            return list(dict.fromkeys(files))[:max_count]
        except Exception:
            return []

    def _get_current_branch(self) -> str:
        """Get current git branch name."""
        try:
            out = subprocess.run(
                ["git", "branch", "--show-current"],
                capture_output=True, text=True, timeout=3,
                cwd=str(self._repo_root),
            )
            return out.stdout.strip() if out.returncode == 0 else ""
        except Exception:
            return ""

    def _get_staged_files(self) -> list[str]:
        """Get currently staged files."""
        try:
            out = subprocess.run(
                ["git", "diff", "--cached", "--name-only"],
                capture_output=True, text=True, timeout=3,
                cwd=str(self._repo_root),
            )
            if out.returncode != 0:
                return []
            return [f.strip() for f in out.stdout.strip().splitlines() if f.strip()]
        except Exception:
            return []

    def _predict_probes(self, files: list[str]) -> list[Prediction]:
        """Predict which probes should run based on changed files."""
        predictions = []
        has_docs = any(f.endswith(".md") for f in files)
        has_deps = any(f in ("requirements.txt", "pyproject.toml", "package.json") for f in files)
        has_tests = any("test" in f.lower() for f in files)

        if has_docs:
            predictions.append(Prediction(
                kind="probe_run",
                target="mirror_drift",
                confidence=0.8,
                reason="Doc files changed — mirror drift check recommended",
            ))
        if has_deps:
            predictions.append(Prediction(
                kind="probe_run",
                target="dependency_drift",
                confidence=0.85,
                reason="Dependency files changed — drift check needed",
            ))
        if has_tests:
            predictions.append(Prediction(
                kind="probe_run",
                target="invariant_coverage",
                confidence=0.6,
                reason="Test files changed — coverage check useful",
            ))
        return predictions

    def _feed_backlog(self, predictions: list[Prediction]) -> int:
        """Feed high-confidence predictions to productive_wait backlog."""
        try:
            from multifleet.productive_wait import ProductiveWaiter

            waiter = ProductiveWaiter(node_id=self._node_id)
            added = 0
            for pred in predictions:
                if pred.confidence >= 0.6:
                    waiter.add_backlog_item({
                        "type": f"anticipate:{pred.kind}",
                        "target": pred.target,
                        "confidence": pred.confidence,
                        "reason": pred.reason,
                    })
                    added += 1
            return added
        except Exception as e:
            logger.debug("Backlog feed failed: %s", e)
            return 0

    def _emit_event(self, result: AnticipateResult) -> None:
        """Emit ghost anticipate event."""
        try:
            from multifleet.event_emitter import emit_typed
            from multifleet.event_schema import EventSource

            from multifleet.event_schema import EventType
            emit_typed(
                event_type=EventType.GHOST_ANTICIPATE_COMPLETED,
                source=EventSource.GHOST,
                payload={
                    "predictions": len(result.predictions),
                    "backlog_added": result.backlog_items_added,
                },
            )
        except (ImportError, Exception):
            pass


def run_anticipate(repo_root: str = ".", **kwargs) -> dict:
    """Convenience function to run anticipation."""
    ghost = GhostAnticipate(repo_root=repo_root, **kwargs)
    return ghost.anticipate().to_dict()
