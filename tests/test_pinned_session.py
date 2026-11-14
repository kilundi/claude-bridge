#!/usr/bin/env python3
"""Prove an order can be delivered *into* a session, instead of starting one.

    python3 tests/test_pinned_session.py

An order may carry ``session: ses_...`` instead of ``dir:`` - a **pinned** order. The watcher
prompts that session rather than starting a fresh agent in a directory. The session already
has its own directory and model, so the order is not asked for a directory it would never use,
and the id is checked for shape before anything is dispatched.

The one thing a pinned order must never do is go somewhere else. There is no fallback to a
fresh session: an order pinned to a pipeline's working session lands there or fails visibly,
because quietly starting it elsewhere is the outcome nobody would notice.

What is proved:

  1. ``session:`` is read as a field, with its aliases
  2. a pinned order needs no directory; an unpinned one still does
  3. a malformed session id is a problem worth asking back, not a doomed start
  4. a pinned order is delivered into its session - and no session is created
  5. a pinned order whose session is gone fails loudly, and starts nothing anywhere
  6. an ordinary order still creates a session and prompts it (the control)
  7. the dispatch hookup records the pinned id as the order's session

Everything runs against fakes: no Chrome, no claude.ai, no opencode, no terminal window.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import bridge  # noqa: E402
import watcher  # noqa: E402

PINNED = "ses_e2a758dc0ffe6naxYQwxm0YXpF"
CREATED = "ses_created000000000000000000"

PINNED_WO = """<<<WORK-ORDER
id: WO-301
session: %s
goal: cut the third episode together
acceptance: episodes/0003 renders and the words line up
>>>""" % PINNED

ALIAS_WO = """<<<WORK-ORDER
id: WO-302
session_id: %s
goal: something
acceptance: it is done
>>>""" % PINNED

PLAIN_WO = """<<<WORK-ORDER
id: WO-303
dir: /tmp
goal: something else
acceptance: it is done
>>>"""

# the calls start_agent makes, in order
CALLS: list[tuple] = []
EXISTS = True


def fake_oc_api(binp, method, path, payload=None, timeout=60):
    CALLS.append((method, path, payload))
    if method == "GET" and path.startswith("/api/session/"):
        if EXISTS and path.rsplit("/", 1)[1] == PINNED:
            return {"data": {"id": PINNED, "location": {"directory": "/home/kilundi"}}}
        raise RuntimeError("opencode api failed: HTTP 404 Not Found")
    if method == "POST" and path == "/api/session":
        return {"data": {"id": CREATED}}
    if method == "POST" and path.endswith("/prompt"):
        return {"ok": True}
    raise AssertionError("unexpected call %s %s" % (method, path))


def tmp_env() -> str:
    d = tempfile.mkdtemp(prefix="pinned-")
    watcher.STATE = os.path.join(d, "state.json")
    watcher.INBOX = os.path.join(d, "inbox")
    watcher.REPORTS = os.path.join(d, "reports")
    watcher.LOG = os.path.join(d, "watcher.log")
    os.makedirs(watcher.REPORTS, exist_ok=True)
    os.makedirs(watcher.INBOX, exist_ok=True)
    return d


def fresh_env():
    global EXISTS
    CALLS.clear()
    EXISTS = True
    tmp_env()
    watcher.oc_api = fake_oc_api


def cfg() -> dict:
    return {"watch": {"opencode_bin": "/bin/true", "default_dir": "/tmp"}}


# --- 1 ---------------------------------------------------------------------------------
def test_the_session_field_is_read():
    one = bridge.extract_work_orders(PINNED_WO)
    assert len(one) == 1, one
    assert one[0]["session"] == PINNED, one[0]
    assert one[0]["dir"] == "", "a pinned order names no directory: %s" % one[0]
    alias = bridge.extract_work_orders(ALIAS_WO)
    assert alias and alias[0]["session"] == PINNED, alias
    plain = bridge.extract_work_orders(PLAIN_WO)
    assert plain and plain[0]["session"] == "", "an ordinary order has no session: %s" % plain
    return "`session:` and `session_id:` both read; an ordinary order keeps an empty one"


# --- 2 ---------------------------------------------------------------------------------
def test_a_pinned_order_needs_no_dir():
    pinned = bridge.extract_work_orders(PINNED_WO)[0]
    assert bridge.order_problems(pinned) == [], bridge.order_problems(pinned)
    # ...even if it happens to carry a directory that does not exist: the session's own
    # directory is the one that matters.
    pinned_with_junk_dir = dict(pinned, dir="/no/such/place")
    assert bridge.order_problems(pinned_with_junk_dir) == [], \
        bridge.order_problems(pinned_with_junk_dir)
    missing = bridge.extract_work_orders("""<<<WORK-ORDER
id: WO-304
goal: no dir and no session
acceptance: it is done
>>>""")[0]
    assert bridge.order_problems(missing) == ["dir"], bridge.order_problems(missing)
    return "no dir asked of a pinned order; an unpinned one is still asked"


# --- 3 ---------------------------------------------------------------------------------
def test_a_malformed_session_id_is_a_problem():
    bad = bridge.extract_work_orders("""<<<WORK-ORDER
id: WO-305
session: e2a758dc0ffe6naxYQwxm0YXpF
goal: an id pasted without its ses_ prefix
acceptance: it is done
>>>""")[0]
    problems = bridge.order_problems(bad)
    assert len(problems) == 1 and problems[0].startswith("session ("), problems
    return "an id without its ses_ prefix is asked back, not dispatched: %s" % problems[0]


# --- 4 ---------------------------------------------------------------------------------
def test_a_pinned_order_is_delivered_and_creates_nothing():
    fresh_env()
    order = bridge.extract_work_orders(PINNED_WO)[0]
    sid = watcher.start_agent(cfg(), order, [], os.path.join(watcher.INBOX, "WO-301.md"))
    assert sid == PINNED, sid
    methods = [(m, p) for m, p, _ in CALLS]
    assert ("POST", "/api/session") not in methods, \
        "a pinned order must never create a session: %s" % methods
    assert methods == [("GET", "/api/session/%s" % PINNED),
                       ("POST", "/api/session/%s/prompt" % PINNED)], methods
    prompt = CALLS[-1][2]["text"]
    assert "existing session" in prompt, prompt[:200]
    return "GET then prompt on the pinned session, no create - and the prompt says so"


# --- 5 ---------------------------------------------------------------------------------
def test_a_dead_pinned_session_fails_loudly():
    fresh_env()
    global EXISTS
    order = bridge.extract_work_orders(PINNED_WO)[0]
    inbox = os.path.join(watcher.INBOX, "WO-301.md")
    # (a) the realistic case: the lookup itself fails, as `opencode api` does on a 404
    EXISTS = False
    try:
        watcher.start_agent(cfg(), order, [], inbox)
    except RuntimeError:
        pass
    else:
        raise AssertionError("a gone pinned session must raise, not fall back")
    # (b) the defensive case: the lookup answers, and the answer is "no such session"
    fresh_env()
    watcher.oc_api = lambda binp, method, path, payload=None, timeout=60: (
        CALLS.append((method, path, payload)) or
        {"_tag": "SessionNotFoundError", "sessionID": PINNED})
    try:
        watcher.start_agent(cfg(), order, [], inbox)
    except RuntimeError as e:
        assert "pinned session is gone" in str(e), str(e)
    else:
        raise AssertionError("a SessionNotFound payload must raise too")
    for m, _ in [(m, p) for m, p, _ in CALLS]:
        assert m == "GET", "and it must never prompt or start anything: %s" % CALLS
    fresh_env()
    return "a gone session raises either way, and starts nothing anywhere - no fallback"


# --- 6 ---------------------------------------------------------------------------------
def test_an_ordinary_order_still_creates_a_session():
    fresh_env()
    order = bridge.extract_work_orders(PLAIN_WO)[0]
    sid = watcher.start_agent(cfg(), order, [], os.path.join(watcher.INBOX, "WO-303.md"))
    assert sid == CREATED, sid
    methods = [(m, p) for m, p, _ in CALLS]
    assert methods == [("POST", "/api/session"),
                       ("POST", "/api/session/%s/prompt" % CREATED)], methods
    prompt = CALLS[-1][2]["text"]
    assert "existing session" not in prompt, prompt[:200]
    return "the unpinned path is untouched: create, then prompt - as before"


# --- 7 ---------------------------------------------------------------------------------
def test_the_dispatch_hookup_records_the_pinned_id():
    fresh_env()
    d = os.path.dirname(watcher.STATE)
    watcher.open_agent_terminal = lambda *a, **k: True
    order = bridge.extract_work_orders(PINNED_WO)[0]
    st = {"starts": {"WO-301": {"order": order, "files": [],
                               "inbox": os.path.join(watcher.INBOX, "WO-301.md")}}}
    sid = watcher.ensure_started(cfg(), st, "WO-301", delay=0)
    assert sid == PINNED, sid
    assert (st.get("sessions") or {}).get("WO-301") == PINNED, st.get("sessions")
    assert st["starts"]["WO-301"]["status"] == "started", st["starts"]["WO-301"]
    assert st["starts"]["WO-301"]["attempts"] == 1, st["starts"]["WO-301"]
    # and the state on disk agrees, because everything downstream reads it from there
    on_disk = json.load(open(os.path.join(d, "state.json")))
    assert (on_disk.get("sessions") or {}).get("WO-301") == PINNED, on_disk.get("sessions")
    assert [m for m, _ in [(m, p) for m, p, _ in CALLS]] == ["GET", "POST"], CALLS
    return "ensure_started records the pinned id, and no second session is ever created"


def main() -> int:
    checks = [
        ("session field is read", test_the_session_field_is_read),
        ("pinned order needs no dir", test_a_pinned_order_needs_no_dir),
        ("bad session id is asked back", test_a_malformed_session_id_is_a_problem),
        ("pinned delivery creates nothing", test_a_pinned_order_is_delivered_and_creates_nothing),
        ("dead pinned session is loud", test_a_dead_pinned_session_fails_loudly),
        ("ordinary order still creates", test_an_ordinary_order_still_creates_a_session),
        ("dispatch records the pinned id", test_the_dispatch_hookup_records_the_pinned_id),
    ]
    failed = 0
    for name, fn in checks:
        try:
            detail = fn()
            print("PASS  %-34s %s" % (name, detail))
        except Exception as e:  # noqa: BLE001
            failed += 1
            print("FAIL  %-34s %s" % (name, e))
    print("\n%d/%d proofs passed" % (len(checks) - failed, len(checks)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
