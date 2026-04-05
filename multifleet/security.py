"""
Fleet Nerve Security — HMAC message authentication, replay prevention, peer validation.

Provides:
  - HMAC-SHA256 signing/verification for all NATS messages
  - Timestamp-based replay attack prevention (5min window)
  - Peer identity validation against known allowlist
  - Session gold sanitization (strips sensitive fields before publish)
  - Keychain-based secret storage (macOS security command)

INVARIANT: Never log HMAC key, message content, or session gold.
Only log: message ID, sender, type, signature status (pass/fail/missing).
"""

import hashlib
import hmac
import json
import logging
import os
import secrets
import subprocess
import time
from typing import Optional

logger = logging.getLogger("fleet_security")

# ── Constants ──
HMAC_KEYCHAIN_SERVICE = "fleet_nerve_hmac_key"
REPLAY_WINDOW_S = 300  # 5 minutes
HMAC_HEADER_KEY = "_hmac"
HMAC_TIMESTAMP_KEY = "_signed_ts"


class FleetSecurity:
    """HMAC signing, verification, peer validation, gold sanitization.

    Graceful degradation: if no HMAC key is available, logs a warning
    and accepts unsigned messages (does not break existing fleet).
    """

    def __init__(self, known_peers: Optional[set] = None):
        self._hmac_key: Optional[bytes] = None
        self._known_peers: set = known_peers or set()
        self._load_hmac_key()
        # Counters for observability (ZSF: no silent failures)
        self._stats = {
            "signed": 0,
            "verified": 0,
            "rejected_bad_sig": 0,
            "rejected_replay": 0,
            "rejected_unknown_peer": 0,
            "accepted_unsigned": 0,
        }

    # ── HMAC Key Management ──

    def _load_hmac_key(self):
        """Load HMAC key from: env var > keychain > generate new.

        Priority:
          1. FLEET_NERVE_HMAC_KEY env var (for CI/testing)
          2. macOS Keychain (fleet_nerve_hmac_key service)
          3. Generate new 32-byte key and store in keychain
        """
        # 1. Env var
        env_key = os.environ.get("FLEET_NERVE_HMAC_KEY")
        if env_key:
            self._hmac_key = env_key.encode("utf-8")
            logger.info("HMAC key loaded from env var")
            return

        # 2. Keychain
        key_from_keychain = self._read_keychain()
        if key_from_keychain:
            self._hmac_key = key_from_keychain.encode("utf-8")
            logger.info("HMAC key loaded from keychain")
            return

        # 3. Generate and store
        if self._generate_and_store_key():
            logger.info("HMAC key generated and stored in keychain")
        else:
            logger.warning(
                "HMAC key unavailable — messages will be unsigned. "
                "Fleet operates in degraded security mode."
            )

    def _read_keychain(self) -> Optional[str]:
        """Read HMAC key from macOS Keychain. Returns None if not found."""
        try:
            result = subprocess.run(
                ["security", "find-generic-password",
                 "-s", HMAC_KEYCHAIN_SERVICE, "-w"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            logger.warning("Keychain read unavailable (%s) — HMAC key not loaded from keychain", type(e).__name__)
            pass
        return None

    def _generate_and_store_key(self) -> bool:
        """Generate 32-byte random key and store in macOS Keychain."""
        key = secrets.token_hex(32)  # 64 hex chars = 32 bytes
        try:
            # Delete existing if present (ignore errors)
            subprocess.run(
                ["security", "delete-generic-password",
                 "-s", HMAC_KEYCHAIN_SERVICE],
                capture_output=True, timeout=5,
            )
        except Exception as e:
            logger.warning("Keychain delete of existing HMAC key failed (%s) — continuing with key generation", type(e).__name__)
            pass
        try:
            user = os.environ.get("USER", "fleet")
            result = subprocess.run(
                ["security", "add-generic-password",
                 "-s", HMAC_KEYCHAIN_SERVICE,
                 "-a", user,
                 "-w", key],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                self._hmac_key = key.encode("utf-8")
                return True
            logger.warning(f"Keychain store failed (exit {result.returncode})")
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            logger.warning(f"Keychain store error: {type(e).__name__}")
        return False

    @property
    def has_hmac_key(self) -> bool:
        return self._hmac_key is not None

    # ── Peer Management ──

    def update_known_peers(self, peer_ids: set):
        """Update the allowlist of known peer node IDs."""
        self._known_peers = set(peer_ids)

    def add_peer(self, peer_id: str):
        self._known_peers.add(peer_id)

    # ── Signing ──

    def sign_message(self, message: dict) -> dict:
        """Add HMAC signature and signed timestamp to a message.

        If no HMAC key is available, returns message unchanged (graceful degradation).
        The signed payload is the canonical JSON of the message WITHOUT the _hmac field.

        Args:
            message: The message dict to sign. Modified in-place and returned.

        Returns:
            The message dict with _hmac and _signed_ts fields added.
        """
        if not self._hmac_key:
            return message

        # Add signing timestamp (used for replay prevention)
        message[HMAC_TIMESTAMP_KEY] = time.time()

        # Compute HMAC over canonical form (without _hmac field)
        sig = self._compute_hmac(message)
        message[HMAC_HEADER_KEY] = sig
        self._stats["signed"] += 1
        return message

    def _compute_hmac(self, message: dict) -> str:
        """Compute HMAC-SHA256 over canonical JSON of message (excluding _hmac field).

        Canonical form: sorted keys, no whitespace, _hmac excluded.
        """
        # Build payload without the signature field
        payload = {k: v for k, v in message.items() if k != HMAC_HEADER_KEY}
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        sig = hmac.new(
            self._hmac_key, canonical.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return sig

    # ── Verification ──

    def verify_message(self, message: dict) -> tuple:
        """Verify HMAC signature, replay window, and peer identity.

        Returns:
            (is_valid: bool, rejection_reason: Optional[str])

        Rejection reasons:
            "bad_signature" — HMAC mismatch
            "replay" — timestamp outside 5min window
            "unknown_peer" — sender not in known peers allowlist
            None — message is valid (or unsigned in degraded mode)
        """
        sender = message.get("from", "")
        msg_id = message.get("id", "?")

        # 1. Peer identity validation
        if self._known_peers and sender and sender not in self._known_peers:
            self._stats["rejected_unknown_peer"] += 1
            logger.warning(
                f"REJECTED unknown peer: id={msg_id[:8]} sender={sender}"
            )
            return False, "unknown_peer"

        # 2. HMAC verification
        sig = message.get(HMAC_HEADER_KEY)
        if not sig:
            # No signature — accept in degraded mode if we also have no key
            if not self._hmac_key:
                self._stats["accepted_unsigned"] += 1
                return True, None
            # We have a key but message is unsigned — accept with warning
            # (graceful: don't break fleet during key rollout)
            self._stats["accepted_unsigned"] += 1
            logger.info(
                f"UNSIGNED message accepted (graceful): id={msg_id[:8]} sender={sender}"
            )
            return True, None

        if not self._hmac_key:
            # Message is signed but we have no key to verify — accept with warning
            self._stats["accepted_unsigned"] += 1
            logger.info(
                f"SIGNED message accepted (no local key): id={msg_id[:8]} sender={sender}"
            )
            return True, None

        # Compute expected HMAC
        expected = self._compute_hmac(message)
        if not hmac.compare_digest(sig, expected):
            self._stats["rejected_bad_sig"] += 1
            logger.warning(
                f"REJECTED bad signature: id={msg_id[:8]} sender={sender}"
            )
            return False, "bad_signature"

        # 3. Replay prevention — check signed timestamp
        signed_ts = message.get(HMAC_TIMESTAMP_KEY)
        if signed_ts is not None:
            age = abs(time.time() - float(signed_ts))
            if age > REPLAY_WINDOW_S:
                self._stats["rejected_replay"] += 1
                logger.warning(
                    f"REJECTED replay: id={msg_id[:8]} sender={sender} age={age:.0f}s"
                )
                return False, "replay"

        self._stats["verified"] += 1
        return True, None

    # ── Session Gold Sanitization ──

    @staticmethod
    def sanitize_gold_for_publish(gold_data: dict) -> dict:
        """Strip sensitive fields from session gold before NATS publish.

        ALLOWED fields (safe for fleet broadcast):
          - node_id, session_count, topic_keywords, idle_s
          - session_id (truncated to 8 chars)
          - timestamp

        STRIPPED (never published):
          - full conversation text, file contents, code artifacts
          - API responses, error tracebacks with file paths
          - environment variables, credentials, tokens
          - gold_text, raw_content, artifacts, code_blocks
          - file_paths, api_responses, error_details

        Returns a new dict with only safe fields.
        """
        # Explicit allowlist — only these fields survive
        safe = {}

        # Core identity
        if "node_id" in gold_data:
            safe["node_id"] = gold_data["node_id"]
        if "nodeId" in gold_data:
            safe["node_id"] = gold_data["nodeId"]

        # Session metadata (counts, not content)
        if "session_count" in gold_data:
            safe["session_count"] = gold_data["session_count"]
        if "session_id" in gold_data:
            safe["session_id"] = str(gold_data["session_id"])[:8]
        if "sessionId" in gold_data:
            safe["session_id"] = str(gold_data["sessionId"])[:8]

        # Topic keywords (safe summary, never full text)
        if "topic_keywords" in gold_data:
            kw = gold_data["topic_keywords"]
            if isinstance(kw, list):
                safe["topic_keywords"] = [str(k)[:50] for k in kw[:10]]
            elif isinstance(kw, str):
                safe["topic_keywords"] = kw[:200]

        # Idle time
        if "idle_s" in gold_data:
            safe["idle_s"] = gold_data["idle_s"]

        # Timestamp
        if "timestamp" in gold_data:
            safe["timestamp"] = gold_data["timestamp"]

        return safe

    # ── Stats ──

    def get_stats(self) -> dict:
        """Return security stats for health endpoint. Never includes key material."""
        return {
            "hmac_enabled": self.has_hmac_key,
            "known_peers": len(self._known_peers),
            "peer_ids": sorted(self._known_peers) if self._known_peers else [],
            **self._stats,
        }
