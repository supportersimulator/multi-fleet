# Home-Office Preset

**When to use:** Desktop + laptop belonging to the same person. Desktop is
always-on (chief), laptop comes and goes.

## Topology

```
desktop (chief, always-on) <---> laptop (worker, mobile)
```

## Setup

1. Copy to config: `cp config/presets/home-office.json .multifleet/config.json`
2. Replace the desktop IP and MAC address with actual values
3. Install NATS on the desktop: `brew install nats-server && nats-server -p 4222`
4. On the laptop, set `MULTIFLEET_NODE_ID=laptop`

## How it works

- Desktop runs NATS and the chief relay. Always available.
- Laptop connects when awake. If it sleeps, desktop queues messages.
- When laptop opens lid, it reconnects and receives queued work.
- Desktop has a MAC address for Wake-on-LAN (P6) if needed in reverse.
- Laptop's `"host": "dynamic"` means it registers via heartbeat discovery.

## Requirements

- Both machines on the same Wi-Fi/LAN (at least intermittently)
- NATS server on the desktop
- Desktop ideally configured to not sleep (Energy Saver settings)
