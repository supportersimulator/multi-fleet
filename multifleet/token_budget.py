"""Token budget tracker for Claude Code API usage.

Monitors estimated consumption, warns before hitting weekly limits,
and persists state across sessions via Redis (optional) or JSON fallback.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger("fleet-nerve.token-budget")

STATUS_FILE = Path("/tmp/token-budget-status.json")

# Default weekly limit (tokens). Conservative estimate for Max plan.
DEFAULT_WEEKLY_LIMIT = 45_000_000


class BudgetLevel(str, Enum):
    GREEN = "GREEN"        # <50%
    YELLOW = "YELLOW"      # 50-80%
    RED = "RED"            # 80-95%
    CRITICAL = "CRITICAL"  # >95%


@dataclass
class TurnRecord:
    timestamp: float
    input_tokens: int
    output_tokens: int
    model: str = ""


class TokenBudget:
    """Tracks estimated session/weekly token consumption.

    Persists via Redis when available, falls back to /tmp JSON.
    """

    REDIS_KEY = "token_budget:turns"
    REDIS_META_KEY = "token_budget:meta"

    def __init__(
        self,
        weekly_limit: int = DEFAULT_WEEKLY_LIMIT,
        redis_client: Any | None = None,
        persistence_path: Path | None = None,
    ) -> None:
        self._weekly_limit = weekly_limit
        self._redis = redis_client
        self._persistence_path = persistence_path or STATUS_FILE
        self._turns: list[TurnRecord] = []
        self._week_start: float = self._current_week_start()
        self._load()

    # ── Public API ──

    def record_turn(
        self, input_tokens: int, output_tokens: int, model: str = ""
    ) -> BudgetLevel:
        """Log an interaction. Returns current budget level."""
        turn = TurnRecord(
            timestamp=time.time(),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=model,
        )
        self._turns.append(turn)
        self._prune_old_turns()
        self._persist()
        return self.get_status()

    def total_tokens_this_week(self) -> int:
        """Sum of all input+output tokens in the current week window."""
        cutoff = self._week_start
        return sum(
            t.input_tokens + t.output_tokens
            for t in self._turns
            if t.timestamp >= cutoff
        )

    def usage_fraction(self) -> float:
        """Fraction of weekly limit consumed (0.0 - 1.0+)."""
        if self._weekly_limit <= 0:
            return 0.0
        return self.total_tokens_this_week() / self._weekly_limit

    def get_status(self) -> BudgetLevel:
        """GREEN/YELLOW/RED/CRITICAL based on consumption."""
        frac = self.usage_fraction()
        if frac >= 0.95:
            return BudgetLevel.CRITICAL
        if frac >= 0.80:
            return BudgetLevel.RED
        if frac >= 0.50:
            return BudgetLevel.YELLOW
        return BudgetLevel.GREEN

    def estimate_remaining(self, weekly_limit: int | None = None) -> dict:
        """Project when limit will be hit based on recent burn rate.

        Returns dict with: remaining_tokens, burn_rate_per_hour,
        hours_until_limit, turns_until_limit.
        """
        limit = weekly_limit or self._weekly_limit
        used = self.total_tokens_this_week()
        remaining = max(0, limit - used)

        # Calculate burn rate from last 1 hour of activity
        now = time.time()
        hour_ago = now - 3600
        recent = [t for t in self._turns if t.timestamp >= hour_ago]
        if len(recent) >= 2:
            span = recent[-1].timestamp - recent[0].timestamp
            tokens_in_span = sum(t.input_tokens + t.output_tokens for t in recent)
            burn_rate = tokens_in_span / max(span / 3600, 0.01)
        elif recent:
            # Single turn in last hour -- estimate from that
            tokens_in_span = sum(t.input_tokens + t.output_tokens for t in recent)
            burn_rate = tokens_in_span  # per hour (single sample)
        else:
            burn_rate = 0.0

        hours_left = remaining / burn_rate if burn_rate > 0 else float("inf")

        avg_per_turn = self._avg_tokens_per_turn()
        turns_left = int(remaining / avg_per_turn) if avg_per_turn > 0 else 0

        return {
            "remaining_tokens": remaining,
            "burn_rate_per_hour": round(burn_rate),
            "hours_until_limit": round(hours_left, 1),
            "turns_until_limit": turns_left,
        }

    def should_switch_model(self) -> bool:
        """Returns True when >80% consumed -- signal to use cheaper model."""
        return self.usage_fraction() >= 0.80

    def get_snapshot(self) -> dict:
        """Full status dict suitable for JSON serialization."""
        est = self.estimate_remaining()
        return {
            "status": self.get_status().value,
            "usage_fraction": round(self.usage_fraction(), 4),
            "total_tokens_this_week": self.total_tokens_this_week(),
            "weekly_limit": self._weekly_limit,
            "turn_count_this_week": len(
                [t for t in self._turns if t.timestamp >= self._week_start]
            ),
            "burn_rate_per_hour": est["burn_rate_per_hour"],
            "hours_until_limit": est["hours_until_limit"],
            "turns_until_limit": est["turns_until_limit"],
            "should_switch_model": self.should_switch_model(),
            "timestamp": time.time(),
        }

    # ── Persistence ──

    def _persist(self) -> None:
        """Write state to Redis or JSON fallback."""
        snapshot = self.get_snapshot()
        snapshot["turns"] = [
            {
                "ts": t.timestamp,
                "in": t.input_tokens,
                "out": t.output_tokens,
                "model": t.model,
            }
            for t in self._turns
            if t.timestamp >= self._week_start
        ]
        if self._redis:
            try:
                self._redis.set(
                    self.REDIS_KEY, json.dumps(snapshot), ex=7 * 86400
                )
                return
            except Exception:
                logger.debug("Redis persist failed, falling back to file")
        try:
            self._persistence_path.write_text(json.dumps(snapshot, indent=2))
        except OSError as e:
            logger.warning("Failed to write token budget: %s", e)

    def _load(self) -> None:
        """Load state from Redis or JSON fallback."""
        data = None
        if self._redis:
            try:
                raw = self._redis.get(self.REDIS_KEY)
                if raw:
                    data = json.loads(raw)
            except Exception:
                pass
        if data is None and self._persistence_path.exists():
            try:
                data = json.loads(self._persistence_path.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        if data and "turns" in data:
            cutoff = self._week_start
            for t in data["turns"]:
                ts = t.get("ts", 0)
                if ts >= cutoff:
                    self._turns.append(
                        TurnRecord(
                            timestamp=ts,
                            input_tokens=t.get("in", 0),
                            output_tokens=t.get("out", 0),
                            model=t.get("model", ""),
                        )
                    )

    def _prune_old_turns(self) -> None:
        """Remove turns older than current week."""
        cutoff = self._week_start
        self._turns = [t for t in self._turns if t.timestamp >= cutoff]

    def _avg_tokens_per_turn(self) -> float:
        """Average tokens per turn (input+output) this week."""
        week_turns = [t for t in self._turns if t.timestamp >= self._week_start]
        if not week_turns:
            return 50_000  # conservative default
        return sum(t.input_tokens + t.output_tokens for t in week_turns) / len(
            week_turns
        )

    @staticmethod
    def _current_week_start() -> float:
        """Timestamp of Monday 00:00 UTC of the current week."""
        import datetime

        now = datetime.datetime.now(datetime.timezone.utc)
        monday = now - datetime.timedelta(days=now.weekday())
        monday = monday.replace(hour=0, minute=0, second=0, microsecond=0)
        return monday.timestamp()
