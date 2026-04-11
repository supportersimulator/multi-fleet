#!/usr/bin/env bash
# install.sh — Unified installer for multi-fleet
# Detects platform, installs deps, creates config, sets up daemon, verifies.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_DIR="$PLUGIN_DIR/config"

# --- Colors (disable if no tty) ---
if [[ -t 1 ]]; then
    GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
else
    GREEN=''; RED=''; YELLOW=''; BLUE=''; NC=''
fi

log()  { echo -e "${GREEN}[fleet-install]${NC} $*"; }
warn() { echo -e "${YELLOW}[fleet-install]${NC} $*" >&2; }
err()  { echo -e "${RED}[fleet-install]${NC} $*" >&2; }
info() { echo -e "${BLUE}[fleet-install]${NC} $*"; }

# --- Platform detection ---
source "$SCRIPT_DIR/platform-detect.sh"
log "Platform: $FLEET_PLATFORM | Temp: $FLEET_TEMP_DIR | Daemon: $FLEET_DAEMON_MGR"

# --- Parse arguments ---
IDE_TARGET=""
SKIP_DAEMON=false
NODE_ID=""
DRY_RUN=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --ide)       IDE_TARGET="$2"; shift 2 ;;
        --node-id)   NODE_ID="$2"; shift 2 ;;
        --skip-daemon) SKIP_DAEMON=true; shift ;;
        --dry-run)   DRY_RUN=true; shift ;;
        --help|-h)
            echo "Usage: install.sh [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --ide <claude-code|vscode|cursor>  Target IDE (default: claude-code)"
            echo "  --node-id <name>                   Node identifier (default: hostname)"
            echo "  --skip-daemon                      Skip daemon setup"
            echo "  --dry-run                          Show what would be done"
            echo "  --help                             Show this help"
            exit 0
            ;;
        *) err "Unknown option: $1"; exit 1 ;;
    esac
done

IDE_TARGET="${IDE_TARGET:-claude-code}"
NODE_ID="${NODE_ID:-$(hostname -s 2>/dev/null || hostname)}"

# --- Step 1: Install Python dependencies ---
install_python_deps() {
    log "Installing Python dependencies..."
    local pip_cmd="pip3"
    if command -v pip3 &>/dev/null; then
        pip_cmd="pip3"
    elif command -v pip &>/dev/null; then
        pip_cmd="pip"
    else
        err "pip not found. Install Python 3 first."
        return 1
    fi

    if $DRY_RUN; then
        info "[dry-run] Would run: $pip_cmd install nats-py"
        return 0
    fi

    $pip_cmd install --quiet nats-py 2>/dev/null || {
        warn "pip install failed, trying with --user flag..."
        $pip_cmd install --quiet --user nats-py
    }
    log "Python dependencies installed."
}

# --- Step 2: Check/install NATS server ---
check_nats_server() {
    if command -v nats-server &>/dev/null; then
        log "nats-server found: $(nats-server --version 2>/dev/null || echo 'installed')"
        return 0
    fi

    warn "nats-server not found."

    if $DRY_RUN; then
        info "[dry-run] Would install nats-server"
        return 0
    fi

    case "$FLEET_PLATFORM" in
        macos)
            if command -v brew &>/dev/null; then
                log "Installing nats-server via Homebrew..."
                brew install nats-server
            else
                warn "Install Homebrew first, or download nats-server from https://nats.io/download/"
                return 1
            fi
            ;;
        linux|wsl)
            if command -v apt-get &>/dev/null; then
                log "Installing nats-server via apt..."
                sudo apt-get update -qq && sudo apt-get install -y -qq nats-server 2>/dev/null || {
                    warn "apt install failed. Download from https://nats.io/download/"
                    warn "Or: curl -sf https://binaries.nats.dev/nats-io/nats-server/v2@latest | sh"
                    return 1
                }
            else
                warn "Download nats-server from https://nats.io/download/"
                return 1
            fi
            ;;
        *)
            warn "Manual nats-server install required: https://nats.io/download/"
            return 1
            ;;
    esac
}

# --- Step 3: Check autossh ---
check_autossh() {
    if command -v autossh &>/dev/null; then
        log "autossh found."
        return 0
    fi

    warn "autossh not found (optional — needed for SSH tunnel persistence)."

    if $DRY_RUN; then
        info "[dry-run] Would install autossh"
        return 0
    fi

    case "$FLEET_PLATFORM" in
        macos)
            if command -v brew &>/dev/null; then
                log "Installing autossh via Homebrew..."
                brew install autossh
            fi
            ;;
        linux|wsl)
            if command -v apt-get &>/dev/null; then
                sudo apt-get install -y -qq autossh 2>/dev/null || warn "autossh install failed."
            fi
            ;;
    esac
}

# --- Step 4: Create config from template ---
create_config() {
    local config_file="$CONFIG_DIR/config.json"
    if [[ -f "$config_file" ]]; then
        log "Config already exists at $config_file — skipping."
        return 0
    fi

    if $DRY_RUN; then
        info "[dry-run] Would create $config_file from template"
        return 0
    fi

    log "Creating config from template..."
    mkdir -p "$CONFIG_DIR"

    # Generate config from template with detected values
    local log_dir="$FLEET_TEMP_DIR/fleet-nerve/logs"
    local pid_dir="$FLEET_TEMP_DIR/fleet-nerve/pids"

    sed -e "s/\"node_id\": \"CHANGE_ME\"/\"node_id\": \"$NODE_ID\"/" \
        -e "s|\"log_dir\": \"\"|\"log_dir\": \"$log_dir\"|" \
        -e "s|\"pid_dir\": \"\"|\"pid_dir\": \"$pid_dir\"|" \
        -e "s/\"ide\": \"claude-code\"/\"ide\": \"$IDE_TARGET\"/" \
        "$CONFIG_DIR/config.template.json" > "$config_file"

    log "Config created: $config_file"
    warn "Edit $config_file to set NATS server address and SSH tunnel targets."
}

# --- Step 5: Setup daemon ---
setup_daemon_macos() {
    local plist_label="com.multifleet.nerve"
    local plist_path="$FLEET_HOME_DIR/Library/LaunchAgents/${plist_label}.plist"
    local log_dir="$FLEET_TEMP_DIR/fleet-nerve/logs"

    if [[ -f "$plist_path" ]]; then
        log "LaunchAgent already exists: $plist_path"
        return 0
    fi

    if $DRY_RUN; then
        info "[dry-run] Would create LaunchAgent at $plist_path"
        return 0
    fi

    mkdir -p "$(dirname "$plist_path")" "$log_dir"

    cat > "$plist_path" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${plist_label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>${PLUGIN_DIR}/tools/fleet_nerve_mcp.py</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>MULTIFLEET_NODE_ID</key>
        <string>${NODE_ID}</string>
        <key>FLEET_NERVE_PORT</key>
        <string>8855</string>
        <key>FLEET_CONFIG</key>
        <string>${CONFIG_DIR}/config.json</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${log_dir}/fleet-nerve.stdout.log</string>
    <key>StandardErrorPath</key>
    <string>${log_dir}/fleet-nerve.stderr.log</string>
</dict>
</plist>
PLIST

    log "LaunchAgent created: $plist_path"
    log "Load with: launchctl load $plist_path"
}

setup_daemon_linux() {
    local service_name="fleet-nerve"
    local unit_dir="$FLEET_HOME_DIR/.config/systemd/user"
    local unit_path="$unit_dir/${service_name}.service"

    if [[ -f "$unit_path" ]]; then
        log "systemd unit already exists: $unit_path"
        return 0
    fi

    if $DRY_RUN; then
        info "[dry-run] Would create systemd unit at $unit_path"
        return 0
    fi

    mkdir -p "$unit_dir"

    cat > "$unit_path" <<UNIT
[Unit]
Description=Fleet Nerve MCP Server
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 ${PLUGIN_DIR}/tools/fleet_nerve_mcp.py
Environment=MULTIFLEET_NODE_ID=${NODE_ID}
Environment=FLEET_NERVE_PORT=8855
Environment=FLEET_CONFIG=${CONFIG_DIR}/config.json
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
UNIT

    log "systemd unit created: $unit_path"
    log "Enable with: systemctl --user enable --now $service_name"
}

setup_daemon() {
    if $SKIP_DAEMON; then
        log "Daemon setup skipped (--skip-daemon)."
        return 0
    fi

    case "$FLEET_DAEMON_MGR" in
        launchctl) setup_daemon_macos ;;
        systemd)   setup_daemon_linux ;;
        none)      warn "No daemon manager available on this platform. Start fleet-nerve manually." ;;
    esac
}

# --- Step 6: Verify ---
verify_install() {
    log "Verifying installation..."
    local ok=true

    # Python + nats-py
    if python3 -c "import nats" 2>/dev/null; then
        log "  nats-py: OK"
    else
        warn "  nats-py: NOT FOUND"
        ok=false
    fi

    # nats-server
    if command -v nats-server &>/dev/null; then
        log "  nats-server: OK"
    else
        warn "  nats-server: NOT FOUND (install separately)"
    fi

    # autossh
    if command -v autossh &>/dev/null; then
        log "  autossh: OK"
    else
        info "  autossh: not installed (optional)"
    fi

    # Config
    if [[ -f "$CONFIG_DIR/config.json" ]]; then
        log "  config: OK ($CONFIG_DIR/config.json)"
    else
        warn "  config: MISSING"
        ok=false
    fi

    # IDE adapter
    local adapter_file="$CONFIG_DIR/ide-adapters/${IDE_TARGET}.json"
    if [[ -f "$adapter_file" ]]; then
        log "  IDE adapter ($IDE_TARGET): OK"
    else
        warn "  IDE adapter ($IDE_TARGET): NOT FOUND"
    fi

    if $ok; then
        log "Installation verified successfully."
    else
        warn "Some checks failed. Review warnings above."
    fi
}

# --- Main ---
main() {
    echo ""
    log "Multi-Fleet Installer"
    log "====================="
    log "Node: $NODE_ID | IDE: $IDE_TARGET | Platform: $FLEET_PLATFORM"
    echo ""

    install_python_deps
    check_nats_server
    check_autossh
    create_config
    setup_daemon
    echo ""
    verify_install
    echo ""
    log "Done. Next steps:"
    log "  1. Edit $CONFIG_DIR/config.json with your NATS server and peer details"
    log "  2. Start the daemon or run fleet-nerve manually"
    log "  3. Use 'fleet-check' skill to verify connectivity"
}

main
