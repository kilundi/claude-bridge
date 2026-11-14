#!/usr/bin/env bash
# Start (or reuse) the dedicated "Claude bridge" Chrome window.
#
#   bash start-bridge-chrome.sh [url]
#   CLAUDE_BRIDGE_ACCOUNT=spare bash start-bridge-chrome.sh [url]
#
# The window uses its own profile so the DevTools port is allowed (Chrome 136+
# refuses it on the default profile). Sign in to claude.ai in this window once;
# the sign-in is kept in the profile from then on.  One profile = one account, so an
# account is signed in once, ever.
#
# With CLAUDE_BRIDGE_ACCOUNT set, that account's profile, port and chat are read from
# config.json (`ask-claude --switch-account` is the normal way to do this).
# An explicit CLAUDE_BRIDGE_PROFILE / CLAUDE_BRIDGE_PORT / url argument still wins.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

PORT="${CLAUDE_BRIDGE_PORT:-9222}"
PROFILE="${CLAUDE_BRIDGE_PROFILE:-$HOME/.config/claude-bridge-chrome}"
URL="${1:-${CLAUDE_BRIDGE_URL:-https://claude.ai/new}}"

if [ -n "${CLAUDE_BRIDGE_ACCOUNT:-}" ]; then
  spec="$(python3 "$HERE/bridge.py" --account-env --account "$CLAUDE_BRIDGE_ACCOUNT" \
          2>/dev/null || true)"
  if [ -n "$spec" ]; then
    IFS=$'\t' read -r a_profile a_port a_chat a_name <<<"$spec"
    PROFILE="${CLAUDE_BRIDGE_PROFILE:-$a_profile}"
    PORT="${CLAUDE_BRIDGE_PORT:-$a_port}"
    URL="${1:-${CLAUDE_BRIDGE_URL:-${a_chat:-https://claude.ai/new}}}"
    echo "account: $a_name"
  else
    echo "no account '$CLAUDE_BRIDGE_ACCOUNT' in config.json - using the defaults" >&2
  fi
fi

CHROME="${CHROME_BIN:-}"
if [ -z "$CHROME" ]; then
  for c in google-chrome google-chrome-stable chromium chromium-browser brave-browser microsoft-edge; do
    if command -v "$c" >/dev/null 2>&1; then CHROME="$(command -v "$c")"; break; fi
  done
fi
[ -n "$CHROME" ] || { echo "no chrome/chromium found" >&2; exit 1; }

mkdir -p "$PROFILE"

FLAGS=(
  --user-data-dir="$PROFILE"
  --remote-debugging-port="$PORT"
  --remote-allow-origins='*'
  --no-first-run
  --no-default-browser-check
)

if curl -s "http://127.0.0.1:$PORT/json/version" >/dev/null 2>&1; then
  echo "bridge Chrome already listening on :$PORT - opening $URL"
  nohup "$CHROME" "${FLAGS[@]}" --new-window "$URL" >/dev/null 2>&1 || true
  exit 0
fi

echo "starting bridge Chrome  (profile: $PROFILE, port: $PORT)"
setsid "$CHROME" "${FLAGS[@]}" --new-window "$URL" >/dev/null 2>&1 &

for _ in $(seq 1 40); do
  if curl -s "http://127.0.0.1:$PORT/json/version" >/dev/null 2>&1; then
    echo "bridge Chrome is up on :$PORT"
    exit 0
  fi
  sleep 0.5
done

echo "bridge Chrome did not come up on :$PORT" >&2
exit 1
