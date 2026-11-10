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
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import wlock  # noqa: E402
from bridge import (Bridge, ClaudeUnavailable, cdp_alive, ensure_chrome,  # noqa: E402
                    extract_work_orders, order_problems, outbox_files)

CONFIG = os.path.join(HERE, "config.json")
STATE = os.path.join(HERE, "state.json")
INBOX = os.path.join(HERE, "inbox")
LOG = os.path.join(HERE, "watcher.log")
TERMINAL_LOG = os.path.join(HERE, "terminal.log")
LOCK = os.path.join(HERE, "watcher.lock")
DOWNLOADS = os.path.join(HERE, "downloads")
# written when a turn is stalled and Claude did not answer the one automatic ask
NEEDS_ORDER = os.path.join(HERE, "NEEDS-ORDER.md")

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


def quiet(st: dict, why: str) -> None:
    """Say a non-action once, not once every 30 seconds."""
    nudge = st.setdefault("nudge", {"streak": 0})
    if nudge.get("last_wait") != why:
        nudge["last_wait"] = why
        log("  %s" % why)


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


def agent_busy(cfg: dict, st: dict, now: float | None = None) -> dict:
    """Best effort: is the most recently started agent still working?

    ``outcome`` cannot be trusted for this - a busy agent can read as
    "interrupted" - so we look at when its transcript last moved.  Fail-open: if
    the API cannot be read we say "not busy" rather than stall the loop.
    """
    sessions = st.get("sessions") or {}
    if not sessions:
        return {}
    starts = st.get("starts") or {}
    oid = max(sessions, key=lambda k: (starts.get(k) or {}).get("updated") or 0)
    sid = sessions[oid]
    w = cfg.get("watch", {})
    binp = os.path.expanduser(w.get("opencode_bin") or "~/.opencode/bin/opencode")
    try:
        rows = oc_api(binp, "GET", "/api/session/%s/message" % sid, timeout=30)
    except Exception as e:
        log("  could not check whether %s is still working: %s" % (oid, e))
        return {}
    rows = rows.get("data") if isinstance(rows, dict) else rows
    if isinstance(rows, dict):
        rows = rows.get("messages") or []
    last = 0.0
    for r in rows or []:
        t = (r.get("time") or {}) if isinstance(r, dict) else {}
        for key in ("completed", "streamed", "created"):
            v = t.get(key)
            if isinstance(v, (int, float)):
                last = max(last, v / 1000.0)
                break
    if not last:
        return {}
    if (now or time.time()) - last < float(w.get("agent_active_seconds", 1800)):
        return {"order": oid, "session": sid, "last_activity": last}
    return {}


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
    if order.get("missing_files"):
        body += ["## Files Claude named but the bridge could not download",
                 "Read them from these paths if they exist (and check the repository):"]
        for p in order["missing_files"]:
            body.append("- %s" % p)
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
    if order.get("missing_files"):
        lines.append("Claude's order names files the bridge could NOT download. Read them from")
        lines.append("these exact paths if they exist, and look in the repository too - do not")
        lines.append("start work without them:")
        for p in order["missing_files"]:
            lines.append("  - %s" % p)
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
    routed = route_orders(st, orders)
    new = routed["dispatch"]

    files: list[dict] = []
    if new:
        try:
            files = download_new_files(b, st)
        except Exception as e:
            log("file download skipped: %s" % e)
        # Hand every file the order names over by absolute path, even when the
        # card download failed: a pointer the agent cannot follow is no pointer.
        for o in new:
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

    handle_orders(cfg, st, new, files, dry_run=dry_run)

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
