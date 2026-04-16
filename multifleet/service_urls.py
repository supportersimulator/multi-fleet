"""Fleet service URL resolution — wraps memory.service_registry for multi-fleet.

Provides get_fleet_daemon_url() and get_service_url() with graceful fallback
to hardcoded defaults when service_registry is unavailable (e.g. standalone
multi-fleet installs without the full superrepo).

All fleet modules SHOULD use these helpers instead of hardcoding URLs.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger("fleet_nats.service_urls")

# Resolve superrepo root so we can import memory.service_registry
_SUPERREPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Lazy-loaded reference to service_registry
_registry = None
_registry_attempted = False

# Hardcoded fallbacks — used when service_registry is unavailable
_FALLBACK_URLS = {
    "fleet_daemon": "http://127.0.0.1:8855",
    "helper_agent": "http://127.0.0.1:8080",
    "local_llm": "http://127.0.0.1:5044",
    "memory_api": "http://127.0.0.1:3456",
    "redis": "redis://127.0.0.1:6379",
    "fleet_event_bus": "http://127.0.0.1:8033",
}


def _ensure_registry():
    """Lazy-load memory.service_registry. Called once, caches result."""
    global _registry, _registry_attempted
    if _registry_attempted:
        return
    _registry_attempted = True

    repo_str = str(_SUPERREPO_ROOT)
    if repo_str not in sys.path:
        sys.path.insert(0, repo_str)

    try:
        from memory import service_registry as sr
        _registry = sr
    except Exception as exc:
        logger.debug("memory.service_registry unavailable: %s", exc)


def get_service_url(name: str) -> str:
    """Get service URL by name, falling back to hardcoded defaults.

    Args:
        name: Service name (e.g. "fleet_daemon", "helper_agent")

    Returns:
        Full URL string (e.g. "http://127.0.0.1:8855")
    """
    _ensure_registry()

    if _registry:
        try:
            return _registry.get_service_url(name)
        except (ValueError, KeyError, Exception) as exc:
            logger.debug("service_registry lookup failed for %s: %s", name, exc)

    # Fallback to hardcoded
    url = _FALLBACK_URLS.get(name)
    if url:
        return url

    raise ValueError(f"Unknown service '{name}' and no fallback configured")


def get_fleet_daemon_url() -> str:
    """Convenience: get fleet daemon URL (the most common lookup)."""
    return get_service_url("fleet_daemon")
