#!/usr/bin/env python3
"""agent-watch - follow a work-order agent live, from any terminal.

An opencode session belongs to the directory it was created in, so an agent the
watcher started in another repo does *not* show up in the `opencode` window you
happen to have open. This follows it where it actually is.

    agent-watch.py                 the agent running right now (newest work order)
    agent-watch.py WO-105          by work-order id
    agent-watch.py ses_e39bc84...  by session id
    agent-watch.py --once          print what has happened so far, then exit

Ctrl-C stops watching. It never writes to the session, so it is safe to leave
running while the agent works.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime

HOME = os.path.expanduser("~")
# Which loop to follow.  CLAUDE_BRIDGE_HOME is the same switch the watcher and the bridge
# take - a second pipeline keeps its state in its own home - and CLAUDE_BRIDGE is the older
# explicit override, still honoured.
BRIDGE = os.path.expanduser(
    os.environ.get("CLAUDE_BRIDGE_HOME")
    or os.environ.get("CLAUDE_BRIDGE")
    or os.path.join(HOME, "claude-bridge"))
OC = os.environ.get("OPENCODE_BIN") or os.path.join(HOME, ".opencode", "bin", "opencode")
TMPDIR = "/tmp/opencode" if os.path.isdir("/tmp/opencode") else tempfile.gettempdir()

INTERVAL = float(os.environ.get("AGENT_WATCH_INTERVAL") or 3)

if sys.stdout.isatty():
    C_RESET, C_DIM, C_BOLD = "\033[0m", "\033[2m", "\033[1m"
    C_TXT, C_TOOL = "\033[0m", "\033[36m"
    C_ERR, C_OK = "\033[31m", "\033[32m"
else:
    C_RESET = C_DIM = C_BOLD = C_TXT = C_TOOL = C_ERR = C_OK = ""


def api(path: str, timeout: int = 60):
    """GET from the running opencode server, via the CLI (which holds the auth)."""
    fd, tmp = tempfile.mkstemp(dir=TMPDIR, suffix=".json")
    os.close(fd)
    try:
        # redirect to a file: the CLI truncates when its stdout is a pipe
        with open(tmp, "wb") as fh:
            p = subprocess.run([OC, "api", "GET", path], stdout=fh,
                               stderr=subprocess.PIPE, timeout=timeout)
        if p.returncode != 0:
            raise RuntimeError((p.stderr or b"").decode()[:300] or "opencode api failed")
        with open(tmp, "rb") as fh:
            raw = fh.read()
        if not raw.strip():
            return {}
        return json.loads(raw.decode())
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def data_of(reply):
    return reply.get("data", reply) if isinstance(reply, dict) else reply


def load_state() -> dict:
    try:
        with open(os.path.join(BRIDGE, "state.json")) as fh:
            return json.load(fh)
    except Exception:
        return {}


def session_info(sid: str) -> dict:
    try:
        return data_of(api("/api/session/%s" % sid)) or {}
    except Exception:
        return {}


def running_ids() -> set:
    try:
        return {k for k, v in (data_of(api("/api/session/active")) or {}).items()
                if (v or {}).get("type") == "running"}
    except Exception:
        return set()


def resolve(arg: str | None) -> str:
    if arg and arg.startswith("ses_"):
        return arg

    sessions = {str(k).upper(): v for k, v in (load_state().get("sessions") or {}).items() if v}

    if arg:
        want = arg.upper()
        if want in sessions:
            return sessions[want]
        hits = [sid for o, sid in sessions.items() if want in o]
        if len(hits) == 1:
            return hits[0]
        raise SystemExit("no work-order session matching %r (known: %s)"
                         % (arg, ", ".join(sorted(sessions)) or "none"))

    if not sessions:
        raise SystemExit("no work-order sessions in %s/state.json" % BRIDGE)

    live = running_ids()
    cands = [(oid, sid) for oid, sid in sessions.items() if sid in live] or list(sessions.items())
    best = max(cands, key=lambda kv: (session_info(kv[1]).get("time") or {}).get("created") or 0)
    return best[1]


def summarize(m: dict) -> list[str]:
    """One printable line per interesting part of a message."""
    lines: list[str] = []
    for p in (m.get("content") or []):
        ptype = p.get("type")
        if ptype == "text":
            txt = (p.get("text") or "").strip()
            if txt:
                lines.append(txt)
        elif ptype == "tool":
            st = p.get("state") or {}
            inp = st.get("input") or {}
            hint = (inp.get("command") or inp.get("filePath") or inp.get("pattern")
                    or inp.get("description") or inp.get("url") or "")
            lines.append("[%s:%s] %s" % (p.get("name"), st.get("status"), str(hint)))
    err = m.get("error") or (m.get("retry") or {}).get("error")
    if err:
        lines.append("!! " + json.dumps(err)[:400])
    return lines


def width() -> int:
    try:
        return max(40, min(200, os.get_terminal_size().columns))
    except OSError:
        return 100


def emit(m: dict, show_snapshot: bool = False) -> None:
    t = (m.get("time") or {}).get("created") or 0
    ts = datetime.fromtimestamp(t / 1000).strftime("%H:%M:%S") if t else "  --  --"
    kind = m.get("type") or "?"
    lines = summarize(m)
    w = width()
    if not lines:
        return
    if m.get("error") or (m.get("retry") or {}).get("error"):
        colour = C_ERR
    elif lines[0].startswith("["):
        colour = C_ERR if ":error" in lines[0] else C_TOOL
    else:
        colour = C_TXT
    for i, line in enumerate(lines):
        stamp = ts if i == 0 else " " * len(ts)
        label = kind[:9] if i == 0 else ""
        for j, chunk in enumerate(line.splitlines() or [""]):
            chunk = chunk.rstrip()
            if not chunk:
                continue
            if len(chunk) > w - 22:
                chunk = chunk[: w - 25] + "..."
            who = label if j == 0 else ""
            flat = i or j  # continuation lines keep the indentation, not the label
            prefix = "%s%s  %-9s %s" % (C_DIM if flat else "", stamp, who, colour)
            print(prefix + chunk + C_RESET)
    sys.stdout.flush()


def main() -> int:
    args = list(sys.argv[1:])
    if any(a in ("-h", "--help") for a in args):
        print(__doc__)
        return 0
    once = "--once" in args
    args = [a for a in args if not a.startswith("-")]

    sid = resolve(args[0] if args else None)
    info = session_info(sid)
    print("%s%s%s" % (C_BOLD, (info.get("title") or sid)[:width() - 2], C_RESET))
    print("%s  %s" % (C_DIM, sid))
    print("%s  dir   %s" % (C_DIM, (info.get("location") or {}).get("directory")))
    model = info.get("model") or {}
    print("%s  model %s/%s%s" % (C_DIM, model.get("providerID"), model.get("id"), C_RESET))
    print()

    seen: set = set()
    idle_running = None
    while True:
        try:
            msgs = data_of(api("/api/session/%s/message" % sid))
        except Exception as exc:
            print("%s  cannot read the session: %s%s" % (C_ERR, str(exc)[:120], C_RESET))
            return 1
        msgs = [m for m in (msgs if isinstance(msgs, list) else []) if isinstance(m, dict)]
        msgs.sort(key=lambda m: (m.get("time") or {}).get("created") or 0)
        for m in msgs:
            if m.get("id") in seen:
                continue
            seen.add(m.get("id"))
            emit(m)

        if once:
            return 0

        live = sid in running_ids()
        if not live:
            idle_running = (idle_running or 0) + 1
            if idle_running >= 2:
                info = session_info(sid)
                out = info.get("outcome")
                print()
                if out and out not in ("interrupted",):
                    print("%s  agent finished: %s%s" % (C_OK, out, C_RESET))
                elif out == "interrupted":
                    print("%s  agent is not running (session was interrupted at some point -"
                          " it may still have been re-prompted since)%s" % (C_DIM, C_RESET))
                else:
                    print("%s  agent is no longer running%s" % (C_DIM, C_RESET))
                return 0
        else:
            idle_running = 0

        time.sleep(INTERVAL)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print()
        raise SystemExit(130)
