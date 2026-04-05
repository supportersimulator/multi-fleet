#!/usr/bin/env python3
"""
Multi-Fleet Packet Store — SQLite-backed packet ledger for the chief node.

Stores all inbound packets from worker nodes.
Provides query interface for synthesis and merge adjudication.

Usage (as library):
  from multifleet.packet_store import PacketStore
  store = PacketStore()
  store.ingest(packet)
  recent = store.recent(packet_type="local_verdict", limit=20)
  nodes = store.active_nodes()
"""

import json
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).parent.parent
DB_PATH = REPO_ROOT / ".contextdna" / "local" / "multifleet.db"


class PacketStore:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS packets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    packet_type TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    fleet_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_type_node ON packets(packet_type, node_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON packets(timestamp)")

    def _conn(self):
        return sqlite3.connect(str(self.db_path))

    def ingest(self, packet: dict) -> int:
        """Store a packet. Returns the row ID."""
        with self._conn() as conn:
            cursor = conn.execute(
                """INSERT INTO packets (packet_type, node_id, fleet_id, timestamp, received_at, payload)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    packet.get("type", "unknown"),
                    packet.get("nodeId", "unknown"),
                    packet.get("fleetId", "contextdna-main"),
                    packet.get("timestamp", datetime.now(timezone.utc).isoformat()),
                    datetime.now(timezone.utc).isoformat(),
                    json.dumps(packet),
                )
            )
            return cursor.lastrowid

    def recent(self, packet_type: str = None, node_id: str = None, limit: int = 50) -> list:
        """Query recent packets, optionally filtered by type and/or node."""
        query = "SELECT payload FROM packets WHERE 1=1"
        params = []
        if packet_type:
            query += " AND packet_type = ?"
            params.append(packet_type)
        if node_id:
            query += " AND node_id = ?"
            params.append(node_id)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(query, params).fetchall()
        return [json.loads(row[0]) for row in rows]

    def active_nodes(self, within_seconds: int = 90) -> list:
        """Return nodes that sent a heartbeat recently."""
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=within_seconds)).isoformat()
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT DISTINCT node_id, MAX(timestamp) as last_seen, payload
                   FROM packets
                   WHERE packet_type = 'heartbeat' AND timestamp > ?
                   GROUP BY node_id
                   ORDER BY last_seen DESC""",
                (cutoff,)
            ).fetchall()
        result = []
        for row in rows:
            node_id, last_seen, payload = row
            p = json.loads(payload)
            result.append({
                "nodeId": node_id,
                "lastSeen": last_seen,
                "status": p.get("status", "unknown"),
                "activeBranch": p.get("activeBranch", "unknown"),
                "surgeons": p.get("surgeonStatus", {}),
            })
        return result

    def latest_verdict(self, node_id: str) -> Optional[dict]:
        """Get the most recent local_verdict from a node."""
        rows = self.recent(packet_type="local_verdict", node_id=node_id, limit=1)
        return rows[0] if rows else None

    def fleet_summary(self) -> dict:
        """High-level fleet status for UI display."""
        nodes = self.active_nodes()
        verdicts = {}
        for node in nodes:
            v = self.latest_verdict(node["nodeId"])
            if v:
                verdicts[node["nodeId"]] = {
                    "summary": v.get("summary", ""),
                    "confidence": v.get("confidence", 0),
                    "risks": v.get("risks", []),
                    "dissent": v.get("dissent", []),
                }
        return {
            "activeNodes": nodes,
            "verdicts": verdicts,
            "totalPackets": self._total_count(),
        }

    def _total_count(self) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM packets").fetchone()[0]

    # ── Fleet messaging (inbox/outbox) ────────────────────────────────────────

    def _ensure_messages_table(self):
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS fleet_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    from_node TEXT NOT NULL,
                    to_node TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    body TEXT NOT NULL,
                    priority TEXT NOT NULL DEFAULT 'normal',
                    sent_at TEXT NOT NULL,
                    read_at TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_msg_to ON fleet_messages(to_node, read_at)")

    def send_message(self, from_node: str, to_node: str, subject: str, body: str,
                     priority: str = "normal") -> int:
        """Store an inter-node message. Returns message ID."""
        self._ensure_messages_table()
        with self._conn() as conn:
            cursor = conn.execute(
                """INSERT INTO fleet_messages (from_node, to_node, subject, body, priority, sent_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (from_node, to_node, subject, body, priority,
                 datetime.now(timezone.utc).isoformat())
            )
            return cursor.lastrowid

    def get_inbox(self, node_id: str, unread_only: bool = True) -> list:
        """Retrieve messages for a node. Returns newest first."""
        self._ensure_messages_table()
        query = "SELECT id, from_node, subject, body, priority, sent_at, read_at FROM fleet_messages WHERE to_node = ?"
        params = [node_id]
        if unread_only:
            query += " AND read_at IS NULL"
        query += " ORDER BY id DESC"
        with self._conn() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            {"id": r[0], "from": r[1], "subject": r[2], "body": r[3],
             "priority": r[4], "sent_at": r[5], "read_at": r[6]}
            for r in rows
        ]

    def mark_read(self, message_ids: list) -> int:
        """Mark messages as read. Returns count updated."""
        if not message_ids:
            return 0
        self._ensure_messages_table()
        placeholders = ",".join("?" * len(message_ids))
        with self._conn() as conn:
            cursor = conn.execute(
                f"UPDATE fleet_messages SET read_at = ? WHERE id IN ({placeholders})",
                [datetime.now(timezone.utc).isoformat()] + list(message_ids)
            )
            return cursor.rowcount
