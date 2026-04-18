#!/usr/bin/env bash
# uninstall.sh — Teardown mirror of install.sh
# Removes daemon unit/plist, optionally config, optionally pip package.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_DIR="$PLUGIN_DIR/config"

if [[ -t 1 ]]; then
    GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
else
    GREEN=''; RED=''; YELLOW=''; BLUE=''; NC=''
fi
log()  { echo -e "${GREEN}[fleet-uninstall]${NC} $*"; }
warn() { echo -e "${YELLOW}[fleet-uninstall]${NC} $*" >&2; }
err()  { echo -e "${RED}[fleet-uninstall]${NC} $*" >&2; }
info() { echo -e "${BLUE}[fleet-uninstall]${NC} $*"; }

source "$SCRIPT_DIR/platform-detect.sh"

DRY_RUN=false
KEEP_CONFIG=true
KEEP_PYTHON=true
ASSUME_YES=false

usage() {
    cat <<EOF
Usage: uninstall.sh [OPTIONS]

Options:
  --purge          Remove EVERYTHING (daemon + config + pip package + logs)
  --daemon-only    Remove only the daemon/service (default scope)
  --pip            Also uninstall the 'multifleet' pip package
  --config         Also remove \$CONFIG_DIR/config.json + keychain entries
  --dry-run        Print actions without executing
  --yes            Skip confirmation prompt
  --help           Show this help

What gets removed by default (--daemon-only):
  macOS: ~/Library/LaunchAgents/com.multifleet.nerve.plist (unloaded first)
  Linux: ~/.config/systemd/user/multifleet-nerve.service (stopped + disabled first)
  Always: /tmp/fleet-seed-*.md, /tmp/fleet-nats.log

What --purge additionally removes:
  \$CONFIG_DIR/config.json (node config, NATS URL, tokens)
  Keychain entries: fleet-nerve, DISCORD_BOT_TOKEN, OPENAI_API_KEY (macOS)
  Python package 'multifleet' via pip
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --purge)       KEEP_CONFIG=false; KEEP_PYTHON=false; shift ;;
        --daemon-only) KEEP_CONFIG=true;  KEEP_PYTHON=true;  shift ;;
        --pip)         KEEP_PYTHON=false; shift ;;
        --config)      KEEP_CONFIG=false; shift ;;
        --dry-run)     DRY_RUN=true; shift ;;
        --yes|-y)      ASSUME_YES=true; shift ;;
        --help|-h)     usage; exit 0 ;;
        *) err "Unknown option: $1"; usage; exit 1 ;;
    esac
done

run() {
    if $DRY_RUN; then
        info "[dry-run] $*"
    else
        "$@"
    fi
}

confirm() {
    $ASSUME_YES && return 0
    $DRY_RUN && return 0
    local msg="$1"
    read -r -p "$msg [y/N] " reply
    [[ "$reply" =~ ^[Yy]$ ]]
}

log "Platform: ${FLEET_PLATFORM:-unknown}"
log "Scope: daemon=yes config=$([ "$KEEP_CONFIG" = false ] && echo yes || echo no) pip=$([ "$KEEP_PYTHON" = false ] && echo yes || echo no)"

if ! confirm "Proceed with uninstall?"; then
    warn "Aborted."
    exit 0
fi

# ── macOS LaunchAgent ──
remove_launchagent() {
    local label="com.multifleet.nerve"
    local plist="${FLEET_HOME_DIR:-$HOME}/Library/LaunchAgents/${label}.plist"
    if [[ -f "$plist" ]]; then
        log "Unloading LaunchAgent: $label"
        run launchctl unload "$plist" 2>/dev/null || true
        run launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
        log "Removing plist: $plist"
        run rm -f "$plist"
    else
        info "No LaunchAgent at $plist"
    fi
}

# ── Linux systemd user unit ──
remove_systemd_unit() {
    local unit="multifleet-nerve.service"
    local unit_path="${FLEET_HOME_DIR:-$HOME}/.config/systemd/user/$unit"
    if [[ -f "$unit_path" ]]; then
        log "Stopping + disabling systemd unit: $unit"
        run systemctl --user stop "$unit" 2>/dev/null || true
        run systemctl --user disable "$unit" 2>/dev/null || true
        log "Removing unit file: $unit_path"
        run rm -f "$unit_path"
        run systemctl --user daemon-reload 2>/dev/null || true
    else
        info "No systemd unit at $unit_path"
    fi
}

case "${FLEET_PLATFORM:-}" in
    macos) remove_launchagent ;;
    linux) remove_systemd_unit ;;
    *)     warn "Unknown platform — attempting both removal paths"
           remove_launchagent
           remove_systemd_unit
           ;;
esac

# ── Tmp seed/log cleanup (always safe) ──
log "Clearing /tmp seed files + logs"
run rm -f /tmp/fleet-seed-*.md /tmp/fleet-nats.log /tmp/fleet-inbox-watcher.log /tmp/discord-wake.log
run rm -rf /tmp/fleet-seed-archive /tmp/fleet-agent-results /tmp/fleet-discord-images

# ── Config removal (--purge or --config) ──
if ! $KEEP_CONFIG; then
    local_config="$CONFIG_DIR/config.json"
    if [[ -f "$local_config" ]]; then
        log "Removing $local_config"
        run rm -f "$local_config"
    fi
    user_config_dir="${FLEET_HOME_DIR:-$HOME}/.multifleet"
    if [[ -d "$user_config_dir" ]]; then
        log "Removing $user_config_dir"
        run rm -rf "$user_config_dir"
    fi
    if [[ "${FLEET_PLATFORM:-}" == "macos" ]]; then
        log "Removing macOS keychain entries (fleet-nerve, DISCORD_BOT_TOKEN, OPENAI_API_KEY)"
        run security delete-generic-password -s fleet-nerve 2>/dev/null || true
        run security delete-generic-password -s DISCORD_BOT_TOKEN 2>/dev/null || true
        run security delete-generic-password -s OPENAI_API_KEY 2>/dev/null || true
    fi
fi

# ── Python pkg removal (--purge or --pip) ──
if ! $KEEP_PYTHON; then
    log "Uninstalling 'multifleet' Python package"
    if command -v pip3 &>/dev/null; then
        run pip3 uninstall -y multifleet 2>/dev/null || true
    elif command -v pip &>/dev/null; then
        run pip uninstall -y multifleet 2>/dev/null || true
    else
        warn "pip not found — skip Python package removal"
    fi
fi

log "Uninstall complete."
$KEEP_CONFIG && info "Config kept at $CONFIG_DIR/config.json (re-run with --config or --purge to remove)"
$KEEP_PYTHON && info "Python package kept (re-run with --pip or --purge to remove)"
exit 0
