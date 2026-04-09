# Configuration

## Config File

Multi-Fleet reads from `.multifleet/config.json` in your project root.

```bash
cp config/config.template.json .multifleet/config.json
```

### Minimal Config

```json
{
  "fleetId": "my-fleet",
  "nodes": {
    "mac1": {
      "host": "192.168.1.10",
      "port": 8855
    }
  }
}
```

### Full Node Config

```json
{
  "fleetId": "my-fleet",
  "nodes": {
    "mac1": {
      "host": "192.168.1.10",
      "port": 8855,
      "ssh_user": "username",
      "ssh_port": 22,
      "role": "chief",
      "mac_address": "AA:BB:CC:DD:EE:FF",
      "nats_port": 4222
    }
  }
}
```

### Node Fields

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `host` | Yes | -- | IP address or hostname |
| `port` | No | `8855` | Fleet daemon HTTP port |
| `ssh_user` | No | current user | SSH username for P4/P5 channels |
| `ssh_port` | No | `22` | SSH port |
| `role` | No | `worker` | Node role: `chief`, `captain`, or `worker` |
| `mac_address` | No | -- | MAC address for Wake-on-LAN (P6) |
| `nats_port` | No | `4222` | NATS server port |

## Environment Variables

| Variable | Description |
|----------|-------------|
| `MULTIFLEET_NODE_ID` | **Required.** This machine's identity in the fleet |
| `FLEET_NERVE_PORT` | Override daemon port (default `8855`) |
| `NATS_URL` | NATS server URL (default `nats://127.0.0.1:4222`) |

## Presets

Pre-built configs for common setups:

| Preset | File | Description |
|--------|------|-------------|
| 2-node LAN | `config/presets/2-node-lan.md` | Two machines on the same network |
| 3-node SSH | `config/presets/3-node-ssh.md` | Three machines connected via SSH |
| Cloud relay | `config/presets/cloud-relay.md` | Nodes behind NAT using cloud relay |
| Home office | `config/presets/home-office.md` | Mixed local + remote machines |

## Security

### HMAC Signing

All NATS messages are signed with HMAC-SHA256. The shared secret is stored in macOS Keychain:

```bash
security add-generic-password -a "$USER" -s fleet_nerve_hmac_key -w "your-secret-key"
```

Or set via environment variable for CI:

```bash
export FLEET_NERVE_HMAC_KEY="your-secret-key"
```

### Replay Prevention

Messages include timestamps. Recipients reject messages older than 5 minutes.

### Log Sanitization

No message bodies, API keys, tokens, or SSH material appear in logs.
