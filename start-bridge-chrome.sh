#!/usr/bin/env bash
# Start (or reuse) the dedicated "Claude bridge" Chrome window.
#
#   bash start-bridge-chrome.sh [url]
#
# The window uses its own profile so the DevTools port is allowed (Chrome 136+
# refuses it on the default profile). Sign in to claude.ai in this window once;
# the sign-in is kept in the profile from then on.
set -euo pipefail

PORT="${CLAUDE_BRIDGE_PORT:-9222}"
PROFILE="${CLAUDE_BRIDGE_PROFILE:-$HOME/.config/claude-bridge-chrome}"
URL="${1:-${CLAUDE_BRIDGE_URL:-https://claude.ai/new}}"

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
