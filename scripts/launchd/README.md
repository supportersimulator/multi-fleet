# Fleet LaunchAgent Templates

Template plist files for the multi-fleet system. These replace hardcoded `/Users/<user>/` paths with placeholders so any user can deploy the fleet.

## Templates

| File | Purpose |
|------|---------|
| `io.contextdna.fleet-nerve.plist.template` | Fleet Nerve HTTP daemon (port 8855) |
| `io.contextdna.nats-server.plist.template` | NATS message broker (port 4222) |
| `io.contextdna.fleet-nats.plist.template` | Fleet NATS bridge daemon |
| `io.contextdna.fleet-tunnel.plist.template` | Persistent SSH tunnels (autossh) |

## Placeholders

| Placeholder | Description | Example |
|-------------|-------------|---------|
| `__HOME__` | User home directory | `/Users/alice` |
| `__REPO_ROOT__` | Path to repo checkout | `/Users/alice/dev/er-simulator-superrepo` |
| `__NODE_ID__` | Fleet node identifier | `mac1`, `mac2`, `mac3` |
| `__PYTHON_PATH__` | Path to python3 binary | `/opt/homebrew/bin/python3` |
| `__NATS_SERVER_PATH__` | Path to nats-server binary | `/opt/homebrew/bin/nats-server` |

## Manual Install

```bash
# Set your values
HOME_DIR="$HOME"
REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
NODE="$(hostname -s | tr '[:upper:]' '[:lower:]')"
PY="$(which python3)"
NATS="$(which nats-server)"

# Generate and install a plist
sed -e "s|__HOME__|$HOME_DIR|g" \
    -e "s|__REPO_ROOT__|$REPO|g" \
    -e "s|__NODE_ID__|$NODE|g" \
    -e "s|__PYTHON_PATH__|$PY|g" \
    io.contextdna.fleet-nerve.plist.template \
    > ~/Library/LaunchAgents/io.contextdna.fleet-nerve.plist

launchctl load ~/Library/LaunchAgents/io.contextdna.fleet-nerve.plist
```

## Automated Install

The setup scripts already generate plists dynamically with the correct paths:

- `scripts/fleet-nerve-setup.sh` -- creates `fleet-nerve` plist using `$HOME`, `$REPO_ROOT`, `$PYTHON`, `$NODE_ID`
- `scripts/fleet-menubar-setup.sh` -- creates `nats-server` and `fleet-nats` plists
- `scripts/fleet-tunnel.sh install` -- creates `fleet-tunnel` plist
- `multi-fleet/scripts/install.sh` -- creates `com.multifleet.nerve` plist

All setup scripts use shell variable expansion (no hardcoded user paths), so running them on any machine produces correct plists for that user. These templates exist as reference and for manual deployment when the full setup scripts are not needed.
