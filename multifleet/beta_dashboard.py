"""Beta User Dashboard -- analytics + feedback collection for early users.

Shows: fleet health across beta users, common issues, feature usage,
onboarding completion rates, and feedback collection.
"""

import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("fleet-nerve.beta-dashboard")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS beta_users (
    user_id TEXT PRIMARY KEY,
    node_count INTEGER NOT NULL DEFAULT 1,
    tier TEXT NOT NULL DEFAULT 'free',
    registered_at REAL NOT NULL,
    onboarding_complete INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS beta_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    category TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at REAL NOT NULL,
    FOREIGN KEY (user_id) REFERENCES beta_users(user_id)
);

CREATE TABLE IF NOT EXISTS beta_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    issue_type TEXT NOT NULL,
    details TEXT NOT NULL,
    resolved INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    FOREIGN KEY (user_id) REFERENCES beta_users(user_id)
);
"""

VALID_TIERS = ("free", "starter", "pro", "enterprise")
VALID_CATEGORIES = ("bug", "feature", "ux", "docs", "performance", "other")
VALID_ISSUE_TYPES = ("crash", "config", "connectivity", "performance", "security", "other")


class BetaDashboard:
    """Beta user analytics and feedback collection."""

    def __init__(self, data_dir: Optional[str] = None):
        self._dir = Path(data_dir) if data_dir else Path.home() / ".fleet-nerve"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._db_path = self._dir / "beta.db"
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def register_user(self, user_id: str, node_count: int = 1,
                      tier: str = "free") -> dict:
        """Register a new beta user. Returns user record."""
        if not user_id or not user_id.strip():
            return {"error": "user_id is required"}
        if tier not in VALID_TIERS:
            return {"error": f"Invalid tier: {tier}. Valid: {', '.join(VALID_TIERS)}"}
        if node_count < 1:
            return {"error": "node_count must be >= 1"}

        now = time.time()
        with self._lock:
            with self._conn() as conn:
                try:
                    conn.execute(
                        "INSERT INTO beta_users (user_id, node_count, tier, registered_at) "
                        "VALUES (?, ?, ?, ?)",
                        (user_id, node_count, tier, now),
                    )
                    conn.commit()
                except sqlite3.IntegrityError:
                    return {"error": f"User {user_id} already registered"}
        return {
            "user_id": user_id, "node_count": node_count, "tier": tier,
            "registered_at": now, "status": "registered",
        }

    def record_feedback(self, user_id: str, category: str,
                        text: str) -> dict:
        """Record feedback from a beta user."""
        if not text or not text.strip():
            return {"error": "feedback text is required"}
        if category not in VALID_CATEGORIES:
            return {"error": f"Invalid category: {category}. Valid: {', '.join(VALID_CATEGORIES)}"}
        if not self._user_exists(user_id):
            return {"error": f"Unknown user: {user_id}"}

        now = time.time()
        with self._lock:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO beta_feedback (user_id, category, text, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (user_id, category, text, now),
                )
                conn.commit()
        return {"user_id": user_id, "category": category, "recorded": True, "created_at": now}

    def record_issue(self, user_id: str, issue_type: str,
                     details: str) -> dict:
        """Record an issue from a beta user."""
        if not details or not details.strip():
            return {"error": "issue details are required"}
        if issue_type not in VALID_ISSUE_TYPES:
            return {"error": f"Invalid issue_type: {issue_type}. Valid: {', '.join(VALID_ISSUE_TYPES)}"}
        if not self._user_exists(user_id):
            return {"error": f"Unknown user: {user_id}"}

        now = time.time()
        with self._lock:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO beta_issues (user_id, issue_type, details, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (user_id, issue_type, details, now),
                )
                conn.commit()
        return {"user_id": user_id, "issue_type": issue_type, "recorded": True, "created_at": now}

    def user_stats(self, user_id: str) -> dict:
        """Get stats for a single beta user."""
        with self._conn() as conn:
            user = conn.execute(
                "SELECT * FROM beta_users WHERE user_id = ?", (user_id,)
            ).fetchone()
            if not user:
                return {"error": f"Unknown user: {user_id}"}
            feedback_count = conn.execute(
                "SELECT COUNT(*) FROM beta_feedback WHERE user_id = ?", (user_id,)
            ).fetchone()[0]
            issue_count = conn.execute(
                "SELECT COUNT(*) FROM beta_issues WHERE user_id = ?", (user_id,)
            ).fetchone()[0]
            open_issues = conn.execute(
                "SELECT COUNT(*) FROM beta_issues WHERE user_id = ? AND resolved = 0",
                (user_id,),
            ).fetchone()[0]
        return {
            "user_id": user_id,
            "node_count": user["node_count"],
            "tier": user["tier"],
            "registered_at": user["registered_at"],
            "onboarding_complete": bool(user["onboarding_complete"]),
            "feedback_count": feedback_count,
            "issue_count": issue_count,
            "open_issues": open_issues,
        }

    def global_stats(self) -> dict:
        """Aggregate stats across all beta users."""
        with self._conn() as conn:
            total_users = conn.execute("SELECT COUNT(*) FROM beta_users").fetchone()[0]
            total_nodes = conn.execute(
                "SELECT COALESCE(SUM(node_count), 0) FROM beta_users"
            ).fetchone()[0]
            onboarded = conn.execute(
                "SELECT COUNT(*) FROM beta_users WHERE onboarding_complete = 1"
            ).fetchone()[0]
            tier_dist = {}
            for row in conn.execute(
                "SELECT tier, COUNT(*) as cnt FROM beta_users GROUP BY tier"
            ):
                tier_dist[row["tier"]] = row["cnt"]
            total_feedback = conn.execute("SELECT COUNT(*) FROM beta_feedback").fetchone()[0]
            category_dist = {}
            for row in conn.execute(
                "SELECT category, COUNT(*) as cnt FROM beta_feedback GROUP BY category"
            ):
                category_dist[row["category"]] = row["cnt"]
            total_issues = conn.execute("SELECT COUNT(*) FROM beta_issues").fetchone()[0]
            open_issues = conn.execute(
                "SELECT COUNT(*) FROM beta_issues WHERE resolved = 0"
            ).fetchone()[0]
            issue_dist = {}
            for row in conn.execute(
                "SELECT issue_type, COUNT(*) as cnt FROM beta_issues GROUP BY issue_type"
            ):
                issue_dist[row["issue_type"]] = row["cnt"]
        onboarding_pct = round(onboarded / total_users * 100, 1) if total_users else 0.0
        return {
            "total_users": total_users,
            "total_nodes": total_nodes,
            "onboarding_complete_pct": onboarding_pct,
            "tier_distribution": tier_dist,
            "total_feedback": total_feedback,
            "feedback_by_category": category_dist,
            "total_issues": total_issues,
            "open_issues": open_issues,
            "issues_by_type": issue_dist,
        }

    def render_html(self) -> str:
        """Render beta dashboard as HTML."""
        stats = self.global_stats()
        rows = ""
        with self._conn() as conn:
            for u in conn.execute("SELECT * FROM beta_users ORDER BY registered_at DESC"):
                ts = datetime.fromtimestamp(u["registered_at"], tz=timezone.utc).strftime("%Y-%m-%d")
                ob = "Yes" if u["onboarding_complete"] else "No"
                rows += (
                    f"<tr><td>{u['user_id']}</td><td>{u['tier']}</td>"
                    f"<td>{u['node_count']}</td><td>{ob}</td><td>{ts}</td></tr>\n"
                )
        return f"""<!DOCTYPE html>
<html><head><title>Beta Dashboard</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 2rem; background: #0d1117; color: #c9d1d9; }}
h1 {{ color: #58a6ff; }} h2 {{ color: #8b949e; margin-top: 2rem; }}
.cards {{ display: flex; gap: 1rem; flex-wrap: wrap; }}
.card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 1rem 1.5rem; min-width: 150px; }}
.card .num {{ font-size: 2rem; font-weight: bold; color: #58a6ff; }}
.card .label {{ color: #8b949e; font-size: 0.85rem; }}
table {{ border-collapse: collapse; width: 100%; margin-top: 1rem; }}
th, td {{ border: 1px solid #30363d; padding: 0.5rem 1rem; text-align: left; }}
th {{ background: #161b22; color: #58a6ff; }}
</style></head><body>
<h1>Fleet Nerve Beta Dashboard</h1>
<div class="cards">
  <div class="card"><div class="num">{stats['total_users']}</div><div class="label">Users</div></div>
  <div class="card"><div class="num">{stats['total_nodes']}</div><div class="label">Nodes</div></div>
  <div class="card"><div class="num">{stats['onboarding_complete_pct']}%</div><div class="label">Onboarded</div></div>
  <div class="card"><div class="num">{stats['total_feedback']}</div><div class="label">Feedback</div></div>
  <div class="card"><div class="num">{stats['open_issues']}</div><div class="label">Open Issues</div></div>
</div>
<h2>Users</h2>
<table><tr><th>User</th><th>Tier</th><th>Nodes</th><th>Onboarded</th><th>Registered</th></tr>
{rows}</table>
</body></html>"""

    def export_feedback(self) -> list:
        """Export all feedback as a list of dicts."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT user_id, category, text, created_at FROM beta_feedback "
                "ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def _user_exists(self, user_id: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM beta_users WHERE user_id = ?", (user_id,)
            ).fetchone()
        return row is not None
