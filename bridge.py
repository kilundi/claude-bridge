#!/usr/bin/env python3
"""Claude Web Bridge - let a local agent talk to Claude running in Chrome.

Drives a dedicated Chrome window over the DevTools Protocol (CDP):
  * types a message into the claude.ai composer and sends it
  * waits for Claude's reply to finish streaming
  * prints the reply text (or JSON, with --json)

Requires:
  * the bridge Chrome running with --remote-debugging-port
    (use start-bridge-chrome.sh, or let this script launch it)
  * signed in to claude.ai in that window
  * python 'websockets' (sync client)

Environment overrides:
  CLAUDE_BRIDGE_HOST     default 127.0.0.1
  CLAUDE_BRIDGE_PORT     default 9222
  CLAUDE_BRIDGE_URL      page to open/create (default https://claude.ai/new)
  CLAUDE_BRIDGE_TIMEOUT  default seconds to wait for a reply (600)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings

warnings.filterwarnings("ignore", message=r"connect\(\) must be used as a context manager.*")

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from wlock import locked_by_other  # noqa: E402  (same-directory helper)

HOST = os.environ.get("CLAUDE_BRIDGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("CLAUDE_BRIDGE_PORT", "9222"))
BASE = f"http://{HOST}:{PORT}"
DEFAULT_TIMEOUT = float(os.environ.get("CLAUDE_BRIDGE_TIMEOUT", "600"))
LAUNCHER = os.path.join(HERE, "start-bridge-chrome.sh")
UP_SCRIPT = os.path.join(HERE, "bridge-up.sh")
CONFIG_PATH = os.path.join(HERE, "config.json")
TARGET_PATH = os.path.join(HERE, "bridge-target.json")

# The web UI has no /compact (its slash menu only lists skills), so "compact" is
# the documented equivalent: ask Claude for a short handoff a fresh chat can pick up.
COMPACT_PROMPT = (
    "Please write a compact handoff summary of this conversation that I can paste into a "
    "fresh chat to continue: the active threads, the decisions already made, the current "
    "state of the work (repos, branches, commits), and the exact next action. Keep it short."
)


class ClaudeUnavailable(RuntimeError):
    """Claude cannot answer right now - out of free messages, or similar.

    Raised instead of waiting out the full reply timeout. ``sent`` says whether the
    message still reached the chat (Claude will read it when the limit resets);
    ``resets_at`` is the reset text the page shows ("7:20 PM") or "".
    """

    def __init__(self, text: str = "", resets_at: str = "", sent: bool = False,
                 queued: str = ""):
        self.text = (text or "").strip()
        self.resets_at = (resets_at or "").strip()
        self.sent = bool(sent)
        self.queued = queued or ""
        msg = self.text or "Claude is not accepting messages right now"
        if self.resets_at:
            msg += f" (resets at {self.resets_at})"
        if self.sent:
            msg += ("; the message was delivered to the chat and will be read when "
                    "Claude can answer again")
        elif self.queued:
            msg += f"; nothing was sent, but the message is saved in {self.queued}"
        else:
            msg += "; nothing was sent"
        super().__init__(msg)


OUTBOX = os.path.join(HERE, "outbox")


def queue_message(text: str, reason: str = "") -> str:
    """Keep a report that could not be delivered, so it is never lost.

    Written to ~/claude-bridge/outbox/ newest-last; ``flush_outbox`` sends them
    once the bridge (or the plan) is back, oldest first.
    """
    os.makedirs(OUTBOX, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()[:40]).strip("-") or "report"
    path = os.path.join(OUTBOX, f"{stamp}-{slug}.md")
    n = 1
    while os.path.exists(path):
        path = os.path.join(OUTBOX, f"{stamp}-{slug}-{n}.md")
        n += 1
    header = f"<!-- queued {time.strftime('%Y-%m-%d %H:%M:%S')}"
    if reason:
        header += f" - {reason}"
    header += " -->\n"
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(header + (text or "") + "\n")
    os.replace(tmp, path)
    return path


def outbox_files() -> list[str]:
    """Queued, undelivered reports, oldest first."""
    try:
        return sorted(os.path.join(OUTBOX, f) for f in os.listdir(OUTBOX) if f.endswith(".md"))
    except FileNotFoundError:
        return []


def read_queued(path: str) -> str:
    """The message text from a queued outbox file (drops the header comment)."""
    try:
        with open(path) as fh:
            text = fh.read()
    except OSError:
        return ""
    return re.sub(r"^<!--.*?-->\s*", "", text, count=1, flags=re.S).strip()


def parse_reset_time(text: str, now: float | None = None) -> float | None:
    """Turn the page's reset text ("7:20 PM") into a local epoch time, or None.

    The time is today's if it is still ahead of us, otherwise tomorrow's.
    """
    m = re.search(r"(\d{1,2}):(\d{2})\s*([AaPp][Mm])?", text or "")
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    ampm = (m.group(3) or "").lower()
    if ampm:
        if ampm == "pm" and hour != 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    base = time.time() if now is None else now
    t = time.localtime(base)
    cand = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, hour, minute, 0, 0, 0, -1))
    if cand <= base:
        cand += 24 * 3600
    return cand


def load_config() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg: dict) -> None:
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)


def load_target() -> str:
    """The CDP id of the tab the bridge drives (so extra tabs cannot confuse it)."""
    try:
        with open(TARGET_PATH) as f:
            return json.load(f).get("target") or ""
    except Exception:
        return ""


def save_target(target_id: str) -> None:
    try:
        tmp = TARGET_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"target": target_id, "saved": time.time()}, f, indent=2)
        os.replace(tmp, TARGET_PATH)
    except Exception:
        pass  # remembering the tab is an optimisation, never a failure


def target_url() -> str:
    return (
        os.environ.get("CLAUDE_BRIDGE_URL")
        or load_config().get("chat_url")
        or NEW_CHAT_URL
    )


NEW_CHAT_URL = "https://claude.ai/new"


def is_new_chat_url(url: str) -> bool:
    """True for claude.ai/new - a chat that has no id yet.

    The url of a fresh chat only becomes stable (``/chat/<id>``) once a message has been
    sent, so a saved ``/new`` means "the chat that is open", not "make a new one".
    """
    return url.split("#")[0].rstrip("/").endswith("/new")


DEFAULT_URL = target_url()

try:
    from websockets.sync.client import connect as ws_connect
except Exception:  # pragma: no cover
    ws_connect = None


# ---------------------------------------------------------------- HTTP (CDP)
def http_json(path: str, method: str = "GET", timeout: float = 6.0):
    req = urllib.request.Request(BASE + path, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode("utf-8", "replace")
    return json.loads(body) if body.strip() else {}


def cdp_alive() -> bool:
    try:
        http_json("/json/version", timeout=2.0)
        return True
    except Exception:
        return False


def browser_ws() -> str | None:
    try:
        return http_json("/json/version").get("webSocketDebuggerUrl")
    except Exception:
        return None


def ensure_chrome(url: str | None = None) -> bool:
    """Make sure the bridge Chrome is up. Returns True if reachable."""
    if cdp_alive():
        return True
    if not os.path.exists(LAUNCHER):
        return False
    try:
        subprocess.Popen(
            ["bash", LAUNCHER, url or DEFAULT_URL],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        return False
    for _ in range(30):
        if cdp_alive():
            return True
        time.sleep(0.5)
    return False


def start_bridge() -> dict:
    """Bring the whole bridge up (window + watcher) and report what happened.

    Safe to call when it is already up - everything is reused.
    """
    out: dict = {"window": False, "watcher": False, "signed_in": None, "chat": None, "detail": ""}
    if os.path.exists(UP_SCRIPT):
        try:
            p = subprocess.run(["bash", UP_SCRIPT], capture_output=True, text=True, timeout=180)
            out["detail"] = (p.stdout or "").strip()[-800:]
        except Exception as e:
            out["detail"] = f"could not run {UP_SCRIPT}: {e}"
    out["window"] = ensure_chrome()
    if out["window"]:
        try:
            b = Bridge()
            c = b.connect(want_composer=False)
            try:
                st = b.state(c)
            finally:
                c.close()
            out["signed_in"] = bool(st.get("composer"))
            out["chat"] = st.get("url")
        except Exception as e:
            out["detail"] += f" (state: {e})"
    pid_file = os.path.join(HERE, "watcher.pid")
    # The watcher is "up" when some process holds watcher.lock - the same lock
    # watcher.sh and watcher.py take.  This is what stops two simultaneous starts
    # from both bringing a watcher up; the pid file is kept as a fallback for an
    # older watcher that predates the lock.
    lock_path = os.path.join(HERE, "watcher.lock")
    out["watcher"] = locked_by_other(lock_path)
    if not out["watcher"] and os.path.exists(pid_file):
        try:
            pid = int(open(pid_file).read().strip())
            os.kill(pid, 0)
            out["watcher"] = True
        except Exception:
            out["watcher"] = False
    return out


def find_page(url_sub: str = "claude.ai"):
    try:
        targets = http_json("/json/list")
    except Exception:
        return None
    for t in targets:
        if t.get("type") == "page" and url_sub in (t.get("url") or ""):
            return t
    return None


def create_page(url: str):
    try:
        return http_json("/json/new?" + urllib.parse.quote(url, safe=":/?#[]@!$&'()*+,;="), method="PUT")
    except Exception:
        try:  # some builds accept GET
            return http_json("/json/new?" + urllib.parse.quote(url, safe=":/?#[]@!$&'()*+,;="), method="GET")
        except Exception:
            return None


# ------------------------------------------------------------------ JS bits
JS_CONTENT_FN = r"""
  const contentOf = (el) => {
    if (!el) return '';
    const c = el.querySelector('.standard-markdown, .font-claude-message, div.prose');
    if (c) return (c.innerText || '').trim();
    const clone = el.cloneNode(true);
    clone.querySelectorAll('.sr-only, [data-testid="message-actions"], [data-testid$="-actions"]').forEach(n => n.remove());
    return (clone.innerText || '').trim();
  };
"""

JS_LAST_MESSAGES = (
    r"""
(() => {
"""
    + JS_CONTENT_FN
    + r"""
  const n = __N__;
  const nodes = [...document.querySelectorAll('[data-testid="assistant-message"]')];
  return JSON.stringify(nodes.slice(-n).map(contentOf));
})()
"""
)

JS_LIMIT_FN = r"""
  const bridgeInTranscript = el => !!(el.closest && el.closest('[data-testid="transcript-row"], [data-testid="assistant-message"]'));
  const bridgeLimit = (() => {
    const rx = /(out of free messages|free messages until|message limit|limit reached|reached your limit|usage limit|you are out of)/i;
    for (const e of document.querySelectorAll('body *')) {
      const own = [...e.childNodes].filter(x => x.nodeType === 3).map(x => x.textContent).join(' ').trim();
      if (!own || own.length > 160 || !rx.test(own) || bridgeInTranscript(e)) continue;
      const full = ((e.parentElement && e.parentElement.innerText) || own).trim().slice(0, 200);
      const m = full.match(/(?:until|resets? at|back at)\s+([0-9]{1,2}:[0-9]{2}\s*(?:AM|PM)?)/i);
      return { text: full, resets: m ? m[1].replace(/\s+/g, ' ').trim() : '' };
    }
    return { text: '', resets: '' };
  })();
"""

JS_STATE = (
    r"""
(() => {
"""
    + JS_CONTENT_FN
    + JS_LIMIT_FN
    + r"""
  const comp = document.querySelector('div.ProseMirror[contenteditable="true"], div[contenteditable="true"]');
  const stop = [...document.querySelectorAll('button')].some(b => /stop/i.test((b.getAttribute('aria-label')||'').trim()))
            || !!document.querySelector('[data-testid*="stop"]');
  const nodes = [...document.querySelectorAll('[data-testid="assistant-message"]')];
  const fresh = nodes.filter(e => !e.hasAttribute('data-bridge-old')).map(contentOf);
  const last = nodes.length ? contentOf(nodes[nodes.length-1]) : '';
  return JSON.stringify({
    url: location.href,
    ready: document.readyState,
    composer: !!comp,
    streaming: stop,
    total: nodes.length,
    fresh: fresh,
    last: last,
    maybeLoggedOut: !comp,
    limited: !!bridgeLimit.text,
    limitText: bridgeLimit.text,
    resetsAt: bridgeLimit.resets
  });
})()
"""
)

JS_LAST_ROW = (
    r"""
(() => {
"""
    + JS_CONTENT_FN
    + r"""
  const rows = [...document.querySelectorAll('[data-testid="transcript-row"]')];
  if (!rows.length) return '';
  return contentOf(rows[rows.length-1]) || (rows[rows.length-1].innerText || '').trim();
})()
"""
)

JS_MARK_OLD = r"""
(() => {
  const nodes = [...document.querySelectorAll('[data-testid="assistant-message"]')];
  if (!nodes.length) return 'none';
  nodes.forEach(n => n.setAttribute('data-bridge-old','1'));
  return 'marked:' + nodes.length;
})()
"""

JS_FOCUS_COMPOSER = r"""
(() => {
  const eds = [...document.querySelectorAll('div[contenteditable="true"]')];
  if (!eds.length) return 'no-composer';
  const el = eds.find(e => e.classList.contains('ProseMirror')) || eds[eds.length-1];
  el.focus();
  const sel = window.getSelection();
  const range = document.createRange();
  range.selectNodeContents(el);
  sel.removeAllRanges();
  sel.addRange(range);
  return 'focused';
})()
"""

JS_COMPOSER_TEXT = r"""
(() => {
  const el = document.querySelector('div.ProseMirror[contenteditable="true"], div[contenteditable="true"]');
  return el ? (el.innerText || el.textContent || '') : '';
})()
"""

JS_CLICK_SEND = r"""
(() => {
  const byId = document.querySelector('[data-testid="chat-input-send"]');
  const btns = [...document.querySelectorAll('button')];
  const send = (byId && byId.tagName === 'BUTTON' ? byId : null)
            || btns.find(b => /^send/i.test((b.getAttribute('aria-label')||'').trim()) && !b.disabled)
            || btns.find(b => /send/i.test(b.getAttribute('aria-label')||'') && !b.disabled)
            || btns.find(b => b.type === 'submit' && !b.disabled);
  if (!send) return 'no-send-button';
  if (send.disabled) return 'send-disabled';
  send.click();
  return 'clicked-send';
})()
"""

JS_SLASH_OPTIONS = r"""
(() => {
  const items = [...document.querySelectorAll('[role="menuitem"],[role="option"]')]
    .filter(e => e.offsetParent !== null);
  return JSON.stringify(items.map(e => (e.textContent || '').trim().split('\n')[0].slice(0, 48)));
})()
"""

JS_CLEAR_COMPOSER = r"""
(() => {
  const el = document.querySelector('div.ProseMirror[contenteditable="true"], div[contenteditable="true"]');
  if (!el) return 'no-composer';
  el.focus();
  const s = window.getSelection();
  const r = document.createRange();
  r.selectNodeContents(el);
  s.removeAllRanges();
  s.addRange(r);
  document.execCommand('delete');
  return 'cleared';
})()
"""

JS_PROBE = r"""
(() => {
  const ids = {};
  document.querySelectorAll('[data-testid]').forEach(e => {
    const v = e.getAttribute('data-testid'); ids[v] = (ids[v]||0)+1;
  });
  const clss = {};
  document.querySelectorAll('div[class]').forEach(e => {
    const c = e.className; if (typeof c === 'string' && /claude|message|markdown|prose/i.test(c)) {
      c.split(/\s+/).forEach(x => { if (/claude|message|markdown|prose/i.test(x)) clss[x]=(clss[x]||0)+1; });
    }
  });
  return JSON.stringify({
    url: location.href,
    title: document.title,
    contentEditables: document.querySelectorAll('[contenteditable="true"]').length,
    dataTestIds: ids,
    interestingClasses: clss
  });
})()
"""

JS_FILE_CARDS = r"""
(() => {
  const cards = [...document.querySelectorAll('[data-testid="file-card-block"]')];
  const clean = s => (s || '').replace(/[\uE000-\uF8FF]/g, '').trim();
  return JSON.stringify(cards.map((el, i) => {
    const dl = el.querySelector('button[aria-label^="Download "]');
    let title = dl ? clean(dl.getAttribute('aria-label')).replace(/^Download\s+/i, '') : '';
    if (!title) {
      const lines = clean(el.innerText).split('\n').map(s => s.trim()).filter(Boolean);
      title = lines.length ? lines[lines.length - 1] : '';
    }
    return { index: i, title: title, download: dl ? clean(dl.getAttribute('aria-label')) : null };
  }));
})()
"""

JS_CLICK_FILE_DOWNLOAD = r"""
(() => {
  const cards = [...document.querySelectorAll('[data-testid="file-card-block"]')];
  const card = cards[__IDX__];
  if (!card) return 'no-card';
  const dl = card.querySelector('button[aria-label^="Download "]');
  if (!dl) return 'no-download';
  dl.click();
  return 'clicked';
})()
"""


WORK_ORDER_OPEN = re.compile(r"<<<\s*WORK[-_ ]?ORDER\s*", re.I)
WORK_ORDER_FENCE = re.compile(r"```+[ \t]*work[-_ ]?order[ \t]*\n(.*?)(?:```+|$)", re.I | re.S)
# one "key: value" line of an order; tolerates "- key:", "1. key:" and "# key:"
ORDER_FIELD_LINE = re.compile(
    r"^[ \t]*(?:[-*][ \t]*|\d+[.)][ \t]*)?(?:#+[ \t]*)?([A-Za-z][A-Za-z _-]{0,24}?)[ \t]*[:=][ \t]*(.*)$")
ORDER_ID_VALUE = re.compile(r"^WO-\d+$", re.I)
LOOSE_MIN_FIELDS = 3

# What an order must carry before it is worth handing to an agent.  Everything
# else is a convenience; these three are the difference between a brief and a
# guess.  ``order_problems`` is what lets the watcher ask for the missing lines
# instead of starting an agent on half an order.
REQUIRED_ORDER_FIELDS = ("dir", "goal", "acceptance")


def _field_key(name: str) -> str:
    return (name or "").strip().lower().replace(" ", "_").replace("-", "_")


def _goal_of(fields: dict) -> str:
    return (fields.get("goal") or fields.get("task") or fields.get("title")
            or fields.get("objective") or "").strip()


def _wrapped_blocks(text: str) -> list[tuple[int, int, str]]:
    """``(start, end, body)`` for every <<<WORK-ORDER ...>>> / ```work-order block.

    Claude sometimes drops the closing >>> of the angle-bracket form, so when
    there is no closing marker the block ends at a blank line or at the end of
    the message.
    """
    out: list[tuple[int, int, str]] = []
    for m in WORK_ORDER_OPEN.finditer(text):
        start = m.end()
        close = text.find(">>>", start)
        if close == -1:
            rest = text[start:]
            stop = re.search(r"\n[ \t]*\n", rest)
            end = start + (stop.start() if stop else len(rest))
            body = text[start:end]
        else:
            end = close
            body = text[start:close]
        out.append((m.start(), end, body))
    for m in WORK_ORDER_FENCE.finditer(text):
        out.append((m.start(), m.end(), m.group(1)))
    return out


def _loose_blocks(text: str, taken: list[tuple[int, int]] | None = None) -> list[str]:
    """Field lists typed with no wrapper at all.

    The wrapped forms are the protocol, but the same instruction often arrives
    as a plain list of the same fields.  Only a *run* of at least three field
    lines that starts at the id, names a ``WO-<n>`` id and carries a goal counts
    - so prose that merely mentions one of these words is not read as an order.
    """
    taken = taken or []
    lines = text.splitlines()
    offs: list[int] = []
    acc = 0
    for ln in lines:
        offs.append(acc)
        acc += len(ln) + 1
    blocks: list[str] = []
    run: list[int] = []

    def flush() -> None:
        if len(run) >= LOOSE_MIN_FIELDS:
            start, end = offs[run[0]], offs[run[-1]] + len(lines[run[-1]])
            first = ORDER_FIELD_LINE.match(lines[run[0]])
            fields = {_field_key(ORDER_FIELD_LINE.match(lines[i]).group(1)):
                      ORDER_FIELD_LINE.match(lines[i]).group(2).strip() for i in run}
            wid = (fields.get("id") or fields.get("work_order") or "").strip()
            overlapped = any(s < end and start < e for s, e in taken)
            if (not overlapped and _field_key(first.group(1)) in ("id", "work_order")
                    and ORDER_ID_VALUE.match(wid) and _goal_of(fields)):
                blocks.append("\n".join(lines[i] for i in run))
        run.clear()

    for i, ln in enumerate(lines):
        m = ORDER_FIELD_LINE.match(ln)
        if m and m.group(2).strip():
            run.append(i)
        else:
            flush()
    flush()
    return blocks


def _work_order_blocks(text: str) -> list[str]:
    """Every work-order body in ``text``, wrapped or bare."""
    wrapped = _wrapped_blocks(text)
    return ([body for _, _, body in wrapped]
            + _loose_blocks(text, [(s, e) for s, e, _ in wrapped]))


def extract_work_orders(text: str) -> list[dict]:
    """Pull work orders out of a Claude message.

    Accepted shapes - the first two are what the protocol asks Claude to use:

        <<<WORK-ORDER
        id: WO-1
        dir: /path/to/repo
        branch: some-branch
        goal: what to build
        acceptance: how we know it is done
        >>>

    or a fenced block whose info string is ``work-order``. The closing marker is
    optional: without it the block ends at a blank line.  A bare field list with
    no wrapper is accepted too (see ``_loose_blocks``) - the same instruction in
    different clothes should not cost a turn.

    Extraction is deliberately *not* validation: an order missing its dir or its
    acceptance test is still returned, so the watcher can say what is missing
    instead of dropping it.  See ``order_problems``.
    """
    if not text:
        return []
    orders: list[dict] = []
    seen: set[str] = set()
    for raw in _work_order_blocks(text):
        fields: dict[str, str] = {}
        for line in raw.splitlines():
            m = ORDER_FIELD_LINE.match(line)
            if not m or not m.group(2).strip():
                continue
            fields[_field_key(m.group(1))] = m.group(2).strip()
        goal = _goal_of(fields)
        if not goal:
            continue
        wid = fields.get("id") or fields.get("work_order") or ""
        if not wid:
            wid = "WO-" + hashlib.sha1(raw.encode()).hexdigest()[:8]
        if wid in seen:
            continue
        seen.add(wid)
        orders.append({
            "id": wid,
            "goal": goal,
            "dir": fields.get("dir") or fields.get("directory") or fields.get("repo") or fields.get("project") or "",
            "branch": fields.get("branch") or "",
            "save": fields.get("save") or fields.get("save_to") or fields.get("output") or fields.get("path") or "",
            "acceptance": fields.get("acceptance") or fields.get("done_when") or fields.get("done") or "",
            "model": fields.get("model") or "",
            "raw": raw.strip(),
        })
    return orders


def order_problems(order: dict) -> list[str]:
    """What is missing before this order can be handed to an agent.

    Returns [] for a complete order.  This is the watcher's guarantee that a
    half-written order never becomes a half-informed agent: whatever is listed
    here is asked back from Claude, once.
    """
    problems: list[str] = []
    for name in REQUIRED_ORDER_FIELDS:
        if not (order.get(name) or "").strip():
            problems.append(name)
    where = (order.get("dir") or "").strip()
    if where:
        where = os.path.expanduser(where)
        if not os.path.isabs(where):
            problems.append("dir (must be an absolute path)")
        elif not os.path.isdir(where):
            problems.append("dir (does not exist: %s)" % where)
    return problems


class CDP:
    def __init__(self, ws_url: str, timeout: float = 20.0):
        if ws_connect is None:
            raise RuntimeError("python 'websockets' is not installed")
        self.ws = ws_connect(ws_url, max_size=None, open_timeout=timeout)
        self._id = 0

    def call(self, method: str, params: dict | None = None, timeout: float = 60.0):
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError(f"CDP {method} timed out")
            raw = self.ws.recv(timeout=remaining)
            msg = json.loads(raw)
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"CDP {method}: {msg['error']}")
                return msg.get("result", {})

    def js(self, expr: str, timeout: float = 60.0, await_promise: bool = False):
        res = self.call(
            "Runtime.evaluate",
            {"expression": expr, "returnByValue": True, "awaitPromise": await_promise},
            timeout=timeout,
        )
        if res.get("exceptionDetails"):
            raise RuntimeError(f"JS error: {res['exceptionDetails'].get('text')}")
        return res.get("result", {}).get("value")

    def press_enter(self):
        for kind in ("keyDown", "keyUp"):
            self.call("Input.dispatchKeyEvent", {
                "type": kind, "key": "Enter", "code": "Enter",
                "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13,
                "text": "\r" if kind == "keyDown" else "",
            })

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


class Bridge:
    def __init__(self, page_url: str | None = None):
        self.page_url = page_url or target_url()

    def _pages(self):
        try:
            return [
                t for t in http_json("/json/list")
                if t.get("type") == "page" and "claude.ai" in (t.get("url") or "")
            ]
        except Exception:
            return []

    def _wait_page(self, c: "CDP", want_composer: bool = True, timeout: float = 45.0) -> dict:
        """Wait until the page is loaded (and, optionally, the composer exists)."""
        deadline = time.time() + timeout
        last: dict = {}
        while time.time() < deadline:
            time.sleep(0.5)
            try:
                last = json.loads(c.js(JS_STATE, timeout=8) or "{}")
            except Exception:
                continue
            if last.get("ready") == "complete" and (last.get("composer") or not want_composer):
                return last
        return last

    def connect(self, want_composer: bool = True, navigate: bool = True) -> CDP:
        if not ensure_chrome(self.page_url):
            raise RuntimeError(
                f"bridge Chrome is not reachable on {BASE}. "
                f"Run: bash {LAUNCHER}"
            )
        want = self.page_url.split("#")[0].rstrip("/")
        pages = self._pages()

        # The tab we drive. With more than one claude.ai tab open (a restored window,
        # the launcher adding one) picking pages[0] at random is how a message ends up
        # in the wrong chat, so remember the id and prefer it.
        pinned = [t for t in pages if t.get("id") and t.get("id") == load_target()]
        exact = [t for t in pages if (t.get("url") or "").split("#")[0].rstrip("/") == want]

        if not navigate:  # --save-chat / --new-chat: the driven tab, exactly as it is
            page = (pinned or pages or [None])[0]
            if page is not None:
                c = CDP(page["webSocketDebuggerUrl"])
                save_target(page.get("id") or "")
                self._wait_page(c, want_composer=want_composer)
                return c

        if pages:  # drive the remembered tab, aiming it at the saved chat
            page = (pinned or exact or pages)[0]
            c = CDP(page["webSocketDebuggerUrl"])
            save_target(page.get("id") or "")
            on_it = (page.get("url") or "").split("#")[0].rstrip("/") == want
            if on_it or is_new_chat_url(want):
                # nothing to do: it is the saved chat already, or the saved chat is
                # "a new chat", whose real url only appears once a message is sent -
                # aiming a page at /new would start a *different* empty chat.
                self._wait_page(c, want_composer=want_composer)
            else:
                c.call("Page.navigate", {"url": self.page_url})
                self._wait_page(c, want_composer=want_composer, timeout=60.0)
            return c

        page = create_page(self.page_url)
        if not page or not page.get("webSocketDebuggerUrl"):
            raise RuntimeError("could not open a claude.ai page")
        c = CDP(page["webSocketDebuggerUrl"])
        save_target(page.get("id") or "")
        self._wait_page(c, want_composer=want_composer, timeout=60.0)
        return c

    def state(self, c: CDP) -> dict:
        return json.loads(c.js(JS_STATE))

    def last_message(self, c: CDP, timeout: float = 20.0) -> dict:
        """Read the page state, waiting briefly for the transcript to render.

        Just after a navigation the SPA can report readyState complete before the
        messages are in the DOM, which shows up as an empty ``last``. Poll until
        a message appears (or we give up, which is what an empty chat looks like).
        """
        deadline = time.time() + timeout
        st = self.state(c)
        while not (st.get("last") or "").strip() and time.time() < deadline:
            time.sleep(0.6)
            st = self.state(c)
        return st

    def last_messages(self, c: CDP, n: int = 1, timeout: float = 20.0) -> list[str]:
        """Return the last ``n`` assistant messages, oldest first."""
        n = max(1, int(n))
        deadline = time.time() + timeout
        out: list[str] = []
        while True:
            try:
                out = json.loads(c.js(JS_LAST_MESSAGES.replace("__N__", str(n))) or "[]")
            except Exception:
                out = []
            if (out and (out[-1] or "").strip()) or time.time() >= deadline:
                return [m for m in out if (m or "").strip()]
            time.sleep(0.6)

    def probe(self, c: CDP) -> dict:
        return json.loads(c.js(JS_PROBE))

    def _set_downloads(self, c: "CDP", path: str) -> str:
        params = {"behavior": "allow", "downloadPath": path, "eventsEnabled": True}
        try:
            c.call("Browser.setDownloadBehavior", params)
            return "Browser.setDownloadBehavior"
        except Exception:
            pass
        ws = browser_ws()
        if ws:
            try:
                bw = CDP(ws)
                try:
                    bw.call("Browser.setDownloadBehavior", params)
                    return "browser:Browser.setDownloadBehavior"
                finally:
                    bw.close()
            except Exception:
                pass
        c.call("Page.setDownloadBehavior", {"behavior": "allow", "downloadPath": path})
        return "Page.setDownloadBehavior"

    def list_files(self) -> list[dict]:
        """List the file cards Claude has produced in the chat."""
        c = self.connect(want_composer=False)
        try:
            return json.loads(c.js(JS_FILE_CARDS) or "[]")
        finally:
            c.close()

    def download(self, match=None, timeout: float = 45.0) -> list[dict]:
        """Download Claude's file card(s) to ~/claude-bridge/downloads and return them.

        match may be a string (substring of the card title), a list of titles, or
        None for every card.
        """
        outdir = os.path.join(HERE, "downloads")
        os.makedirs(outdir, exist_ok=True)
        c = self.connect(want_composer=False)
        try:
            self._set_downloads(c, outdir)
            cards = json.loads(c.js(JS_FILE_CARDS) or "[]")

            def wanted(k: dict) -> bool:
                t = k.get("title") or ""
                if not match:
                    return True
                if isinstance(match, (list, tuple, set)):
                    return any(m.lower() in t.lower() or t.lower() == m.lower() for m in match)
                return match.lower() in t.lower()

            targets = [k for k in cards if wanted(k)]
            if not targets:
                raise RuntimeError(
                    f"no file card matches {match!r}; cards are: {[k.get('title') for k in cards] or 'none'}"
                )
            before = set(os.listdir(outdir))
            for k in targets:
                res = c.js(JS_CLICK_FILE_DOWNLOAD.replace("__IDX__", str(k["index"])))
                if res != "clicked":
                    raise RuntimeError(f"could not click download on {k.get('title')!r}: {res}")
                time.sleep(1.5)
            deadline = time.time() + timeout
            names: list[str] = []
            while time.time() < deadline:
                time.sleep(0.7)
                cur = set(os.listdir(outdir))
                names = sorted(cur - before)
                if names and not any(f.endswith(".crdownload") for f in cur):
                    break
            files = []
            for f in names:
                p = os.path.join(outdir, f)
                files.append({"name": f, "path": p, "bytes": os.path.getsize(p) if os.path.exists(p) else 0})
            if not files:
                raise RuntimeError("the download produced no file (check the bridge window)")
            return files
        finally:
            c.close()

    def availability(self) -> dict:
        """Can Claude answer right now?

        Returns {available, limited, signed_in, text, resets_at, wait_seconds}.
        ``text`` is the notice the page shows ("You are out of free messages until
        7:20 PM"); ``wait_seconds`` is how long until the stated reset (or None).
        """
        c = self.connect(want_composer=False)
        try:
            st = self.state(c)
        finally:
            c.close()
        signed_in = bool(st.get("composer"))
        limited = bool(st.get("limited"))
        resets_at = (st.get("resetsAt") or "").strip()
        reset_epoch = parse_reset_time(resets_at or st.get("limitText") or "")
        return {
            "available": signed_in and not limited,
            "limited": limited,
            "signed_in": signed_in,
            "text": (st.get("limitText") or "").strip(),
            "resets_at": resets_at,
            "wait_seconds": int(max(0, reset_epoch - time.time())) if reset_epoch else None,
        }

    def wait_available(self, timeout: float = 3600.0, interval: float = 30.0,
                       poll_every: float = 30.0) -> dict:
        """Block until Claude can answer again (or ``timeout`` runs out).

        Sleeps toward the reset time the page gives, waking every ``interval``
        seconds to re-check anyway (so it resumes as soon as the limit really lifts).
        Returns the availability dict as of the last check.
        """
        deadline = time.time() + max(0.0, timeout)
        while True:
            av = self.availability()
            if av["available"] or time.time() >= deadline:
                return av
            nap = min(interval, max(1.0, deadline - time.time()))
            if av.get("wait_seconds") is not None:
                nap = min(nap, max(1.0, av["wait_seconds"] + 5))
            time.sleep(min(nap, poll_every))

    def send(self, c: CDP, text: str) -> dict:
        st = self.state(c)
        if not st.get("composer"):
            raise ClaudeUnavailable(
                "not signed in to claude.ai in the bridge window", sent=False)
        before = st["total"]
        c.js(JS_MARK_OLD)
        focus = c.js(JS_FOCUS_COMPOSER)
        if focus != "focused":
            raise RuntimeError(f"could not focus composer: {focus}")
        c.call("Input.insertText", {"text": text})
        time.sleep(0.25)
        landed = c.js(JS_COMPOSER_TEXT) or ""
        if text.strip() and text.strip() not in landed:
            # fallback for editors that ignore CDP insertText
            c.js("document.execCommand('insertText', false, " + json.dumps(text) + ")")
            time.sleep(0.15)
            landed = c.js(JS_COMPOSER_TEXT) or ""
        click = c.js(JS_CLICK_SEND)
        if click != "clicked-send":
            c.press_enter()
            click = "pressed-enter"
        return {"before": before, "click": click, "landed": landed[:200]}

    def wait_reply(self, c: CDP, before: int, timeout: float) -> str:
        deadline = time.time() + timeout
        last_text = ""
        stable = 0
        while time.time() < deadline:
            time.sleep(1.2)
            try:
                st = self.state(c)
            except Exception:
                continue
            if st.get("limited") and not st.get("streaming"):
                # The message is in the chat, but no reply can come until the limit
                # resets - fail fast with the reason instead of waiting it out.
                raise ClaudeUnavailable(st.get("limitText") or "",
                                        st.get("resetsAt") or "", sent=True)
            streamed = st.get("fresh") or []
            text = streamed[-1] if streamed else ""
            streaming = st.get("streaming")
            if text:
                if text == last_text and not streaming:
                    stable += 1
                    if stable >= 2:
                        return text
                else:
                    stable = 0
                last_text = text
            elif st.get("total", 0) > before and st.get("last") and not streaming:
                if st["last"] == last_text:
                    stable += 1
                    if stable >= 1:
                        return st["last"]
                last_text = st["last"]
        raise TimeoutError(f"no reply within {timeout:.0f}s")

    def send_slash(self, c: CDP, command: str) -> dict:
        """Type a slash command and run it - but only if the slash menu offers it.

        Claude's web slash menu lists skills (deep-research, ...), not the CLI's
        /compact. If the command is not offered we clear the composer and refuse,
        so a stray "/compact" is never posted into the user's thread as a message.
        """
        st = self.state(c)
        if not st.get("composer"):
            raise RuntimeError(
                "No composer on the claude.ai page - sign in to claude.ai in the bridge window first."
            )
        before = st["total"]
        c.js(JS_MARK_OLD)
        if c.js(JS_FOCUS_COMPOSER) != "focused":
            raise RuntimeError("could not focus composer")
        c.call("Input.insertText", {"text": command})
        time.sleep(1.0)  # let the slash menu appear
        name = command.strip().lstrip("/").split()[0].lower()
        try:
            opts = json.loads(c.js(JS_SLASH_OPTIONS) or "[]")
        except Exception:
            opts = []
        offered = [o for o in opts if o.lower().lstrip("/").startswith(name)]
        if not offered:
            c.js(JS_CLEAR_COMPOSER)
            raise RuntimeError(
                f"the claude.ai web slash menu does not offer '{command}' "
                f"(it offers: {', '.join(opts[:8]) or 'nothing'}). Nothing was sent."
            )
        c.press_enter()  # selects the highlighted command
        time.sleep(0.6)
        return {"before": before, "command": command, "offered": offered[:3]}

    def wait_stable(self, c: CDP, timeout: float = 300.0, quiet: float = 4.0) -> str:
        """Wait until the transcript stops changing; return the last row's text."""
        deadline = time.time() + timeout
        last = ""
        last_change = time.time()
        while time.time() < deadline:
            time.sleep(1.2)
            try:
                row = c.js(JS_LAST_ROW) or ""
            except Exception:
                continue
            if row != last:
                last = row
                last_change = time.time()
            elif (time.time() - last_change) >= quiet:
                return last
        return last

    def slash(self, command: str, timeout: float = 300.0) -> str:
        c = self.connect()
        try:
            self.send_slash(c, command)
            return self.wait_stable(c, timeout=timeout)
        finally:
            c.close()

    def compact(self, timeout: float = 300.0) -> str:
        """Keep a long conversation going.

        The web UI has no /compact (its slash menu only lists skills), so when the
        command is not offered, fall back to the documented equivalent: ask Claude
        for a compact handoff summary a fresh chat can continue from.
        """
        try:
            return self.slash("/compact", timeout=timeout)
        except RuntimeError as e:
            if "does not offer" not in str(e):
                raise
        return self.ask(COMPACT_PROMPT, timeout=timeout)

    def ask(self, text: str, timeout: float = DEFAULT_TIMEOUT) -> str:
        try:
            c = self.connect()
        except Exception as e:
            # The window is down and could not be launched.  Do not lose the
            # report - keep it and raise a signal the caller can act on.
            raise ClaudeUnavailable(
                "the bridge window is not reachable", sent=False,
                queued=queue_message(text, f"bridge down: {e}")) from e
        try:
            return self._ask_with(c, text, timeout)
        finally:
            c.close()

    def _ask_with(self, c: CDP, text: str, timeout: float) -> str:
        """Send and wait on an already-connected session (see ``ask``)."""
        try:
            info = self.send(c, text)
        except ClaudeUnavailable as e:
            # Nothing reached the chat (signed out, no composer): keep a copy.
            if not e.queued:
                e.queued = queue_message(text, e.text or "not deliverable")
            raise
        try:
            return self.wait_reply(c, info["before"], timeout)
        except ClaudeUnavailable as e:
            e.sent = True  # send() ran, so the message is in the chat
            raise

    def flush_outbox(self, timeout: float = DEFAULT_TIMEOUT,
                     max_messages: int | None = None) -> dict:
        """Deliver queued reports, oldest first, once Claude can take them.

        Stops at the first one that cannot be delivered (whatever is left stays
        queued). Returns {"sent", "remaining", "stopped"}.
        """
        sent: list[str] = []
        remaining = outbox_files()
        stopped: str | None = None
        for path in list(remaining):
            if max_messages is not None and len(sent) >= max_messages:
                break
            body = read_queued(path)
            if not body:
                remaining.remove(path)
                os.remove(path)          # empty file: nothing to send
                continue
            try:
                self.ask(body, timeout=timeout)
            except ClaudeUnavailable as e:
                stopped = str(e)
                if e.sent:
                    # It did reach the chat, so never send it again; Claude will
                    # read it when the limit lifts or the window comes back.
                    sent.append(path)
                    remaining.remove(path)
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                break
            sent.append(path)
            remaining.remove(path)
            try:
                os.remove(path)
            except OSError:
                pass
        return {"sent": sent, "remaining": remaining, "stopped": stopped}


# --------------------------------------------------------------------- CLI
def main(argv=None):
    ap = argparse.ArgumentParser(description="Talk to Claude in the bridge Chrome window.")
    ap.add_argument("message", nargs="*", help="message to send (omit with read/status/probe/open)")
    ap.add_argument("--read", action="store_true", help="print the last assistant message, send nothing")
    ap.add_argument("--status", action="store_true", help="print bridge/page status")
    ap.add_argument("--available", action="store_true",
                    help="print whether Claude can answer now (exit 3 when it cannot)")
    ap.add_argument("--wait-available", action="store_true",
                    help="block until Claude can answer again, then print (exit 3 if still blocked)")
    ap.add_argument("--outbox", action="store_true",
                    help="list reports saved because the bridge was not reachable")
    ap.add_argument("--flush", action="store_true",
                    help="deliver queued reports now, oldest first")
    ap.add_argument("--probe", action="store_true", help="dump DOM hints (for debugging selectors)")
    ap.add_argument("--open", action="store_true", help="just make sure the bridge window is open")
    ap.add_argument("--up", action="store_true",
                    help="bring the whole bridge up (window + watcher), same as bridge-up.sh")
    ap.add_argument("--compact", action="store_true", help="run /compact to keep a long conversation going")
    ap.add_argument("--slash", metavar="CMD", default=None, help="run a slash command, e.g. --slash /compact")
    ap.add_argument("--goto", metavar="URL", default=None, help="navigate the bridge window to a url")
    ap.add_argument("--work", action="store_true", help="print work orders found in Claude's last message")
    ap.add_argument("--files", action="store_true", help="list Claude's file cards in the chat")
    ap.add_argument("--download", metavar="MATCH", nargs="?", const="", default=None,
                    help="download Claude's file card(s); optional MATCH on the title")
    ap.add_argument("--save-chat", action="store_true", help="remember the claude.ai chat currently open in the bridge window")
    ap.add_argument("--new-chat", action="store_true",
                    help="open a fresh chat in the bridge window and make it the chat we use "
                         "(add a message to seed it, e.g. a handoff summary)")
    ap.add_argument("--url", default=None, help="page url to open/create")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    b = Bridge(args.url or args.goto)
    try:
        if args.open:
            ok = ensure_chrome(args.url)
            print(json.dumps({"chrome": ok, "endpoint": BASE}) if args.json else f"bridge chrome: {'up' if ok else 'down'} at {BASE}")
            return 0 if ok else 1

        if args.up:
            res = start_bridge()
            if args.json:
                print(json.dumps(res, indent=1))
            else:
                print(f"bridge window : {'up' if res['window'] else 'DOWN'}")
                print(f"signed in     : {res['signed_in']}")
                print(f"chat          : {res['chat']}")
                print(f"watcher       : {'running' if res['watcher'] else 'NOT running'}")
                if res.get("detail"):
                    print("--- detail ---")
                    print(res["detail"])
            return 0 if (res["window"] and res["watcher"]) else 1

        if args.available or args.wait_available:
            av = (b.wait_available(timeout=args.timeout) if args.wait_available
                  else b.availability())
            if args.json:
                print(json.dumps(av, indent=1))
            else:
                if av["available"]:
                    print("claude is answering again" if args.wait_available
                          else "claude can answer now")
                else:
                    print("claude cannot answer right now: %s" % (av["text"] or "unknown reason"))
                    if av.get("resets_at"):
                        mins = av.get("wait_seconds")
                        extra = " (in ~%d min)" % max(1, round(mins / 60)) if mins else ""
                        print("  the page says it resets at %s%s" % (av["resets_at"], extra))
            return 0 if av["available"] else 3

        if args.outbox:
            files = outbox_files()
            if args.json:
                print(json.dumps({"outbox": files, "count": len(files)}, indent=1))
            else:
                print("%d queued report(s) in %s" % (len(files), OUTBOX))
                for f in files:
                    print("  %s" % f)
            return 0

        if args.flush:
            res = b.flush_outbox(timeout=args.timeout)
            if args.json:
                print(json.dumps(res, indent=1))
            else:
                print("sent %d queued report(s)" % len(res["sent"]))
                for f in res["sent"]:
                    print("  sent: %s" % f)
                if res["remaining"]:
                    print("  still queued: %d" % len(res["remaining"]))
                if res["stopped"]:
                    print("  stopped: %s" % res["stopped"])
            return 0 if not res["remaining"] else 3

        # --save-chat and --new-chat have to see the page as it is: aiming it at the saved
        # chat first is exactly what made "remember this new chat" impossible.
        c = b.connect(navigate=not (args.save_chat or args.new_chat))
        try:
            if args.goto:
                c.call("Page.navigate", {"url": args.goto})
                for _ in range(60):
                    time.sleep(0.5)
                    try:
                        if b.state(c).get("ready") == "complete":
                            break
                    except Exception:
                        continue
                time.sleep(0.4)
                if not (args.probe or args.status or args.read or args.save_chat or args.new_chat or args.compact or args.slash or args.message):
                    args.status = True
            if args.new_chat:
                c.call("Page.navigate", {"url": NEW_CHAT_URL})
                b._wait_page(c, want_composer=True, timeout=60.0)
                text = " ".join(args.message).strip()
                reply = b._ask_with(c, text, args.timeout) if text else None
                st = b.state(c)
                url = (st.get("url") or "").split("#")[0]
                if "claude.ai" not in url:
                    raise RuntimeError(f"the bridge page is not a claude.ai chat: {url}")
                cfg = load_config()
                cfg["chat_url"] = url
                save_config(cfg)
                if args.json:
                    print(json.dumps({"new_chat": url, "seeded": bool(text), "reply": reply}))
                else:
                    print(f"new chat: {url}")
                    if text:
                        print("--- Claude opened the thread with ---")
                        print(reply or "")
                    else:
                        print("say something (ask-claude \"...\"), then run "
                              "ask-claude --save-chat to pin the chat's real id")
                return 0
            if args.save_chat:
                st = b.state(c)
                url = (st.get("url") or "").split("#")[0]
                if "claude.ai" not in url:
                    raise RuntimeError(f"the bridge page is not a claude.ai chat: {url}")
                cfg = load_config()
                cfg["chat_url"] = url
                save_config(cfg)
                out = {"saved": url, "config": CONFIG_PATH}
                if args.json:
                    print(json.dumps(out))
                else:
                    print(f"remembered chat: {url}")
                return 0
            if args.probe:
                out = b.probe(c)
            elif args.status:
                out = b.state(c)
                out["target"] = load_target()
                out["tabs"] = [t.get("url") for t in b._pages()]
            elif args.read:
                st = b.last_message(c)
                out = {"last": st.get("last", ""), "streaming": st.get("streaming"), "total": st.get("total")}
            elif args.compact or args.slash:
                cmd = args.slash or "/compact"
                b.send_slash(c, cmd)
                res = b.wait_stable(c, timeout=args.timeout)
                if args.json:
                    print(json.dumps({"command": cmd, "result": res}))
                else:
                    print(res)
                return 0
            elif args.work:
                st = b.last_message(c)
                orders = extract_work_orders(st.get("last", ""))
                if args.json:
                    out_orders = [dict(o, problems=order_problems(o)) for o in orders]
                    print(json.dumps({"orders": out_orders, "streaming": st.get("streaming")}, indent=1))
                else:
                    if orders:
                        for o in orders:
                            print(f"[{o['id']}] {o['goal']}  dir={o['dir'] or '-'} branch={o['branch'] or '-'}")
                            missing = order_problems(o)
                            if missing:
                                print("  ! not dispatchable - missing: %s" % ", ".join(missing))
                    else:
                        print("(no work order in Claude's last message)")
                return 0
            elif args.files:
                print(json.dumps(b.list_files(), indent=1))
                return 0
            elif args.download is not None:
                print(json.dumps(b.download(args.download or None, timeout=max(45.0, args.timeout)), indent=1))
                return 0
            else:
                if args.message and args.message[0] == "-":
                    text = sys.stdin.read().strip()
                else:
                    text = " ".join(args.message).strip()
                if not text:
                    ap.error("no message given (use --read/--status/--probe/--open)")
                reply = b._ask_with(c, text, args.timeout)
                if args.json:
                    print(json.dumps({"sent": text, "reply": reply}))
                else:
                    print(reply)
                return 0
        finally:
            c.close()
        print(json.dumps(out, indent=1) if args.json else json.dumps(out, indent=1))
        return 0
    except ClaudeUnavailable as e:
        reset_epoch = parse_reset_time(e.resets_at or e.text)
        out = {
            "unavailable": True,
            "reason": e.text or "claude is not accepting messages right now",
            "resets_at": e.resets_at,
            "wait_seconds": int(max(0, reset_epoch - time.time())) if reset_epoch else None,
            "sent": e.sent,
            "queued": e.queued or None,
        }
        if args.json:
            print(json.dumps(out))
        else:
            print("claude cannot answer right now: %s" % out["reason"])
            if out["resets_at"]:
                mins = out["wait_seconds"]
                extra = " (in ~%d min)" % max(1, round(mins / 60)) if mins else ""
                print("  the page says it resets at %s%s" % (out["resets_at"], extra))
            if e.sent:
                print("  your message is in the chat and will be read when Claude "
                      "can answer again")
            elif e.queued:
                print("  nothing was sent - the report is saved in %s" % e.queued)
                print("  it is delivered automatically once the bridge is back "
                      "(or now with: ask-claude --flush)")
            else:
                print("  nothing was sent")
            print("  check later with --available, or wait with --wait-available")
        return 3
    except Exception as e:
        if args.json:
            print(json.dumps({"error": str(e)}))
        else:
            print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
