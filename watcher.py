#!/usr/bin/env python3
"""Watch the Claude chat for work orders and hand them to an agent.

Claude in the browser has no way to push work outward, so this closes the loop the
other way round:

    Claude writes a WORK-ORDER block in the chat
        -> this watcher notices it
        -> saves it to ./inbox/<id>.md and pulls down any file cards Claude attached
        -> starts an opencode session on it in the right directory
        -> that agent does the work and reports DONE back through the ask_claude tool
        -> Claude reads the report and writes the next work order

Run it in the background with ./watcher.sh start (see ./watcher.sh status).
A single pass is available for cron/systemd timers:  python3 watcher.py --once
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import wlock  # noqa: E402
from bridge import Bridge, cdp_alive, ensure_chrome, extract_work_orders, outbox_files  # noqa: E402

CONFIG = os.path.join(HERE, "config.json")
STATE = os.path.join(HERE, "state.json")
INBOX = os.path.join(HERE, "inbox")
LOG = os.path.join(HERE, "watcher.log")
TERMINAL_LOG = os.path.join(HERE, "terminal.log")
LOCK = os.path.join(HERE, "watcher.lock")

# how many times start_agent may be retried after the first failure, and how
# long to wait between attempts (overridable from config.json watch settings)
START_MAX_RETRIES = 2
START_RETRY_DELAY = 5.0

# set once in main(); holding it for the whole run is what makes a second
# watcher refuse to start (see wlock.py)
_LOCK_FD: int | None = None


def load_config() -> dict:
    try:
        with open(CONFIG) as fh:
            return json.load(fh)
    except Exception:
        return {}


def log(msg: str) -> None:
    line = "%s  %s" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG, "a") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def load_state() -> dict:
    try:
        with open(STATE) as fh:
            return json.load(fh)
    except Exception:
        return {}


def _merge(base, new):
    """Recursively overlay ``new`` on ``base``; dicts are unioned.

    Keys only present on disk survive, so re-reading before a write never drops
    marks another process made.
    """
    if isinstance(base, dict) and isinstance(new, dict):
        out = dict(base)
        for k, v in new.items():
            out[k] = _merge(out[k], v) if k in out else v
        return out
    return new


def _apply_inplace(target: dict, source: dict) -> None:
    """Copy ``source`` into ``target``, keeping existing nested dicts the same
    objects so callers holding a record keep mutating the live copy."""
    for k, v in source.items():
        if (k in target and isinstance(target[k], dict) and isinstance(v, dict)
                and target[k] is not v):
            _apply_inplace(target[k], v)
        else:
            target[k] = v


def save_state(st: dict) -> None:
    """Write state atomically without ever reverting another process's marks.

    The watcher holds ``watcher.lock`` for its whole life, so this runs under the
    lock: we re-read the file, merge our updates on top of it, then swap it in via
    a per-process temp file and ``os.replace``.
    """
    merged = _merge(load_state(), st)
    _apply_inplace(st, merged)
    tmp = "%s.%d.tmp" % (STATE, os.getpid())
    with open(tmp, "w") as fh:
        json.dump(merged, fh, indent=1)
    os.replace(tmp, STATE)


def acquire_watcher_lock() -> int | None:
    """Take ``watcher.lock`` for the life of this process.

    Returns the held fd, or ``None`` when another watcher already holds it.
    watcher.sh starts us with the lock already held on an inherited fd
    (``WATCHER_LOCK_FD``); started by hand we open and lock it ourselves.
    """
    global _LOCK_FD
    env = os.environ.get("WATCHER_LOCK_FD")
    inherited = int(env) if env and env.isdigit() else None
    fd = wlock.acquire(LOCK, inherited) if inherited is not None else wlock.acquire(LOCK)
    if fd is None and inherited is not None:
        fd = wlock.acquire(LOCK)  # inherited fd unusable - fall back to our own
    _LOCK_FD = fd
    return fd


def _hash(text: str) -> str:
    return hashlib.sha1((text or "").encode()).hexdigest()[:12]


def oc_api(binpath: str, method: str, path: str, payload: dict | None = None, timeout: int = 60):
    cmd = [binpath, "api", method, path]
    if payload is not None:
        cmd += ["-d", json.dumps(payload)]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError("opencode api failed: %s%s" % (p.stdout[:300], p.stderr[:300]))
    try:
        return json.loads(p.stdout)
    except Exception:
        return {"raw": p.stdout}


def order_to_inbox(order: dict, files: list[dict]) -> str:
    os.makedirs(INBOX, exist_ok=True)
    body = ["# Work order %s" % order["id"], ""]
    if order.get("dir"):
        body.append("dir: %s" % order["dir"])
    if order.get("branch"):
        body.append("branch: %s" % order["branch"])
    if order.get("save"):
        body.append("save to: %s" % order["save"])
    body += ["", "## Goal", order["goal"], ""]
    if order.get("acceptance"):
        body += ["## Acceptance", order["acceptance"], ""]
    if files:
        body += ["## Files from Claude"]
        for f in files:
            body.append("- %s (%s bytes)" % (f["path"], f["bytes"]))
        body.append("")
    body += ["## Work order exactly as Claude wrote it", "```", order["raw"], "```", ""]
    path = os.path.join(INBOX, "%s.md" % order["id"])
    with open(path, "w") as fh:
        fh.write("\n".join(body) + "\n")
    return path


def build_prompt(order: dict, files: list[dict], inbox_path: str) -> str:
    lines = ["Work order %s from Claude, received over the claude-bridge chat queue." % order["id"], ""]
    if order.get("dir"):
        lines.append("Repository: %s" % order["dir"])
    if order.get("branch"):
        lines.append("Branch: %s" % order["branch"])
    if order.get("save"):
        lines.append("Save the result to: %s" % order["save"])
    lines += ["", "Goal:", order["goal"], ""]
    if order.get("acceptance"):
        lines += ["Done when:", order["acceptance"], ""]
    if files:
        lines.append("Claude attached these files (already downloaded for you):")
        for f in files:
            lines.append("  - %s" % f["path"])
        lines.append("")
    lines += [
        "When Claude says to save a file in a particular directory or at a particular path, do exactly that:",
        "write the file there (create the directory if needed), and copy any attached file listed above to the",
        "place Claude asked for. If Claude names a file it wrote but did not attach, look for it under",
        "~/claude-bridge/downloads/ and in the repository it belongs to.",
        "",
        "The full order, exactly as Claude wrote it, is saved at %s." % inbox_path,
        "",
        "Do the work: make the change, run the checks, and commit and push to the branch above.",
        "When you are finished, report back to Claude with the ask_claude tool:",
        "  DONE",
        "  - project: <name>",
        "  - branch: <branch>",
        "  - commits: <hashes> (pushed / not pushed)",
        "  - tests: <what you ran and the result>",
        "  - notes: <anything odd, or a question>",
        "  NEXT: what should I take on?",
        "Then wait for Claude's next work order.",
    ]
    return "\n".join(lines)


def start_agent(cfg: dict, order: dict, files: list[dict], inbox_path: str) -> str:
    w = cfg.get("watch", {})
    binp = os.path.expanduser(w.get("opencode_bin") or "~/.opencode/bin/opencode")
    directory = os.path.expanduser(order.get("dir") or w.get("default_dir") or "~")
    if not os.path.isdir(directory):
        raise RuntimeError("directory does not exist: %s" % directory)
    model = w.get("model") or {}
    if order.get("model") and "/" in order["model"]:
        pid, mid = order["model"].split("/", 1)
        model = {"providerID": pid, "id": mid}
    payload = {"title": "%s: %s" % (order["id"], order["goal"][:80]), "location": {"directory": directory}}
    if model:
        payload["model"] = model
    created = oc_api(binp, "POST", "/api/session", payload, timeout=90)
    sid = (created.get("data") or created).get("id")
    if not sid:
        raise RuntimeError("could not create session: %s" % str(created)[:300])
    prompt: dict = {"text": build_prompt(order, files, inbox_path)}
    if files:
        prompt["files"] = [{"uri": "file://" + f["path"], "name": f["name"]} for f in files]
    oc_api(binp, "POST", "/api/session/%s/prompt" % sid, prompt, timeout=180)
    return sid


# --- a visible terminal for the agent ------------------------------------------------
# An opencode session belongs to the repo it was started in, so an agent working in
# another directory is invisible in whichever opencode window the user happens to have
# open. Opening a real terminal on the session is how the user actually watches it.
TERMINALS = (
    # (binary, argv template). {dir} = working directory, {cmd} = the launcher as ONE
    # argument, {file} = the launcher script (for terminals that want argv, not a string).
    ("qterminal", ["-w", "{dir}", "-e", "{cmd}"]),
    ("xfce4-terminal", ["--working-directory={dir}", "-x", "{cmd}"]),
    ("kitty", ["--directory", "{dir}", "bash", "{file}"]),
    ("alacritty", ["--working-directory", "{dir}", "-e", "bash", "{file}"]),
    ("wezterm", ["start", "--cwd", "{dir}", "--", "bash", "{file}"]),
    ("konsole", ["--workdir", "{dir}", "-e", "bash", "{file}"]),
    ("gnome-terminal", ["--working-directory={dir}", "--", "bash", "{file}"]),
    ("kgx", ["--working-directory={dir}", "--", "bash", "{file}"]),
    ("ptyxis", ["--working-directory={dir}", "--", "bash", "{file}"]),
    ("foot", ["--working-directory={dir}", "bash", "{file}"]),
    ("tilix", ["--working-directory={dir}", "-e", "{cmd}"]),
    ("mate-terminal", ["--working-directory={dir}", "-e", "{cmd}"]),
    ("lxterminal", ["--working-directory={dir}", "-e", "{cmd}"]),
    ("xterm", ["-e", "{cmd}"]),
)
TERMINALS_DIR = os.path.join(HERE, "terminals")


def write_terminal_script(oid: str, sid: str, directory: str, title: str, opencode_bin: str) -> str:
    """Write the tiny script the window runs.

    It is a file rather than a quoted command line because `qterminal -e` (and friends)
    re-parse their argument, which mangles nested quoting - a real path needs none.
    """
    os.makedirs(TERMINALS_DIR, exist_ok=True)
    path = os.path.join(TERMINALS_DIR, "%s.sh" % sid)
    body = (
        "#!/usr/bin/env bash\n"
        "# opened by watcher.py for %s - attaches this window to the agent's session\n"
        "cd %s || exit 1\n"
        "printf '\\033]2;%%s\\007' %s\n"
        "exec %s -s %s\n"
        % (oid, shlex.quote(directory), shlex.quote(title),
           shlex.quote(opencode_bin), shlex.quote(sid))
    )
    with open(path, "w") as fh:
        fh.write(body)
    os.chmod(path, 0o755)
    return path


def open_agent_terminal(cfg: dict, oid: str, sid: str, directory: str) -> bool:
    """Open a terminal window attached to the agent's session. Best effort only.

    The agent is already running on the server by this point, so a failure here costs
    nothing but the window - it is logged, and the order still succeeds.
    """
    w = cfg.get("watch", {})
    if not w.get("launch_terminal", True):
        return False
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        log("  no display - cannot open a terminal for %s "
            "(watch it with: python3 %s/agent-watch.py %s)" % (oid, HERE, oid))
        return False

    directory = os.path.expanduser(directory)
    opencode_bin = os.path.expanduser(w.get("opencode_bin") or "opencode")
    title = "%s - %s" % (oid, os.path.basename(directory.rstrip("/")) or directory)
    fname = write_terminal_script(oid, sid, directory, title, opencode_bin)
    one_arg = "bash %s" % shlex.quote(fname)

    template = w.get("terminal")
    if template:
        argv0 = template if isinstance(template, list) else shlex.split(str(template))
        candidates = [list(argv0)]
    else:
        candidates = [[binary] + list(argv) for binary, argv in TERMINALS if shutil.which(binary)]

    for candidate in candidates:
        argv = [a.replace("{dir}", directory).replace("{cmd}", one_arg).replace("{file}", fname)
                for a in candidate]
        try:
            with open(TERMINAL_LOG, "ab") as fh:
                subprocess.Popen(argv, stdout=fh, stderr=fh, stdin=subprocess.DEVNULL,
                                 cwd=directory, start_new_session=True)
        except Exception as e:  # missing binary, no X, ...
            log("  could not open %s for %s: %s" % (argv[0], oid, e))
            continue
        log("  opened %s on session %s (%s)" % (argv[0], sid, title))
        return True

    log("  no terminal emulator to open for %s - watch it with: "
        "python3 %s/agent-watch.py %s" % (oid, HERE, oid))
    return False


def download_new_files(b: Bridge, st: dict) -> list[dict]:
    """Download any file card Claude has produced that we have not fetched yet."""
    done = set(st.get("files") or [])
    cards = b.list_files()
    fresh = [c["title"] for c in cards if c.get("title") and c["title"] not in done]
    if not fresh:
        return []
    files = b.download(fresh)
    st["files"] = sorted(done | set(fresh))
    log("downloaded %d file(s) from Claude: %s" % (len(files), ", ".join(f["name"] for f in files)))
    return files


def ensure_started(cfg: dict, st: dict, oid: str, delay: float | None = None) -> str | None:
    """Start the agent for one order, at most once, with a bounded retry path.

    * never starts a second session for an order that already has a session id in
      state (or in the ``starts`` record)
    * on failure records ``status="failed"``, ``attempts`` and ``last_error`` and
      retries at most ``start_retries`` more times, then gives up - it never loops
    """
    starts = st.setdefault("starts", {})
    rec = starts.get(oid)
    if not rec:
        return None
    sessions = st.setdefault("sessions", {})
    if sessions.get(oid) or rec.get("session"):
        rec["status"] = "started"
        return sessions.get(oid) or rec.get("session")
    if rec.get("status") in ("started", "dry_run", "skipped"):
        return None

    watch = cfg.get("watch", {})
    max_retries = int(watch.get("start_retries", START_MAX_RETRIES))
    if delay is None:
        delay = float(watch.get("start_retry_delay", START_RETRY_DELAY))
    attempts = int(rec.get("attempts") or 0)
    if attempts > max_retries:
        log("  %s already failed to start %d time(s) - not retrying" % (oid, attempts))
        return None

    order = rec.get("order") or {}
    files = rec.get("files") or []
    inbox_path = rec.get("inbox") or ""
    while attempts <= max_retries:
        attempts += 1
        rec["attempts"] = attempts
        rec["status"] = "starting"
        rec["updated"] = time.time()
        save_state(st)
        try:
            sid = start_agent(cfg, order, files, inbox_path)
        except Exception as e:
            rec["status"] = "failed"
            rec["last_error"] = str(e)
            rec["updated"] = time.time()
            save_state(st)
            log("  could not start an agent for %s (attempt %d/%d): %s"
                % (oid, attempts, max_retries + 1, e))
            if attempts > max_retries:
                log("  giving up on %s after %d attempt(s) - status failed" % (oid, attempts))
                return None
            time.sleep(delay)
            continue
        sessions[oid] = sid
        rec["status"] = "started"
        rec["session"] = sid
        rec["last_error"] = None
        rec["updated"] = time.time()
        save_state(st)
        log("  started opencode session %s in %s"
            % (sid, order.get("dir") or watch.get("default_dir")))
        open_agent_terminal(cfg, oid, sid,
                            order.get("dir") or watch.get("default_dir") or "~")
        return sid
    return None


def reconcile_starts(cfg: dict, st: dict, dry_run: bool = False, delay: float | None = None) -> None:
    """Recover orders that were marked seen but never got a session.

    This is the retry path for a process killed between the mark and the start:
    the normal "new order" path will not fire again (the mark is already saved),
    so we try to start it once here - bounded by the attempt limit in
    ``ensure_started``, which records every failure.
    """
    if dry_run or not cfg.get("watch", {}).get("auto_start", True):
        return
    max_retries = int(cfg.get("watch", {}).get("start_retries", START_MAX_RETRIES))
    for oid in list((st.get("starts") or {}).keys()):
        rec = (st.get("starts") or {}).get(oid) or {}
        if (st.get("sessions") or {}).get(oid) or rec.get("session"):
            continue
        if int(rec.get("attempts") or 0) > max_retries:
            continue  # terminal failure - never loop, and keep the log quiet
        if rec.get("status") in ("pending", "failed", "starting"):
            ensure_started(cfg, st, oid, delay=delay)


def handle_orders(cfg: dict, st: dict, orders: list[dict], files: list[dict] | None = None,
                  dry_run: bool = False, retry_delay: float | None = None) -> dict:
    """Mark and start every not-yet-seen order, then reconcile stuck ones.

    The seen mark (and the start record) are saved *before* the slow agent start,
    so a crash or a second watcher can never start an order twice.
    """
    files = files or []
    seen = st.get("orders") or {}
    if isinstance(seen, list):  # tolerate an older state file
        seen = {k: "" for k in seen}

    for o in orders:
        inbox_path = order_to_inbox(o, files)
        log("new work order %s -> %s" % (o["id"], inbox_path))
        seen[o["id"]] = _hash(o["raw"])
        st["orders"] = seen
        rec = st.setdefault("starts", {}).setdefault(o["id"], {"status": "pending", "attempts": 0})
        rec.update({"order": o, "inbox": inbox_path, "files": files, "updated": time.time()})
        if dry_run:
            rec["status"] = "dry_run"
        save_state(st)
        if dry_run:
            log("  (dry run: not starting an agent)")
        elif cfg.get("watch", {}).get("auto_start", True):
            ensure_started(cfg, st, o["id"], delay=retry_delay)
        else:
            rec["status"] = "pending"

    reconcile_starts(cfg, st, dry_run=dry_run, delay=retry_delay)
    return st


def pass_once(cfg: dict, st: dict, dry_run: bool = False) -> dict:
    if not cdp_alive():
        if cfg.get("watch", {}).get("auto_launch_bridge", False) and ensure_chrome():
            log("bridge window was down - launched it")
        else:
            if st.get("bridge_up", True):  # only say it once, not every pass
                log("bridge window is down, so work orders cannot be picked up. "
                    "Run: bash ~/claude-bridge/bridge-up.sh  (or tell an agent to launch the bridge)")
                st["bridge_up"] = False
                save_state(st)
            return st
    if not st.get("bridge_up", True):
        st["bridge_up"] = True
        log("bridge window is back up - listening for work orders again")
        save_state(st)
    b = Bridge()
    c = b.connect(want_composer=False)
    try:
        stt = b.last_message(c)
        # Claude's usage limit does not stop us reading the chat, so an order that
        # was written before the limit hit is still picked up.  But say it once, so
        # it is clear why no reply is coming, and say so again when it lifts.
        if stt.get("limited") and not stt.get("streaming"):
            if st.get("claude_up", True):
                resets = (stt.get("resetsAt") or "").strip()
                log("claude is out of messages%s - still reading, no replies until it resets"
                    % ((" until %s" % resets) if resets else ""))
                st["claude_up"] = False
                st["claude_limit"] = {"text": (stt.get("limitText") or "").strip(),
                                      "resets_at": resets, "seen": time.time()}
                save_state(st)
        elif not st.get("claude_up", True):
            log("claude can answer again - the loop is back to normal")
            st["claude_up"] = True
            st.pop("claude_limit", None)
            save_state(st)
            if cfg.get("watch", {}).get("compact_on_resume"):
                try:
                    log("  compact_on_resume: asking Claude for a compact handoff")
                    b.compact(timeout=300)
                except Exception as e:
                    log("  compact_on_resume skipped: %s" % e)
        if stt.get("streaming"):
            return st
        # A report that finished while the bridge was down is sitting in the outbox.
        # Deliver it as soon as Claude can take it, oldest first.
        try:
            if outbox_files():
                res = b.flush_outbox(timeout=float(cfg.get("watch", {}).get("flush_timeout", 300)))
                log("outbox: delivered %d queued report(s)%s"
                    % (len(res["sent"]),
                       (", %d still waiting" % len(res["remaining"])) if res["remaining"] else ""))
        except Exception as e:
            log("outbox flush skipped: %s" % e)
        lookback = int(cfg.get("watch", {}).get("lookback", 3))
        msgs = b.last_messages(c, lookback)
    finally:
        c.close()
    if not msgs:
        return st
    text = msgs[-1]
    orders: list[dict] = []
    for m in msgs:
        orders.extend(extract_work_orders(m))
    seen = st.get("orders") or {}
    if isinstance(seen, list):  # tolerate an older state file
        seen = {k: "" for k in seen}
    new = [o for o in orders if seen.get(o["id"]) != _hash(o["raw"])]

    files: list[dict] = []
    if new:
        try:
            files = download_new_files(b, st)
        except Exception as e:
            log("file download skipped: %s" % e)

    handle_orders(cfg, st, new, files, dry_run=dry_run)
    st["last_seen"] = text[:400]
    st["updated"] = time.time()
    return st


def main() -> int:
    ap = argparse.ArgumentParser(description="Watch the Claude chat for work orders and hand them to an agent.")
    ap.add_argument("--once", action="store_true", help="do a single pass and exit (for a timer)")
    ap.add_argument("--interval", type=int, default=None, help="seconds between passes (default from config, 30)")
    ap.add_argument("--dry-run", action="store_true", help="report work orders but start no agent")
    args = ap.parse_args()

    if acquire_watcher_lock() is None:
        log("watcher already running (watcher.lock is held) - exiting")
        return 0

    cfg = load_config()
    interval = args.interval or int(cfg.get("watch", {}).get("interval_seconds") or 30)
    log("watcher starting (interval %ss, auto_start=%s)%s" % (
        interval, cfg.get("watch", {}).get("auto_start", True), " [dry run]" if args.dry_run else ""))
    while True:
        try:
            save_state(pass_once(cfg, load_state(), dry_run=args.dry_run))
        except Exception as e:
            log("pass failed: %s" % e)
        if args.once:
            return 0
        time.sleep(interval)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
