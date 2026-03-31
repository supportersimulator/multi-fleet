---
name: fleet-wake
description: Wake a sleeping or idle machine — WoL magic packet, SSH ping, or caffeinate
---

# Fleet Wake

Three strategies to bring a node back online, tried in order.

## Strategies

### 1. Health Check (is it already up?)
```bash
curl -s --max-time 2 http://<target-ip>:8855/health
```
If this responds, the node is awake — no action needed.

### 2. SSH Ping + Caffeinate (awake but idle)
```bash
ssh <user>@<target-ip> "caffeinate -dims -t 3600 &"
```
Prevents the machine from sleeping for 1 hour. Useful when the machine is awake but might sleep soon.

### 3. Wake-on-LAN (sleeping machine)
```python
import socket
mac_bytes = bytes.fromhex(mac_address.replace(":", ""))
magic = b'\xff' * 6 + mac_bytes * 16
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
sock.sendto(magic, ('<broadcast>', 9))
```
Sends a magic packet to wake a fully sleeping machine. Requires WoL enabled in System Settings and the target's MAC address in the peer registry.

## Wake Flow

```
Want to reach target node
  → GET target:8855/health — responds? Done.
  → SSH caffeinate — connected? Wait for daemon. Done.
  → Send WoL magic packet → poll health every 5s (max 60s)
  → Target wakes → LaunchAgent starts Fleet Nerve → health responds
  → Deliver queued messages
```

## After Wake

Once a node is awake, its Fleet Nerve daemon auto-starts via LaunchAgent. Queued messages in the chief relay or local outbox will be delivered automatically.
