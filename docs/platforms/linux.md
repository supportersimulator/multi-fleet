# Linux Setup

Linux uses systemd for service management instead of macOS LaunchAgents. Everything else (Python, NATS, fleet daemon) is identical.

---

## Install NATS Server

```bash
# Debian/Ubuntu
sudo apt install nats-server

# Fedora/RHEL
sudo dnf install nats-server

# From binary (any distro)
NATS_VER="2.10.24"
ARCH=$(uname -m); [ "$ARCH" = "x86_64" ] && ARCH="amd64"
curl -sSL "https://github.com/nats-io/nats-server/releases/download/v${NATS_VER}/nats-server-v${NATS_VER}-linux-${ARCH}.tar.gz" \
  | tar xz -C /tmp/
sudo mv "/tmp/nats-server-v${NATS_VER}-linux-${ARCH}/nats-server" /usr/local/bin/
nats-server --version
```

---

## systemd Unit Files

### NATS Server

```bash
sudo tee /etc/systemd/system/nats-server.service <<EOF
[Unit]
Description=NATS Server (Multi-Fleet)
After=network.target

[Service]
Type=simple
ExecStart=$(which nats-server) -p 4222 -m 8222
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
User=$USER

[Install]
WantedBy=multi-user.target
EOF
```

### Fleet Nerve Daemon

```bash
sudo tee /etc/systemd/system/fleet-nerve.service <<EOF
[Unit]
Description=Fleet Nerve Daemon (Multi-Fleet)
After=network.target nats-server.service
Wants=nats-server.service

[Service]
Type=simple
ExecStart=$HOME/.venv/bin/python3 $HOME/dev/er-simulator-superrepo/tools/fleet_nerve_nats.py serve
WorkingDirectory=$HOME/dev/er-simulator-superrepo
EnvironmentFile=$HOME/.fleet-nerve/env
Environment=MULTIFLEET_NODE_ID=$(hostname -s)
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
User=$USER

[Install]
WantedBy=multi-user.target
EOF
```

**Important**: Replace paths above with your actual repo location and venv path.

### Enable and Start

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now nats-server
sudo systemctl enable --now fleet-nerve

# Check status
systemctl status nats-server
systemctl status fleet-nerve

# View logs
journalctl -u fleet-nerve -f
journalctl -u nats-server -f
```

### User-Level Units (No sudo)

If you prefer user-level systemd (no root required), place unit files in `~/.config/systemd/user/` and use `systemctl --user`:

```bash
mkdir -p "$HOME/.config/systemd/user"
# Move the unit files there, then:
systemctl --user daemon-reload
systemctl --user enable --now nats-server
systemctl --user enable --now fleet-nerve
```

---

## Secret Management

Linux has no system keychain equivalent to macOS Keychain. Use an environment file with restricted permissions:

```bash
mkdir -p "$HOME/.fleet-nerve"

cat > "$HOME/.fleet-nerve/env" <<EOF
# Fleet Nerve secrets -- chmod 600, never commit
MULTIFLEET_NODE_ID=$(hostname -s)
NATS_URL=nats://127.0.0.1:4222
ANTHROPIC_API_KEY=sk-ant-...
Context_DNA_OPENAI=sk-...
Context_DNA_Deepseek=sk-...
EOF

chmod 600 "$HOME/.fleet-nerve/env"
```

The systemd unit reads this file via `EnvironmentFile=`. Verify permissions:

```bash
ls -la "$HOME/.fleet-nerve/env"
# Should show: -rw------- 1 $USER $USER
```

Add to `.gitignore`:

```
.fleet-nerve/
```

---

## Firewall Rules

### UFW (Ubuntu/Debian)

```bash
# Allow NATS and fleet daemon from LAN only
sudo ufw allow from 192.168.1.0/24 to any port 4222 proto tcp comment "NATS"
sudo ufw allow from 192.168.1.0/24 to any port 8855 proto tcp comment "Fleet Nerve"

# Or allow from anywhere
sudo ufw allow 4222/tcp
sudo ufw allow 8855/tcp

sudo ufw status
```

### iptables (any distro)

```bash
sudo iptables -A INPUT -p tcp --dport 4222 -s 192.168.1.0/24 -j ACCEPT
sudo iptables -A INPUT -p tcp --dport 8855 -s 192.168.1.0/24 -j ACCEPT

# Persist across reboots
sudo iptables-save | sudo tee /etc/iptables/rules.v4
```

### firewalld (Fedora/RHEL)

```bash
sudo firewall-cmd --permanent --add-port=4222/tcp
sudo firewall-cmd --permanent --add-port=8855/tcp
sudo firewall-cmd --reload
```

---

## Sleep Prevention

```bash
# Prevent sleep while fleet daemon runs
systemd-inhibit --what=sleep --who="fleet-nerve" --why="Fleet operations active" \
  sleep infinity &
```

---

## Verify

```bash
curl -s http://127.0.0.1:8855/health | python3 -m json.tool
curl -s http://127.0.0.1:8222/varz | python3 -m json.tool
fleet status
fleet doctor
```
