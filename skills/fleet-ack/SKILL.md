---
name: fleet-ack
description: Delivery confirmation protocol — ACK tracking, retry on timeout, failure alerting
---

# Fleet ACK Protocol

Every message sent via the fleet gets tracked for delivery confirmation. If an ACK is not received within the timeout, the message is automatically retried. After max retries, a failure alert is injected.

## How It Works

1. **Send** — message is stored in SQLite (`FleetNerveStore`) with `ack_status=awaiting`
2. **Deliver** — recipient receives message, publishes ACK back on `fleet.<sender>.ack`
3. **Confirm** — sender receives ACK, updates status to `delivered`
4. **Retry** — if no ACK after timeout, message is re-sent via `send_with_fallback`
5. **Fail** — after max retries, status set to `failed` and alert injected into session

## NATS Subjects

- `fleet.<node>.ack` — each node subscribes to its own ACK subject
- ACK payload: `{"ref": "<msg_id>", "status": "delivered", "node": "<sender>", "channel": "P1_nats"}`

## HTTP Endpoint

### GET /ack/<msg_id> — Check ACK status

```bash
curl -sf http://127.0.0.1:8855/ack/abc123 | python3 -m json.tool
```

Returns: `{"id": "abc123", "type": "task", "from_node": "mac1", "to_node": "mac2", "ack_status": "delivered", "retry_count": 0}`

Status values: `none`, `awaiting`, `delivered`, `processed`, `failed`

## Retry Mechanism

Two retry systems work in parallel:

### In-memory retry (`_pending_acks` dict)
- Checked during `_watchdog_check_pending_acks()` every 30s
- Retries messages where `age > ack_timeout_s` and `retries < max_retries`
- Removes entries after successful ACK or max retries exhausted

### Persistent retry (`_ack_retry_loop`)
- Runs every 30s, queries SQLite for messages with `ack_status=awaiting`
- Handles messages that survived daemon restarts (persistent across crashes)
- On max retries: marks `ack_status=failed` and injects alert into session

## Configuration

| Env Variable | Default | Description |
|-------------|---------|-------------|
| `FLEET_ACK_TIMEOUT_S` | 60 | Seconds to wait for ACK before retry |
| `FLEET_ACK_MAX_RETRIES` | 3 | Maximum retry attempts |

## ACK Flow Diagram

```
Sender                          Receiver
  |-- message (P1 NATS) -------->|
  |                               |-- process message
  |<---- ACK (fleet.<sender>.ack)|
  |-- update status: delivered    |
```

On timeout (no ACK received):
```
Sender
  |-- 60s elapsed, no ACK
  |-- retry via send_with_fallback (tries next channel)
  |-- retry 2... retry 3...
  |-- max retries: mark failed, inject alert
```

## Store Schema

Messages are persisted in SQLite via `FleetNerveStore`:
- `ack_status` column: `none|awaiting|delivered|processed|failed`
- `retry_count` column: incremented on each retry
- `last_retry_at` column: timestamp of last retry attempt

## Failure Alerting

When a message fails after max retries, the ACK retry loop injects a context message:

```
Fleet: message <id> to <target> failed after 3 retries. 
Check target connectivity. Last channel attempted: <channel>
```

This surfaces delivery failures rather than letting them persist silently (Zero Silent Failures invariant).

## When to Use

- Verifying a critical message was actually delivered
- Debugging why a task never executed (check ACK status)
- Tuning retry timing for slow or unreliable networks
- Understanding the delivery confirmation guarantee model
