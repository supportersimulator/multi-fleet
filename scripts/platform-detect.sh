#!/usr/bin/env bash
# platform-detect.sh — Cross-platform detection for multi-fleet
# Exports: FLEET_PLATFORM, FLEET_TEMP_DIR, FLEET_DAEMON_MGR, FLEET_HOME_DIR
# Usage: source this file or eval "$(./platform-detect.sh --export)"
set -euo pipefail

detect_platform() {
    local uname_s
    uname_s="$(uname -s 2>/dev/null || echo "Unknown")"

    case "$uname_s" in
        Darwin)
            FLEET_PLATFORM="macos"
            FLEET_TEMP_DIR="${TMPDIR:-/tmp}"
            # Remove trailing slash from macOS TMPDIR
            FLEET_TEMP_DIR="${FLEET_TEMP_DIR%/}"
            FLEET_DAEMON_MGR="launchctl"
            FLEET_HOME_DIR="$HOME"
            ;;
        Linux)
            # Check for WSL
            if grep -qiE '(microsoft|wsl)' /proc/version 2>/dev/null; then
                FLEET_PLATFORM="wsl"
                # Use Linux-native temp even on WSL
                FLEET_TEMP_DIR="/tmp"
                FLEET_DAEMON_MGR="systemd"
                FLEET_HOME_DIR="$HOME"
            else
                FLEET_PLATFORM="linux"
                FLEET_TEMP_DIR="/tmp"
                FLEET_DAEMON_MGR="systemd"
                FLEET_HOME_DIR="$HOME"
            fi
            ;;
        MINGW*|MSYS*|CYGWIN*)
            FLEET_PLATFORM="windows"
            FLEET_TEMP_DIR="${TEMP:-${TMP:-/tmp}}"
            FLEET_DAEMON_MGR="none"
            FLEET_HOME_DIR="${USERPROFILE:-$HOME}"
            ;;
        *)
            FLEET_PLATFORM="unknown"
            FLEET_TEMP_DIR="/tmp"
            FLEET_DAEMON_MGR="none"
            FLEET_HOME_DIR="$HOME"
            ;;
    esac

    export FLEET_PLATFORM FLEET_TEMP_DIR FLEET_DAEMON_MGR FLEET_HOME_DIR
}

print_detection() {
    echo "FLEET_PLATFORM=$FLEET_PLATFORM"
    echo "FLEET_TEMP_DIR=$FLEET_TEMP_DIR"
    echo "FLEET_DAEMON_MGR=$FLEET_DAEMON_MGR"
    echo "FLEET_HOME_DIR=$FLEET_HOME_DIR"
}

detect_platform

if [[ "${1:-}" == "--export" ]]; then
    # Output export statements for eval
    echo "export FLEET_PLATFORM=\"$FLEET_PLATFORM\""
    echo "export FLEET_TEMP_DIR=\"$FLEET_TEMP_DIR\""
    echo "export FLEET_DAEMON_MGR=\"$FLEET_DAEMON_MGR\""
    echo "export FLEET_HOME_DIR=\"$FLEET_HOME_DIR\""
elif [[ "${1:-}" == "--json" ]]; then
    printf '{"platform":"%s","temp_dir":"%s","daemon_mgr":"%s","home_dir":"%s"}\n' \
        "$FLEET_PLATFORM" "$FLEET_TEMP_DIR" "$FLEET_DAEMON_MGR" "$FLEET_HOME_DIR"
elif [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    # Executed directly (not sourced) — print summary
    echo "Fleet Platform Detection"
    echo "========================"
    print_detection
fi
