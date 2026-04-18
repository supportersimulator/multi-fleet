# macOS Setup

macOS is the primary platform. The setup script handles everything:

```bash
bash scripts/fleet-menubar-setup.sh            # interactive, auto-detects node
bash scripts/fleet-menubar-setup.sh mac2        # explicit node ID
```

This installs xbar, menu bar plugins, NATS LaunchAgent, fleet daemon LaunchAgent, and Keychain secrets. The sections below explain what it does if you need to customize or troubleshoot.

---

## LaunchAgents (Auto-Start on Boot)

### NATS Server

```bash
cat > "$HOME/Library/LaunchAgents/io.contextdna.nats-server.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>io.contextdna.nats-server</string>
    <key>ProgramArguments</key>
    <array>
        <string>$(which nats-server)</string>
        <string>-p</string>
        <string>4222</string>
        <string>-m</string>
        <string>8222</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/nats-server.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/nats-server.log</string>
</dict>
</plist>
EOF
```

### Fleet Daemon

```bash
cat > "$HOME/Library/LaunchAgents/io.contextdna.fleet-nats.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>io.contextdna.fleet-nats</string>
    <key>ProgramArguments</key>
    <array>
        <string>$(which python3)</string>
        <string>$REPO_ROOT/tools/fleet_nerve_nats.py</string>
        <string>serve</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key>
        <string>$HOME</string>
        <key>MULTIFLEET_NODE_ID</key>
        <string>$NODE_ID</string>
        <key>NATS_URL</key>
        <string>nats://127.0.0.1:4222</string>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
    <key>WorkingDirectory</key>
    <string>$REPO_ROOT</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/fleet-nats.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/fleet-nats.log</string>
</dict>
</plist>
EOF
```

### Managing LaunchAgents

```bash
# Load (start)
launchctl load "$HOME/Library/LaunchAgents/io.contextdna.nats-server.plist"
launchctl load "$HOME/Library/LaunchAgents/io.contextdna.fleet-nats.plist"

# Unload (stop)
launchctl unload "$HOME/Library/LaunchAgents/io.contextdna.nats-server.plist"

# Check status
launchctl list | grep contextdna

# View logs
tail -f /tmp/nats-server.log
tail -f /tmp/fleet-nats.log
```

---

## xbar Menu Bar Plugins

The setup script installs two xbar plugins:

1. **Fleet status** (`fleet-status.5m.sh`) -- shows `F:N` indicator with node count, refreshes every 5 minutes
2. **Context DNA** (`context-dna.1m.py`) -- brain icon for Context DNA status, refreshes every minute

Plugins live in `~/Library/Application Support/xbar/plugins/`.

Install xbar if missing:

```bash
brew install --cask xbar
```

---

## macOS Keychain for Secrets

Plists contain **zero secrets**. All API keys go in macOS Keychain under the service name `fleet-nerve`.

```bash
# Store a secret
security add-generic-password -s "fleet-nerve" -a "ANTHROPIC_API_KEY" -w "sk-ant-..."

# Read a secret
security find-generic-password -s "fleet-nerve" -a "ANTHROPIC_API_KEY" -w

# Update (delete + re-add)
security delete-generic-password -s "fleet-nerve" -a "ANTHROPIC_API_KEY"
security add-generic-password -s "fleet-nerve" -a "ANTHROPIC_API_KEY" -w "new-value"

# Check if a key exists
security find-generic-password -s "fleet-nerve" -a "ANTHROPIC_API_KEY" -w &>/dev/null && echo "exists"
```

Required keys: `Context_DNA_OPENAI`, `Context_DNA_Deepseek`, `ANTHROPIC_API_KEY`.

Non-secret config goes in `~/.fleet-nerve/env` (chmod 600, no API keys).

---

## NATS Server

```bash
brew install nats-server
nats-server --version

# Manual start (without LaunchAgent)
nats-server -p 4222 -m 8222

# Verify
curl -s http://127.0.0.1:8222/varz | python3 -m json.tool
```

---

## Caffeinate (Prevent Sleep)

Prevent macOS from sleeping during long fleet operations:

```bash
# Keep awake while a command runs
caffeinate -i fleet dashboard --web

# Keep awake for 2 hours (7200 seconds)
caffeinate -t 7200 &

# Keep awake while fleet daemon is running
caffeinate -w $(pgrep -f fleet_nerve_nats) &
```

---

## Firewall

Allow NATS (4222) and fleet daemon (8855) through the macOS firewall:

**System Preferences** (recommended): System Settings > Network > Firewall > Options > add `nats-server` and `python3` to allowed apps.

**Command line** (`pf`):

```bash
# Add rules to /etc/pf.conf
echo "pass in proto tcp from any to any port 4222" | sudo tee -a /etc/pf.conf
echo "pass in proto tcp from any to any port 8855" | sudo tee -a /etc/pf.conf
sudo pfctl -f /etc/pf.conf
```

For LAN-only access, restrict to your subnet:

```bash
echo "pass in proto tcp from 192.168.1.0/24 to any port {4222, 8855}" | sudo tee -a /etc/pf.conf
```
