# Install on macOS

Multi-Fleet's home platform. All 9 channels (P0-P8) fully supported, including P8 direct text input via `osascript`.

Tested on macOS 13 Ventura through macOS 15 Sequoia, Intel and Apple Silicon.

## Prerequisites

- macOS 13+
- Python 3.10+ (`python3 --version`)
- [Homebrew](https://brew.sh) (recommended)
- `nats-server` via Homebrew or direct binary

```bash
# Homebrew (if not installed)
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# Dependencies
brew install python@3.12 nats-server autossh
```

## Install Multi-Fleet

```bash
pip3 install --user multifleet
pip3 install --user 'multifleet[full]'   # optional cryptography extras

fleet --version
fleet doctor
```

If `fleet` is not on `$PATH`, add Python user base bin:

```bash
echo 'export PATH="$(python3 -m site --user-base)/bin:$PATH"' >> ~/.zshrc && source ~/.zshrc
```

## Secret storage (Keychain)

macOS Keychain is the default HMAC key backend. Multi-Fleet reads `fleet_nerve_hmac_key` automatically.

```bash
# Generate and store a 32-byte key
openssl rand -hex 32 | xargs -I{} security add-generic-password \
  -a "$USER" -s fleet_nerve_hmac_key -w "{}" -U

# Verify
security find-generic-password -a "$USER" -s fleet_nerve_hmac_key -w
```

Override with an env var for CI or shared dev boxes:

```bash
export MULTIFLEET_HMAC_KEY="$(openssl rand -hex 32)"
```

## launchd (run at login)

Use the packaged installer to generate and load LaunchAgent plists from templates:

```bash
bash scripts/install-launchd-plists.sh
```

This installs:

- `~/Library/LaunchAgents/com.multifleet.nats.plist` — NATS server
- `~/Library/LaunchAgents/com.multifleet.nerve.plist` — Fleet Nerve daemon

Manual load if needed:

```bash
launchctl load -w ~/Library/LaunchAgents/com.multifleet.nerve.plist
launchctl list | grep multifleet
```

## Verify

```bash
curl -sf http://127.0.0.1:8855/health | jq .
fleet status
fleet doctor
tail -f /tmp/fleet-nerve/logs/fleet-nerve.stdout.log
```

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `fleet: command not found` | Add `$(python3 -m site --user-base)/bin` to `$PATH` |
| Keychain prompt loops | Click "Always Allow"; or set `MULTIFLEET_HMAC_KEY` env var |
| LaunchAgent not starting | `launchctl bootout gui/$UID ~/Library/LaunchAgents/com.multifleet.nerve.plist` then re-load |
| P8 text input blocked | Grant Terminal/VS Code Accessibility permission in System Settings -> Privacy & Security |
| `nats-server: command not found` | `brew install nats-server` or download from [nats.io/download](https://nats.io/download/) |
