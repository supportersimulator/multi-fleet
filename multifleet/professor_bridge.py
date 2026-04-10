"""Professor Bridge -- connects ContextDNA professor wisdom to fleet + IDE.

Routes professor queries through the memory system and serves results
to the IDE via event stream and web server endpoints.
"""

import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("fleet-nerve.professor-bridge")

# Memory module path — use _paths module, don't pollute sys.path
from ._paths import REPO_ROOT as _REPO_ROOT_PATH
_REPO_ROOT = str(_REPO_ROOT_PATH)


@dataclass
class CachedWisdom:
    """A cached professor query result."""
    topic: str
    guidance: Dict[str, Any]
    queried_at: float
    ttl: float = 300.0  # 5 min default

    @property
    def expired(self) -> bool:
        return (time.time() - self.queried_at) > self.ttl


class ProfessorBridge:
    """Bridge between ContextDNA professor wisdom and fleet/IDE consumers."""

    def __init__(self, cache_ttl: float = 300.0):
        self._cache: Dict[str, CachedWisdom] = {}
        self._cache_ttl = cache_ttl
        self._professor = None
        self._query_count = 0
        self._cache_hits = 0

    def _get_professor(self):
        """Lazy-init professor instance."""
        if self._professor is not None:
            return self._professor
        try:
            from memory.professor import Professor
            self._professor = Professor()
            return self._professor
        except Exception as e:
            logger.warning("Professor import failed: %s", e)
            return None

    def query(self, topic: str) -> dict:
        """Query professor for wisdom on a topic.

        Returns dict with: topic, the_one_thing, landmines, pattern,
        domains, cached, queried_at.
        """
        self._query_count += 1

        # Check cache
        cached = self._cache.get(topic)
        if cached and not cached.expired:
            self._cache_hits += 1
            result = dict(cached.guidance)
            result["cached"] = True
            return result

        # Query professor
        prof = self._get_professor()
        if prof is None:
            return self._fallback_result(topic, "Professor unavailable")

        try:
            guidance = prof.consult(task=topic)
            result = {
                "topic": topic,
                "the_one_thing": guidance.the_one_thing or "",
                "landmines": list(guidance.landmines) if guidance.landmines else [],
                "pattern": guidance.pattern or "",
                "domains": list(guidance.domains) if guidance.domains else [],
                "context": guidance.context or "",
                "first_principles": list(guidance.first_principles) if guidance.first_principles else [],
                "queried_at": time.time(),
                "cached": False,
            }

            # Cache the result
            self._cache[topic] = CachedWisdom(
                topic=topic,
                guidance=result,
                queried_at=time.time(),
                ttl=self._cache_ttl,
            )
            return result

        except Exception as e:
            logger.warning("Professor query failed for '%s': %s", topic, e)
            return self._fallback_result(topic, str(e))

    def get_injection_context(self, branch: str = "", files: Optional[List[str]] = None) -> str:
        """Get professor wisdom formatted for webhook injection.

        Queries about current branch/files and returns concise markdown
        suitable for systemMessage inclusion.
        """
        topics = []
        if branch:
            topics.append(branch)
        if files:
            topics.extend(files[:3])  # cap at 3 files

        if not topics:
            return ""

        parts = []
        for topic in topics:
            result = self.query(topic)
            one_thing = result.get("the_one_thing", "")
            landmines = result.get("landmines", [])

            if one_thing or landmines:
                section = []
                if one_thing:
                    section.append(f"**{topic}**: {one_thing}")
                for mine in landmines[:2]:  # top 2 landmines
                    section.append(f"  - AVOID: {mine}")
                parts.append("\n".join(section))

        if not parts:
            return ""

        return "## Professor Wisdom\n" + "\n".join(parts)

    def get_panel_data(self) -> dict:
        """Get professor data formatted for IDE panels.

        Returns structured data for activity bar tree view consumption.
        """
        return {
            "recent_wisdom": self._get_recent(),
            "active_gotchas": self._get_gotchas(),
            "architecture_notes": self._get_arch_notes(),
            "stats": {
                "total_queries": self._query_count,
                "cache_hits": self._cache_hits,
                "cache_size": len(self._cache),
                "cache_hit_rate": (
                    round(self._cache_hits / self._query_count, 2)
                    if self._query_count > 0 else 0.0
                ),
            },
        }

    def _get_recent(self) -> List[dict]:
        """Get recent cached wisdom entries, newest first."""
        entries = sorted(
            self._cache.values(),
            key=lambda c: c.queried_at,
            reverse=True,
        )
        return [
            {
                "topic": e.topic,
                "the_one_thing": e.guidance.get("the_one_thing", ""),
                "landmine_count": len(e.guidance.get("landmines", [])),
                "queried_at": e.queried_at,
                "expired": e.expired,
            }
            for e in entries[:10]
        ]

    def _get_gotchas(self) -> List[dict]:
        """Aggregate active gotchas (landmines) from all cached entries."""
        gotchas = []
        for entry in self._cache.values():
            if entry.expired:
                continue
            for mine in entry.guidance.get("landmines", []):
                gotchas.append({
                    "topic": entry.topic,
                    "gotcha": mine,
                    "queried_at": entry.queried_at,
                })
        return gotchas

    def _get_arch_notes(self) -> List[dict]:
        """Get architecture-relevant patterns from cached entries."""
        notes = []
        for entry in self._cache.values():
            if entry.expired:
                continue
            pattern = entry.guidance.get("pattern", "")
            if pattern:
                notes.append({
                    "topic": entry.topic,
                    "pattern": pattern,
                    "queried_at": entry.queried_at,
                })
        return notes

    def clear_cache(self) -> int:
        """Clear all cached entries. Returns count of cleared items."""
        count = len(self._cache)
        self._cache.clear()
        return count

    def _fallback_result(self, topic: str, reason: str) -> dict:
        """Return a minimal result when professor is unavailable."""
        return {
            "topic": topic,
            "the_one_thing": "",
            "landmines": [],
            "pattern": "",
            "domains": [],
            "context": "",
            "first_principles": [],
            "queried_at": time.time(),
            "cached": False,
            "error": reason,
        }


# ── Singleton ──

_bridge: Optional[ProfessorBridge] = None


def get_professor_bridge(cache_ttl: float = 300.0) -> ProfessorBridge:
    """Get or create the singleton ProfessorBridge."""
    global _bridge
    if _bridge is None:
        _bridge = ProfessorBridge(cache_ttl=cache_ttl)
    return _bridge
