#!/usr/bin/env bash
# Bring the whole bridge back up after a restart or a closed window:
#   1. the dedicated bridge Chrome window for the active account (its own profile, so the
#      debug port works)
#   2. a check that claude.ai is signed in and the saved chat is in front
#   3. the work-order watcher
#
# Safe to run twice: everything is reused if it is already up.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

spec="$(python3 "$HERE/bridge.py" --account-env 2>/dev/null || true)"
IFS=$'\t' read -r ACCT_PROFILE ACCT_PORT ACCT_CHAT ACCT_NAME <<<"$spec"
ACCT_PORT="${ACCT_PORT:-9222}"
ACCT_NAME="${ACCT_NAME:-main}"

echo "1. bridge Chrome (account: $ACCT_NAME)"
if curl -s --max-time 3 "http://127.0.0.1:$ACCT_PORT/json/version" >/dev/null 2>&1; then
  echo "   already up on :$ACCT_PORT"
else
  CLAUDE_BRIDGE_ACCOUNT="$ACCT_NAME" bash "$HERE/start-bridge-chrome.sh" || exit 1
fi

echo
echo "2. bridge state"
cd "$HERE"
python3 bridge.py --status --json 2>/dev/null | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    print("   could not read the bridge state")
    raise SystemExit
print("   account   :", d.get("account"), "on :%s" % d.get("port"))
print("   chat      :", d.get("url"))
print("   signed in :", d.get("composer"))
if not d.get("composer"):
    print("   -> sign in to claude.ai in the bridge window, then run this again")
'

echo
echo "3. watcher"
"$HERE/watcher.sh" start
"$HERE/watcher.sh" status | head -n 1
