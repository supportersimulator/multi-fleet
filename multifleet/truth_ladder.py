"""Truth Ladder — Inbox → Vision → Snapshot → DAO promotion pipeline.

Aaron's explicit request: ideas flow through increasing levels of verification
before becoming authoritative wisdom. Each promotion requires evidence.

Layers:
  inbox    — Raw ideas, observations, proposals. No evidence required.
  vision   — Coherent direction with tradeoffs noted. Promoted from inbox.
  snapshot — Code reality verified, probe findings validate. Promoted from vision.
  dao      — Aspirational alignment, sufficient stability + evidence. From snapshot ONLY.
"""

import json
import logging
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal, Optional

logger = logging.getLogger("fleet-nerve.truth-ladder")

LAYERS = ("inbox", "vision", "snapshot", "dao")
LAYER_ORDER = {name: idx for idx, name in enumerate(LAYERS)}

# Promotion rules: src_layer -> dst_layer
VALID_PROMOTIONS = {
    "inbox": "vision",
    "vision": "snapshot",
    "snapshot": "dao",
}

DEFAULT_DB = Path.home() / ".fleet-nerve" / "truth_ladder.db"


@dataclass
class TruthEntry:
    id: str
    layer: Literal["inbox", "vision", "snapshot", "dao"]
    title: str
    content: str
    evidence_refs: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    promoted_at: float = 0.0
    promoted_from: str = ""
    promoted_by: str = ""  # node_id that promoted
    status: str = "active"
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "TruthEntry":
        return cls(
            id=row["id"],
            layer=row["layer"],
            title=row["title"],
            content=row["content"],
            evidence_refs=json.loads(row["evidence_refs"]) if row["evidence_refs"] else [],
            created_at=row["created_at"],
            promoted_at=row["promoted_at"] or 0.0,
            promoted_from=row["promoted_from"] or "",
            promoted_by=row["promoted_by"] or "",
            status=row["status"],
            metadata=json.loads(row["metadata"]) if row["metadata"] else {},
        )


class TruthLadder:
    """SQLite-backed truth promotion pipeline."""

    def __init__(self, db_path: str = None):
        self.db_path = str(db_path or DEFAULT_DB)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn_cache: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        if self._conn_cache is not None:
            return self._conn_cache
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        self._conn_cache = conn
        return conn

    def _init_db(self):
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS truth_entries (
                    id TEXT PRIMARY KEY,
                    layer TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    evidence_refs TEXT DEFAULT '[]',
                    created_at REAL NOT NULL,
                    promoted_at REAL DEFAULT 0,
                    promoted_from TEXT DEFAULT '',
                    promoted_by TEXT DEFAULT '',
                    status TEXT DEFAULT 'active',
                    metadata TEXT DEFAULT '{}'
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_truth_layer
                ON truth_entries(layer, status)
            """)

    def add_inbox(self, title: str, content: str, metadata: dict = None) -> TruthEntry:
        """Add a raw idea to the inbox. No evidence required."""
        entry = TruthEntry(
            id=str(uuid.uuid4()),
            layer="inbox",
            title=title,
            content=content,
            metadata=metadata or {},
        )
        self._insert(entry)
        logger.info("truth_ladder add_inbox id=%s title=%s", entry.id, title[:60])

        # Record truth entry creation to evidence ledger (truth_ladder <-> evidence_ledger wiring)
        try:
            from multifleet.evidence_ledger import EvidenceLedger
            EvidenceLedger().record(
                event_type="truth_entry",
                node_id="local",
                subject=title,
                payload={
                    "entry_id": entry.id,
                    "layer": "inbox",
                    "title": title,
                    "content": content[:200],
                },
            )
        except Exception:
            pass  # Best-effort

        # Emit to EventBus (truth_ladder <-> event_bus wiring)
        try:
            from multifleet.event_emitter import emit_typed
            from multifleet.event_schema import EventType, EventSource
            emit_typed(EventType.TRUTH_ENTRY_CREATED, EventSource.MULTI_FLEET, {
                "entry_id": entry.id,
                "layer": "inbox",
                "title": title,
            })
        except Exception:
            pass  # Best-effort — EventBus emission must never break truth ladder

        return entry

    def promote_to_vision(self, inbox_id: str, evidence_refs: list[str] = None,
                          promoted_by: str = "") -> TruthEntry:
        """Promote inbox item to vision. Requires: coherent direction, tradeoffs noted."""
        return self._promote(inbox_id, "inbox", "vision", evidence_refs, promoted_by)

    def promote_to_snapshot(self, vision_id: str, evidence_refs: list[str] = None,
                            promoted_by: str = "") -> TruthEntry:
        """Promote vision to snapshot. Requires: execution observed, code reality verified."""
        return self._promote(vision_id, "vision", "snapshot", evidence_refs, promoted_by)

    def promote_to_dao(self, snapshot_id: str, evidence_refs: list[str] = None,
                       promoted_by: str = "") -> TruthEntry:
        """Promote snapshot to DAO. Requires: sufficient stability + evidence.
        Cannot promote directly from vision — MUST go through snapshot."""
        return self._promote(snapshot_id, "snapshot", "dao", evidence_refs, promoted_by)

    def query(self, layer: str = None, status: str = "active", limit: int = 50) -> list[TruthEntry]:
        """Query entries, optionally filtered by layer and status."""
        sql = "SELECT * FROM truth_entries WHERE 1=1"
        params: list = []
        if layer:
            if layer not in LAYERS:
                raise ValueError(f"Invalid layer: {layer}. Must be one of {LAYERS}")
            sql += " AND layer = ?"
            params.append(layer)
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = self._conn().execute(sql, params).fetchall()
        return [TruthEntry.from_row(r) for r in rows]

    def get(self, entry_id: str) -> Optional[TruthEntry]:
        """Get a specific entry by ID."""
        row = self._conn().execute(
            "SELECT * FROM truth_entries WHERE id = ?", (entry_id,)
        ).fetchone()
        return TruthEntry.from_row(row) if row else None

    def get_promotion_chain(self, entry_id: str) -> list[TruthEntry]:
        """Trace an entry's promotion history: inbox → vision → snapshot → dao."""
        chain: list[TruthEntry] = []
        current = self.get(entry_id)
        if not current:
            return chain
        # Walk backward to find origin
        backward: list[TruthEntry] = [current]
        while current and current.promoted_from:
            current = self.get(current.promoted_from)
            if current:
                backward.append(current)
        backward.reverse()
        # Walk forward from origin to find all promotions
        chain = backward
        # Also look for entries promoted FROM the given id (forward chain)
        tip = self.get(entry_id)
        if tip:
            forward = self._find_promoted_from(tip.id)
            while forward:
                chain.append(forward)
                forward = self._find_promoted_from(forward.id)
        return chain

    @property
    def stats(self) -> dict:
        """Count per layer."""
        result = {layer: 0 for layer in LAYERS}
        rows = self._conn().execute(
            "SELECT layer, COUNT(*) as cnt FROM truth_entries WHERE status = 'active' GROUP BY layer"
        ).fetchall()
        for row in rows:
            result[row["layer"]] = row["cnt"]
        result["total"] = sum(result.values())
        return result

    # ── Internal ──

    def _insert(self, entry: TruthEntry):
        with self._lock:
            with self._conn() as conn:
                conn.execute(
                    """INSERT INTO truth_entries
                       (id, layer, title, content, evidence_refs, created_at,
                        promoted_at, promoted_from, promoted_by, status, metadata)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (entry.id, entry.layer, entry.title, entry.content,
                     json.dumps(entry.evidence_refs), entry.created_at,
                     entry.promoted_at, entry.promoted_from, entry.promoted_by,
                     entry.status, json.dumps(entry.metadata)),
                )

    def _promote(self, source_id: str, expected_layer: str, target_layer: str,
                 evidence_refs: list[str] = None, promoted_by: str = "") -> TruthEntry:
        with self._lock:
            source = self.get(source_id)
            if not source:
                raise ValueError(f"Entry not found: {source_id}")
            if source.layer != expected_layer:
                raise ValueError(
                    f"Cannot promote from {source.layer} to {target_layer}. "
                    f"Expected source layer: {expected_layer}"
                )
            if source.status != "active":
                raise ValueError(f"Entry {source_id} is not active (status={source.status})")
            valid_target = VALID_PROMOTIONS.get(expected_layer)
            if valid_target != target_layer:
                raise ValueError(f"Invalid promotion: {expected_layer} -> {target_layer}")

            # Create promoted entry
            now = time.time()
            promoted = TruthEntry(
                id=str(uuid.uuid4()),
                layer=target_layer,
                title=source.title,
                content=source.content,
                evidence_refs=evidence_refs or [],
                created_at=now,
                promoted_at=now,
                promoted_from=source_id,
                promoted_by=promoted_by,
                status="active",
                metadata={**source.metadata, "promotion_chain": f"{expected_layer}->{target_layer}"},
            )
            # Direct insert without re-acquiring _lock (we already hold it)
            with self._conn() as conn:
                conn.execute(
                    """INSERT INTO truth_entries
                       (id, layer, title, content, evidence_refs, created_at,
                        promoted_at, promoted_from, promoted_by, status, metadata)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (promoted.id, promoted.layer, promoted.title, promoted.content,
                     json.dumps(promoted.evidence_refs), promoted.created_at,
                     promoted.promoted_at, promoted.promoted_from, promoted.promoted_by,
                     promoted.status, json.dumps(promoted.metadata)),
                )

            # Mark source as promoted
            with self._conn() as conn:
                conn.execute(
                    "UPDATE truth_entries SET status = 'promoted' WHERE id = ?",
                    (source_id,),
                )

        logger.info(
            "truth_ladder promote %s->%s id=%s from=%s by=%s",
            expected_layer, target_layer, promoted.id, source_id, promoted_by,
        )

        # P1-5: Record promotion to evidence ledger (best-effort)
        try:
            from multifleet.evidence_ledger import EvidenceLedger
            EvidenceLedger().record(
                event_type="truth_promotion",
                node_id=promoted_by or "unknown",
                subject=promoted.title,
                payload={
                    "entry_id": promoted.id,
                    "from_layer": expected_layer,
                    "to_layer": target_layer,
                    "source_id": source_id,
                    "evidence_refs": evidence_refs or [],
                },
            )
        except Exception:
            pass  # Best-effort

        # Emit promotion to EventBus (truth_ladder <-> event_bus wiring)
        try:
            from multifleet.event_emitter import emit_typed
            from multifleet.event_schema import EventType, EventSource
            emit_typed(EventType.TRUTH_PROMOTION, EventSource.MULTI_FLEET, {
                "entry_id": promoted.id,
                "from_layer": expected_layer,
                "to_layer": target_layer,
                "source_id": source_id,
                "promoted_by": promoted_by or "unknown",
            })
        except Exception:
            pass  # Best-effort — EventBus emission must never break truth ladder

        return promoted

    def _find_promoted_from(self, source_id: str) -> Optional[TruthEntry]:
        """Find the entry that was promoted FROM a given source."""
        row = self._conn().execute(
            "SELECT * FROM truth_entries WHERE promoted_from = ? AND status = 'active'",
            (source_id,),
        ).fetchone()
        return TruthEntry.from_row(row) if row else None
