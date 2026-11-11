#!/usr/bin/env python3
"""Prove that only a complete order reaches an agent, and that a stalled turn is
asked about exactly once and then made loud.

    python3 tests/test_work_order_quality.py

Offline: no Chrome, no opencode, no network. Everything runs against temp files.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import bridge  # noqa: E402
import watcher  # noqa: E402

COMPLETE = """<<<WORK-ORDER
id: WO-201
dir: /tmp
branch: feature/x
goal: add the widget
acceptance: the widget spec is green
>>>"""

BARE = """Here is the next one.

id: WO-202
dir: /tmp
branch: main
goal: rewrite the frobnicator
acceptance: frobnicator specs pass"""

HALF = """<<<WORK-ORDER
id: WO-203
dir: /tmp
goal: do the thing
>>>"""

REPORT = ("Done: 3 commits, suite green, pushed. The two flaky specs are environmental "
          "and I have left them out of this phase.")

PROSE_PROMISE = ("Triage: one blocker. I'm fixing all eleven findings in one small phase. "
                 "A replayed create returns secret: null.")


def tmp_env() -> str:
    d = tempfile.mkdtemp(prefix="order-quality-")
    watcher.STATE = os.path.join(d, "state.json")
    watcher.INBOX = os.path.join(d, "inbox")
    watcher.LOG = os.path.join(d, "watcher.log")
    watcher.DOWNLOADS = os.path.join(d, "downloads")
    watcher.REPORTS = os.path.join(d, "reports")
    watcher.NEEDS_ORDER = os.path.join(d, "NEEDS-ORDER.md")
    os.makedirs(watcher.INBOX, exist_ok=True)
    os.makedirs(watcher.DOWNLOADS, exist_ok=True)
    # a test must never open a real terminal window on the user's desktop
    watcher.open_agent_terminal = lambda cfg_, oid, sid, directory: False
    # ... nor read a real agent transcript from the opencode server
    watcher.session_activity = lambda cfg_, st_, now=None: []
    return d


def cfg(**watch) -> dict:
    base = {"ask_for_order": True, "ask_max_streak": 1, "ask_grace_seconds": 600,
            "ask_min_seconds": 120, "start_retries": 0, "start_retry_delay": 0}
    base.update(watch)
    return {"watch": base}


# --- reading Claude's orders -----------------------------------------------------------

def test_shapes_are_read() -> str:
    w = bridge.extract_work_orders(COMPLETE)
    assert len(w) == 1 and w[0]["id"] == "WO-201", w
    fenced = bridge.extract_work_orders("```work-order\nid: WO-204\ndir: /tmp\n"
                                        "goal: g\nacceptance: a\n```")
    assert len(fenced) == 1 and fenced[0]["id"] == "WO-204", fenced
    bare = bridge.extract_work_orders(BARE)
    assert len(bare) == 1 and bare[0]["id"] == "WO-202", bare
    assert bare[0]["acceptance"] == "frobnicator specs pass", bare[0]
    return "wrapped, fenced and bare field lists all read as orders"


def test_prose_is_not_an_order() -> str:
    for text in (REPORT, PROSE_PROMISE,
                 "Some notes:\nid: 42\nstatus: ok\ngoal: nothing real",   # not WO-<n>
                 "status: ok\ngoal: g\nacceptance: a",                    # does not start at the id
                 "goal: g\nacceptance: a"):                               # too short to be a run
        got = bridge.extract_work_orders(text)
        assert got == [], "read an order out of prose: %r -> %r" % (text, got)
    return "prose, short field runs and non-WO ids are not orders"


def test_completeness() -> str:
    good = bridge.extract_work_orders(COMPLETE)[0]
    assert bridge.order_problems(good) == [], bridge.order_problems(good)
    half = bridge.extract_work_orders(HALF)[0]
    assert bridge.order_problems(half) == ["acceptance"], bridge.order_problems(half)
    assert bridge.order_problems({"goal": "g"}) == ["dir", "acceptance"]
    rel = bridge.order_problems({"dir": "relative/path", "goal": "g", "acceptance": "a"})
    assert rel == ["dir (must be an absolute path)"], rel
    gone = bridge.order_problems({"dir": "/no/such/place/xyz", "goal": "g", "acceptance": "a"})
    assert gone == ["dir (does not exist: /no/such/place/xyz)"], gone
    return "missing fields, a relative dir and a missing dir are all named"


# --- routing: what the watcher does with each ------------------------------------------

def test_routing() -> str:
    good = bridge.extract_work_orders(COMPLETE)[0]
    # what handle_orders actually stores is the hash of the block body it parsed
    st = {"orders": {good["id"]: watcher._hash(good["raw"])}, "sessions": {}, "starts": {}}
    routed = watcher.route_orders(st, bridge.extract_work_orders(COMPLETE))
    assert [o["id"] for o in routed["ignored"]] == ["WO-201"], routed
    assert not routed["dispatch"], routed

    routed = watcher.route_orders({}, bridge.extract_work_orders(BARE))
    assert [o["id"] for o in routed["dispatch"]] == ["WO-202"], routed

    routed = watcher.route_orders({}, bridge.extract_work_orders(HALF))
    assert [o["id"] for o, _ in routed["incomplete"]] == ["WO-203"], routed
    assert routed["incomplete"][0][1] == ["acceptance"], routed
    assert not routed["dispatch"], routed

    # an id that already ran, re-posted with different text
    st = {"orders": {"WO-201": "somethingelse"}, "sessions": {"WO-201": "ses_1"}, "starts": {}}
    routed = watcher.route_orders(st, bridge.extract_work_orders(COMPLETE))
    assert [o["id"] for o in routed["duplicate"]] == ["WO-201"], routed
    assert not routed["dispatch"], routed
    return "complete->dispatch, half->incomplete, re-post after a run->duplicate, same text->ignored"


def test_incomplete_never_starts_an_agent() -> str:
    tmp_env()
    calls: list[str] = []
    watcher.start_agent = lambda cfg_, order, files, inbox: (calls.append(order["id"]) or "ses_x")
    half = bridge.extract_work_orders(HALF)[0]
    st = {}
    watcher.handle_orders(cfg(), st, [half], files=[])
    assert calls == [], "an incomplete order started an agent: %r" % calls
    assert not (st.get("orders") or {}), "an incomplete order was marked as handled: %r" % st
    log = open(watcher.LOG).read()
    assert "refusing WO-203" in log, log

    good = bridge.extract_work_orders(COMPLETE)[0]
    watcher.handle_orders(cfg(), st, [good], files=[])
    assert calls == ["WO-201"], calls
    return "incomplete order -> log + no agent; complete order -> one agent"


def test_dispatch_clears_the_stall_flag() -> str:
    d = tmp_env()
    watcher.start_agent = lambda cfg_, order, files, inbox: "ses_x"
    st = {"nudge": {"streak": 1, "at": time.time(), "last_wait": "holding"}}
    watcher.write_needs_order(st, "asked once", "the ask text")
    assert os.path.exists(watcher.NEEDS_ORDER), d
    assert st["needs_order"]["why"] == "asked once"

    watcher.handle_orders(cfg(), st, bridge.extract_work_orders(COMPLETE), files=[])
    assert st["nudge"]["streak"] == 0, st["nudge"]
    assert "needs_order" not in st, st
    assert not os.path.exists(watcher.NEEDS_ORDER), "NEEDS-ORDER.md survived a dispatch"
    return "a dispatched order resets the ask streak and removes NEEDS-ORDER.md"


# --- the stalled turn ------------------------------------------------------------------

def test_promise_detection() -> str:
    assert watcher.looks_like_work_promise(PROSE_PROMISE)
    for yes in ("I'll post the order now.", "Next up: Phase 8.",
                "Let me fix the dispatcher fairness next.",
                "I'm going to add the migration."):
        assert watcher.looks_like_work_promise(yes), yes
    for no in (REPORT, "Done and pushed.", "The suite is green.",
               "I've fixed all eleven findings.", "Phase 7b is complete.",
               "I'll leave those two flaky specs out of this phase."):
        assert not watcher.looks_like_work_promise(no), no
    return "future intent matches, past tense and plain reports do not"


def test_ask_once_then_loud() -> str:
    st = {}
    routed = {"incomplete": [], "duplicate": [], "dispatch": [], "ignored": []}
    plan = watcher.plan_attention(st, PROSE_PROMISE, routed, {}, cfg())
    assert plan["action"] == "ask" and plan["kind"] == "stall", plan
    st["nudge"] = {"streak": 1, "at": time.time()}          # the ask was sent
    plan = watcher.plan_attention(st, PROSE_PROMISE, routed, {}, cfg())
    assert plan["action"] == "wait", plan                     # grace period, not a second ask
    st["nudge"]["at"] = time.time() - 1000
    plan = watcher.plan_attention(st, PROSE_PROMISE, routed, {}, cfg())
    assert plan["action"] == "needs_order", plan              # now it is loud
    return "one ask, then a grace period, then NEEDS-ORDER instead of asking again"


def test_attention_skips_what_it_must() -> str:
    routed = {"incomplete": [], "duplicate": [], "dispatch": [], "ignored": []}
    assert watcher.plan_attention({}, REPORT, routed, {}, cfg())["action"] == "none"
    assert watcher.plan_attention({}, PROSE_PROMISE, routed,
                                  {"order": "WO-108", "session": "s"},
                                  cfg())["action"] == "wait"        # an agent is working
    assert watcher.plan_attention({}, PROSE_PROMISE, routed, {}, cfg(ask_for_order=False))["action"] == "off"
    good = bridge.extract_work_orders(COMPLETE)[0]
    assert watcher.plan_attention({}, PROSE_PROMISE,
                                  {"incomplete": [], "duplicate": [], "dispatch": [good], "ignored": []},
                                  {}, cfg())["action"] == "none"    # the turn is alive
    assert watcher.plan_attention({}, COMPLETE,
                                  {"incomplete": [], "duplicate": [], "dispatch": [], "ignored": [good]},
                                  {}, cfg())["action"] == "none"    # this order already ran
    # the cheap pre-check must agree, so the watcher does not poll transcripts needlessly
    assert not watcher.attention_needed({"dispatch": [], "ignored": [good]}, PROSE_PROMISE)
    assert not watcher.attention_needed({"dispatch": [good], "ignored": []}, PROSE_PROMISE)
    assert watcher.attention_needed({"dispatch": [], "ignored": []}, PROSE_PROMISE)
    half = bridge.extract_work_orders(HALF)[0]
    plan = watcher.plan_attention({}, HALF,
                                  {"incomplete": [(half, ["acceptance"])], "duplicate": [],
                                   "dispatch": [], "ignored": []}, {}, cfg())
    assert plan["action"] == "ask" and plan["kind"] == "incomplete", plan
    return "no ask for a report, a live order or a working agent; ask for a half order"


def test_ask_text_says_what_is_missing() -> str:
    half = bridge.extract_work_orders(HALF)[0]
    routed = {"incomplete": [(half, ["acceptance"])], "duplicate": [], "dispatch": [], "ignored": []}
    ask = watcher.build_ask({"kind": "incomplete"}, routed, HALF)
    assert "missing: acceptance" in ask and "WO-203" in ask, ask
    assert "WO-<next free>" not in ask, "the template still has a placeholder: %s" % ask
    assert "acceptance: how we know it is done" in ask, ask
    assert "file card" in ask, ask

    stall = watcher.build_ask({"kind": "stall"}, routed, PROSE_PROMISE)
    assert "no agent has started" in stall and "I'm fixing all eleven findings" in stall, stall
    assert "id: WO-<next free>" in stall, stall

    dup = watcher.build_ask({"kind": "duplicate"}, {"duplicate": [half]}, HALF)
    assert "already ran" in dup, dup
    return "the ask carries the exact template and only what is missing"


# --- the files that travel with an order -----------------------------------------------

def test_files_resolve_by_absolute_path() -> str:
    d = tmp_env()
    order = {"raw": "goal: g\nuse agent-prompt-8-brief.md and agent-prompt-9-brief.md "
                    "and docs/PROGRESS.md"}
    with open(os.path.join(watcher.DOWNLOADS, "agent-prompt-8-brief.md"), "w") as fh:
        fh.write("brief")
    # the browser sometimes lands a copy as "... (1).md"
    with open(os.path.join(watcher.DOWNLOADS, "agent-prompt-9-brief (1).md"), "w") as fh:
        fh.write("brief 9")
    with open(os.path.join(watcher.DOWNLOADS, "PROGRESS.md"), "w") as fh:
        fh.write("repo file: not an attachment")

    refs = watcher.order_file_refs(order, watcher.DOWNLOADS)
    assert refs == ["agent-prompt-8-brief.md", "agent-prompt-9-brief.md"], refs

    files, missing = watcher.resolve_order_files(order, [], watcher.DOWNLOADS)
    names = sorted(os.path.basename(f["path"]) for f in files)
    assert names == ["agent-prompt-8-brief.md", "agent-prompt-9-brief (1).md"], names
    assert missing == [], missing

    # a name the bridge could not download is handed over as an absolute path
    order2 = {"raw": "goal: g\nread agent-prompt-99-brief.md"}
    files, missing = watcher.resolve_order_files(order2, [], watcher.DOWNLOADS)
    assert files == [] and missing == [os.path.join(watcher.DOWNLOADS, "agent-prompt-99-brief.md")], missing
    return "named files are attached by path (incl. a renamed copy); a missing one is reported"


def test_missing_file_reaches_the_agent() -> str:
    tmp_env()
    order = bridge.extract_work_orders(COMPLETE)[0]
    order["missing_files"] = [os.path.join(watcher.DOWNLOADS, "agent-prompt-99-brief.md")]
    path = watcher.order_to_inbox(order, [])
    body = open(path).read()
    assert "could not download" in body and "agent-prompt-99-brief.md" in body, body
    assert os.path.isdir(watcher.REPORTS), "the report drop is promised, so it must exist"
    assert watcher.report_file(order["id"]) in body, body
    prompt = watcher.build_prompt(order, [], path)
    assert "could NOT download" in prompt and "agent-prompt-99-brief.md" in prompt, prompt
    return "the inbox file and the agent prompt both carry the fallback path"


def test_needs_order_is_loud_and_cleared() -> str:
    tmp_env()
    st = {}
    watcher.write_needs_order(st, "asked 1 time(s) and no order followed",
                              watcher.build_ask({"kind": "stall"}, {}, PROSE_PROMISE), PROSE_PROMISE)
    assert os.path.exists(watcher.NEEDS_ORDER), "no NEEDS-ORDER.md written"
    body = open(watcher.NEEDS_ORDER).read()
    assert "stalled" in body and "one small phase" in body, body
    assert "NEEDS ORDER" in open(watcher.LOG).read()
    watcher.clear_needs_order(st)
    assert not os.path.exists(watcher.NEEDS_ORDER) and "needs_order" not in st
    return "the stalled turn leaves a readable NEEDS-ORDER.md, cleared by a dispatch"


def main() -> int:
    checks = [
        ("order shapes are read", test_shapes_are_read),
        ("prose is not an order", test_prose_is_not_an_order),
        ("an order must be complete", test_completeness),
        ("routing", test_routing),
        ("half an order never starts an agent", test_incomplete_never_starts_an_agent),
        ("dispatch clears the stall flag", test_dispatch_clears_the_stall_flag),
        ("promise detection", test_promise_detection),
        ("ask once, then loud", test_ask_once_then_loud),
        ("attention skips what it must", test_attention_skips_what_it_must),
        ("the ask names what is missing", test_ask_text_says_what_is_missing),
        ("files travel by absolute path", test_files_resolve_by_absolute_path),
        ("a missing file reaches the agent", test_missing_file_reaches_the_agent),
        ("the stall is loud", test_needs_order_is_loud_and_cleared),
    ]
    failed = 0
    for name, fn in checks:
        try:
            detail = fn()
            print("PASS  %-38s %s" % (name, detail))
        except Exception as e:  # noqa: BLE001
            failed += 1
            print("FAIL  %-38s %s" % (name, e))
    print("\n%d/%d proofs passed" % (len(checks) - failed, len(checks)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
