"""MCP Tool Rate Limiter -- prevents fleet message flooding."""

import logging
import time
from collections import defaultdict

logger = logging.getLogger("fleet-nerve.ratelimit")

# Defaults: per-tool limits (calls per minute)
DEFAULT_LIMITS = {
    "fleet_send": 30,
    "fleet_broadcast": 10,
    "fleet_inject": 10,
    "fleet_worker_dispatch": 5,
    "fleet_repair": 5,
    "fleet_wake": 3,
    "fleet_lease": 20,
    "fleet_rebuttal": 15,
    # Read-only tools: no limit (fleet_status, fleet_probe, etc.)
}


class RateLimiter:
    """Sliding-window rate limiter for MCP tool calls."""

    def __init__(self, limits: dict | None = None):
        self._limits = limits or DEFAULT_LIMITS
        self._windows: dict[str, list[float]] = defaultdict(list)
        self._stats = {"allowed": 0, "denied": 0}

    def check(self, tool_name: str) -> tuple[bool, str]:
        """Check if a tool call is allowed. Returns (allowed, reason)."""
        limit = self._limits.get(tool_name)
        if limit is None:
            # No limit configured -- allow (read-only tools, etc.)
            self._stats["allowed"] += 1
            return True, ""

        now = time.monotonic()
        window = self._windows[tool_name]
        # Prune entries older than 60s
        window[:] = [t for t in window if now - t < 60]

        if len(window) >= limit:
            self._stats["denied"] += 1
            reason = f"Rate limit: {tool_name} max {limit}/min (current: {len(window)})"
            logger.warning(reason)
            return False, reason

        window.append(now)
        self._stats["allowed"] += 1
        return True, ""

    @property
    def stats(self) -> dict:
        """Return allowed/denied counts."""
        return dict(self._stats)
