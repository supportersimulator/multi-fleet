---
name: fleet-security
description: HMAC message signing, replay prevention, peer validation, and session gold sanitization
---

# Fleet Security

All fleet messages can be HMAC-SHA256 signed for authentication. The security layer also provides replay prevention, peer identity validation, and session gold sanitization.

Implemented in `tools/fleet_nerve_security.py` via the `FleetSecurity` class.

## Features

### HMAC-SHA256 Message Signing

Every outbound message gets `_hmac` (signature) and `_signed_ts` (timestamp) fields. Canonical form: sorted keys, no whitespace, `_hmac` field excluded from digest.

**Graceful degradation:** If no HMAC key exists, messages are sent unsigned. Fleet continues to operate in degraded security mode. Unsigned messages are accepted with a logged warning.

### Replay Prevention

Messages with `_signed_ts` older than 5 minutes (REPLAY_WINDOW_S=300) are rejected. Prevents captured messages from being replayed against the fleet.

### Peer Identity Validation

Inbound messages are checked against a known-peers allowlist. Unknown senders are rejected. The allowlist is populated from `.multifleet/config.json` nodes.

### HTTP Peer IP Validation (CV-001)

All POST requests to the daemon's HTTP server (port 8855) are validated against known peer IPs. Requests from unknown IPs get a `403 Forbidden` response. Loopback (127.0.0.1) is always allowed.

### Session Gold Sanitization

Before publishing session gold over NATS, sensitive fields are stripped. Only safe metadata survives: `node_id`, `session_count`, `session_id` (truncated to 8 chars), `topic_keywords`, `idle_s`, `timestamp`.

Stripped: conversation text, file contents, code artifacts, API responses, error tracebacks, environment variables, credentials.

## HMAC Key Management

Key loading priority:
1. `FLEET_NERVE_HMAC_KEY` env var (for CI/testing)
2. macOS Keychain (`fleet_nerve_hmac_key` service)
3. Auto-generate 32-byte key and store in Keychain

```bash
# Check if HMAC is active
curl -sf http://127.0.0.1:8855/health | python3 -c "
import sys,json; h=json.load(sys.stdin)
print('HMAC enabled:', h.get('security',{}).get('hmac_enabled', False))
"

# Manually set HMAC key (all fleet nodes must share the same key)
security add-generic-password -s fleet_nerve_hmac_key -a $USER -w "your-shared-secret"
```

## Security Stats

The `FleetSecurity.get_stats()` method exposes counters (never key material):

| Counter | Meaning |
|---------|---------|
| `signed` | Messages signed by this node |
| `verified` | Messages with valid HMAC |
| `rejected_bad_sig` | HMAC mismatch (tampered or wrong key) |
| `rejected_replay` | Timestamp outside 5min window |
| `rejected_unknown_peer` | Sender not in known peers |
| `accepted_unsigned` | No signature, accepted in degraded mode |

## INVARIANT

Never log HMAC key, message content, or session gold. Only log: message ID, sender, type, signature status (pass/fail/missing).

## When to Use

- Setting up HMAC across all fleet nodes (must share the same key)
- Debugging rejected messages (check `rejected_bad_sig` vs `rejected_replay`)
- Auditing fleet security posture via stats endpoint
- Understanding why a message was dropped (peer validation, replay, signature)
