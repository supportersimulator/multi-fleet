"""MemoryScope bridge — maps Python memory sources to TS canonical scopes.

TS canonical scopes: core | profile | workspace | session | task
TS legacy scopes:    global -> core, project -> workspace

Python memory system is flat (no scope concept). This module provides the
mapping layer so panel manifests and memory items can carry scope information
that the TS engine expects.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional


class MemoryScope(str, Enum):
    """Canonical 5-scope set matching TS ``MemoryScope`` type."""
    CORE = "core"
    PROFILE = "profile"
    WORKSPACE = "workspace"
    SESSION = "session"
    TASK = "task"


# Legacy TS scope aliases (global->core, project->workspace)
_LEGACY_MAP: dict[str, MemoryScope] = {
    "global": MemoryScope.CORE,
    "project": MemoryScope.WORKSPACE,
}

# Python memory source -> TS scope mapping
_SOURCE_MAP: dict[str, MemoryScope] = {
    # Core — system-level, persists across everything
    "professor": MemoryScope.CORE,
    "brain": MemoryScope.CORE,
    "gold_mining": MemoryScope.CORE,
    "architecture_enhancer": MemoryScope.CORE,
    "corrigibility": MemoryScope.CORE,
    # Profile — user/node preferences and config
    "auto_capture": MemoryScope.PROFILE,
    "butler_repair_miner": MemoryScope.PROFILE,
    # Workspace — project-scoped knowledge
    "query": MemoryScope.WORKSPACE,
    "semantic_search": MemoryScope.WORKSPACE,
    "semantic_rescue": MemoryScope.WORKSPACE,
    "code_chunk_fts5": MemoryScope.WORKSPACE,
    "context": MemoryScope.WORKSPACE,
    # Session — current session state
    "session_historian": MemoryScope.SESSION,
    "anticipation_engine": MemoryScope.SESSION,
    "injection_metrics": MemoryScope.SESSION,
    # Task — ephemeral, single-task context
    "ab_autonomous": MemoryScope.TASK,
    "synaptic_agent": MemoryScope.TASK,
    "task_persistence": MemoryScope.TASK,
}

# Query type -> scope mapping
_QUERY_TYPE_MAP: dict[str, MemoryScope] = {
    "professor": MemoryScope.CORE,
    "architecture": MemoryScope.CORE,
    "bug_fix": MemoryScope.WORKSPACE,
    "search": MemoryScope.WORKSPACE,
    "context": MemoryScope.WORKSPACE,
    "session": MemoryScope.SESSION,
    "rehydrate": MemoryScope.SESSION,
    "task": MemoryScope.TASK,
    "ab_test": MemoryScope.TASK,
}


def normalize_scope(scope: str) -> Optional[MemoryScope]:
    """Normalize a scope string (canonical or legacy) to MemoryScope.

    Mirrors TS ``normalizeScope()`` from ``scope-utils.ts``.
    Returns None for invalid/unknown scopes.
    """
    try:
        return MemoryScope(scope)
    except ValueError:
        return _LEGACY_MAP.get(scope)


def map_python_source(source: str) -> MemoryScope:
    """Map a Python memory source name to a TS-compatible MemoryScope.

    Unknown sources default to WORKSPACE (safest general-purpose scope).
    """
    return _SOURCE_MAP.get(source, MemoryScope.WORKSPACE)


def get_scope_for_query(query_type: str) -> MemoryScope:
    """Determine the appropriate scope for a memory query type.

    Unknown query types default to WORKSPACE.
    """
    return _QUERY_TYPE_MAP.get(query_type, MemoryScope.WORKSPACE)
