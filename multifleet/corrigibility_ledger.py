"""Corrigibility Ledger — records when surgeon input changes decisions.

Proves corrigibility is real: logs the BEFORE (proposed action) and
AFTER (final action) whenever surgeons disagree or a gate blocks.

SQLite at ~/.multifleet/corrigibility.db. Append-only.
"""

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("fleet-nerve.corrigibility-ledger")

_DEFAULT_DB = Path.home() / ".multifleet" / "corrigibility.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS decision_points (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    context TEXT NOT NULL,
    proposed_action TEXT NOT NULL,
    surgeon_input TEXT NOT NULL,
    final_action TEXT NOT NULL,
    changed INTEGER NOT NULL,
    source TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS disagreements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    topic TEXT NOT NULL,
    surgeon_a_position TEXT NOT NULL,
    surgeon_b_position TEXT NOT NULL,
    resolution TEXT NOT NULL,
    evidence TEXT NOT NULL DEFAULT '{}'
);
"""


class CorrigibilityLedger:
    """Tracks when surgeon input actually changed a decision."""

    def __init__(self, db_path: Optional[str] = None):
        self._db_path = str(db_path or _DEFAULT_DB)
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self._db_path) as conn:
            conn.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def record_decision_point(
        self,
        context: str,
        proposed_action: str,
        surgeon_input: str,
        final_action: str,
        changed: bool,
        source: str = "",
    ) -> int:
        """Record a decision point with before/after state.

        Returns the row id.
        """
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO decision_points (ts, context, proposed_action, "
                "surgeon_input, final_action, changed, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (time.time(), context, proposed_action, surgeon_input,
                 final_action, int(changed), source),
            )
            row_id = cur.lastrowid

        self._emit_event(context, changed, source)
        return row_id

    def record_disagreement(
        self,
        topic: str,
        surgeon_a_position: str,
        surgeon_b_position: str,
        resolution: str,
        evidence: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Record a surgeon disagreement and how it resolved.

        Returns the row id.
        """
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO disagreements (ts, topic, surgeon_a_position, "
                "surgeon_b_position, resolution, evidence) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (time.time(), topic, surgeon_a_position, surgeon_b_position,
                 resolution, json.dumps(evidence or {})),
            )
            return cur.lastrowid

    def get_impact_stats(self) -> Dict[str, Any]:
        """How many decisions were changed by surgeon input."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as total, "
                "SUM(changed) as changed_count "
                "FROM decision_points"
            ).fetchone()
            total = row["total"] or 0
            changed = row["changed_count"] or 0
            disagreement_count = conn.execute(
                "SELECT COUNT(*) as cnt FROM disagreements"
            ).fetchone()["cnt"] or 0

        return {
            "total_decisions": total,
            "changed_by_surgeon": changed,
            "unchanged": total - changed,
            "change_rate": round(changed / total, 4) if total else 0.0,
            "total_disagreements": disagreement_count,
        }

    def get_recent_disagreements(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Return recent disagreements, newest first."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM disagreements ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "id": r["id"],
                "ts": r["ts"],
                "topic": r["topic"],
                "surgeon_a_position": r["surgeon_a_position"],
                "surgeon_b_position": r["surgeon_b_position"],
                "resolution": r["resolution"],
                "evidence": json.loads(r["evidence"]),
            }
            for r in rows
        ]

    @staticmethod
    def _emit_event(context: str, changed: bool, source: str):
        """Best-effort event emission."""
        try:
            from multifleet.event_emitter import emit_typed
            from multifleet.event_schema import EventType, EventSource
            emit_typed(
                EventType.CORRIGIBILITY_DECISION_RECORDED,
                EventSource.THREE_SURGEONS,
                payload={"context": context[:200], "changed": changed, "source": source},
            )
        except Exception:
            pass  # Never break caller
