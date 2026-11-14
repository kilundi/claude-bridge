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
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import wlock  # noqa: E402
from bridge import (Bridge, ClaudeUnavailable, cdp_alive, ensure_chrome,  # noqa: E402
                    extract_work_orders, order_problems, outbox_files, queue_message,
                    UP_SCRIPT)

# The watcher's own home: its config, state, log, lock, inbox and reports.  A second
# watcher - a separate pipeline beside the coding loop - sets CLAUDE_BRIDGE_HOME to its own
# directory, and the two then share nothing: no lock, no queue, no state, no chat, no
# slots.  Unset, HOME is HERE and every path below is exactly what it has always been, so
# the coding loop cannot be changed by this existing.
HOME = os.path.abspath(os.path.expanduser(os.environ.get("CLAUDE_BRIDGE_HOME") or HERE))

CONFIG = os.path.join(HOME, "config.json")
STATE = os.path.join(HOME, "state.json")
INBOX = os.path.join(HOME, "inbox")
LOG = os.path.join(HOME, "watcher.log")
TERMINAL_LOG = os.path.join(HOME, "terminal.log")
LOCK = os.path.join(HOME, "watcher.lock")
DOWNLOADS = os.path.join(HOME, "downloads")
# where an agent leaves its DONE report; the file appearing is what frees the order's
# slot without anybody having to read the report first
REPORTS = os.path.join(HOME, "reports")
# written when a turn is stalled and Claude did not answer the one automatic ask
NEEDS_ORDER = os.path.join(HOME, "NEEDS-ORDER.md")
# where the opencode background server registers itself (pid, url, password)
SERVICE_JSON = os.path.expanduser("~/.local/state/opencode/service.json")

# how many times start_agent may be retried after the first failure, and how
# long to wait between attempts (overridable from config.json watch settings)
START_MAX_RETRIES = 2
START_RETRY_DELAY = 5.0

# Free memory (MemAvailable, MB) an order needs before its agent may start.  On by
# default, because the failure it prevents is not a slow loop but a dead one: the
# background server died at 246 MiB free on 11-11 and every in-flight turn died with it.
# A config.json value of 0 turns the guard off.
#
# 800 and not the 1500 this plan first guessed: measured on this box, one agent plus
# Chrome routinely leaves ~1.0-1.2 GB, so 1500 held orders while the box had room for one
# more, and a held order is a stalled queue - the thing this list ranks *above* memory
# pressure.  800 is three times the level the server actually died at, and the cap (two
# agents) is what keeps the total bounded.
DEFAULT_MIN_FREE_MB = 800

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


def quiet(st: dict, why: str) -> None:
    """Say a non-action once, not once every 30 seconds."""
    nudge = st.setdefault("nudge", {"streak": 0})
    if nudge.get("last_wait") != why:
        nudge["last_wait"] = why
        log("  %s" % why)


def bridge_down_advice() -> str:
    """What to run when the bridge window is down - pointed at *this* pipeline's home.

    A second pipeline has its own Chrome, on its own account: the advice must carry its
    home, or the launcher it names would bring up the *other* pipeline's window.  For the
    coding loop, whose home is the checkout itself, the command is plain, as always.
    """
    how = "bash %s" % UP_SCRIPT
    if HOME != HERE:
        how = "CLAUDE_BRIDGE_HOME=%s %s" % (HOME, how)
    return ("bridge window is down, so work orders cannot be picked up. "
            "Run: %s  (or tell an agent to launch the bridge)" % how)


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
    # `held` is the one map whose *absences* carry meaning.  Its owner recomputes it from
    # the transcript every pass - a wait that is over, an order that has just started,
    # one the chat has scrolled past - and a union with the file would resurrect exactly
    # those: `watcher.sh done WO-116` popped the marker and the next save put it back, so
    # a finished order kept showing as waiting for a slot.
    merged["held"] = dict(st.get("held") or {})
    # And no *finished* order may keep a resume or waiting mark, whatever a stale copy still
    # says: merge puts one back every save otherwise, which is how WO-109 and WO-111-WO-114
    # went on reading as "resumed 3 time(s) - have a look at it" for days after they were
    # closed.  Only the rule is applied here, not the whole map: a second process's marks for
    # *live* orders are still merged, so a resume that is in cooldown stays in cooldown.
    done = set(merged.get("finished") or {})
    for key in ("resume", "waiting"):
        marks = merged.get(key)
        if isinstance(marks, dict):
            for oid in [o for o in marks if o in done]:
                marks.pop(oid, None)
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
    if service_down_seconds_left() > 0:
        # Every `opencode api` call with the service down spawns a client that tries to bring
        # a service *up*, and a new service replaces the registered one - so a pass that probes
        # every session is a herd of starters, which is the opposite of the one-starter design.
        raise RuntimeError("the background service is down: %s" % (_SERVICE_DOWN.get("why") or ""))
    cmd = [binpath, "api", method, path]
    if payload is not None:
        cmd += ["-d", json.dumps(payload)]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        text = (p.stdout or "") + (p.stderr or "")
        if "Background service failed to start" in text:
            note_service_down(text)
        raise RuntimeError("opencode api failed: %s%s" % (p.stdout[:300], p.stderr[:300]))
    try:
        return json.loads(p.stdout)
    except Exception:
        return {"raw": p.stdout}


# The service is not answering, and for this long no further call may spawn a starter.  Short
# on purpose: a recovered service must be noticed by itself, without anything being restarted
# to find out.
SERVICE_DOWN_MEMO = 120.0
_SERVICE_DOWN: dict = {}


def service_down_seconds_left(now: float | None = None) -> float:
    if not _SERVICE_DOWN:
        return 0.0
    left = SERVICE_DOWN_MEMO - ((now or time.time()) - float(_SERVICE_DOWN.get("at") or 0))
    return max(0.0, left)


def note_service_down(text: str = "") -> None:
    """Remember that the service is down, so no more calls this pass spawn a starter.

    The watcher is the *one* starter by design (see :func:`ensure_service`), but every
    ``opencode api`` call with the service down starts a client that tries to bring one up,
    and a new service replaces the registered one - which is written in the 11-11 log:
    *"managed service registration replaced; shutting down"*.  The watcher probed every
    session on every pass: 187 ``Background service failed to start`` lines in five minutes
    that evening, 94 in two minutes on 11-13, and 39 pid changes of which only 5 were ours.
    A probe must not be a second starter.  Loud on the way in, and at most one line each
    ten minutes while it lasts.
    """
    fresh = not _SERVICE_DOWN
    _SERVICE_DOWN["at"] = time.time()
    _SERVICE_DOWN["why"] = " ".join((text or "").split())[:160] or "the API did not answer"
    if fresh:
        log("the opencode background service is not answering (%s) - not probing anything "
            "further for %ds, so that no client starts a second one; the supervisor starts "
            "at most one a minute" % (_SERVICE_DOWN["why"], int(SERVICE_DOWN_MEMO)))
    else:
        log_once("service-still-down",
                 "the opencode background service is still down (%s)" % _SERVICE_DOWN["why"],
                 every=600)


def service_recovered() -> None:
    """Forget the outage: the service answers, so calls may try again."""
    if _SERVICE_DOWN:
        log("the opencode background service is answering again")
    _SERVICE_DOWN.clear()


# Log lines that would otherwise repeat every pass, keyed by what they are about.
_SAID: dict[str, float] = {}


def log_once(key: str, msg: str, every: float = 1800.0,
             now: float | None = None) -> None:
    """Say a thing once per ``every`` seconds per key, not once per pass.

    The log held 312 ``could not read ... session record`` lines and 125 ``is no longer in
    the chat`` lines, all of them the same fact about the same order repeated on every
    pass.  A log that repeats itself is a log nobody reads, which is how a real failure
    gets missed.
    """
    at = now or time.time()
    last = _SAID.get(key)
    if last is not None and at - last < every:
        return
    _SAID[key] = at
    log(msg)


# --- order quality: never start an agent on half a brief -----------------------------
# The protocol is a template, not a memory test.  A work order has to arrive
# complete; when it does not - or when Claude only *says* it will do the work and
# no order follows - the watcher asks for exactly what is missing, once, and then
# says so loudly instead of waiting forever.  None of this depends on Claude
# remembering the format as a conversation grows.

# future/continuous intent: "I'm fixing ...", "I'll post the order", "next up".
# Past tense ("I've fixed ...") and plain reports must not match - those are
# finished work, not a sign that an order is coming.
PROMISE_RE = re.compile(
    r"\b(?:"
    r"i(?:'m| am|'ll| will)\s+(?:\w+\s+){0,2}(?:fix|fixing|write|writing|implement|implementing|"
    r"build|building|draft|drafting|post|posting|send|sending|start|starting|do|doing|run|running|"
    r"move|moving|add|adding|take|taking|pick|picking|switch|switching|create|creating|begin|beginning)"
    r"|i(?:'m| am)\s+going\s+to\b"
    r"|next(?:\s+up)?\s*[:,]?\s*i(?:'ll| will| am|'m)?\b"
    r"|next\s+up\b"
    r"|let\s+me\s+(?:now\s+)?(?:fix|write|implement|build|do|start|post|draft|run|take|pick)"
    r"|about\s+to\s+(?:fix|write|implement|build|start|post|do)"
    r"|one\s+small\s+phase"
    r"|i(?:'ll| will)\s+write\s+the\s+(?:next\s+)?(?:work[- ]?order|order)"
    r"|i(?:'ll| will)\s+post\s+the\s+(?:next\s+)?(?:work[- ]?order|order)"
    r")", re.I)

FILE_REF_RE = re.compile(r"(?<![\w/.])([A-Za-z0-9][\w.-]*\.(?:md|txt|json|ya?ml|patch|diff))", re.I)
# a name that reads like something Claude wrote for the agent, not a repo file
ATTACH_NAME_RE = re.compile(r"^[\w.-]*(?:prompt|brief|order|handoff)[\w.-]*\.(?:md|txt|json|patch|diff)$", re.I)

ASK_FOOTER = ("(This is the work-order watcher checking in automatically. It asks at most once "
              "per turn, it never reads prose as an order, and it ignores this message if it "
              "is wrong.)")

ORDER_TEMPLATE = """<<<WORK-ORDER
id: {id}
dir: {dir}
branch: {branch}
goal: {goal}
acceptance: {acceptance}
>>>"""


def looks_like_work_promise(text: str) -> bool:
    """Does this message read as work Claude is about to do itself?"""
    return bool(PROMISE_RE.search(text or ""))


def _norm_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def find_downloaded(name: str, outdir: str = DOWNLOADS) -> str | None:
    """The local copy of a file Claude named, tolerating " (1)" and renames."""
    exact = os.path.join(outdir, name)
    if os.path.isfile(exact):
        return exact
    want = _norm_name(name)
    stem = _norm_name(re.sub(r"\.[A-Za-z0-9]{1,5}$", "", name))
    ext = os.path.splitext(name)[1].lower()
    try:
        entries = sorted(os.listdir(outdir))
    except OSError:
        return None
    for f in entries:
        p = os.path.join(outdir, f)
        if os.path.isfile(p) and _norm_name(f) == want:
            return p
    for f in entries:
        p = os.path.join(outdir, f)
        if (os.path.isfile(p) and len(stem) >= 6 and stem in _norm_name(f)
                and (not ext or f.lower().endswith(ext))):
            return p
    return None


def order_file_refs(order: dict, outdir: str = DOWNLOADS) -> list[str]:
    """File names in an order that are worth chasing (Claude's own attachments)."""
    names: list[str] = []
    for m in FILE_REF_RE.finditer(order.get("raw") or ""):
        name = m.group(1)
        if name in names or "/" in name:
            continue
        if ATTACH_NAME_RE.match(name) or find_downloaded(name, outdir):
            names.append(name)
    return names


def resolve_order_files(order: dict, files: list[dict],
                        outdir: str = DOWNLOADS) -> tuple[list[dict], list[str]]:
    """Attach every file the order names that exists; report the ones that do not.

    The absolute path is what matters: if the download step failed, the order
    still says exactly where to look, instead of the agent starting blind.
    """
    attached = list(files)
    known = {f.get("path") for f in attached}
    missing: list[str] = []
    for name in order_file_refs(order, outdir):
        found = find_downloaded(name, outdir)
        if found:
            if found not in known:
                attached.append({"name": name, "path": found, "bytes": os.path.getsize(found)})
                known.add(found)
        else:
            missing.append(os.path.join(outdir, name))
    return attached, missing


def route_orders(st: dict, orders: list[dict]) -> dict:
    """Split extracted orders into what the watcher should do with each.

    * dispatch   - new, complete, never run
    * incomplete - told us to do something, but not where or how it is done
    * duplicate  - an id that already ran, re-posted with different text
    * ignored    - byte-identical re-post of something already handled
    """
    out: dict[str, list] = {"dispatch": [], "incomplete": [], "duplicate": [], "ignored": []}
    seen = st.get("orders") or {}
    sessions = st.get("sessions") or {}
    starts = st.get("starts") or {}
    if isinstance(seen, list):                     # tolerate an older state file
        seen = {k: "" for k in seen}
    for o in orders:
        if seen.get(o["id"]) == _hash(o["raw"]):
            out["ignored"].append(o)
            continue
        problems = order_problems(o)
        if problems:
            out["incomplete"].append((o, problems))
            continue
        rec = starts.get(o["id"]) or {}
        ran = bool(sessions.get(o["id"]) or rec.get("session")
                   or rec.get("status") in ("started", "dry_run"))
        out["duplicate" if ran else "dispatch"].append(o)
    return out


# --- the background server, and what survives it dying --------------------------------
# Every agent's turn runs inside one process: the opencode background server. When it
# dies - under memory pressure, on a restart, when the desktop session is torn down -
# every in-flight turn dies with it, silently, and neither side of the loop notices.
# These helpers are the watcher noticing, and doing what it can about it: keep the
# server up, cap how many agents run at once (five of them on a 6 GB machine is what
# killed it), resume the turns a restart interrupted, and close the windows left behind.


def service_pid() -> int | None:
    """The pid of the running opencode background server, or None if unreadable."""
    try:
        with open(SERVICE_JSON) as fh:
            return int((json.load(fh) or {}).get("pid") or 0) or None
    except Exception:
        return None


def note_service(st: dict, dry_run: bool = False) -> dict | None:
    """Remember which server we saw; return {"from","to"} when the pid changed.

    The pid is kept in state.json, so a restart is still noticed after the watcher
    itself was restarted - or after the whole machine was.
    """
    pid = service_pid()
    if not pid:
        return None
    prev = (st.get("service") or {}).get("pid")
    if not dry_run:
        rec = st.setdefault("service", {})
        rec["pid"] = pid
        rec["seen"] = time.time()
    if prev and prev != pid:
        return {"from": prev, "to": pid}
    return None


def explain_service_restart(st: dict, change: dict) -> str:
    """Say whether the watcher started this server or something else replaced it.

    The pid changing is the expensive event - every in-flight turn dies with the old
    server - and for months the log said only that it happened.  Of the 39 changes on
    record only 5 were ours (``service.started``: the watcher's one-per-cooldown start),
    so the useful question is which it was, and only the state can answer it.
    """
    started = float((st.get("service") or {}).get("started") or 0)
    if started and abs(time.time() - started) <= 180:
        return "by the watcher (it found no live server)"
    return "by something else (an opencode client started its own; the watcher did not)"


def record_service_change(st: dict, change: dict, why: str) -> None:
    """Keep the last few replacements, with the reason, so `status` can explain today's."""
    rec = st.setdefault("service", {})
    ring = rec.setdefault("changes", [])
    ring.append({"at": time.time(), "from": change.get("from"), "to": change.get("to"),
                 "why": why})
    del ring[:-5]


def _pid_alive(pid: int) -> bool:
    """Is this pid a process that still exists?"""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def ensure_service(cfg: dict, st: dict, dry_run: bool = False) -> bool:
    """Start the opencode background server if it is gone.  At most once a minute.

    Nothing owns that process: when it died at 03:53 the only reason it came back was
    a stampede of clients each starting one, which is why it flapped twice.  One
    starter is the fix, and it is the watcher.  A live server is never touched.
    """
    if dry_run:
        return False
    pid = service_pid()
    if pid and _pid_alive(pid):
        service_recovered()
        return False                           # alive - never interfere with it
    w = cfg.get("watch") or {}
    rec = st.setdefault("service", {})
    if time.time() - float(rec.get("started") or 0) < float(w.get("service_start_cooldown", 60)):
        return False
    rec["started"] = time.time()
    binp = os.path.expanduser(w.get("opencode_bin") or "~/.opencode/bin/opencode")
    recovered = False
    try:
        p = subprocess.run([binp, "service", "start"], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=180)
        log("  the opencode background server was gone - started it (pid %s): %s"
            % (service_pid(), " ".join((p.stdout or p.stderr or "").split())[:160]))
        recovered = True
    except Exception as e:
        log("  could not start the opencode background server: %s" % e)
        return False
    if recovered:
        service_recovered()
    return True


def _moved_at(binp: str, sid: str) -> float | None:
    """When this session last moved, from its own record.

    Deliberately not the transcript: a long session's message page is big enough that
    the CLI's output is truncated mid-JSON (261 KB, unterminated string), which would
    read as "unknown" exactly when an agent has the most work behind it.  The session
    record is a few hundred bytes and carries ``time.updated``.
    """
    data = oc_api(binp, "GET", "/api/session/%s" % sid, timeout=30)
    data = data.get("data") if isinstance(data, dict) else data
    if not isinstance(data, dict):
        return None
    t = data.get("time") or {}
    for key in ("updated", "created"):
        v = t.get(key)
        if isinstance(v, (int, float)):
            return v / 1000.0
    return None


# A slot belongs to a running agent, and the only honest way to know is to look at the
# session's transcript.  The answers are kept for a few seconds: one pass asks about the
# same sessions more than once, and a hung API would charge its full timeout each time.
_PROBE_MEMO: dict[str, tuple[float, str | None]] = {}

# The tools whose only job is to ask a person something.  A turn parked on one of these
# is alive but nobody is working, which is the one case where a slot is held by
# something that is not computing.
PARKED_TOOLS = ("question", "permission")


def _parked_on_a_human(message: dict) -> bool:
    """Is this turn stopped, waiting for a person to answer?

    The ``question`` tool (and a permission prompt) keeps its part ``running`` while it
    waits, so the turn stays open and a timestamp cannot tell it from real work.  Only
    the part says which it is - and only these two tools wait on a human by
    construction: a long shell is work, and it must keep its slot.
    """
    for p in (message.get("content") or []):
        if not isinstance(p, dict) or p.get("type") != "tool":
            continue
        name = str(p.get("name") or "").lower()
        if not any(t in name for t in PARKED_TOOLS):
            continue
        if (p.get("state") or {}).get("status") in ("running", "pending"):
            return True
    return False


def _turn_verdict(binp: str, sid: str, ttl: float = 15.0) -> str | None:
    """What this session's turn is doing right now: ``working``, ``waiting``, ``idle``.

    A timestamp cannot answer it: a row of end-to-end tests is silent for 48 minutes
    and a turn killed with the background server is silent too, and the two want
    opposite things.  The transcript itself can: its newest entry is an ``idle`` marker
    once a turn has ended, so a newest entry that is anything else means work is in
    progress - and if that entry is parked on a human, the work is *not* happening.
    Freeing that slot is the difference between a queue that keeps moving while a person
    reads a question and one that stops behind it (WO-113 sat on a slot for ten minutes
    waiting for an answer).

    ``limit=1`` keeps that answer to a few hundred bytes.  The whole page is ~400 KB
    and the CLI truncates it mid-JSON, which would read as "unknown" exactly when the
    longest session has the most work behind it.  ``None`` means it could not be read,
    and callers must hold the slot then - never free one on a guess.

    Cached for ``ttl`` seconds: a single pass asks about the same sessions two or three
    times, and when the API hangs each ask costs its full timeout.
    """
    hit = _PROBE_MEMO.get(sid)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    answer: str | None
    try:
        page = oc_api(binp, "GET", "/api/session/%s/message?limit=1" % sid, timeout=20)
    except Exception:
        answer = None
    else:
        items = page.get("data") if isinstance(page, dict) else page
        if not isinstance(items, list) or not items:
            answer = None
        elif (items[0] or {}).get("type") == "idle":
            answer = "idle"
        elif _parked_on_a_human(items[0] or {}):
            answer = "waiting"
        else:
            answer = "working"
    _PROBE_MEMO[sid] = (time.time(), answer)
    return answer


# A DONE report is the one thing an agent tells us that the loop cannot see: it is sent
# to the planner, not to the watcher.  Recognising it is what lets a finished order free
# its own slot, so the test is deliberately strict - a line that says DONE as a word,
# plus at least two of the bullets the report format defines.
REPORT_BULLETS = ("- project:", "- branch:", "- commits:", "- tests:", "- notes:")
_DONE_WORD = re.compile(r"(^|[^A-Za-z])DONE([^A-Za-z]|$)")


def looks_like_a_done_report(text: str) -> bool:
    """Is this text a DONE report, or just something that mentions one?"""
    text = text or ""
    if not _DONE_WORD.search(text):
        return False
    return sum(1 for b in REPORT_BULLETS if b in text) >= 2


def report_file(oid: str) -> str:
    """Where an order's agent is asked to leave its report."""
    return os.path.join(REPORTS, "%s.md" % oid)


# a report is looked for once per turn, not once per pass: keyed by session and by when
# that session last moved
_REPORT_SCAN: dict[tuple[str, float], bool] = {}

# How far back a report is looked for.  One turn can leave several messages *after* the
# report itself - WO-120 reported DONE at 15:51:37 and then talked about the next order for
# two more messages before its turn ended, which a two-entry scan missed entirely.  Six
# entries stay small enough to survive the CLI's truncation in normal use; the report file
# is the reliable path for everything else.
REPORT_SCAN_MESSAGES = 6


def reported_done(cfg: dict, sid: str, moved: float | None) -> bool:
    """Is a DONE report in this session's own transcript?

    The newest ``REPORT_SCAN_MESSAGES`` entries are read, and only the agent's own *input*
    counts: a tool's *output* would match every time an agent read the AGENTS.md template,
    which is not a report.  Anything unreadable answers ``False`` - the report file and
    ``watcher.sh done`` still work, and a wrong "done" would be worse than a late one.
    """
    key = (sid, float(moved or 0))
    if key in _REPORT_SCAN:
        return _REPORT_SCAN[key]
    found = False
    try:
        binp = os.path.expanduser((cfg.get("watch") or {}).get("opencode_bin")
                                  or "~/.opencode/bin/opencode")
        page = oc_api(binp, "GET", "/api/session/%s/message?limit=%d"
                      % (sid, REPORT_SCAN_MESSAGES), timeout=20)
        items = page.get("data") if isinstance(page, dict) else page
        for m in (items if isinstance(items, list) else []):
            for p in ((m or {}).get("content") or []):
                if isinstance(p, dict) and p.get("type") == "tool":
                    state = p.get("state") or {}
                    if looks_like_a_done_report(json.dumps(state.get("input") or "")):
                        found = True
    except Exception:
        found = False
    if len(_REPORT_SCAN) > 200:               # a long-lived watcher, a bounded memo
        _REPORT_SCAN.clear()
    _REPORT_SCAN[key] = found
    return found


def session_activity(cfg: dict, st: dict, now: float | None = None) -> list[dict]:
    """When each agent last moved: [{"order","session","last","live","finished"}].

    ``outcome`` is not a liveness signal: a busy agent reads as "interrupted", and so
    does one a *person* stopped (WO-122, 2026-11-11 15:55) - the two are identical, so a
    resume stays tied to the service restart it can actually prove.  The session's own
    timestamps are what we count.  ``last`` is None when it cannot be read: unknown, and
    never treated as idle.  ``finished`` is carried along so the slot count can leave
    those orders out: a person poking a finished session must not hold a worker slot.
    Oldest start first.
    """
    now = now or time.time()
    w = cfg.get("watch") or {}
    binp = os.path.expanduser(w.get("opencode_bin") or "~/.opencode/bin/opencode")
    window = float(w.get("agent_active_seconds", 1800))
    starts = st.get("starts") or {}
    done = st.get("finished") or {}
    out: list[dict] = []
    for oid, sid in (st.get("sessions") or {}).items():
        last: float | None = None
        try:
            last = _moved_at(binp, sid)
        except Exception as e:
            # once per order per half hour, not once per order per pass: this line alone
            # filled the log 312 times while the service was down
            log_once("unreadable-%s" % oid,
                     "  could not read %s's session record: %s" % (oid, e), every=1800)
        out.append({"order": oid, "session": sid, "last": last, "finished": oid in done,
                    "live": bool(last and now - last < window)})
    out.sort(key=lambda a: (starts.get(a["order"]) or {}).get("updated") or 0)
    return out


def live_agents(activity: list[dict]) -> list[dict]:
    """The agents whose transcript moved inside ``agent_active_seconds``."""
    return [a for a in activity if a.get("live")]


def slot_agents(activity: list[dict], cfg: dict, now: float | None = None,
                probe=None) -> list[dict]:
    """The agents holding one of the ``max_agents`` slots.

    A slot belongs to a *running* agent, not a recently touched one, and a timestamp
    cannot tell the difference: a row of end-to-end tests is silent for 48.8 minutes and
    a turn killed with the background server is silent too - and they want opposite
    things.  So the clock only decides *when* to ask:

    * quiet for less than ``slot_probe_seconds`` (2 min) - hold, no question asked.  That
      is the race guard: the window in which a prompt (a start, a resume, a new message
      from the user) has been accepted but its turn has not appeared yet.
    * quiet for longer - ask ``probe`` what the turn is doing
      (:func:`_turn_verdict`).  Hold while it is *working*; free the slot the moment it
      has ended *or* parked itself on a human question - an agent waiting for input
      deserves the first, and a queue must not stop behind the second.  Note that a
      *resume is not work*: it moved the clock, so the old rule held the slot for an
      hour even when the resumed turn died in five seconds, and the queue stopped
      behind it.
    * past ``slot_seconds`` (an hour) - freed without asking.  The backstop for a probe
      that cannot answer, not the normal rule.

    An order marked finished holds no slot whatever its session is doing: the person may
    still be typing in that window, and that is not a worker.
    ``watcher.sh done <id>`` frees a slot at once, which is what a DONE report means.
    """
    now = now or time.time()
    w = cfg.get("watch") or {}
    hold = float(w.get("slot_seconds", 3600))
    grace = float(w.get("slot_probe_seconds", 120))
    out: list[dict] = []
    for a in activity:
        if a.get("finished"):
            continue
        last = a.get("last")
        if last is None:              # unreadable: never counted, as it never was
            continue
        age = now - last
        if age >= hold:
            continue
        if age < grace:               # just moved, or just poked: never guess here
            out.append(a)
            continue
        verdict = probe(a) if probe else None
        a["turn"] = verdict
        a["in_turn"] = None if verdict is None else (verdict == "working")
        if verdict in (None, "working"):
            out.append(a)             # working, or nobody could say: hold, never guess
    return out


def turn_probe(cfg: dict):
    """A ``probe`` for :func:`slot_agents`: what is this session's turn doing?"""
    binp = os.path.expanduser((cfg.get("watch") or {}).get("opencode_bin")
                              or "~/.opencode/bin/opencode")
    return lambda a: _turn_verdict(binp, a.get("session") or "")


def agent_busy(cfg: dict, st: dict, now: float | None = None) -> dict:
    """Best effort: is the most recently started agent still working?

    For callers that ask only that (the ask logic).  A pass already holds the
    activity list, so it uses ``live_agents`` on that instead of re-reading here.

    An order marked finished is not a worker, whatever its session is doing: the
    person may still be typing in that window, which keeps the transcript moving.
    ``slot_agents`` has always left those out; this path did not, and on 2026-11-12
    WO-130's finished window sat here and answered "WO-130 is still working" on every
    pass - so the ask WO-149 needed (its block had no ``acceptance`` line) was held
    forever and the queue never moved.  A live transcript is not evidence of work;
    a *finished* order is evidence there is none.
    """
    live = [a for a in live_agents(session_activity(cfg, st, now=now))
            if not a.get("finished")]
    return live[-1] if live else {}


def resume_prompt(oid: str, order: dict) -> str:
    """What to say to an agent whose turn died with the server."""
    d = order.get("dir") or "the repository"
    return "\n".join([
        "Your previous turn was interrupted: the opencode background server went down",
        "(the machine ran out of memory) and took your turn with it.  You were stopped",
        "mid-task and nothing told you, so carry the order on from where it stopped.",
        "",
        "Work order %s.  First look at what is already done:" % oid,
        "  cd %s && git status && git log --oneline -5" % d,
        "",
        "Then: do NOT redo committed work - continue the remaining steps, committing",
        "each one as you go, run the checks the order asks for, and report DONE back",
        "with ask_claude.  If the order is in fact finished, just send the DONE report.",
    ])


def resume_stalled(cfg: dict, st: dict, activity: list[dict], restarted: dict | None,
                   dry_run: bool = False) -> list[str]:
    """Tell the agents a server restart interrupted to carry on.

    Only a restart triggers this: on a healthy server an idle session is just idle
    (finished, or waiting on something of its own) and re-prompting it would burn
    turns for nothing.  Bounded three ways - ``resume_max`` per order, one per
    ``resume_cooldown_seconds``, and ``resume_max_per_pass`` at a time, so a reboot
    cannot fire up every agent that ever ran - and it never touches an agent that is
    working, an order marked finished, or one whose agent is parked on a question
    somebody still has to answer.
    """
    w = cfg.get("watch") or {}
    if dry_run or not w.get("resume_interrupted", True) or not restarted:
        return []
    now = time.time()
    cooldown = float(w.get("resume_cooldown_seconds", 1800))
    most = int(w.get("resume_max", 3))
    per_pass = int(w.get("resume_max_per_pass", 2))
    freshest = float(w.get("resume_max_age_seconds", 21600))
    res = st.setdefault("resume", {})
    done = st.get("finished") or {}
    waiting = set(st.get("waiting") or {})
    cands: list[dict] = []
    for a in activity:
        oid = a["order"]
        rec = (st.get("starts") or {}).get(oid) or {}
        r = res.get(oid) or {}
        if rec.get("status") != "started" or oid in done or oid in waiting or a.get("live"):
            continue
        if a.get("last") is None or now - a["last"] > freshest:
            continue                     # unreadable, or long past - leave it be
        if int(r.get("count") or 0) >= most:
            continue                     # asked enough times - a human's turn
        if now - float(r.get("at") or 0) < cooldown:
            continue
        cands.append(a)
    if not cands:
        return []
    cands.sort(key=lambda a: a["last"], reverse=True)          # freshest first
    binp = os.path.expanduser(w.get("opencode_bin") or "~/.opencode/bin/opencode")
    out: list[str] = []
    for a in cands[:per_pass]:
        oid, sid = a["order"], a["session"]
        order = ((st.get("starts") or {}).get(oid) or {}).get("order") or {}
        try:
            oc_api(binp, "POST", "/api/session/%s/prompt" % sid,
                   {"text": resume_prompt(oid, order)}, timeout=120)
        except Exception as e:
            log("  could not resume %s: %s" % (oid, e))
            continue
        r = res.get(oid) or {}
        r.update({"count": int(r.get("count") or 0) + 1, "at": now})
        res[oid] = r
        log("  resumed %s (session %s): the restart killed its turn - told it to check "
            "git and carry on" % (oid, sid))
        out.append(oid)
    if len(cands) > len(out):
        quiet(st, "%d more interrupted order(s) - the rest resume on a later pass"
              % (len(cands) - len(out)))
    return out


def prune_marks(st: dict, orders: list[dict],
                activity: list[dict] | None = None) -> list[tuple[str, str]]:
    """Drop the held and waiting markers that are no longer a real wait.

    Both are recomputed every pass from the transcript, so only the markers for orders
    read *this* pass and still open survive.  A **held** mark (an order waiting for a
    slot) only means something while the order is still in the chat's own transcript: one
    that has scrolled out will never be read again, and ``status`` showing it would be a
    lie about the queue.

    A **waiting** mark (an agent parked on a question) is not like that, and that is why
    this reads the activity as well: the question is answered *in that agent's window*, so
    the mark is what keeps the view from being reaped, and it is still true after the
    order's message has scrolled out of the chat.  It goes when there is no session left
    to be parked in - or the order is finished (``note_waiting`` refuses to re-add that
    one; a finish removes it).

    Before this, one parked order whose message had scrolled out came back every single
    pass - ``note_waiting`` re-added it from the live session, this dropped it as "no
    longer in the chat", and the log said so 125 times.

    Returns ``(order, why)`` pairs, for a log line that says the right reason.
    """
    here = {o["id"] for o in orders}
    with_a_session = {a["order"] for a in (activity or [])}
    done = set(st.get("finished") or {})
    gone: list[tuple[str, str]] = []
    seen: set[str] = set()
    for key in ("held", "waiting"):
        marks = st.setdefault(key, {})
        for oid in list(marks):
            why = None
            if oid in done:
                why = "finished"
            elif oid not in here:
                if key == "held":
                    why = "no longer in the chat"
                elif oid not in with_a_session:
                    why = "no longer in the chat, and no session left to wait in"
            if not why:
                continue
            marks.pop(oid, None)
            if oid not in seen:
                seen.add(oid)
                gone.append((oid, why))
    return gone


def free_memory_mb() -> int | None:
    """What the kernel says a new process may have, in MB (``MemAvailable``).

    ``MemAvailable`` and not ``MemFree``: page cache counts, and on this box that is the
    difference between "180 MB" and "two gigabytes".  ``None`` means the reading failed -
    the guard then holds nothing, because a broken ``/proc/meminfo`` must never stop work.
    """
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        return None
    return None


def memory_holds(cfg: dict, st: dict, orders: list[dict]) -> bool:
    """Hold these orders when free memory is under the floor, and say what it read.

    Memory is what killed the background server on 11-11 (246 MiB free, and every
    in-flight turn with it) and left the same signature in the log on 11-13, right before
    the reboot.  ``max_agents`` counts *agents*, and one `jest --runInBand` peaks near
    1.8 GB: two agents are fine or fatal depending on what they happen to be running, so
    the count was never the whole guard.  ``min_free_mb`` (1500 by default, ``0`` turns it
    off) is the rest of it.

    An order held here is deliberately left unmarked, exactly like one held over the cap:
    a later pass picks it up the moment memory frees.  An agent that is already running is
    never touched - a turn in flight is work, and killing it to save memory is how work is
    lost.
    """
    floor = int((cfg.get("watch") or {}).get("min_free_mb", DEFAULT_MIN_FREE_MB) or 0)
    if floor <= 0 or not orders:
        return False
    avail = free_memory_mb()
    if avail is None:
        return False                     # unreadable: never hold work on a failed reading
    st["memory"] = {"available_mb": avail, "floor_mb": floor, "at": time.time(),
                    "holding": []}
    if avail >= floor:
        return False
    holds = st.setdefault("held", {})
    for o in orders:
        if o["id"] not in holds:
            log("holding %s: %d MB free is under min_free_mb=%d, and low memory is what "
                "killed the background server and every turn in it on 11-11; it starts when "
                "memory frees up" % (o["id"], avail, floor))
        holds[o["id"]] = time.time()
    st["memory"]["holding"] = [o["id"] for o in orders]
    return True


def dispatch_batch(cfg: dict, st: dict, orders: list[dict], dry_run: bool = False,
                   activity: list[dict] | None = None) -> tuple[list[dict], list[dict]]:
    """How many of these orders may start now, and which have to wait for a slot.

    An order that has to wait is deliberately left unmarked in state, so the next pass
    sees it again and starts it the moment an agent frees a slot: nothing is dropped,
    and the wait is visible in the log.  ``max_agents`` of 0 means no cap, and the
    free-memory floor is checked first because it says nothing about how many agents
    there are (:func:`memory_holds`).
    """
    cap = int((cfg.get("watch") or {}).get("max_agents") or 0)
    if dry_run or not orders:
        return list(orders), []
    if memory_holds(cfg, st, orders):
        return [], list(orders)
    if cap <= 0:
        return list(orders), []
    act = activity if activity is not None else session_activity(cfg, st)
    working = slot_agents(act, cfg, probe=turn_probe(cfg))
    room = max(0, cap - len(working))
    holds = st.setdefault("held", {})
    for o in orders[:room]:
        holds.pop(o["id"], None)
    held = list(orders[room:])
    for o in held:
        if o["id"] not in holds:
            log("holding %s: %d/%d agents are already working (max_agents) - it starts "
                "when one frees a slot" % (o["id"], len(working), cap))
        holds[o["id"]] = time.time()
    return list(orders[:room]), held


def attention_needed(routed: dict, text: str) -> bool:
    """Would this message need an ask at all?  Deliberately cheap: no API calls,
    so the watcher only checks whether an agent is busy when it has to."""
    if routed.get("dispatch") or routed.get("ignored"):
        return False
    return bool(routed.get("incomplete") or routed.get("duplicate")
                or looks_like_work_promise(text))


def plan_attention(st: dict, text: str, routed: dict, busy: dict, cfg: dict) -> dict:
    """Decide what Claude's newest message needs - nothing, an ask, or the loud flag.

    Pure apart from the ``nudge`` record in ``st``: it is re-run every pass while
    the same message stays newest, so it must not consume anything unless it is
    really going to send.  ``nudge.streak`` counts the asks since the last order
    was dispatched, which is what bounds this to one message per turn.
    """
    watch = cfg.get("watch", {})
    if not watch.get("ask_for_order", True):
        return {"action": "off", "why": "ask_for_order is off"}
    if not attention_needed(routed, text):
        return {"action": "none", "why": "the newest message needs nothing"}
    if busy:
        return {"action": "wait", "why": "%s is still working" % busy.get("order")}
    has_incomplete = bool(routed.get("incomplete"))
    has_duplicate = bool(routed.get("duplicate"))
    kind = "incomplete" if has_incomplete else ("duplicate" if has_duplicate else "stall")
    nudge = st.setdefault("nudge", {"streak": 0})
    streak = int(nudge.get("streak") or 0)
    since = time.time() - float(nudge.get("at") or 0)
    max_streak = int(watch.get("ask_max_streak", 1))
    if streak >= max_streak:
        if since < float(watch.get("ask_grace_seconds", 600)):
            return {"action": "wait", "kind": kind,
                    "why": "asked %d min ago, giving Claude time" % max(1, int(since / 60))}
        return {"action": "needs_order", "kind": kind,
                "why": "asked %d time(s) and no order followed" % streak}
    if since < float(watch.get("ask_min_seconds", 120)):
        return {"action": "wait", "kind": kind, "why": "asked %d s ago" % int(since)}
    return {"action": "ask", "kind": kind, "why": "no dispatchable order in the message"}


def build_ask(plan: dict, routed: dict, text: str) -> str:
    """The one message we send back: the exact template, and only what is missing."""
    kind = plan.get("kind")
    if kind == "incomplete":
        order, problems = (routed.get("incomplete") or [({}, [])])[0]
        return (
            "WATCHER: your %s block is not dispatchable yet - missing: %s. Nothing was started: "
            "an agent with no dir, or no acceptance test, burns a whole turn guessing. If the "
            "work is real, re-post the order complete, same id, with the missing lines:\n\n%s\n\n"
            "If you wrote an agent prompt file, attach it as a file card as well - and put its "
            "absolute path inside the goal as a fallback.\n%s"
            % (order.get("id") or "work order", ", ".join(problems),
               ORDER_TEMPLATE.format(id=order.get("id") or "WO-<next free>",
                                     dir=order.get("dir") or "/absolute/path/to/repo",
                                     branch=order.get("branch") or "branch",
                                     goal=order.get("goal") or "what to build",
                                     acceptance=order.get("acceptance") or "how we know it is done"),
               ASK_FOOTER))
    if kind == "duplicate":
        order = (routed.get("duplicate") or [{}])[0]
        return (
            "WATCHER: %s already ran and the text you just posted differs from the order it was "
            "given, so nothing was started - one id is one agent, and a second one is how two "
            "agents end up in the same repo. If this is new work, post it under the next free id; "
            "if you were quoting the old order, ignore this message.\n%s"
            % (order.get("id") or "that order", ASK_FOOTER))
    snippet = " ".join((text or "").split())[-240:]
    return (
        "WATCHER: your last message reads as work you are about to do - \"%s\" - but it carries no "
        "WORK-ORDER block, so nothing was dispatched and no agent has started. If that work is "
        "real, post it now as:\n\n%s\n\n"
        "If you were only summarising, ignore this message.\n%s"
        % (snippet,
           ORDER_TEMPLATE.format(id="WO-<next free>", dir="/absolute/path/to/repo", branch="branch",
                                 goal="what to build", acceptance="how we know it is done"),
           ASK_FOOTER))


def send_ask(st: dict, b: Bridge, text: str, timeout: float = 300.0) -> str:
    """Send one automatic ask and remember that we did.

    The bookkeeping happens whether or not Claude can answer: if the window is
    down the message is queued and delivered later, which still counts as asked.
    """
    nudge = st.setdefault("nudge", {"streak": 0})
    nudge["streak"] = int(nudge.get("streak") or 0) + 1
    nudge["at"] = time.time()
    nudge["text"] = text[:200]
    try:
        b.ask(text, timeout=timeout)
        return "sent"
    except ClaudeUnavailable as e:
        if e.sent:
            return "sent (Claude reads it when the limit lifts)"
        return "queued for delivery (%s)" % (e.queued or e.text)
    except TimeoutError:
        # the message is in the chat; its reply can finish on a later pass
        return "sent, reply still coming"


def clear_needs_order(st: dict) -> None:
    st.pop("needs_order", None)
    try:
        os.remove(NEEDS_ORDER)
    except OSError:
        pass


def write_needs_order(st: dict, why: str, detail: str, snippet: str = "") -> None:
    """The loud part: a file next to the bridge, a log line, and a state flag."""
    st["needs_order"] = {"why": why, "at": time.time(), "detail": detail[:600]}
    lines = [
        "# The work-order queue is stalled",
        "",
        "When : %s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "Why  : %s" % why,
        "",
        "What the watcher already asked Claude:",
        "",
        detail.strip(),
        "",
    ]
    if snippet:
        lines += ["Claude's newest message (tail):", "```",
                  " ".join((snippet or "").split())[-600:], "```", ""]
    lines += [
        "No agent is running for this turn. The watcher asks once per turn and will not",
        "ask again, so this one is on a human: get Claude to post a complete WORK-ORDER",
        "block (ask-claude \"...\") or start the work yourself.",
        "",
        "This file is deleted the moment an order is dispatched.",
    ]
    with open(NEEDS_ORDER, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    log("NEEDS ORDER - %s - see %s" % (why, NEEDS_ORDER))


def order_to_inbox(order: dict, files: list[dict]) -> str:
    os.makedirs(INBOX, exist_ok=True)
    # the order promises the agent a place to leave its report, so make it real before
    # the agent runs: an agent whose write has nowhere to go cannot free its own slot
    os.makedirs(REPORTS, exist_ok=True)
    body = ["# Work order %s" % order["id"], ""]
    if order.get("dir"):
        body.append("dir: %s" % order["dir"])
    if order.get("session"):
        body.append("session: %s (delivered into this session, not a fresh one)" % order["session"])
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
    if order.get("missing_files"):
        body += ["## Files Claude named but the bridge could not download",
                 "Read them from these paths if they exist (and check the repository):"]
        for p in order["missing_files"]:
            body.append("- %s" % p)
        body.append("")
    body += ["## Reporting",
             "Send your DONE report to the planner as usual, and save the same report to",
             "  %s" % report_file(order["id"]),
             "That file appearing is what marks this order finished and frees its slot, with",
             "nobody having to notice the report first. Then stop - a continuation arrives as",
             "a new work order, not in this session.",
             ""]
    body += ["## Work order exactly as Claude wrote it", "```", order["raw"], "```", ""]
    path = os.path.join(INBOX, "%s.md" % order["id"])
    with open(path, "w") as fh:
        fh.write("\n".join(body) + "\n")
    return path


def build_prompt(order: dict, files: list[dict], inbox_path: str) -> str:
    lines = ["Work order %s from Claude, received over the claude-bridge chat queue." % order["id"], ""]
    if order.get("session"):
        lines.append("This order was delivered into your existing session (%s), not a fresh"
                     % order["session"])
        lines.append("one - carry on from what you already know about this project.")
        lines.append("")
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
    if order.get("missing_files"):
        lines.append("Claude's order names files the bridge could NOT download. Read them from")
        lines.append("these exact paths if they exist, and look in the repository too - do not")
        lines.append("start work without them:")
        for p in order["missing_files"]:
            lines.append("  - %s" % p)
        lines.append("")
    report = report_file(order["id"])
    lines += [
        "When Claude says to save a file in a particular directory or at a particular path, do exactly that:",
        "write the file there (create the directory if needed), and copy any attached file listed above to the",
        "place Claude asked for. If Claude names a file it wrote but did not attach, look for it under",
        "~/claude-bridge/downloads/ and in the repository it belongs to.",
        "",
        "The full order, exactly as Claude wrote it, is saved at %s." % inbox_path,
        "",
    ]
    if order.get("branch"):
        lines.append("Do the work: make the change, run the checks, and commit and push to the "
                     "branch above.")
    else:
        lines.append("Do the work: make the change and run the checks the order asks for, and "
                     "commit and push it if this project is a git repository.")
    lines += [
        "",
        "Write your DONE report in this shape:",
        "  DONE",
        "  - project: <name>",
        "  - branch: <branch>",
        "  - commits: <hashes> (pushed / not pushed)",
        "  - tests: <what you ran and the result>",
        "  - notes: <anything odd, or a question>",
        "  NEXT: what should I take on?",
        "",
    ]
    if HOME == HERE:
        lines += [
            "Send that report to the planner with the ask_claude tool, and save the same text to",
            "%s as well - that file appearing is what marks this order" % report,
            "finished and frees its slot for the next one, with nobody having to notice the report",
            "first.",
        ]
    else:
        # A second pipeline's planner is a different chat reached through a different home, and
        # ask_claude is one global tool wired to the *coding* chat (the opencode server starts it
        # with no CLAUDE_BRIDGE_HOME, so it resolves the coding home).  An agent that used it here
        # would post a report into the coding planner's thread, which that planner would read as
        # coding work - the one contamination this whole separation exists to prevent.  The file
        # goes to *this* pipeline's planner, and nothing has to be noticed for that to happen.
        lines += [
            "Save it to %s - that file appearing is what marks this order" % report,
            "finished and frees its slot for the next one, and the loop posts it to the planner of",
            "*this* pipeline.",
            "Do NOT use the ask_claude tool for this: it is wired to a different pipeline's planner,"
            " a different chat that would read your report as its own work. Write the file instead.",
        ]
    lines += [
        "Then stop: a continuation arrives as a new work order, not in this session.",
        "If you are blocked on something only the person can decide, say so plainly in the",
        "report - your slot is freed while you wait, and your window is kept open for the",
        "answer.",
    ]
    return "\n".join(lines)


def start_agent(cfg: dict, order: dict, files: list[dict], inbox_path: str) -> str:
    w = cfg.get("watch", {})
    binp = os.path.expanduser(w.get("opencode_bin") or "~/.opencode/bin/opencode")
    prompt: dict = {"text": build_prompt(order, files, inbox_path)}
    if files:
        prompt["files"] = [{"uri": "file://" + f["path"], "name": f["name"]} for f in files]

    pinned = (order.get("session") or "").strip()
    if pinned:
        # A pinned order is delivered *into* the session it named; no session is created.
        # That session already carries its own directory and model, which is why the order
        # was not asked for a directory (see bridge.order_problems).
        #
        # There is deliberately no fallback: an order pinned to a pipeline's working session
        # must land there or the order fails visibly.  Quietly starting it in a fresh session
        # elsewhere is the one outcome nobody would notice, and the whole point of pinning.
        info = oc_api(binp, "GET", "/api/session/%s" % pinned, timeout=30)
        data = info.get("data") if isinstance(info, dict) else None
        if not isinstance(data, dict) or data.get("_tag") or data.get("id") != pinned:
            raise RuntimeError("the pinned session is gone: %s" % str(info)[:300])
        oc_api(binp, "POST", "/api/session/%s/prompt" % pinned, prompt, timeout=180)
        return pinned

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

# --- one window per repository -----------------------------------------------------------
# A window per work order fills the desktop with near-identical windows as soon as several
# orders run in one repo: five orders in `~/projects` were five qterminal windows, all
# titled "... - projects".  So a repository gets ONE window, and each order becomes a tab in
# it.  The tabs cannot come from the terminal emulator - qterminal 2.4.0 has no flag for
# opening a tab in an existing window and no D-Bus service to ask - so the repository's tabs
# live in a tmux session, and the window is opened only when the repository has none.
# `tabs: false` in config.json gives back one window per order.


def tmux_binary() -> str | None:
    return shutil.which("tmux")


def tmux_session_name(directory: str) -> str:
    """A stable, tmux-safe session name for a directory: cb-<repo>-<hash>.

    The directory is normalised first, so `/a/b` and `/a/b/` are the same repository - and
    therefore the same window.
    """
    directory = directory.rstrip("/") or "/"
    base = re.sub(r"[^A-Za-z0-9_-]+", "-", os.path.basename(directory) or "root")
    base = base.strip("-")[:24] or "root"
    return "cb-%s-%s" % (base, hashlib.sha1(directory.encode("utf-8")).hexdigest()[:8])


def window_name(oid: str) -> str:
    """A tab name tmux can still read as a target: no `:`, no `.`."""
    return re.sub(r"[^A-Za-z0-9_-]+", "-", oid).strip("-") or "order"


def tmux(*args: str, timeout: float = 10) -> tuple[int, str]:
    """Run tmux and never raise: (returncode, output).  Best effort, like the window."""
    exe = tmux_binary()
    if not exe:
        return 1, "tmux is not installed"
    try:
        p = subprocess.run([exe, *args], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=timeout)
    except Exception as e:  # no server, no permission, ...
        return 1, str(e)
    return p.returncode, p.stdout.decode("utf-8", "replace")


def tmux_sessions() -> list[str]:
    """The tmux sessions this watcher made - ours by name, so nothing else is touched."""
    rc, out = tmux("list-sessions", "-F", "#{session_name}")
    if rc != 0:
        return []
    return [s for s in out.split() if s.startswith("cb-")]


def tmux_tabs(slug: str) -> list[str]:
    """The tabs (tmux windows) of one repository's session, in tab order."""
    rc, out = tmux("list-windows", "-t", slug, "-F", "#{window_name}")
    if rc != 0:
        return []
    return out.split()


def close_tab(target: str) -> bool:
    """Close one tab, and the repository's window with the last tab of it.

    Killing the tmux window kills the agent's TUI with it - that is the memory being freed.
    The terminal window exits by itself when the session it is attached to ends.
    """
    slug = target.split(":", 1)[0]
    rc, out = tmux("kill-window", "-t", target)
    if rc != 0:
        log("  could not close the tab %s: %s" % (target, out.strip()))
        return False
    if not tmux_tabs(slug):
        tmux("kill-session", "-t", slug)
    return True


def script_path(sid: str) -> str:
    """The launcher script of an order's view: the tab script when tabs are in use."""
    tab = os.path.join(TERMINALS_DIR, "tab-%s.sh" % sid)
    return tab if os.path.exists(tab) else os.path.join(TERMINALS_DIR, "%s.sh" % sid)


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


def write_tab_script(oid: str, sid: str, directory: str, title: str, slug: str) -> str:
    """Write the per-repository window script: the session, this order as its first tab.

    One script does both jobs, because a window is only ever opened when the repository has
    none: `has-session` decides between attaching to the tabs that are already there and
    creating the session with this order as its first tab.  A session whose window was
    closed by hand is re-attached the same way, with all of its tabs intact.

    The status line is the only hint a person gets about how to move between tabs, so it
    says so; `mouse on` lets them click a tab instead of learning the key.
    """
    os.makedirs(TERMINALS_DIR, exist_ok=True)
    path = os.path.join(TERMINALS_DIR, "tab-%s.sh" % sid)
    ts = shlex.quote(tmux_binary() or "tmux")
    s = shlex.quote(slug)
    first = shlex.quote("bash " + os.path.join(TERMINALS_DIR, "%s.sh" % sid))
    body = (
        "#!/usr/bin/env bash\n"
        "# opened by watcher.py for %s - one window per repository, one tab per work order\n"
        "cd %s || exit 1\n"
        "printf '\\033]2;%%s\\007' %s\n"
        "if %s has-session -t %s 2>/dev/null; then\n"
        "  exec %s attach -t %s\n"
        "fi\n"
        "%s new-session -d -s %s -n %s %s\n"
        "%s set-option -t %s mouse on\n"
        "%s set-option -t %s status-left '[Ctrl-b n/p] '\n"
        "exec %s attach -t %s\n"
        % (oid, shlex.quote(directory), shlex.quote(title),
           ts, s, ts, s,
           ts, s, shlex.quote(window_name(oid)), first,
           ts, s, ts, s, ts, s)
    )
    with open(path, "w") as fh:
        fh.write(body)
    os.chmod(path, 0o755)
    return path


def open_agent_terminal(cfg: dict, oid: str, sid: str, directory: str) -> bool:
    """Open a terminal window on the agent - or a tab in its repository's window.

    Best effort only: the agent is already running on the server by this point, so a
    failure here costs nothing but the view - it is logged, and the order still succeeds.
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
    write_terminal_script(oid, sid, directory, title, opencode_bin)
    fname = os.path.join(TERMINALS_DIR, "%s.sh" % sid)

    if w.get("tabs", True) and tmux_binary():
        slug = tmux_session_name(directory)
        fname = write_tab_script(oid, sid, directory, title, slug)
        if tmux("has-session", "-t", slug)[0] == 0:
            # this repository already has its window: the order becomes a tab in it
            rc, out = tmux("new-window", "-t", slug, "-n", window_name(oid),
                           "bash %s" % shlex.quote(os.path.join(TERMINALS_DIR, "%s.sh" % sid)))
            if rc != 0:
                log("  could not add a tab for %s to %s: %s" % (oid, slug, out.strip()))
            else:
                tmux("select-window", "-t", "%s:%s" % (slug, window_name(oid)))
                if tmux("list-clients", "-t", slug)[1].strip():
                    log("  opened a tab for %s in the %s window" % (oid, slug))
                    return True
                # its window was closed by hand and the tabs live on, detached
                log("  the %s window was gone - re-attaching it to its tabs" % slug)

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


def prune_state(st: dict, days: float = 7.0, now: float | None = None) -> dict:
    """Drop what a finished order no longer needs, so state.json stops growing.

    ``starts[<id>]`` keeps the whole parsed order - goal, acceptance and the raw text - plus
    the inbox path and every downloaded file: everything needed to *prompt* an agent, which a
    finished order never does again.  It is the bulk of the file (188 KB over 45 orders) and
    it grows with every order, so an order finished more than ``state_keep_days`` ago loses
    its whole start record.

    What stays is what is still read: ``finished`` (the reap rule and ``status``), the hash in
    ``orders`` (which stops a message still in the chat's lookback being dispatched twice) and
    the session mapping (which is how a leftover tab is recognised as ours and reaped).

    Nothing about a live order is touched, and neither is a recently finished one: the record
    is what ``watcher.sh status`` and a person reading the state after a surprise would want.
    Deleting it is also the safe move for the start path - ``ensure_started`` sees no record
    and does nothing, where a record with a status it does not know could be started again.
    """
    now = now or time.time()
    cutoff = now - float(days) * 86400
    starts = st.get("starts") or {}
    finished = st.get("finished") or {}
    dropped: list[str] = []
    freed = 0
    for oid, at in list(finished.items()):
        if float(at or 0) > cutoff:
            continue                        # recent: keep everything about it
        rec = starts.get(oid)
        if rec is None:
            continue
        freed += len(json.dumps(rec))
        starts.pop(oid, None)
        dropped.append(oid)
    return {"orders": sorted(dropped), "count": len(dropped), "bytes": freed}


def agent_windows(windows: list[dict] | None = None,
                  st: dict | None = None) -> list[dict]:
    """Every view an agent is running in: [{"pid","sid","script","tmux"}].

    Two places to look, because a repository now has one window for all of its orders:

    * the repository's tmux session - one entry per tab, which is the only way to see an
      order whose tab shares somebody else's window, and the only way to see the tabs of a
      session whose window was closed by hand (the agents keep running, detached);
    * ``/proc``, for terminal windows still running one of our own scripts - a window opened
      with ``tabs: false``, or before tabs existed.  Read from /proc rather than pgrep on
      purpose: a pgrep pattern matches the command line of the shell looking for it, and
      kills it.
    """
    if windows is not None:
        return windows
    out: list[dict] = []
    seen: set[str] = set()
    sessions = (st or {}).get("sessions") or {}
    rev = {window_name(oid): oid for oid in sessions}
    for slug in tmux_sessions():
        for name in tmux_tabs(slug):
            oid = rev.get(name)
            if oid is None:
                continue                    # not one of our orders - leave it alone
            sid = sessions[oid]
            out.append({"pid": 0, "sid": sid, "script": script_path(sid),
                        "tmux": "%s:%s" % (slug, name)})
            seen.add(sid)
    pat = re.compile(re.escape(TERMINALS_DIR) + r"/[^\s\"']+\.sh")
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open("/proc/%s/cmdline" % name, "rb") as fh:
                args = fh.read().decode("utf-8", "replace").split("\0")
        except Exception:
            continue
        m = pat.search(" ".join(a for a in args if a))
        if not m:
            continue
        script = m.group(0)
        sid = os.path.basename(script)[:-3]
        if sid.startswith("tab-"):
            sid = sid[4:]                   # the window script of a repository's tabs
        if sid in seen:
            continue
        out.append({"pid": int(name), "sid": sid, "script": script, "tmux": None})
    return out


def _close_window(pid: int, script: str) -> None:
    """SIGTERM the window's whole process group: the terminal and the agent's TUI."""
    try:
        pgid = os.getpgid(pid)
    except Exception:
        pgid = None
    if pgid and pgid == pid:
        os.killpg(pgid, signal.SIGTERM)
    else:
        os.kill(pid, signal.SIGTERM)


def reap_windows(cfg: dict, st: dict, activity: list[dict] | None = None,
                 force: bool = False, dry_run: bool = False,
                 windows: list[dict] | None = None) -> list[str]:
    """Close the views of agents that are not working, so idle TUIs stop holding memory.

    ``force`` (what ``watcher.sh reap`` asks for) closes every agent view that is not
    working right now.  Otherwise a view goes only once its agent has been quiet for
    ``reap_idle_seconds`` and the view itself is at least that old - closing eagerly
    would close the very tab a 48 minute test run is sitting in.  An agent parked on
    a question is never closed either: that view is where the answer is typed.

    A tab is closed through its tmux session, which kills the agent's TUI with it - and
    the last tab takes the repository's window with it.  That also reaches the tabs whose
    window was closed by hand: nobody is looking at them, but they are still running.
    """
    w = cfg.get("watch") or {}
    if dry_run or not w.get("launch_terminal", True):
        return []
    idle_after = float(w.get("reap_idle_seconds", 7200))
    now = time.time()
    act = {a["order"]: a for a in
           (activity if activity is not None else session_activity(cfg, st))}
    by_sid = {sid: oid for oid, sid in (st.get("sessions") or {}).items()}
    done = set(st.get("finished") or {})
    waiting = set(st.get("waiting") or {})
    closed: list[str] = []
    for win in agent_windows(windows, st):
        oid = by_sid.get(win["sid"])
        if oid is None or oid in closed:
            continue                        # not one of ours - leave it alone
        if oid in waiting:
            continue                        # it asked a question: that is where it is answered
        a = act.get(oid) or {}
        if a.get("live") and oid not in done:
            continue                        # it is working: never close its view
        age = now - os.path.getmtime(win["script"]) if os.path.exists(win["script"]) else 0
        idle = (now - a["last"]) if a.get("last") else 0
        # a view goes once it is old *and* its agent is quiet.  Either one alone is not
        # enough: "launch me the tab that did WO-130" brings back an agent that may have
        # been quiet for hours, and closing that tab 30 seconds later helps nobody.
        if not force and (age < idle_after or idle < idle_after):
            continue
        how = ("you asked" if force
               else "quiet for over %d min" % int(idle_after // 60))
        if win.get("tmux"):
            if not close_tab(win["tmux"]):
                continue
            log("  closed the tab for %s (%s) - its agent is not working" % (oid, how))
        else:
            try:
                _close_window(win["pid"], win["script"])
            except Exception as e:
                log("  could not close the window for %s: %s" % (oid, e))
                continue
            log("  closed the window for %s (%s) - its agent is not working" % (oid, how))
        for p in (win["script"], os.path.join(TERMINALS_DIR, "%s.sh" % win["sid"])):
            try:
                os.remove(p)
            except Exception:
                pass
        closed.append(oid)
    return closed


def finish_order(st: dict, oid: str) -> None:
    """Mark an order finished: frees its agent slot and stops any resume.

    Nothing in the loop can *see* the agent report DONE - it goes to the planner - so
    :func:`finish_reported_orders` reads the two signals that say it anyway (the report
    file, or the report in the agent's own transcript), and a human says so for the
    rest (``watcher.sh done WO-111``).  Memory-wise that is the difference between a
    slot freed now and one freed two hours from now.
    """
    st.setdefault("finished", {})[oid] = time.time()
    (st.get("resume") or {}).pop(oid, None)
    (st.get("held") or {}).pop(oid, None)
    (st.get("waiting") or {}).pop(oid, None)
    save_state(st)


def tidy_marks(st: dict) -> list[str]:
    """Drop the marks a *finished* order must not carry: resume, waiting, held.

    ``save_state`` merges everything except ``held``, so a pop by ``watcher.sh done`` is
    reverted by the very next save if the watcher is holding a copy from before it - which
    is the normal case, since it holds one for the whole pass.  The watcher owns these maps,
    so it does the dropping itself, every pass, where no race can win: WO-109, WO-111-WO-114,
    WO-139 and WO-157 all sat in ``resume`` for days after they were finished, and `status`
    advertised *"resumed 3 time(s) - have a look at it"* about closed orders.
    """
    done = set(st.get("finished") or {})
    gone: list[str] = []
    for key in ("resume", "waiting", "held"):
        marks = st.get(key)
        if not isinstance(marks, dict):
            continue
        for oid in [o for o in marks if o in done]:
            marks.pop(oid, None)
            if oid not in gone:
                gone.append(oid)
    return gone


def note_waiting(st: dict, verdicts: dict) -> list[str]:
    """Record the agents whose turn is parked on a person, for status and reap.

    ``verdicts`` is this pass's ``order -> _turn_verdict`` map, so the slot count, the
    status rows and this mark are all the same reading of the same turn.  An order that
    was parked and is not any more drops the mark: it has been answered, and it is
    working again.  A *finished* order never holds a mark either - its window is not
    waiting for anything, and a mark left behind made reaping treat a done order's view as
    parked (WO-130 and WO-148 both outlived their orders by days that way).  Returns the
    orders that have just parked, for the log.
    """
    wait = st.setdefault("waiting", {})
    was = set(wait)
    done = set(st.get("finished") or {})
    for oid, v in verdicts.items():
        if v == "waiting" and oid not in done:
            wait.setdefault(oid, time.time())    # keep the moment it asked
        else:
            wait.pop(oid, None)
    return sorted(set(wait) - was)


def queue_report(st: dict, oid: str) -> str | None:
    """Queue an agent's report file for the planner, when the agent never sent it.

    A report file is proof the *work* is done - that is what frees the slot - but it is not
    proof the planner ever read it.  On 2026-11-13 WO-157 wrote ``reports/WO-157.md``, its
    order was finished from that file, and the planner answered *"WO-157 was posted earlier
    and I haven't seen its report"*: the queue stopped on a report that was written and
    never delivered.  The outbox is the delivery the watcher already flushes
    (``Bridge.flush_outbox``), so the file goes there and out on the next pass, saying where
    it came from.  Called once per order: a finished order never runs this again.
    """
    path = report_file(oid)
    try:
        with open(path) as fh:
            body = fh.read().strip()
    except Exception as e:
        log("  could not read %s to queue it: %s" % (path, e))
        return None
    if not body:
        return None
    note = ("[%s wrote this to %s and its own report never reached you, so the watcher is "
            "sending it.  Treat it as that order's report.]" % (oid, os.path.basename(path)))
    try:
        return queue_message("%s\n\n%s" % (body, note),
                             reason="the report file of %s, never sent by its agent" % oid)
    except Exception as e:
        log("  could not queue %s's report: %s" % (oid, e))
        return None


def finish_reported_orders(cfg: dict, st: dict, activity: list[dict], verdicts: dict,
                           check=None) -> list[str]:
    """Finish the orders whose agent has reported DONE, and return them.

    Only ever for an order that is started, not finished, and whose *turn is over*: a
    report arrives at the end of one.  Two signals say it, either is enough:

    * the report file the order asks the agent to leave (:func:`report_file`), which is
      the reliable one and stays on disk for the person to read;
    * the report in the agent's own transcript (:func:`reported_done`), which catches a
      report sent the usual way without an agent having to do anything new - checked
      once per turn, and it costs one small read per candidate.

    Finishing from the file *alone* also queues that file for the planner
    (:func:`queue_report`): the slot is freed either way, but a report the planner never
    saw leaves the loop waiting on work that is already done.
    """
    done = st.get("finished") or {}
    out: list[str] = []
    for a in activity:
        oid = a["order"]
        if oid in done or verdicts.get(oid) != "idle":
            continue                        # finished already, or still in a turn
        if (st.get("starts") or {}).get(oid, {}).get("status") != "started":
            continue
        on_file = os.path.exists(report_file(oid))
        reported = bool(check and check(oid, a.get("session") or "", a.get("last")))
        if on_file:
            why = "left %s" % os.path.basename(report_file(oid))
        elif reported:
            why = "reported DONE in its transcript"
        else:
            continue
        finish_order(st, oid)
        log("%s is finished - its agent %s, so its slot is free" % (oid, why))
        if on_file and not reported:
            # the file is proof the work is done, not that the planner ever saw it
            queued = queue_report(st, oid)
            if queued:
                log("  %s finished on its report file alone - queued %s for the planner"
                    % (oid, os.path.basename(queued)))
        out.append(oid)
    return out


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
        where = order.get("dir") or watch.get("default_dir") or "~"
        if (order.get("session") or "").strip():
            log("  delivered %s into the pinned session %s - no new session was created"
                % (oid, sid))
        else:
            log("  started opencode session %s in %s" % (sid, where))
        open_agent_terminal(cfg, oid, sid, where)
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
                  dry_run: bool = False, retry_delay: float | None = None,
                  activity: list[dict] | None = None) -> dict:
    """Mark and start every order that fits, then reconcile stuck ones.

    The seen mark (and the start record) are saved *before* the slow agent start,
    so a crash or a second watcher can never start an order twice.  Orders over the
    ``max_agents`` cap are handed back untouched by ``dispatch_batch`` and picked up
    by a later pass.
    """
    files = files or []
    seen = st.get("orders") or {}
    if isinstance(seen, list):  # tolerate an older state file
        seen = {k: "" for k in seen}
    orders, _held = dispatch_batch(cfg, st, orders, dry_run=dry_run, activity=activity)

    for o in orders:
        problems = order_problems(o)
        if problems:
            # belt and braces: route_orders already filters these out, but no path
            # may ever start an agent on half a brief
            log("  refusing %s - not dispatchable (missing %s)" % (o["id"], ", ".join(problems)))
            continue
        inbox_path = order_to_inbox(o, files)
        log("new work order %s -> %s" % (o["id"], inbox_path))
        seen[o["id"]] = _hash(o["raw"])
        st["orders"] = seen
        (st.get("held") or {}).pop(o["id"], None)   # it is starting: the wait is over
        rec = st.setdefault("starts", {}).setdefault(o["id"], {"status": "pending", "attempts": 0})
        rec.update({"order": o, "inbox": inbox_path, "files": files, "updated": time.time()})
        # a dispatched order is progress: the turn is alive again, so any ask
        # streak or stall flag left over from the previous turn is finished with
        st["nudge"] = {"streak": 0, "at": 0}
        clear_needs_order(st)
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
    # Before anything about the chat: what happened to the agents.  Every turn runs
    # inside the opencode background server, so if it restarted, turns died with it -
    # and that must be noticed even when the bridge window is down.
    restarted = note_service(st, dry_run)
    if ensure_service(cfg, st, dry_run):
        rec = st.setdefault("service", {})
        pid = service_pid()
        if pid:
            if not restarted:
                restarted = {"from": rec.get("pid"), "to": pid}
            rec["pid"] = pid
    if restarted:
        why = explain_service_restart(st, restarted)
        record_service_change(st, restarted, why)
        log("the opencode background server was restarted (%s -> %s) %s - checking whether "
            "any running order lost its turn with it"
            % (restarted.get("from"), restarted.get("to"), why))
    activity = [] if dry_run else session_activity(cfg, st)
    tidy_marks(st)
    verdicts: dict[str, str] = {}
    if activity:
        # One probe per session for the whole pass: the same reading decides the slot
        # count, the "waiting for you" rows, and whether an agent has reported DONE.
        # (The probe memo makes the later calls free.)
        probe = turn_probe(cfg)
        for a in activity:
            v = probe(a)
            if v is not None:
                verdicts[a["order"]] = v
        for oid in note_waiting(st, verdicts):
            log("%s is waiting for an answer - its slot is free while it waits, and its "
                "window stays open for the reply" % oid)
        # before the resume and the reap: a reported order is finished, so neither
        # should touch it
        finish_reported_orders(cfg, st, activity, verdicts,
                               check=lambda oid, sid, moved: reported_done(cfg, sid, moved))
        resume_stalled(cfg, st, activity, restarted, dry_run=dry_run)
        reap_windows(cfg, st, activity, dry_run=dry_run)
    if not cdp_alive():
        if cfg.get("watch", {}).get("auto_launch_bridge", False) and ensure_chrome():
            log("bridge window was down - launched it")
        else:
            if st.get("bridge_up", True):  # only say it once, not every pass
                log(bridge_down_advice())
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
    routed = route_orders(st, orders)
    new = routed["dispatch"]
    # An order finished a week ago needs nothing but its `finished` mark, and its start
    # record is the bulk of state.json.  Safe to do on every pass: it only ever touches
    # records that are finished and old.
    pruned = prune_state(st, days=float(cfg.get("watch", {}).get("state_keep_days", 7)))
    if pruned["count"]:
        log_once("state-pruned",
                 "dropped the start record of %d order(s) finished more than %s day(s) ago "
                 "(%d KB of state.json): %s"
                 % (pruned["count"], cfg.get("watch", {}).get("state_keep_days", 7),
                    pruned["bytes"] // 1024, ", ".join(pruned["orders"][:8])),
                 every=24 * 3600)
    for gone, why in prune_marks(st, orders, activity):
        # once per order per six hours, not once per pass: this line alone said the same
        # thing about the same order 125 times
        log_once("gone-%s" % gone,
                 "%s is %s - it is not waiting for anything any more" % (gone, why),
                 every=6 * 3600)

    # Only the orders that fit under max_agents have their files pulled down now; a
    # held one is fetched when it starts, so a loaded machine does not do that work
    # for work that cannot run yet.
    batch, _held = dispatch_batch(cfg, st, new, dry_run=dry_run, activity=activity)
    files: list[dict] = []
    if batch:
        try:
            files = download_new_files(b, st)
        except Exception as e:
            log("file download skipped: %s" % e)
        # Hand every file the order names over by absolute path, even when the
        # card download failed: a pointer the agent cannot follow is no pointer.
        for o in batch:
            files, missing = resolve_order_files(o, files)
            if missing:
                try:
                    files, missing = resolve_order_files(
                        o, files + b.download([os.path.basename(p) for p in missing],
                                              timeout=60.0))
                except Exception as e:
                    log("  could not fetch %s: %s"
                        % (", ".join(os.path.basename(p) for p in missing), e))
            o["missing_files"] = missing
            for p in missing:
                log("  %s names %s but it is not on disk - handing the path to the agent"
                    % (o["id"], os.path.basename(p)))

    handle_orders(cfg, st, batch, files, dry_run=dry_run, activity=activity)

    # What Claude's newest message needs, if anything: an incomplete order, a
    # re-post of one that already ran, or a promise with no order behind it.
    # Dry runs never message Claude.
    if not dry_run:
        newest = route_orders(st, extract_work_orders(text))
        busy = agent_busy(cfg, st) if attention_needed(newest, text) else {}
        plan = plan_attention(st, text, newest, busy, cfg)
        if plan["action"] == "ask" and st.get("claude_up", True):
            ask = build_ask(plan, newest, text)
            log("asking Claude for the %s order: %s" % (plan["kind"], plan["why"]))
            log("  the ask: %s" % " ".join(ask.split())[:200])
            log("  result : %s"
                % send_ask(st, b, ask, timeout=float(cfg.get("watch", {}).get("ask_timeout", 180))))
        elif plan["action"] == "needs_order":
            write_needs_order(st, plan["why"], build_ask(plan, newest, text), text)
        elif plan["action"] == "ask":
            quiet(st, "Claude cannot answer right now - holding the ask")
        elif plan["action"] in ("wait", "off"):
            quiet(st, plan["why"])
    st["last_seen"] = text[:400]
    st["updated"] = time.time()
    return st


def report_status(cfg: dict, st: dict) -> int:
    """Print what the loop is doing with agents right now (``watcher.sh status``)."""
    w = cfg.get("watch") or {}
    cap = int(w.get("max_agents") or 0)
    grace = float(w.get("slot_probe_seconds", 120))
    act = session_activity(cfg, st)
    # this is the same decision the next pass will make, probe and all, so what is
    # printed here is what will happen, not a guess beside it
    slots = {a["order"] for a in slot_agents(act, cfg, probe=turn_probe(cfg))}
    done = set(st.get("finished") or {})
    waiting = set(st.get("waiting") or {})
    now = time.time()
    print("agents: %d of %s slots used (max_agents=%s)"
          % (len(slots), cap or "unlimited", cap or "none"))
    # Which planner the loop reports to.  The bridge resolves it from config.json once, so
    # this is also the honest answer to "did the switch take?" - and it only prints when a
    # config really names accounts, which keeps a proof's own config out of it.
    accts = cfg.get("accounts") or {}
    if accts:
        who = str(cfg.get("active_account") or "main")
        chat = str((accts.get(who) or {}).get("chat_url") or "")
        print("planner: %s account%s" % (who, (" -> %s" % chat) if chat else " (no chat yet)"))
    floor = int(w.get("min_free_mb", DEFAULT_MIN_FREE_MB) or 0)
    avail = free_memory_mb()
    if floor > 0:
        if avail is None:
            print("memory: unreadable (/proc/meminfo) - the floor cannot hold anything")
        else:
            print("memory: %d MB available (min_free_mb=%d)%s"
                  % (avail, floor, " - below the floor: no new agent will start"
                     if avail < floor else ""))
    svc = st.get("service") or {}
    changes = list(svc.get("changes") or [])
    if changes:
        c = changes[-1]
        print("service: pid %s, last replaced %s (%s -> %s)"
              % (svc.get("pid"), time.strftime("%m-%d %H:%M", time.localtime(c.get("at") or 0)),
                 c.get("from"), c.get("to")))
        print("         %s" % c.get("why"))
    elif svc.get("pid"):
        print("service: pid %s (no replacement seen since this watcher started)" % svc.get("pid"))
    # The views we own: which window or tab each agent is running in, and how long its agent
    # has been quiet. "Who is on screen" should not need pgrep and tmux ls.
    by_sid = {sid: oid for oid, sid in (st.get("sessions") or {}).items()}
    last = {a["order"]: a.get("last") for a in act}
    views = agent_windows(None, st)
    if not views:
        print("views: none open")
    else:
        print("views: %d open" % len(views))
        for v in views:
            oid = by_sid.get(v.get("sid"), v.get("sid") or "?")
            where = ("tab %s" % v["tmux"]) if v.get("tmux") else ("window pid %s" % v.get("pid"))
            age = last.get(oid)
            quiet = ("agent last moved %.0f min ago" % ((now - age) / 60)) if age \
                else "agent silent"
            print("  %-7s %-40s %s" % (oid, where, quiet))
    for a in act:
        oid = a["order"]
        age = (now - a["last"]) if a.get("last") else None
        if oid in done:
            state = "finished"
        elif oid in waiting:
            state = "waiting for you"     # parked on a question: no slot, window kept
        elif age is None:
            state = "unreadable"
        elif age < grace:
            state = "just moved"          # held without asking: the race guard
        elif oid in slots:
            state = "working" if a.get("in_turn") else "quiet, slot held"
        else:
            state = "idle"
        ago = ("%.0f min ago" % (age / 60)) if age is not None else "unknown"
        print("  %-7s %-16s last moved %s" % (oid, state, ago))
    for oid, r in sorted((st.get("resume") or {}).items()):
        if oid in done:
            continue              # a finished order is not going to be resumed by anybody
        n = int(r.get("count") or 0)
        print("  %-7s resumed %d time(s)%s"
              % (oid, n, " - have a look at it" if n >= int(w.get("resume_max", 3)) else ""))
    for oid in sorted((st.get("held") or {}).keys()):
        if oid in (st.get("starts") or {}):
            continue          # it started after all: a stale marker, not a wait
        why = "free memory" if oid in set((st.get("memory") or {}).get("holding") or []) \
            else "a free agent slot"
        print("  %-7s waiting for %s" % (oid, why))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Watch the Claude chat for work orders and hand them to an agent.")
    ap.add_argument("--once", action="store_true", help="do a single pass and exit (for a timer)")
    ap.add_argument("--interval", type=int, default=None, help="seconds between passes (default from config, 30)")
    ap.add_argument("--dry-run", action="store_true", help="report work orders but start no agent")
    ap.add_argument("--status", action="store_true",
                    help="what is running right now: live agents, slots, resumed and waiting orders")
    ap.add_argument("--reap", action="store_true",
                    help="close the windows of agents that are not working, then exit")
    ap.add_argument("--finish", metavar="ORDER", default=None,
                    help="mark an order finished (frees its slot, stops any resume), then exit")
    ap.add_argument("--prune-state", action="store_true",
                    help="drop the start records of orders finished more than "
                         "watch.state_keep_days (7) days ago, then exit")
    args = ap.parse_args()
    cfg = load_config()

    # These three only read state and merge their change back, so they are safe to run
    # beside a live watcher - which is exactly when you want them.
    if args.finish:
        finish_order(load_state(), args.finish)
        print("marked %s finished - its slot is free, and 'watcher.sh reap' will close "
              "its window" % args.finish)
        return 0
    if args.reap:
        closed = reap_windows(cfg, load_state(), force=True)
        print("closed %d agent window(s)%s"
              % (len(closed), (": " + ", ".join(closed)) if closed else " - none was idle"))
        return 0
    if args.prune_state:
        st = load_state()
        days = float(cfg.get("watch", {}).get("state_keep_days", 7))
        res = prune_state(st, days=days)
        if res["count"]:
            save_state(st)
        print("dropped the start record of %d order(s) finished more than %s day(s) ago "
              "(%d KB%s)%s"
              % (res["count"], days, res["bytes"] // 1024,
                 ", and state.json rewritten" if res["count"] else "",
                 (": " + ", ".join(res["orders"][:10])) if res["orders"] else ""))
        return 0
    if args.status:
        return report_status(cfg, load_state())

    if acquire_watcher_lock() is None:
        log("watcher already running (watcher.lock is held) - exiting")
        return 0

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
