#!/usr/bin/env bash
# Start, stop or check the claude-bridge work-order watcher.
#   ./watcher.sh start|stop|status|log|reap
#   ./watcher.sh run                foreground, for a supervisor (the systemd user unit)
#   ./watcher.sh done WO-111        it reported DONE: free its agent slot now
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
PID="$HERE/watcher.pid"
LOCK="$HERE/watcher.lock"
CMD="${1:-status}"

running() { [ -f "$PID" ] && kill -0 "$(cat "$PID")" 2>/dev/null; }

# A supervisor (the systemd user unit) owns its own process, so there is no pid file to
# read: watcher.lock being held *is* a watcher running.  Without this, `status` would say
# "not running" while the loop is plainly working.
supervised() {
  [ -f "$LOCK" ] || return 1
  exec 8>"$LOCK" 2>/dev/null || return 1
  flock -n 8 && return 1                # we took the lock: nobody else holds it
  return 0
}

case "$CMD" in
  run)
    # Foreground: the supervisor owns the process and restarts it.  The watcher takes
    # watcher.lock itself, so this cannot double up with a `start`.
    exec python3 "$HERE/watcher.py"
    ;;
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
    if running; then kill "$(cat "$PID")" 2>/dev/null; rm -f "$PID"; echo "watcher stopped"
    elif supervised; then
      echo "a supervisor owns this watcher - stop it there:"
      echo "  systemctl --user stop claude-bridge-watcher"
      exit 1
    else echo "watcher not running"; rm -f "$PID"; fi
    ;;
  status)
    if running; then echo "watcher running (pid $(cat "$PID"))"
    elif supervised; then echo "watcher running (started by a supervisor: systemctl --user status claude-bridge-watcher)"
    else echo "watcher not running"; fi
    python3 "$HERE/watcher.py" --status 2>/dev/null || echo "  (could not read the agents)"
    if [ -f "$HERE/NEEDS-ORDER.md" ]; then
      echo "STALLED: the queue needs an order from Claude - see $HERE/NEEDS-ORDER.md"
    fi
    echo "--- last log lines ---"
    tail -n 8 "$HERE/watcher.log" 2>/dev/null || echo "(no log yet)"
    ;;
  reap)
    # Close the windows of agents that are not working.  Safe beside the watcher:
    # it only reads state, and never closes a window whose agent is working.
    python3 "$HERE/watcher.py" --reap
    ;;
  done)
    if [ -z "${2:-}" ]; then echo "usage: $0 done <WORK-ORDER-ID>"; exit 2; fi
    python3 "$HERE/watcher.py" --finish "$2"
    ;;
  log)
    tail -n 40 -f "$HERE/watcher.log"
    ;;
  *)
    echo "usage: $0 {start|stop|status|log|reap|run|done <WORK-ORDER-ID>}"; exit 2;;
esac
