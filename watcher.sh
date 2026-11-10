#!/usr/bin/env bash
# Start, stop or check the claude-bridge work-order watcher.
#   ./watcher.sh start|stop|status|log
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
PID="$HERE/watcher.pid"
LOCK="$HERE/watcher.lock"
CMD="${1:-status}"

running() { [ -f "$PID" ] && kill -0 "$(cat "$PID")" 2>/dev/null; }

case "$CMD" in
  start)
    if running; then echo "watcher already running (pid $(cat "$PID"))"; exit 0; fi
    # Take the same lock the watcher itself takes, so two simultaneous starts
    # cannot both launch one.  fd 9 is inherited by the watcher, which holds the
    # lock for its whole life; when this shell exits the child keeps it.
    exec 9>"$LOCK"
    if ! flock -n 9; then echo "watcher already running (watcher.lock is held)"; exit 0; fi
    WATCHER_LOCK_FD=9 setsid nohup python3 "$HERE/watcher.py" >>"$HERE/watcher.out" 2>&1 &
    echo $! >"$PID"
    sleep 1
    if running; then echo "watcher started (pid $(cat "$PID"))"; else echo "watcher failed to start - see $HERE/watcher.log"; exit 1; fi
    ;;
  stop)
    if running; then kill "$(cat "$PID")" 2>/dev/null; rm -f "$PID"; echo "watcher stopped"; else echo "watcher not running"; rm -f "$PID"; fi
    ;;
  status)
    if running; then echo "watcher running (pid $(cat "$PID"))"; else echo "watcher not running"; fi
    echo "--- last log lines ---"
    tail -n 8 "$HERE/watcher.log" 2>/dev/null || echo "(no log yet)"
    ;;
  log)
    tail -n 40 -f "$HERE/watcher.log"
    ;;
  *)
    echo "usage: $0 {start|stop|status|log}"; exit 2;;
esac
