#!/usr/bin/env bash
# Multi-Fleet Install Validation
# Proves the zero-config promise by checking every prerequisite,
# config, service, and communication path from a fresh-install perspective.
#
# Usage: bash multi-fleet/scripts/validate-install.sh
#        Or: cd multi-fleet && bash scripts/validate-install.sh

set -o pipefail

# --- Resolve paths ---
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLUGIN_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$PLUGIN_ROOT/.." && pwd)"

# --- Counters ---
PASS=0
FAIL=0
TOTAL=0

# --- Output helpers ---
pass() {
  PASS=$((PASS + 1))
  TOTAL=$((TOTAL + 1))
  printf '[PASS] %s\n' "$1"
}

fail() {
  FAIL=$((FAIL + 1))
  TOTAL=$((TOTAL + 1))
  printf '[FAIL] %s\n' "$1"
}

separator() {
  echo ""
  echo "--- $1 ---"
}

# ============================================================
echo ""
echo "Multi-Fleet Install Validation"
echo "=============================="
echo ""

# ----------------------------------------------------------
separator "Prerequisites"
# ----------------------------------------------------------

# 1. Python 3.10+
if command -v python3 &>/dev/null; then
  PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null)
  PY_MAJOR=$(echo "$PY_VER" | cut -d. -f1)
  PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)
  if [ "$PY_MAJOR" -ge 3 ] 2>/dev/null && [ "$PY_MINOR" -ge 10 ] 2>/dev/null; then
    pass "Python ${PY_VER}"
  else
    fail "Python ${PY_VER} found but 3.10+ required"
  fi
else
  fail "python3 not found -- install Python 3.10+"
fi

# 2. nats-py importable
if python3 -c "import nats" 2>/dev/null; then
  pass "nats-py installed"
else
  fail "nats-py not importable -- pip install nats-py"
fi

# 3. tmux
if command -v tmux &>/dev/null; then
  pass "tmux $(tmux -V 2>/dev/null | head -1)"
else
  fail "tmux not found -- brew install tmux"
fi

# 4. jq
if command -v jq &>/dev/null; then
  pass "jq installed"
else
  fail "jq not found -- brew install jq"
fi

# 5. git
if command -v git &>/dev/null; then
  pass "git installed"
else
  fail "git not found -- install git"
fi

# ----------------------------------------------------------
separator "Configuration"
# ----------------------------------------------------------

# 6. Config file
CONFIG_FOUND=""
for candidate in \
  "$PLUGIN_ROOT/.multifleet/config.json" \
  "$PLUGIN_ROOT/config/config.template.json" \
  "$HOME/.multifleet/config.json"; do
  if [ -f "$candidate" ]; then
    CONFIG_FOUND="$candidate"
    break
  fi
done

if [ -n "$CONFIG_FOUND" ]; then
  pass "Config file exists ($CONFIG_FOUND)"
else
  fail "No config found -- copy config/config.template.json to .multifleet/config.json"
fi

# ----------------------------------------------------------
separator "Services"
# ----------------------------------------------------------

# 7. Daemon health
DAEMON_URL="http://127.0.0.1:8855"
if curl -sf --max-time 3 "$DAEMON_URL/health" >/dev/null 2>&1; then
  DAEMON_STATUS=$(curl -sf --max-time 3 "$DAEMON_URL/health" 2>/dev/null)
  pass "Fleet daemon running on :8855"
else
  fail "Fleet daemon not responding -- python3 tools/fleet_nerve_nats.py serve"
fi

# 8. NATS server
if nc -z 127.0.0.1 4222 2>/dev/null; then
  pass "NATS server reachable on :4222"
elif command -v nats-server &>/dev/null; then
  fail "nats-server installed but not running on :4222 -- nats-server -p 4222 &"
else
  fail "NATS not reachable on :4222 -- brew install nats-server && nats-server -p 4222 &"
fi

# ----------------------------------------------------------
separator "Plugin Structure"
# ----------------------------------------------------------

# 9. Hook runner
HOOK_FILE="$PLUGIN_ROOT/hooks/run-hook.cmd"
if [ -f "$HOOK_FILE" ]; then
  if [ -x "$HOOK_FILE" ]; then
    pass "run-hook.cmd exists and executable"
  else
    fail "run-hook.cmd exists but not executable -- chmod +x hooks/run-hook.cmd"
  fi
else
  fail "run-hook.cmd not found at hooks/run-hook.cmd"
fi

# 10. Skills count
SKILLS_DIR="$PLUGIN_ROOT/skills"
if [ -d "$SKILLS_DIR" ]; then
  SKILL_COUNT=$(find "$SKILLS_DIR" -name "*.md" -type f 2>/dev/null | wc -l | tr -d ' ')
  if [ "$SKILL_COUNT" -gt 0 ]; then
    pass "Skills directory: ${SKILL_COUNT} skill file(s)"
  else
    fail "Skills directory exists but contains no .md files"
  fi
else
  fail "skills/ directory not found"
fi

# 11. MCP server
MCP_FILE="$REPO_ROOT/tools/fleet_nerve_mcp.py"
if [ -f "$MCP_FILE" ]; then
  pass "fleet_nerve_mcp.py exists"
else
  # Also check plugin-local path
  if [ -f "$PLUGIN_ROOT/bin/fleet-nerve-mcp" ]; then
    pass "fleet-nerve-mcp launcher exists (MCP server via bin/)"
  else
    fail "fleet_nerve_mcp.py not found at tools/fleet_nerve_mcp.py"
  fi
fi

# ----------------------------------------------------------
separator "Secrets"
# ----------------------------------------------------------

# 12. OPENAI_API_KEY available via env / keychain / aws
SECRET_FOUND=""
SECRET_SOURCE=""

# Check env vars (generic names first, then legacy names for backward compat)
for var in FLEET_OPENAI_API_KEY OPENAI_API_KEY Context_DNA_OPENAI; do
  if [ -n "${!var:-}" ]; then
    SECRET_FOUND="yes"
    SECRET_SOURCE="env ($var)"
    break
  fi
done

# Check macOS Keychain (generic + legacy names)
if [ -z "$SECRET_FOUND" ] && command -v security &>/dev/null; then
  for keyname in FLEET_OPENAI_API_KEY fleet-nerve-openai Context_DNA_OPENAI OPENAI_API_KEY; do
    if security find-generic-password -s "$keyname" -w &>/dev/null; then
      SECRET_FOUND="yes"
      SECRET_SOURCE="keychain ($keyname)"
      break
    fi
  done
fi

# Check AWS Secrets Manager (existence only, no fetch — try generic then legacy)
if [ -z "$SECRET_FOUND" ] && command -v aws &>/dev/null; then
  for secret_id in FLEET_OPENAI_API_KEY Context_DNA_OPENAI; do
    if aws secretsmanager describe-secret --secret-id "$secret_id" &>/dev/null 2>&1; then
      SECRET_FOUND="yes"
      SECRET_SOURCE="AWS Secrets Manager ($secret_id)"
      break
    fi
  done
fi

if [ -n "$SECRET_FOUND" ]; then
  pass "OpenAI API key available via ${SECRET_SOURCE}"
else
  fail "No OpenAI API key found -- set FLEET_OPENAI_API_KEY (or OPENAI_API_KEY), add to keychain, or configure AWS SM"
fi

# ----------------------------------------------------------
separator "Self-Test"
# ----------------------------------------------------------

# 13. Send a message to self via daemon and verify seed file
NODE_ID="${MULTIFLEET_NODE_ID:-$(hostname -s | tr -cd 'a-zA-Z0-9_-')}"
SEED_FILE="/tmp/fleet-seed-${NODE_ID}.md"
SELF_TEST_PASS=false

if curl -sf --max-time 3 "$DAEMON_URL/health" >/dev/null 2>&1; then
  # Clean up any existing seed file
  rm -f "$SEED_FILE"

  # Send self-test message via daemon
  SELF_TEST_PAYLOAD="validate-install self-test $(date +%s)"
  SEND_RESULT=$(curl -sf --max-time 5 -X POST "$DAEMON_URL/message" \
    -H "Content-Type: application/json" \
    -d "{\"type\":\"seed\",\"target\":\"${NODE_ID}\",\"payload\":\"${SELF_TEST_PAYLOAD}\"}" 2>&1) || true

  # Give daemon a moment to write the seed file
  sleep 1

  if [ -f "$SEED_FILE" ] && grep -q "validate-install" "$SEED_FILE" 2>/dev/null; then
    SELF_TEST_PASS=true
    pass "Self-test: message sent and seed file written"
    # Clean up test artifact
    rm -f "$SEED_FILE"
  else
    fail "Self-test: message sent but seed file not written -- check daemon logs at /tmp/fleet-daemon-${NODE_ID}.log"
  fi
else
  fail "Self-test: skipped (daemon not running)"
fi

# ----------------------------------------------------------
# Summary
# ----------------------------------------------------------
echo ""
echo "=============================="

if [ "$FAIL" -eq 0 ]; then
  echo "Score: ${PASS}/${TOTAL} -- All checks passed"
  echo ""
  echo "Multi-Fleet is ready to use."
  exit 0
else
  echo "Score: ${PASS}/${TOTAL} -- ${FAIL} item(s) need attention"
  echo ""
  if [ "$FAIL" -le 2 ]; then
    echo "Almost there. Fix the items above and re-run."
  else
    echo "Several items need setup. Run: bash multi-fleet/scripts/install.sh"
  fi
  exit 1
fi
