#!/usr/bin/env bash
# enable-jetstream.sh — turn on NATS JetStream durability (blocker #2)
#
# Without this, nats-server runs without -js and every daemon boot logs
# `JetStream setup failed: ServiceUnavailableError`. Durable replay is off,
# offline nodes lose messages. With this, daemons create the FLEET stream on
# connect (24h retention) and offline peers catch up via durable consumers.
#
# Usage:
#   ./enable-jetstream.sh            # enable now (detects macOS launchd / Linux systemd)
#   ./enable-jetstream.sh --dry-run  # print what would change
#   NATS_STORE_DIR=/custom/path ./enable-jetstream.sh
#
# After running, restart nats-server + your fleet-nats daemon. Script offers to
# do it for you unless --no-restart is passed.

set -uo pipefail

DRY_RUN=false
NO_RESTART=false
STORE_DIR="${NATS_STORE_DIR:-$HOME/.multifleet/jetstream}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)     DRY_RUN=true; shift ;;
        --no-restart)  NO_RESTART=true; shift ;;
        --help|-h)
            head -16 "$0" | tail -14 | sed 's/^# //'
            exit 0
            ;;
        *) echo "unknown: $1" >&2; exit 1 ;;
    esac
done

run() { $DRY_RUN && echo "[dry-run] $*" || "$@"; }

log() { echo "[jetstream] $*"; }

detect_platform() {
    case "$(uname -s)" in
        Darwin) echo macos ;;
        Linux)  echo linux ;;
        *)      echo unknown ;;
    esac
}

PLATFORM=$(detect_platform)
log "platform: $PLATFORM"
log "store dir: $STORE_DIR"

run mkdir -p "$STORE_DIR"

case "$PLATFORM" in
    macos)
        PLIST="$HOME/Library/LaunchAgents/io.contextdna.nats-server.plist"
        if [[ ! -f "$PLIST" ]]; then
            log "no plist at $PLIST — run install.sh first or create nats-server plist"
            exit 2
        fi

        if grep -q '\-js' "$PLIST"; then
            log "plist already has -js flag — no change"
        else
            log "patching plist: add -js -sd $STORE_DIR"
            if ! $DRY_RUN; then
                # Insert before the last </array> that closes ProgramArguments
                /usr/bin/python3 - "$PLIST" "$STORE_DIR" <<'PY'
import sys, re, pathlib
path, sd = sys.argv[1], sys.argv[2]
p = pathlib.Path(path)
txt = p.read_text()
new_args = f"\t\t<string>-js</string>\n\t\t<string>-sd</string>\n\t\t<string>{sd}</string>\n\t"
# Add immediately before the first </array>
patched, n = re.subn(r"(\s*)</array>", lambda m: "\n" + new_args + m.group(0).lstrip(), txt, count=1)
if n == 0:
    sys.exit("could not find </array> in plist")
p.write_text(patched)
print(f"patched: {path}")
PY
            fi
        fi

        if ! $NO_RESTART; then
            log "reloading launchd unit"
            run launchctl unload "$PLIST"
            run launchctl load   "$PLIST"
        fi
        ;;

    linux)
        # systemd unit path used by install.sh
        UNIT="$HOME/.config/systemd/user/multifleet-nerve.service"
        SERVER_UNIT="$HOME/.config/systemd/user/nats-server.service"
        if [[ ! -f "$SERVER_UNIT" ]]; then
            log "no systemd nats-server.service at $SERVER_UNIT"
            log "systemd path expected: ExecStart=/usr/bin/nats-server -p 4222 -m 8222 -js -sd $STORE_DIR"
            log "Edit your nats-server.service manually then: systemctl --user daemon-reload; systemctl --user restart nats-server"
            exit 2
        fi

        if grep -q '\-js' "$SERVER_UNIT"; then
            log "systemd unit already has -js flag — no change"
        else
            log "patching systemd unit: add -js -sd $STORE_DIR"
            if ! $DRY_RUN; then
                run sed -i -E "s|^(ExecStart=.+nats-server.*)$|\\1 -js -sd $STORE_DIR|" "$SERVER_UNIT"
            fi
        fi

        if ! $NO_RESTART; then
            run systemctl --user daemon-reload
            run systemctl --user restart nats-server
        fi
        ;;

    *)
        log "unsupported platform — manually add '-js -sd $STORE_DIR' to your nats-server invocation"
        exit 2
        ;;
esac

log "done. Verify: 'nats --server nats://127.0.0.1:4222 stream list' should eventually show FLEET"
