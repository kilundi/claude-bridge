#!/usr/bin/env python3
"""Prove an order is read even after the screen has scrolled past it.

    python3 tests/test_chat_transcript.py

Everything here runs against a fake page - no Chrome, no claude.ai, nothing sent.

On 2026-11-18 the planner was waiting on WO-237 and WO-238, and the watcher had never
seen either.  Both orders were in the chat; what was missing was the *reading*.  The
page renders only the last handful of messages, the watcher read its ``lookback`` of
three, and the message carrying the two orders sat one above that window with three
"another copy of the report" replies below it - so the orders were never extracted,
and no widening of ``lookback`` can help, because lookback only reaches further into
the same handful of messages the page has actually rendered.

What must hold:

  1. with nothing waiting, the screen is enough - no transcript fetch at all
  2. a **held** order whose message is off the screen widens the read, and the order is
     found there (the WO-238 case); prose that merely *names* WO-238 is not the order
  3. the first read of a chat widens, so a chat whose older messages were never read
     cannot hide an order behind its own history
  4. the pass after the bridge window was down widens - an order written while we could
     not read the chat is the one most likely to have scrolled away
  5. a widened read is remembered, so it is not a megabyte fetch every pass
  6. an unreadable transcript changes nothing and says so - the screen is still used
  7. the transcript reader keeps real messages and returns nothing for a bad response
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

# what the screen showed on 2026-11-18: three replies, and *prose* naming both orders
SCREEN = [
    "Yet another copy of the WO-235 report. It is still commit 8619973, and it is closed.",
    "Another copy of the WO-235 report, still commit 8619973. It is closed.",
    "I'm still waiting on the WO-237 and WO-238 reports, WO-239 to be dispatched, and your "
    "three answers (the 20:05 save, the KSh 3 order, hold 1).",
]

# ...and one message above it, the one the screen no longer rendered
ORDER_MESSAGE = """Two consequences. wms-web's generated schema.d.ts is now behind, so it needs
a careful order.

<<<WORK-ORDER
id: WO-238
dir: /home/kilundi/projects/orderflowTamplate/wms-web
branch: wo-238-web-regen-schema-nullable-types
goal: regenerate schema.d.ts against the wms-api contract and drop the workarounds for the
empty-object types.
acceptance: types build, and every remaining empty-object node is gone.
>>>"""

TRANSCRIPT = ["The live-test report is accepted.", ORDER_MESSAGE] + SCREEN


class FakeBridge:
    """A Bridge whose ``transcript`` answers with what the real one returns."""

    def __init__(self, msgs=None, boom=False) -> None:
        self.msgs = list(msgs or [])
        self.boom = boom
        self.calls: list[int] = []

    def transcript(self, c, n=20):
        self.calls.append(n)
        if self.boom:
            raise RuntimeError("the page is not signed in")
        return list(self.msgs)


class FakeCDP:
    def __init__(self, payload) -> None:
        self.payload = payload

    def js(self, expr, timeout=60.0, await_promise=False):
        return self.payload


def tmp_env() -> str:
    d = tempfile.mkdtemp(prefix="chat-transcript-")
    watcher.LOG = os.path.join(d, "watcher.log")
    watcher._SAID.clear()
    return d


def cfg() -> dict:
    return {"watch": {"lookback": 3}}


def read_log() -> str:
    try:
        with open(watcher.LOG) as fh:
            return fh.read()
    except OSError:
        return ""


def msg_hash(text: str) -> str:
    """The same hash the watcher keys its last read on."""
    import hashlib
    return hashlib.sha1((text or "").strip().encode()).hexdigest()[:16]


def ids(msgs: list[str]) -> list[str]:
    return [o["id"] for m in msgs for o in bridge.extract_work_orders(m)]


def test_the_screen_is_enough_when_nothing_is_new() -> str:
    tmp_env()
    fake = FakeBridge(TRANSCRIPT)
    st = {"chat_read": {"at": 1.0, "newest": msg_hash(SCREEN[-1]), "messages": 5}}
    got, note = watcher.widen_read(fake, None, cfg(), st, SCREEN)
    assert got == SCREEN, "with the newest message already read, the screen is all of it"
    assert note == "", note
    assert fake.calls == [], "nothing new was said: no transcript fetch"
    return "the newest message is the one already read: no fetch, screen only"


def test_a_new_message_reads_the_transcript_and_finds_the_lost_order() -> str:
    tmp_env()
    # the trap: the screen *names* WO-238 in prose, which is not the order itself
    assert ids(SCREEN) == [], "prose naming an order must never be read as the order"
    fake = FakeBridge(TRANSCRIPT)
    st: dict = {}
    got, note = watcher.widen_read(fake, None, cfg(), st, SCREEN)
    assert ids(got) == ["WO-238"], "the order off the screen must be read: %r" % ids(got)
    assert fake.calls and fake.calls[0] >= 20, \
        "the transcript must reach deeper than the screen's lookback: %r" % fake.calls
    assert "transcript" in note, note
    assert st["chat_read"]["newest"] == msg_hash(SCREEN[-1]), st["chat_read"]
    return "a new message reads the transcript, and WO-238 off the screen is found there"


def test_a_read_the_screen_still_carries_survives_the_next_pass() -> str:
    tmp_env()
    fake = FakeBridge(TRANSCRIPT)
    st: dict = {}
    watcher.widen_read(fake, None, cfg(), st, SCREEN)            # reads, remembers
    again, note = watcher.widen_read(fake, None, cfg(), st, SCREEN)
    assert len(fake.calls) == 1, "a second pass must not fetch it again: %r" % fake.calls
    assert ids(again) == ["WO-238"], \
        "the kept read must still carry the order: %r" % ids(again)
    assert note == "", note
    return "one fetch per new message, and its result keeps the order readable"


def test_the_first_read_of_a_chat_reads_the_transcript() -> str:
    tmp_env()
    fake = FakeBridge(TRANSCRIPT)
    st: dict = {}                        # a chat whose history was never read
    got, _ = watcher.widen_read(fake, None, cfg(), st, SCREEN)
    assert "WO-238" in ids(got), ids(got)
    assert st["chat_read"]["messages"] == len(TRANSCRIPT), st.get("chat_read")
    return "the first read of a chat goes to the transcript, and records that it did"


def test_the_pass_after_the_window_was_down_reads_it_too() -> str:
    tmp_env()
    fake = FakeBridge(TRANSCRIPT)
    # the window was down: messages arrived, so the newest one is not the one we read
    st = {"chat_read": {"at": 1.0, "newest": msg_hash("something older"), "messages": 5}}
    got, note = watcher.widen_read(fake, None, cfg(), st, SCREEN)
    assert "WO-238" in ids(got), ids(got)
    assert "transcript" in note, note
    return "an order written while the window was down is read when it comes back"


def test_an_unreadable_transcript_leaves_the_screen_alone() -> str:
    tmp_env()
    fake = FakeBridge([])               # the page answered, but with nothing usable
    st = {"chat_read": {"at": 1.0, "newest": "older", "messages": 5}}
    got, note = watcher.widen_read(fake, None, cfg(), st, SCREEN)
    assert got == SCREEN, got
    assert "screen shows" in note, note
    assert "chat_read" not in st or not st["chat_read"].get("at"), st.get("chat_read")
    return "an unusable transcript leaves the screen's messages and says so"


def test_a_transcript_that_raises_is_not_fatal() -> str:
    tmp_env()
    fake = FakeBridge(boom=True)
    st = {"chat_read": {"at": 1.0, "newest": "older", "messages": 5}}
    got, note = watcher.widen_read(fake, None, cfg(), st, SCREEN)
    assert got == SCREEN, got
    assert "raised" in note, note
    return "a transcript that raises leaves the pass working off the screen"


def test_a_broken_transcript_is_not_fetched_every_pass() -> str:
    tmp_env()
    fake = FakeBridge(boom=True)
    st = {"chat_read": {"at": 1.0, "newest": "older", "messages": 5}}
    watcher.widen_read(fake, None, cfg(), st, SCREEN)            # fails once
    watcher.widen_read(fake, None, cfg(), st, SCREEN)            # must not try again
    assert len(fake.calls) == 1, "a broken transcript must not be hammered: %r" % fake.calls
    return "a failed transcript is remembered, so the next pass does not fetch it again"


def test_a_held_order_survives_a_pass_the_screen_barely_shows() -> str:
    tmp_env()
    # a new reply arrives; the screen shows three messages and none of them is WO-238's
    fake = FakeBridge(TRANSCRIPT + ["A brand new reply from the planner."])
    st: dict = {}
    watcher.widen_read(fake, None, cfg(), st, SCREEN)
    later = SCREEN[1:] + ["A brand new reply from the planner."]
    got, _ = watcher.widen_read(fake, None, cfg(), st, later)
    orders = [o["id"] for m in got for o in bridge.extract_work_orders(m)]
    assert "WO-238" in orders, \
        "a held order's message must still be read after the screen moved on: %r" % orders
    assert len(fake.calls) == 2, "one fetch per new message: %r" % fake.calls
    return "the screen moved on, the read still carries WO-238 for a later slot"


def test_prune_marks_keeps_a_held_order_the_read_carries() -> str:
    tmp_env()
    fake = FakeBridge(TRANSCRIPT)
    st: dict = {}
    got, _ = watcher.widen_read(fake, None, cfg(), st, SCREEN)
    st["held"] = {"WO-238": 1794957121.0}
    orders = [o for m in got for o in bridge.extract_work_orders(m)]
    gone = watcher.prune_marks(st, orders, activity=[])
    assert not gone, "the read still carries WO-238, so it is still waiting: %r" % (gone,)
    assert st["held"].get("WO-238"), st.get("held")
    return "a held order the kept read carries is not pruned as gone from the chat"


def test_the_transcript_reader_keeps_only_real_messages() -> str:
    tmp_env()
    b = bridge.Bridge.__new__(bridge.Bridge)     # no page lookup, no Chrome
    got = b.transcript(FakeCDP(json.dumps({"ok": True, "total": 1755, "assistant": 150,
                                           "msgs": ["first", "", "   ", "WO-238 here"]})), 5)
    assert got == ["first", "WO-238 here"], got
    assert b.transcript(FakeCDP("not json"), 5) == []
    assert b.transcript(FakeCDP(json.dumps({"ok": False, "why": "403"}))) == []
    return "the reader hands back the real messages and nothing at all for a bad answer"


def main() -> int:
    checks = [
        ("the screen is enough when nothing is new", test_the_screen_is_enough_when_nothing_is_new),
        ("a new message finds the lost order", test_a_new_message_reads_the_transcript_and_finds_the_lost_order),
        ("a kept read survives the next pass", test_a_read_the_screen_still_carries_survives_the_next_pass),
        ("the first read of a chat", test_the_first_read_of_a_chat_reads_the_transcript),
        ("after the window was down", test_the_pass_after_the_window_was_down_reads_it_too),
        ("an unreadable transcript", test_an_unreadable_transcript_leaves_the_screen_alone),
        ("a transcript that raises", test_a_transcript_that_raises_is_not_fatal),
        ("a broken transcript is not hammered", test_a_broken_transcript_is_not_fetched_every_pass),
        ("a held order survives a later pass", test_a_held_order_survives_a_pass_the_screen_barely_shows),
        ("a carried held order is not pruned", test_prune_marks_keeps_a_held_order_the_read_carries),
        ("the reader keeps real messages", test_the_transcript_reader_keeps_only_real_messages),
    ]
    failed = 0
    for name, fn in checks:
        try:
            detail = fn()
            print("PASS  %-40s %s" % (name, detail))
        except Exception as e:  # noqa: BLE001
            failed += 1
            print("FAIL  %-40s %s" % (name, e))
    print("\n%d/%d proofs passed" % (len(checks) - failed, len(checks)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
