#!/usr/bin/env bash
# Start, stop or check the claude-bridge work-order watcher.
#   ./watcher.sh start|stop|restart|status|log|reap
#   ./watcher.sh run                foreground, for a supervisor (the systemd user unit)
#   ./watcher.sh prune-state        drop the start records of orders finished over a week ago
#   ./watcher.sh unit               which systemd user unit supervises this home
#   ./watcher.sh done WO-111        it reported DONE: free its agent slot now
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
# The *code* is here; the pid, the lock and the logs live in the *home*.  A second pipeline
# (see CLAUDE_BRIDGE_HOME) has its own home and its own unit and shares nothing with the
# first - which is what keeps two loops from ever contending for one lock.  Unset, the home
# is HERE, exactly as before.
HOME_DIR="${CLAUDE_BRIDGE_HOME:-$HERE}"
# The unit that supervises this watcher follows the *home* too, so one env var is enough: a
# home that names its unit in config.json (watch.systemd_unit) is restarted through that
# unit, never the other pipeline's.  An unnamed home - the coding loop - is
# claude-bridge-watcher, exactly as before, and CLAUDE_BRIDGE_UNIT still wins outright.
UNIT="${CLAUDE_BRIDGE_UNIT:-}"
if [ -z "$UNIT" ]; then
  UNIT="$(python3 -c 'import json,sys; print((json.load(open(sys.argv[1])).get("watch") or {}).get("systemd_unit") or "")' \
            "$HOME_DIR/config.json" 2>/dev/null || true)"
  UNIT="${UNIT:-claude-bridge-watcher}"
fi
PID="$HOME_DIR/watcher.pid"
LOCK="$HOME_DIR/watcher.lock"
CMD="${1:-status}"

running() { [ -f "$PID" ] && kill -0 "$(cat "$PID")" 2>/dev/null; }

# A supervisor (the systemd user unit) owns its own process, so there is no pid file to
# read: watcher.lock being held *is* a watcher running.  Without this, `status` would say
# "not running" while the loop is plainly working.
supervised() {
  [ -f "$LOCK" ] || return 1
  exec 8>"$LOCK" 2>/dev/null || return 1
  if flock -n 8; then
    exec 8>&-                           # we took the lock: nobody else holds it
    return 1
  fi
  exec 8>&-                             # held by somebody else: drop our attempt
  return 0
}

case "$CMD" in
  run)
    # Foreground: the supervisor owns the process and restarts it.  The watcher takes
    # watcher.lock itself, so this cannot double up with a `start`.
    exec python3 "$HERE/watcher.py"
    ;;
  start)
    mkdir -p "$HOME_DIR"
    if running; then echo "watcher already running (pid $(cat "$PID"))"; exit 0; fi
    # Take the same lock the watcher itself takes, so two simultaneous starts
    # cannot both launch one.  fd 9 is inherited by the watcher, which holds the
    # lock for its whole life; when this shell exits the child keeps it.
    exec 9>"$LOCK"
    if ! flock -n 9; then echo "watcher already running (watcher.lock is held)"; exit 0; fi
    WATCHER_LOCK_FD=9 setsid nohup python3 "$HERE/watcher.py" >>"$HOME_DIR/watcher.out" 2>&1 &
    echo $! >"$PID"
    sleep 1
    if running; then echo "watcher started (pid $(cat "$PID"))"; else echo "watcher failed to start - see $HERE/watcher.log"; exit 1; fi
    ;;
  stop)
    if running; then kill "$(cat "$PID")" 2>/dev/null; rm -f "$PID"; echo "watcher stopped"
    elif supervised; then
      echo "a supervisor owns this watcher - stop it there:"
      echo "  systemctl --user stop $UNIT"
      exit 1
    else echo "watcher not running"; rm -f "$PID"; fi
    ;;
  restart)
    # A restart is not a stop+start when a supervisor owns the process: systemd has to do
    # the replacing, or it would see its unit's process vanish and start a second one.
    if supervised; then
      systemctl --user restart "$UNIT" && sleep 2 \
        && echo "watcher restarted (systemd user unit, pid $(systemctl --user show -p MainPID --value "$UNIT"))"
    else
      "$0" stop
      "$0" start
    fi
    ;;
  prune-state)
    # The start records of orders finished more than a week ago are the bulk of
    # state.json; nothing reads them.  It happens on its own every pass, too.
    python3 "$HERE/watcher.py" --prune-state
    ;;
  status)
    if running; then echo "watcher running (pid $(cat "$PID"))"
    elif supervised; then echo "watcher running (started by a supervisor: systemctl --user status $UNIT)"
    else echo "watcher not running"; fi
    python3 "$HERE/watcher.py" --status 2>/dev/null || echo "  (could not read the agents)"
    if [ -f "$HOME_DIR/NEEDS-ORDER.md" ]; then
      echo "STALLED: the queue needs an order from Claude - see $HOME_DIR/NEEDS-ORDER.md"
    fi
    echo "--- last log lines ---"
    tail -n 8 "$HOME_DIR/watcher.log" 2>/dev/null || echo "(no log yet)"
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
  unit)
    # Which systemd user unit supervises *this* home - so a script can check before it acts.
    echo "$UNIT"
    ;;
  *)
    echo "usage: $0 {start|stop|restart|status|log|reap|run|prune-state|unit|done <WORK-ORDER-ID>}"; exit 2;;
esac
