#!/usr/bin/env python3
"""Prove a queued report is delivered once, and never multiplied by a blocked composer.

    python3 tests/test_outbox_delivery.py

Everything here runs against fakes - no Chrome, no claude.ai, no messages sent.

On 2026-11-18 the outbox held **132 copies** of WO-235's report, written one per pass
(23:52 to 01:00), and the planner answered "another copy of the WO-235 report" three
times over before asking us to stop.  The copies were not the watcher queueing the
report again - that happened once - but every *refused* send: the composer held
somebody's own draft, ``_ask_with`` kept the text by queueing it, the flush did not
remove the file it had failed to deliver, and the next pass queued one more copy of the
same words.

So what must hold:

  1. queueing the same message twice keeps one copy (and returns the path already
     waiting, so a caller's "saved in ..." points at the real file)
  2. two *different* messages are still two - nothing is lost to the de-duplication
  3. a composer that holds a draft, pass after pass, never grows the queue: five
     refused sends leave exactly the one copy that was already there
  4. a blocked flush stops and says why instead of raising, and the message stays
     queued (it is not thrown away)
  5. once the composer is clear the queued report goes out exactly once, is removed
     from the queue, and its body is the report, not the header
"""
from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import bridge  # noqa: E402

REPORT = ("DONE\n\n- project: wms-api\n- branch: wo-235-wms-api-reset-on-save\n"
          "- commits: 8619973 (pushed)\n\nAll acceptance checks pass.")


def tmp_outbox() -> str:
    """Point the outbox at a scratch directory: never the live one."""
    d = tempfile.mkdtemp(prefix="outbox-")
    bridge.OUTBOX = os.path.join(d, "outbox")
    os.makedirs(bridge.OUTBOX, exist_ok=True)
    return bridge.OUTBOX


def queued() -> list[str]:
    return bridge.outbox_files()


def bodies() -> list[str]:
    return [bridge.read_queued(p) for p in queued()]


class FakeAsk:
    """A Bridge whose ``ask`` does what a real blocked one does, or delivers.

    ``blocked`` makes it raise exactly what ``_ask_with`` raises for a composer that
    holds somebody's draft; a real ``send`` is not needed to prove the queue's
    behaviour, and ``flush_outbox`` only ever calls ``ask``.
    """

    def __init__(self, blocked: bool = False):
        self.blocked = blocked
        self.asked: list[str] = []

    def ask(self, text: str, timeout: float = 0, images=None) -> str:
        self.asked.append(text)
        if self.blocked:
            # what the real path does: keep the words, then say the box was in the way
            path = bridge.queue_message(text, "the composer held a draft")
            raise bridge.ComposerBlocked(
                "the bridge composer is not empty - it holds text ('a half-written "
                "thought')", queued=path)
        return "delivered"


def as_bridge(fake: FakeAsk) -> bridge.Bridge:
    """A Bridge object with only ``ask`` replaced - nothing else is touched."""
    b = bridge.Bridge.__new__(bridge.Bridge)
    b.ask = fake.ask                       # type: ignore[method-assign]
    return b


def test_the_same_message_is_queued_once() -> str:
    tmp_outbox()
    first = bridge.queue_message(REPORT, "the report of WO-235")
    assert len(queued()) == 1, queued()
    again = bridge.queue_message(REPORT, "the composer held a draft")
    assert len(queued()) == 1, "the same words must not be queued twice: %r" % (queued(),)
    assert again == first, "the second call must point at the copy already waiting: %r" % (again,)
    # the header may be re-stamped, but the *body* the planner reads is unchanged
    assert bodies() == [REPORT], bodies()
    return "the same message queued twice is one copy, and the caller is told where it is"


def test_two_different_messages_are_still_two() -> str:
    tmp_outbox()
    bridge.queue_message(REPORT, "the report of WO-235")
    bridge.queue_message("DONE\n\n- project: abacus\n- branch: main\n", "another order")
    assert len(queued()) == 2, "de-duplication must not swallow a different report: %r" % (queued(),)
    open(os.path.join(bridge.OUTBOX, "an-empty-one.md"), "w").close()
    assert bridge.queue_message("", "nothing at all") != ""
    return "two different reports stay two, and an empty message does not match them"


def test_a_blocked_composer_never_grows_the_queue() -> str:
    """The regression: five passes over a composer that holds somebody's draft."""
    tmp_outbox()
    bridge.queue_message(REPORT, "the report of WO-235")
    assert len(queued()) == 1, queued()
    for _ in range(5):                      # five passes, i.e. five refused sends
        fake = FakeAsk(blocked=True)
        as_bridge(fake).flush_outbox(timeout=0.1)
        assert len(queued()) == 1, (
            "pass %d left %d copies - this is the 132-file bug: %r"
            % (_, len(queued()), queued()))
    assert bodies() == [REPORT], bodies()
    return "five refused sends leave exactly the one copy that was already queued"


def test_a_blocked_flush_stops_and_says_why() -> str:
    tmp_outbox()
    queued_path = bridge.queue_message(REPORT, "the report of WO-235")
    fake = FakeAsk(blocked=True)
    res = as_bridge(fake).flush_outbox(timeout=0.1)
    assert res["sent"] == [], res
    assert res["remaining"] == [queued_path], res
    assert "composer is not empty" in (res["stopped"] or ""), res
    assert len(queued()) == 1 and os.path.exists(queued_path), "never thrown away"
    return "a blocked flush stops with the reason, and the report stays queued"


def test_a_clear_composer_delivers_it_once() -> str:
    tmp_outbox()
    bridge.queue_message(REPORT, "the report of WO-235")
    fake = FakeAsk()
    res = as_bridge(fake).flush_outbox(timeout=0.1)
    assert len(res["sent"]) == 1 and res["remaining"] == [], res
    assert queued() == [], "a delivered report leaves the queue: %r" % (queued(),)
    assert fake.asked == [REPORT], (
        "the planner must get the report, not the queue header: %r" % (fake.asked,))
    # and delivering it does not put it back
    assert as_bridge(FakeAsk()).flush_outbox(timeout=0.1)["sent"] == []
    return "once the box is clear the report goes out exactly once, and the queue is empty"


def main() -> int:
    checks = [
        ("the same message is queued once", test_the_same_message_is_queued_once),
        ("different messages stay different", test_two_different_messages_are_still_two),
        ("a blocked composer grows nothing", test_a_blocked_composer_never_grows_the_queue),
        ("a blocked flush stops, keeps it", test_a_blocked_flush_stops_and_says_why),
        ("a clear composer sends it once", test_a_clear_composer_delivers_it_once),
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
