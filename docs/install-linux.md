# Install on Linux

Multi-Fleet on Linux uses **systemd user services** for daemon management and a choice of secret backends (`pass`, GNOME Keyring, or a file with mode `0600`). No macOS Keychain dependency.

Tested on Ubuntu 22.04+, Debian 12+, Fedora 39+, Arch. Other distros with `systemd --user` should work.

## Prerequisites

- Python 3.10+ (`python3 --version`)
- `systemd --user` (`systemctl --user status`)
- `nats-server` (from [GitHub releases](https://github.com/nats-io/nats-server/releases/latest))
- Optional: `pass` or `gnome-keyring` for secret storage

### Install NATS server

```bash
# Download latest release (Linux amd64)
curl -fsSL https://binaries.nats.dev/nats-io/nats-server/v2@latest | sh
sudo install -m 755 nats-server /usr/local/bin/
nats-server --version

# Or on Debian/Ubuntu if available in repos:
sudo apt-get install -y nats-server
```

## Install Multi-Fleet

```bash
pip install --user multifleet
# With optional cryptography extras (HMAC hardening):
pip install --user 'multifleet[full]'

# Verify
fleet --version
fleet doctor
```

If `fleet` is not on `$PATH`, add `~/.local/bin`:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc && source ~/.bashrc
```

## Secret storage (HMAC key)

macOS Keychain does not exist on Linux. Pick one backend:

### Option A: `pass` (recommended)

```bash
sudo apt-get install -y pass
pass init "your-gpg-key-id"
pass insert multifleet/hmac
# Multi-Fleet reads via: pass show multifleet/hmac
export MULTIFLEET_HMAC_CMD='pass show multifleet/hmac'
```

### Option B: GNOME Keyring via `keyring` package

```bash
pip install --user 'keyring[secretservice]'
python3 -c "import keyring; keyring.set_password('multifleet', 'hmac', 'YOUR_KEY')"
```

### Option C: File with mode 0600 (simplest)

```bash
mkdir -p ~/.config/multifleet
umask 077
openssl rand -hex 32 > ~/.config/multifleet/hmac.key
chmod 600 ~/.config/multifleet/hmac.key
export MULTIFLEET_HMAC_KEY_FILE=~/.config/multifleet/hmac.key
```

## systemd user services

Replace macOS `launchd` plists with systemd user units.

### `multifleet-nats.service`

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/multifleet-nats.service <<'UNIT'
[Unit]
Description=NATS server for Multi-Fleet
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/nats-server -p 4222 -m 8222
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
UNIT

systemctl --user daemon-reload
systemctl --user enable --now multifleet-nats.service
systemctl --user status multifleet-nats.service
```

### `multifleet-nerve.service`

```bash
cat > ~/.config/systemd/user/multifleet-nerve.service <<UNIT
[Unit]
Description=Multi-Fleet Nerve daemon
After=multifleet-nats.service
Wants=multifleet-nats.service

[Service]
Type=simple
Environment=MULTIFLEET_NODE_ID=$(hostname -s)
Environment=FLEET_NERVE_PORT=8855
Environment=MULTIFLEET_HMAC_KEY_FILE=%h/.config/multifleet/hmac.key
ExecStart=%h/.local/bin/fleet-nerve
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
UNIT

systemctl --user daemon-reload
systemctl --user enable --now multifleet-nerve.service
```

### Keep services running after logout

```bash
sudo loginctl enable-linger "$USER"
```

## Verify

```bash
curl -sf http://127.0.0.1:8855/health | jq .
fleet status
fleet doctor
journalctl --user -u multifleet-nerve.service -f
```

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `systemctl --user` fails | Enable lingering: `sudo loginctl enable-linger $USER` |
| `fleet: command not found` | Add `~/.local/bin` to `$PATH` |
| NATS connection refused | Check firewall: `sudo ufw allow 4222/tcp` (LAN only) |
| HMAC key not found | Confirm `MULTIFLEET_HMAC_KEY_FILE` path, check mode `600` |
| Port 8855 in use | `ss -tlnp | grep 8855`; adjust `FLEET_NERVE_PORT` |
