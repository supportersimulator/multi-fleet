"""Rebuttal State Machine — structured disagreement and consensus protocol.

States: proposed → under_review → accepted | rebutted → counter_proposed → resolved
Auto-accept timeout: 5 minutes (if no rebuttal received, proposal auto-accepts).
"""

import json
import logging
import time
import uuid
from typing import Optional

logger = logging.getLogger("fleet-nerve.rebuttal")

# States
PROPOSED = "proposed"
UNDER_REVIEW = "under_review"
ACCEPTED = "accepted"
REBUTTED = "rebutted"
COUNTER_PROPOSED = "counter_proposed"
RESOLVED = "resolved"
EXPIRED = "expired"

# Valid transitions
TRANSITIONS = {
    PROPOSED: [UNDER_REVIEW, ACCEPTED, EXPIRED],
    UNDER_REVIEW: [ACCEPTED, REBUTTED],
    REBUTTED: [COUNTER_PROPOSED, RESOLVED],
    COUNTER_PROPOSED: [UNDER_REVIEW, RESOLVED],
}

AUTO_ACCEPT_TIMEOUT_S = 300  # 5 minutes


class RebuttalManager:
    """Manages proposals, rebuttals, and consensus backed by SQLite.

    Each proposal tracks its full transition history as a JSON array.
    Stale proposals auto-accept after AUTO_ACCEPT_TIMEOUT_S.
    """

    def __init__(self, store, node_id: str):
        self._store = store
        self._node_id = node_id
        self._init_table()

    def _init_table(self):
        """Create rebuttals table if not exists."""
        with self._store._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS rebuttals (
                    proposal_id TEXT PRIMARY KEY,
                    proposer TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    body TEXT NOT NULL,
                    scope TEXT DEFAULT 'general',
                    state TEXT NOT NULL DEFAULT 'proposed',
                    parent_id TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    resolved_at REAL,
                    resolution TEXT,
                    history TEXT NOT NULL DEFAULT '[]'
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_rebuttals_state
                ON rebuttals(state)
            """)

    def propose(self, subject: str, body: str, scope: str = "general") -> dict:
        """Create a new proposal. Returns proposal dict with ID."""
        proposal_id = str(uuid.uuid4())
        now = time.time()
        history = [{"state": PROPOSED, "actor": self._node_id,
                     "detail": "proposal created", "ts": now}]
        with self._store._conn() as conn:
            conn.execute(
                """INSERT INTO rebuttals
                   (proposal_id, proposer, subject, body, scope, state,
                    created_at, updated_at, history)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (proposal_id, self._node_id, subject, body, scope,
                 PROPOSED, now, now, json.dumps(history)),
            )
        logger.info("Proposal %s created by %s: %s", proposal_id[:8], self._node_id, subject)
        return self.get(proposal_id)

    def review(self, proposal_id: str) -> dict:
        """Mark proposal as under review by this node."""
        if not self._transition(proposal_id, UNDER_REVIEW, self._node_id, "reviewing"):
            row = self.get(proposal_id)
            return row or {"error": f"proposal {proposal_id} not found or invalid transition"}
        return self.get(proposal_id)

    def accept(self, proposal_id: str, reason: str = "") -> dict:
        """Accept a proposal."""
        detail = reason or "accepted"
        if not self._transition(proposal_id, ACCEPTED, self._node_id, detail):
            row = self.get(proposal_id)
            return row or {"error": f"proposal {proposal_id} not found or invalid transition"}
        # Mark resolution
        now = time.time()
        with self._store._conn() as conn:
            conn.execute(
                "UPDATE rebuttals SET resolved_at = ?, resolution = ? WHERE proposal_id = ?",
                (now, "accepted", proposal_id),
            )
        logger.info("Proposal %s accepted by %s", proposal_id[:8], self._node_id)
        return self.get(proposal_id)

    def rebut(self, proposal_id: str, reason: str, counter: str = "") -> dict:
        """Rebut a proposal with a reason and optional counter-proposal text."""
        detail = f"rebutted: {reason}"
        if counter:
            detail += f" | counter: {counter}"
        if not self._transition(proposal_id, REBUTTED, self._node_id, detail):
            row = self.get(proposal_id)
            return row or {"error": f"proposal {proposal_id} not found or invalid transition"}
        logger.info("Proposal %s rebutted by %s: %s", proposal_id[:8], self._node_id, reason)
        return self.get(proposal_id)

    def counter_propose(self, proposal_id: str, new_body: str) -> dict:
        """Submit a counter-proposal linked to the original."""
        # Transition original to counter_proposed
        if not self._transition(proposal_id, COUNTER_PROPOSED, self._node_id,
                                f"counter-proposal submitted"):
            row = self.get(proposal_id)
            return row or {"error": f"proposal {proposal_id} not found or invalid transition"}

        # Create new linked proposal
        original = self.get(proposal_id)
        subject = f"Counter: {original['subject']}" if original else "Counter-proposal"
        new_id = str(uuid.uuid4())
        now = time.time()
        history = [{"state": PROPOSED, "actor": self._node_id,
                     "detail": f"counter to {proposal_id[:8]}", "ts": now}]
        with self._store._conn() as conn:
            conn.execute(
                """INSERT INTO rebuttals
                   (proposal_id, proposer, subject, body, scope, state,
                    parent_id, created_at, updated_at, history)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (new_id, self._node_id, subject, new_body,
                 original.get("scope", "general") if original else "general",
                 PROPOSED, proposal_id, now, now, json.dumps(history)),
            )
        logger.info("Counter-proposal %s created by %s (parent: %s)",
                     new_id[:8], self._node_id, proposal_id[:8])
        return self.get(new_id)

    def resolve(self, proposal_id: str, resolution: str) -> dict:
        """Resolve a rebuttal chain (final decision)."""
        if not self._transition(proposal_id, RESOLVED, self._node_id, f"resolved: {resolution}"):
            row = self.get(proposal_id)
            return row or {"error": f"proposal {proposal_id} not found or invalid transition"}
        now = time.time()
        with self._store._conn() as conn:
            conn.execute(
                "UPDATE rebuttals SET resolved_at = ?, resolution = ? WHERE proposal_id = ?",
                (now, resolution, proposal_id),
            )
        logger.info("Proposal %s resolved: %s", proposal_id[:8], resolution)
        return self.get(proposal_id)

    def get(self, proposal_id: str) -> Optional[dict]:
        """Get proposal state and history."""
        conn = self._store._conn()
        row = conn.execute(
            "SELECT * FROM rebuttals WHERE proposal_id = ?", (proposal_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["history"] = json.loads(d["history"])
        except (json.JSONDecodeError, TypeError):
            d["history"] = []
        return d

    def list_active(self, state: str = None) -> list:
        """List active proposals, optionally filtered by state."""
        conn = self._store._conn()
        terminal = (ACCEPTED, RESOLVED, EXPIRED)
        if state:
            rows = conn.execute(
                "SELECT * FROM rebuttals WHERE state = ? ORDER BY created_at DESC",
                (state,),
            ).fetchall()
        else:
            placeholders = ",".join("?" for _ in terminal)
            rows = conn.execute(
                f"SELECT * FROM rebuttals WHERE state NOT IN ({placeholders}) ORDER BY created_at DESC",
                terminal,
            ).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            try:
                d["history"] = json.loads(d["history"])
            except (json.JSONDecodeError, TypeError):
                d["history"] = []
            result.append(d)
        return result

    def expire_stale(self) -> int:
        """Auto-accept proposals older than AUTO_ACCEPT_TIMEOUT_S. Returns count expired."""
        now = time.time()
        cutoff = now - AUTO_ACCEPT_TIMEOUT_S
        conn = self._store._conn()
        rows = conn.execute(
            "SELECT proposal_id FROM rebuttals WHERE state = ? AND created_at < ?",
            (PROPOSED, cutoff),
        ).fetchall()
        count = 0
        for row in rows:
            pid = row[0] if isinstance(row, tuple) else row["proposal_id"]
            if self._transition(pid, ACCEPTED, "system", "auto-accepted (timeout)"):
                conn.execute(
                    "UPDATE rebuttals SET resolved_at = ?, resolution = ? WHERE proposal_id = ?",
                    (now, "auto-accepted", pid),
                )
                count += 1
                logger.info("Proposal %s auto-accepted (stale %ds)", pid[:8], AUTO_ACCEPT_TIMEOUT_S)
        if count:
            conn.commit()
        return count

    def _transition(self, proposal_id: str, new_state: str, actor: str,
                    detail: str = "") -> bool:
        """Validate and execute a state transition. Returns success."""
        conn = self._store._conn()
        row = conn.execute(
            "SELECT state, history FROM rebuttals WHERE proposal_id = ?",
            (proposal_id,),
        ).fetchone()
        if not row:
            logger.warning("Transition failed: proposal %s not found", proposal_id[:8])
            return False

        current = row["state"] if isinstance(row, dict) else row[0]
        allowed = TRANSITIONS.get(current, [])
        if new_state not in allowed:
            logger.warning("Invalid transition %s: %s → %s (allowed: %s)",
                           proposal_id[:8], current, new_state, allowed)
            return False

        # Append to history
        hist_raw = row["history"] if isinstance(row, dict) else row[1]
        try:
            history = json.loads(hist_raw) if isinstance(hist_raw, str) else hist_raw
        except (json.JSONDecodeError, TypeError):
            history = []
        history.append({"state": new_state, "actor": actor, "detail": detail, "ts": time.time()})

        now = time.time()
        conn.execute(
            "UPDATE rebuttals SET state = ?, updated_at = ?, history = ? WHERE proposal_id = ?",
            (new_state, now, json.dumps(history), proposal_id),
        )
        conn.commit()
        return True

    @property
    def stats(self) -> dict:
        """Return rebuttal stats for health endpoint."""
        conn = self._store._conn()
        rows = conn.execute(
            "SELECT state, COUNT(*) as cnt FROM rebuttals GROUP BY state"
        ).fetchall()
        by_state = {}
        for r in rows:
            st = r["state"] if isinstance(r, dict) else r[0]
            cnt = r["cnt"] if isinstance(r, dict) else r[1]
            by_state[st] = cnt
        total = sum(by_state.values())
        active = total - by_state.get(ACCEPTED, 0) - by_state.get(RESOLVED, 0) - by_state.get(EXPIRED, 0)
        return {"total": total, "active": active, "by_state": by_state}
