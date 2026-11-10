#!/usr/bin/env bash
# Bring the whole bridge back up after a restart or a closed window:
#   1. the dedicated bridge Chrome window (its own profile, so the debug port works)
#   2. a check that claude.ai is signed in and the saved chat is in front
#   3. the work-order watcher
#
# Safe to run twice: everything is reused if it is already up.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "1. bridge Chrome"
if curl -s --max-time 3 "http://127.0.0.1:9222/json/version" >/dev/null 2>&1; then
  echo "   already up on :9222"
else
  bash "$HERE/start-bridge-chrome.sh" || exit 1
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
print("   chat      :", d.get("url"))
print("   signed in :", d.get("composer"))
if not d.get("composer"):
    print("   -> sign in to claude.ai in the bridge window, then run this again")
'

echo
echo "3. watcher"
"$HERE/watcher.sh" start
"$HERE/watcher.sh" status | head -n 1
