"""
JetStream persistence for multi-fleet (fleet-open blocker #2).

Adds durable message store + replay on top of NATS pub/sub so fleet peers
that were offline when a message was sent can catch up on reconnect.

Two streams:
  - FLEET_MESSAGES  — subjects `fleet.>`   (fleet comms: direct, broadcast, ack)
  - FLEET_EVENTS    — subjects `event.>`   (event bus, audit trail)

Retention per stream:
  - max_age   = 7 days
  - max_bytes = 100 MB
  - storage   = file
  - retention = limits (drop oldest when caps hit)

Every node creates a durable consumer per stream (name = "{stream}_{node_id}")
which tracks its own ack-delivery cursor. On reconnect, the node pulls any
messages the server delivered since the cursor and replays them locally.

All functions are **graceful**:
  - If JetStream is disabled on the server → log once, return None
  - If stream creation races with another node → treat as success
  - If pull-fetch times out → treat as "no missed messages"

Gate: `config.jetstream.enabled` (default False → zero behavior change).
Enable via .multifleet/config.json:

    {
      "jetstream": {
        "enabled": true,
        "max_age_days": 7,
        "max_bytes_mb": 100,
        "replay_max_messages": 200
      }
    }

No network traffic happens at import time. All awaits are inside helpers.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Optional

logger = logging.getLogger("fleet.jetstream")

# ── Stream identity ───────────────────────────────────────────────────────

STREAM_MESSAGES = "FLEET_MESSAGES"
STREAM_EVENTS = "FLEET_EVENTS"

STREAM_SUBJECTS: dict[str, list[str]] = {
    STREAM_MESSAGES: ["fleet.>"],
    STREAM_EVENTS: ["event.>"],
}

# ── Retention defaults ────────────────────────────────────────────────────

DEFAULT_MAX_AGE_DAYS = 7
DEFAULT_MAX_BYTES_MB = 100
DEFAULT_REPLAY_MAX_MESSAGES = 200
DEFAULT_REPLAY_FETCH_BATCH = 50
DEFAULT_REPLAY_FETCH_TIMEOUT_S = 2.0


# ── Config helpers ────────────────────────────────────────────────────────

def _load_config_file() -> dict:
    """Best-effort load of .multifleet/config.json from known locations.

    Returns empty dict if no config file or unparseable. Never raises.
    """
    candidates: list[Path] = []
    env_path = os.environ.get("MULTIFLEET_CONFIG")
    if env_path:
        candidates.append(Path(env_path))
    # Try package-relative location (_paths.REPO_ROOT)
    try:
        from ._paths import REPO_ROOT  # local import, best-effort
        candidates.append(Path(REPO_ROOT) / ".multifleet" / "config.json")
    except Exception:
        pass
    # Also try CWD for tests / unusual layouts
    candidates.append(Path.cwd() / ".multifleet" / "config.json")

    for path in candidates:
        try:
            if path.exists():
                return json.loads(path.read_text()) or {}
        except Exception as e:
            logger.debug("jetstream: config load from %s failed: %s", path, e)
    return {}


def get_jetstream_config(config: Optional[dict] = None) -> dict:
    """Return the `jetstream` block from config, with defaults filled in.

    Args:
        config: Optional pre-loaded config. If None, loads from disk.

    Returns:
        Dict with keys: enabled, max_age_s, max_bytes, replay_max_messages.
    """
    if config is None:
        config = _load_config_file()
    js_cfg = dict(config.get("jetstream") or {})
    env_enabled = os.environ.get("MULTIFLEET_JETSTREAM_ENABLED")
    if env_enabled is not None:
        js_cfg["enabled"] = env_enabled.lower() in ("1", "true", "yes", "on")

    max_age_days = int(js_cfg.get("max_age_days", DEFAULT_MAX_AGE_DAYS))
    max_bytes_mb = int(js_cfg.get("max_bytes_mb", DEFAULT_MAX_BYTES_MB))
    replay_max = int(js_cfg.get("replay_max_messages", DEFAULT_REPLAY_MAX_MESSAGES))
    return {
        "enabled": bool(js_cfg.get("enabled", False)),
        "max_age_s": max_age_days * 24 * 3600,
        "max_bytes": max_bytes_mb * 1024 * 1024,
        "replay_max_messages": replay_max,
    }


def is_jetstream_enabled(config: Optional[dict] = None) -> bool:
    """Convenience gate for callers. Honors MULTIFLEET_JETSTREAM_ENABLED env."""
    return get_jetstream_config(config)["enabled"]


# ── Stream lifecycle ──────────────────────────────────────────────────────

async def _stream_exists(js: Any, stream: str) -> bool:
    """Return True if the named stream already exists on the server.

    Uses stream_info which raises NotFoundError when missing. Any other
    error is treated as "unknown" (returns False so caller attempts create).
    """
    try:
        await js.stream_info(stream)
        return True
    except Exception as e:
        # NotFoundError or timeout → treat as absent; caller will try add
        logger.debug("jetstream: stream_info(%s) → %s", stream, e)
        return False


async def ensure_streams(
    nc: Any,
    *,
    config: Optional[dict] = None,
    streams: Optional[Iterable[str]] = None,
) -> Optional[Any]:
    """Create fleet JetStream streams if missing. Idempotent and best-effort.

    Args:
        nc: Connected nats.NATS client. If None or JetStream unavailable,
            returns None and logs a warning (pub/sub continues to work).
        config: Optional pre-loaded multifleet config dict.
        streams: Optional subset of stream names to ensure (defaults to both).

    Returns:
        The JetStream context on success, or None when JetStream is unusable
        or creation failed hard. Partial success (one stream created, other
        failed) still returns the context so callers can use what works.
    """
    if nc is None:
        logger.warning("jetstream: ensure_streams called with nc=None")
        return None

    try:
        js = nc.jetstream()
    except Exception as e:
        logger.warning("jetstream: nc.jetstream() unavailable (%s) — server without JS?", e)
        return None

    js_cfg = get_jetstream_config(config)
    max_age = js_cfg["max_age_s"]
    max_bytes = js_cfg["max_bytes"]

    targets = list(streams) if streams else [STREAM_MESSAGES, STREAM_EVENTS]
    for stream in targets:
        subjects = STREAM_SUBJECTS.get(stream)
        if not subjects:
            logger.warning("jetstream: unknown stream name %r — skipping", stream)
            continue

        try:
            if await _stream_exists(js, stream):
                logger.info("jetstream: stream %s already present", stream)
                continue

            # Create with limits retention — oldest dropped when caps hit.
            # kwargs form works with nats-py; it converts to StreamConfig.
            await js.add_stream(
                name=stream,
                subjects=subjects,
                retention="limits",
                storage="file",
                max_age=max_age,
                max_bytes=max_bytes,
            )
            logger.info(
                "jetstream: created stream %s (subjects=%s, max_age=%ds, max_bytes=%d)",
                stream, subjects, max_age, max_bytes,
            )
        except Exception as e:
            # Race: another node created it between our check and add → accept.
            msg = str(e).lower()
            if "already" in msg or "in use" in msg or "stream name" in msg:
                logger.info("jetstream: stream %s created concurrently — OK", stream)
                continue
            logger.warning(
                "jetstream: failed to create stream %s (%s) — degrading to pub/sub only",
                stream, e,
            )
            # Continue to next stream; one failure doesn't kill the other.

    return js


# ── Durable consumers ─────────────────────────────────────────────────────

def durable_consumer_name(stream: str, node_id: str) -> str:
    """Canonical durable name per (stream, node). Stable across restarts."""
    # JetStream disallows '.' and '>' in durable names; node_ids are ascii safe
    # but sanitize defensively.
    safe_node = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in node_id)
    return f"{stream}_{safe_node}"


async def create_durable_consumer(
    js: Any,
    stream: str,
    durable_name: str,
    subject: str,
    *,
    deliver_policy: str = "new",
    ack_wait_s: int = 30,
) -> Optional[Any]:
    """Create or fetch a pull-based durable consumer for this node.

    Args:
        js: JetStream context from `ensure_streams`.
        stream: Stream name (FLEET_MESSAGES or FLEET_EVENTS).
        durable_name: Stable per-node identifier (see durable_consumer_name).
        subject: Filter subject (e.g. "fleet.<node>.>" or "fleet.>").
        deliver_policy: "new" | "all" | "last" — where to start if consumer
            is newly created. Once durable exists, cursor wins.
        ack_wait_s: Seconds before redelivery if client doesn't ack.

    Returns:
        A pull subscription object, or None on failure.
    """
    if js is None:
        logger.warning("jetstream: create_durable_consumer called with js=None")
        return None

    try:
        # pull_subscribe creates the consumer if missing, reuses if present.
        sub = await js.pull_subscribe(
            subject=subject,
            durable=durable_name,
            stream=stream,
            config=_build_consumer_config(
                durable_name=durable_name,
                filter_subject=subject,
                deliver_policy=deliver_policy,
                ack_wait_s=ack_wait_s,
            ),
        )
        logger.info(
            "jetstream: durable consumer ready — stream=%s durable=%s subject=%s",
            stream, durable_name, subject,
        )
        return sub
    except TypeError:
        # Older nats-py accepts simpler kwargs; fall back without config.
        try:
            sub = await js.pull_subscribe(
                subject=subject,
                durable=durable_name,
                stream=stream,
            )
            logger.info(
                "jetstream: durable consumer ready (simple) — stream=%s durable=%s",
                stream, durable_name,
            )
            return sub
        except Exception as e:
            logger.warning("jetstream: pull_subscribe failed for %s: %s", durable_name, e)
            return None
    except Exception as e:
        logger.warning(
            "jetstream: failed to create durable consumer %s on %s: %s",
            durable_name, stream, e,
        )
        return None


def _build_consumer_config(
    *,
    durable_name: str,
    filter_subject: str,
    deliver_policy: str,
    ack_wait_s: int,
) -> Any:
    """Return a ConsumerConfig if nats-py supports it, else None.

    Kept isolated so tests can monkeypatch without needing nats-py.
    """
    try:
        from nats.js.api import ConsumerConfig  # type: ignore
    except Exception:
        return None
    kwargs: dict[str, Any] = {
        "durable_name": durable_name,
        "filter_subject": filter_subject,
        "ack_wait": ack_wait_s,
    }
    # deliver_policy is an enum on newer versions; pass as string — nats-py
    # coerces. Wrap in try to be resilient to API drift.
    try:
        return ConsumerConfig(deliver_policy=deliver_policy, **kwargs)
    except Exception:
        try:
            return ConsumerConfig(**kwargs)
        except Exception:
            return None


# ── Publish (best-effort mirror to JetStream) ─────────────────────────────

async def publish_to_stream(
    js: Any,
    subject: str,
    payload: bytes,
    *,
    timeout_s: float = 2.0,
) -> bool:
    """Publish to a JetStream subject. Returns True on ack, False otherwise.

    Never raises — caller should treat False as "not persisted, continue with
    regular pub/sub". Timeout is short to avoid blocking the message path.
    """
    if js is None:
        return False
    try:
        ack = await asyncio.wait_for(js.publish(subject, payload), timeout=timeout_s)
        # ack is a PubAck — truthy on success
        return ack is not None
    except asyncio.TimeoutError:
        logger.debug("jetstream: publish(%s) timed out after %.1fs", subject, timeout_s)
        return False
    except Exception as e:
        logger.debug("jetstream: publish(%s) failed: %s", subject, e)
        return False


# ── Replay on reconnect ───────────────────────────────────────────────────

async def replay_missed(
    sub: Any,
    handler: Callable[[Any], Awaitable[None]],
    *,
    max_messages: int = DEFAULT_REPLAY_MAX_MESSAGES,
    batch_size: int = DEFAULT_REPLAY_FETCH_BATCH,
    fetch_timeout_s: float = DEFAULT_REPLAY_FETCH_TIMEOUT_S,
) -> int:
    """Drain missed messages from a durable consumer's pending queue.

    Loops `sub.fetch(batch)` until the server returns no more messages or
    `max_messages` is hit. Each message is passed to `handler(msg)` and
    then acked. Handler errors do NOT crash replay — they are logged and
    the message is NACK'd so the server may redeliver.

    Args:
        sub: Pull subscription from `create_durable_consumer`.
        handler: Async callable receiving each message.
        max_messages: Hard cap on replayed messages per call.
        batch_size: Messages per fetch round.
        fetch_timeout_s: Per-round timeout; empty fetch ends the loop.

    Returns:
        Number of successfully handled messages.
    """
    if sub is None:
        return 0

    delivered = 0
    remaining = max_messages
    while remaining > 0:
        take = min(batch_size, remaining)
        try:
            msgs = await sub.fetch(take, timeout=fetch_timeout_s)
        except asyncio.TimeoutError:
            break
        except Exception as e:
            # Many nats-py versions raise FetchTimeoutError which isn't
            # asyncio.TimeoutError; treat any fetch error as "no more".
            name = type(e).__name__
            if "Timeout" in name or "NoMessages" in name:
                break
            logger.warning("jetstream: replay fetch failed: %s", e)
            break

        if not msgs:
            break

        # Honor max_messages even if the server returns more than we asked for.
        # Extra messages get nak'd so they remain on the stream for next replay.
        accepted = list(msgs)[:remaining]
        overflow = list(msgs)[remaining:]
        for msg in accepted:
            try:
                await handler(msg)
                await msg.ack()
                delivered += 1
            except Exception as e:
                logger.warning("jetstream: replay handler error — NACKing: %s", e)
                try:
                    await msg.nak()
                except Exception:
                    pass
        for msg in overflow:
            try:
                await msg.nak()
            except Exception:
                pass
        remaining -= len(accepted)

    if delivered:
        logger.info("jetstream: replayed %d missed message(s)", delivered)
    return delivered


# ── Public exports ────────────────────────────────────────────────────────

__all__ = [
    "STREAM_MESSAGES",
    "STREAM_EVENTS",
    "STREAM_SUBJECTS",
    "DEFAULT_MAX_AGE_DAYS",
    "DEFAULT_MAX_BYTES_MB",
    "DEFAULT_REPLAY_MAX_MESSAGES",
    "get_jetstream_config",
    "is_jetstream_enabled",
    "ensure_streams",
    "create_durable_consumer",
    "durable_consumer_name",
    "publish_to_stream",
    "replay_missed",
]
