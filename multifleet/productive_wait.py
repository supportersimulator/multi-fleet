"""Productive Waiting — sessions never idle.

When waiting for fleet replies, dispatch subagents for backlog items.
When fleet reply arrives, pause productive work and process it.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Optional, Callable

logger = logging.getLogger("fleet-nerve.productive-wait")


@dataclass
class WaitState:
    """Tracks what we're waiting for and what we're doing meanwhile."""
    waiting_for: str  # task_id or message type we're blocking on
    started_at: float = field(default_factory=time.time)
    productive_task: Optional[str] = None  # What we're doing while waiting
    productive_started_at: Optional[float] = None
    reply_received: bool = False
    reply_data: Optional[dict] = None


class ProductiveWaiter:
    """Manages productive work while waiting for fleet replies."""

    def __init__(self, node_id: str):
        self.node_id = node_id
        self._active_waits: dict[str, WaitState] = {}
        self._backlog: list[dict] = []  # Work items to pick up
        self._on_reply: Optional[Callable] = None

    def start_wait(self, task_id: str, description: str = "") -> WaitState:
        """Register that we're waiting for a fleet reply."""
        state = WaitState(waiting_for=task_id)
        self._active_waits[task_id] = state
        logger.info("Waiting for %s — checking backlog for productive work", task_id)
        return state

    def add_backlog_item(self, item: dict):
        """Add a work item to the productive backlog."""
        self._backlog.append(item)

    def get_next_work(self) -> Optional[dict]:
        """Get next backlog item to work on while waiting."""
        if not self._backlog:
            return None
        return self._backlog.pop(0)

    def notify_reply(self, task_id: str, reply: dict) -> bool:
        """Notify that a reply arrived for a waited task."""
        state = self._active_waits.get(task_id)
        if not state:
            return False
        state.reply_received = True
        state.reply_data = reply
        logger.info("Reply received for %s (waited %.1fs)", task_id, time.time() - state.started_at)
        return True

    def is_waiting(self) -> bool:
        """Check if any waits are active."""
        return bool(self._active_waits)

    def pending_replies(self) -> list[str]:
        """List task IDs we're still waiting for."""
        return [tid for tid, s in self._active_waits.items() if not s.reply_received]

    def completed_waits(self) -> list[WaitState]:
        """Get waits that have received replies."""
        return [s for s in self._active_waits.values() if s.reply_received]

    def clear_completed(self):
        """Remove completed waits."""
        self._active_waits = {k: v for k, v in self._active_waits.items() if not v.reply_received}

    def status(self) -> dict:
        """Current waiting status."""
        return {
            "active_waits": len(self._active_waits),
            "pending_replies": self.pending_replies(),
            "backlog_size": len(self._backlog),
            "completed": len(self.completed_waits()),
        }
