"""Per-peer rate limiting for fleet messages (inbound + outbound).

A misbehaving or compromised peer could flood the fleet. This module enforces
per-peer token-bucket limits on both directions of traffic independently.

Design:
- Sliding-window token bucket per (peer_id, direction).
- Default: 60 req/min per peer per direction (configurable via env/init).
- Thread-safe (single threading.Lock guards all buckets).
- Optional persistence to ``~/.multifleet/rate_limits.json`` on ``save_state()``.
  Persistence is best-effort; rate limiting works without a writable home dir.
- Observability: every check increments a counter regardless of outcome;
  ``get_peer_stats()`` exposes accepted + limited counts per peer per direction.

Wiring (nats_client.py):
- Inbound: ``record_inbound(from_peer)`` called at the top of ``_handle_message``.
  When False is returned, the message is dropped and a warning is logged.
- Outbound: ``record_outbound(target)`` called in ``send_with_fallback`` before
  each channel attempt. When False, the channel is skipped and cascade tries the
  next one. (Rate limiting a channel is treated like channel-down — we don't
  burn the peer's entire budget on one message.)
- A ``fleet.rate_limited`` event is emitted via FleetEventBus on every drop.

ZSF invariant (zero silent failures):
- Every limit hit increments a counter AND emits a structured log line.
- Counters are queryable via ``get_peer_stats()`` so health endpoints can
  surface per-peer pressure.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("multifleet.peer_rate_limit")


DEFAULT_MAX_PER_MINUTE = 60
DEFAULT_WINDOW_S = 60.0
DEFAULT_STATE_PATH = Path.home() / ".multifleet" / "rate_limits.json"


class _Bucket:
    """Sliding-window counter for one (peer, direction) pair.

    Uses a monotonic-time window. When the window expires, counts reset.
    This approximates a token bucket with ``refill_rate = max_per_window / window``
    but avoids fractional-token math.
    """

    __slots__ = ("count", "window_start", "accepted", "limited", "last_seen")

    def __init__(self, now: float):
        self.count: int = 0
        self.window_start: float = now
        self.accepted: int = 0
        self.limited: int = 0
        self.last_seen: float = now

    def to_dict(self) -> dict:
        return {
            "count": self.count,
            "window_start": self.window_start,
            "accepted": self.accepted,
            "limited": self.limited,
            "last_seen": self.last_seen,
        }

    @classmethod
    def from_dict(cls, data: dict, now: float) -> "_Bucket":
        b = cls(now)
        b.count = int(data.get("count", 0))
        b.window_start = float(data.get("window_start", now))
        b.accepted = int(data.get("accepted", 0))
        b.limited = int(data.get("limited", 0))
        b.last_seen = float(data.get("last_seen", now))
        return b


class PeerRateLimiter:
    """Thread-safe per-peer rate limiter with inbound/outbound separation.

    Usage::

        limiter = PeerRateLimiter(max_per_minute=60)
        if not limiter.record_inbound("mac1"):
            drop_message()

    Args:
        max_per_minute: Requests allowed per ``window_s`` per peer per direction.
        window_s: Sliding-window length in seconds. Default 60.0.
        max_per_minute_inbound: Override for inbound (default: same as max_per_minute).
        max_per_minute_outbound: Override for outbound (default: same as max_per_minute).
        state_path: Where to persist bucket state on ``save_state()``.
        on_limit: Optional callback ``(peer_id, direction, count) -> None``
            invoked whenever a request is rejected.
    """

    INBOUND = "inbound"
    OUTBOUND = "outbound"

    def __init__(
        self,
        max_per_minute: int = DEFAULT_MAX_PER_MINUTE,
        window_s: float = DEFAULT_WINDOW_S,
        *,
        max_per_minute_inbound: Optional[int] = None,
        max_per_minute_outbound: Optional[int] = None,
        state_path: Optional[Path] = None,
        on_limit=None,
    ):
        if max_per_minute <= 0:
            raise ValueError("max_per_minute must be > 0")
        if window_s <= 0:
            raise ValueError("window_s must be > 0")

        self._max_in = int(max_per_minute_inbound if max_per_minute_inbound is not None else max_per_minute)
        self._max_out = int(max_per_minute_outbound if max_per_minute_outbound is not None else max_per_minute)
        self._window_s = float(window_s)
        self._state_path = Path(state_path) if state_path is not None else DEFAULT_STATE_PATH
        self._on_limit = on_limit

        self._lock = threading.Lock()
        # Nested: {direction: {peer_id: _Bucket}}
        self._buckets: dict[str, dict[str, _Bucket]] = {
            self.INBOUND: {},
            self.OUTBOUND: {},
        }

    # ── Configuration ────────────────────────────────────────────────

    @property
    def max_inbound(self) -> int:
        return self._max_in

    @property
    def max_outbound(self) -> int:
        return self._max_out

    @property
    def window_s(self) -> float:
        return self._window_s

    def set_limits(
        self,
        *,
        inbound: Optional[int] = None,
        outbound: Optional[int] = None,
    ) -> None:
        """Update per-direction limits at runtime. Existing buckets keep their counts."""
        with self._lock:
            if inbound is not None:
                if inbound <= 0:
                    raise ValueError("inbound limit must be > 0")
                self._max_in = int(inbound)
            if outbound is not None:
                if outbound <= 0:
                    raise ValueError("outbound limit must be > 0")
                self._max_out = int(outbound)

    # ── Core API ─────────────────────────────────────────────────────

    def record_inbound(self, peer_id: str) -> bool:
        """Record an inbound request from ``peer_id``.

        Returns True if within limit, False if the peer has exceeded its
        inbound budget for the current sliding window.
        """
        return self._record(self.INBOUND, peer_id, self._max_in)

    def record_outbound(self, peer_id: str) -> bool:
        """Record an outbound request to ``peer_id``.

        Returns True if within limit, False if the outbound budget to this
        peer is exhausted.
        """
        return self._record(self.OUTBOUND, peer_id, self._max_out)

    def _record(self, direction: str, peer_id: str, limit: int) -> bool:
        if not peer_id:
            # Defensive: unknown peer → don't block, but count against a sentinel.
            peer_id = "<unknown>"
        now = time.monotonic()
        with self._lock:
            bucket_map = self._buckets[direction]
            bucket = bucket_map.get(peer_id)
            if bucket is None:
                bucket = _Bucket(now)
                bucket_map[peer_id] = bucket

            # Roll the sliding window
            if now - bucket.window_start >= self._window_s:
                bucket.count = 0
                bucket.window_start = now

            bucket.last_seen = now

            if bucket.count >= limit:
                bucket.limited += 1
                logger.warning(
                    "peer_rate_limit: %s from/to %s exceeded (limit=%d/%.0fs, hits=%d)",
                    direction,
                    peer_id,
                    limit,
                    self._window_s,
                    bucket.limited,
                )
                if self._on_limit is not None:
                    try:
                        self._on_limit(peer_id, direction, bucket.count)
                    except Exception as exc:  # pragma: no cover — callback must never break limiter
                        logger.debug("on_limit callback failed: %s", exc)
                return False

            bucket.count += 1
            bucket.accepted += 1
            return True

    # ── Introspection ────────────────────────────────────────────────

    def get_peer_stats(self) -> dict[str, dict]:
        """Return a snapshot of per-peer stats.

        Shape::

            {
              "mac1": {
                "inbound":  {"count": 12, "accepted": 120, "limited": 0, "limit": 60, "window_s": 60.0, "last_seen_age_s": 2.1},
                "outbound": {"count":  3, "accepted":  31, "limited": 0, "limit": 60, "window_s": 60.0, "last_seen_age_s": 2.1},
              },
              ...
            }
        """
        now = time.monotonic()
        with self._lock:
            peers: set[str] = set()
            peers.update(self._buckets[self.INBOUND].keys())
            peers.update(self._buckets[self.OUTBOUND].keys())

            out: dict[str, dict] = {}
            for peer in sorted(peers):
                out[peer] = {}
                for direction, limit in (
                    (self.INBOUND, self._max_in),
                    (self.OUTBOUND, self._max_out),
                ):
                    b = self._buckets[direction].get(peer)
                    if b is None:
                        continue
                    out[peer][direction] = {
                        "count": b.count,
                        "accepted": b.accepted,
                        "limited": b.limited,
                        "limit": limit,
                        "window_s": self._window_s,
                        "last_seen_age_s": round(now - b.last_seen, 2),
                    }
            return out

    def get_totals(self) -> dict[str, dict[str, int]]:
        """Aggregate accepted/limited counts across all peers per direction."""
        with self._lock:
            totals: dict[str, dict[str, int]] = {}
            for direction in (self.INBOUND, self.OUTBOUND):
                accepted = sum(b.accepted for b in self._buckets[direction].values())
                limited = sum(b.limited for b in self._buckets[direction].values())
                totals[direction] = {"accepted": accepted, "limited": limited}
            return totals

    def reset(self, peer_id: Optional[str] = None) -> None:
        """Reset all buckets, or just those for a specific peer."""
        with self._lock:
            if peer_id is None:
                for direction in self._buckets:
                    self._buckets[direction].clear()
            else:
                for direction in self._buckets:
                    self._buckets[direction].pop(peer_id, None)

    # ── Persistence (best-effort) ────────────────────────────────────

    def save_state(self, path: Optional[Path] = None) -> bool:
        """Persist bucket state to JSON. Returns True on success.

        Best-effort: failures are logged but never raised. Called on clean
        daemon shutdown; on crash, we lose a few seconds of counters which
        is acceptable.
        """
        target = Path(path) if path is not None else self._state_path
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                snapshot = {
                    "version": 1,
                    "window_s": self._window_s,
                    "max_inbound": self._max_in,
                    "max_outbound": self._max_out,
                    "saved_at_wall": time.time(),
                    "saved_at_monotonic": time.monotonic(),
                    "buckets": {
                        direction: {
                            peer: bucket.to_dict()
                            for peer, bucket in self._buckets[direction].items()
                        }
                        for direction in self._buckets
                    },
                }
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_text(json.dumps(snapshot, indent=2))
            os.replace(tmp, target)
            return True
        except Exception as exc:
            logger.warning("peer_rate_limit: save_state failed (%s): %s", target, exc)
            return False

    def load_state(self, path: Optional[Path] = None) -> bool:
        """Restore bucket state previously saved via ``save_state``.

        Because monotonic time is process-local, window_start values are
        recalibrated relative to the current monotonic clock by preserving
        elapsed time since save. If the save is stale (> window_s old)
        we discard the counts for that bucket.
        Returns True if state was loaded.
        """
        target = Path(path) if path is not None else self._state_path
        if not target.exists():
            return False
        try:
            data = json.loads(target.read_text())
        except Exception as exc:
            logger.warning("peer_rate_limit: load_state parse failed: %s", exc)
            return False

        now_mono = time.monotonic()
        saved_mono = float(data.get("saved_at_monotonic", 0.0))
        saved_wall = float(data.get("saved_at_wall", 0.0))
        # Fall back to wall-clock delta if monotonic delta is useless (new process)
        wall_elapsed = max(0.0, time.time() - saved_wall) if saved_wall > 0 else 0.0

        with self._lock:
            for direction in (self.INBOUND, self.OUTBOUND):
                src = data.get("buckets", {}).get(direction, {}) or {}
                for peer, bdict in src.items():
                    b = _Bucket.from_dict(bdict, now_mono)
                    # Recalibrate window_start: shift into current clock
                    # by the age of the save.
                    age = wall_elapsed if wall_elapsed > 0 else 0.0
                    if age >= self._window_s:
                        # Save is stale → reset live count, keep stats.
                        b.count = 0
                        b.window_start = now_mono
                    else:
                        b.window_start = now_mono - age
                    b.last_seen = now_mono - age
                    self._buckets[direction][peer] = b
        return True


__all__ = ["PeerRateLimiter", "DEFAULT_MAX_PER_MINUTE", "DEFAULT_WINDOW_S"]
