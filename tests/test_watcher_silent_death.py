#!/usr/bin/env python3
"""Prove a dead session is reported once, and a reported order is never re-dispatched.

Runs against a throwaway copy of the state, a temp claims/reports directory and a fake
``start_agent`` - it never touches the live watcher, its state, its inbox, its lock, its
claims or its reports, and it opens no terminal and connects to nothing.

    python3 tests/test_watcher_silent_death.py

Proves:
  1. an agent session that dies with no report gives exactly ONE failure report, only
     after the grace period (not before), and the claim is released
  2. a normal report that arrives *inside* the grace gives no failure report
  3. a repeated block for an id that is in the ledger is logged and ignored - no agent
  4. ``requeue`` clears the ledger entry (and the failure report) so the order runs once
  5. the ledger lives in state.json, so it survives a simulated restart
  6. an agent whose pid is still alive is never called a silent death
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import watcher  # noqa: E402

ORDER = {
    "id": "WO-DEAD",
    "goal": "prove a session that ended without a report is reported exactly once",
    "dir": "/tmp",
    "branch": "",
    "save": "",
    "acceptance": "one failure report, after the grace",
    "model": "",
    "raw": "id: WO-DEAD\ngoal: prove a silent death is reported once",
}


def tmp_env() -> str:
    d = tempfile.mkdtemp(prefix="watcher-silent-death-test-")
    watcher.STATE = os.path.join(d, "state.json")
    watcher.INBOX = os.path.join(d, "inbox")
    watcher.LOG = os.path.join(d, "watcher.log")
    watcher.LOCK = os.path.join(d, "watcher.lock")
    watcher.CLAIMS = os.path.join(d, "claims")
    watcher.REPORTS = os.path.join(d, "reports")
    watcher.NEEDS_ORDER = os.path.join(d, "NEEDS-ORDER.md")
    for p in (watcher.INBOX, watcher.CLAIMS, watcher.REPORTS):
        os.makedirs(p, exist_ok=True)
    # a proof must never open a real terminal window, read a real agent transcript or see a
    # real tmux window; nor write to the real outbox
    watcher.open_agent_terminal = lambda cfg_, oid, sid, directory: False
    watcher.session_activity = lambda cfg_, st_, now=None: []
    watcher.agent_windows = lambda *a, **k: []
    watcher._SAID.clear()
    return d


def cfg(grace: float = 90.0) -> dict:
    return {"watch": {"auto_start": True, "start_retries": 2, "start_retry_delay": 0,
                      "min_free_mb": 0, "silent_death_grace_seconds": grace}}


class Dead:
    """Stub ``_pid_alive`` so no proof depends on a real pid or a real reboot."""

    def __enter__(self):
        self.real = watcher._pid_alive
        watcher._pid_alive = lambda pid: False
        return self

    def __exit__(self, *exc):
        watcher._pid_alive = self.real
        return False


def seed(state: dict) -> None:
    with open(watcher.STATE, "w") as fh:
        json.dump(state, fh)


def order(oid: str) -> dict:
    """A complete order for a given id (the seed id and the routed order must agree)."""
    o = dict(ORDER)
    o["id"] = oid
    o["raw"] = "id: %s\ngoal: prove a silent death is reported once" % oid
    return o


def repost(oid: str) -> dict:
    """The same id, posted again with *different* text - what re-dispatched WO-236/238/249."""
    o = order(oid)
    o["goal"] = "the same id, posted again with different text"
    o["raw"] = o["raw"] + "\n# posted again"
    return o


def seed_started(oid: str, t0: float) -> None:
    """The state a started order leaves: a session, a start record, no report."""
    o = order(oid)
    seed({
        "orders": {oid: watcher._hash(o["raw"])},
        "sessions": {oid: "ses_" + oid},
        "starts": {oid: {"status": "started", "attempts": 1, "session": "ses_" + oid,
                         "order": o, "inbox": os.path.join(watcher.INBOX, "%s.md" % oid),
                         "files": []}},
    })


def write_dead_claim(oid: str, t0: float, pid: int = 999999) -> str:
    path = os.path.join(watcher.CLAIMS, "%s.lock" % watcher.window_name(oid))
    with open(path, "w") as fh:
        json.dump({"oid": oid, "state": "started", "starter_pid": pid, "pid": pid,
                   "window": watcher.window_name(oid), "session": "ses_" + oid,
                   "started": t0}, fh)
    return path


def read_report(oid: str) -> str:
    with open(watcher.report_file(oid)) as fh:
        return fh.read()


# --- 1 ---------------------------------------------------------------------------------
def test_silent_death_one_report_after_grace() -> str:
    tmp_env()
    oid = "WO-DEAD"
    t0 = 1_000_000.0
    claim = write_dead_claim(oid, t0)
    seed_started(oid, t0)
    queued: list[str] = []
    watcher.queue_message = lambda text, reason="": (queued.append(text) or "/tmp/fake-outbox.md")

    with Dead():
        assert watcher.detect_silent_deaths(cfg(), watcher.load_state(), now=t0 + 10) == []
        assert not os.path.exists(watcher.report_file(oid)), "no report inside the grace"
        # 89 s after it was first seen gone: still inside the 90 s grace
        assert watcher.detect_silent_deaths(cfg(), watcher.load_state(), now=t0 + 10 + 89) == []
        assert not os.path.exists(watcher.report_file(oid)), "no report before the grace is up"
        # 91 s after: exactly one failure report, and the claim is released
        got = watcher.detect_silent_deaths(cfg(), watcher.load_state(), now=t0 + 10 + 91)
        assert got == [oid], "expected exactly one failure report, got %r" % got
        # a later pass writes nothing more
        assert watcher.detect_silent_deaths(cfg(), watcher.load_state(), now=t0 + 999) == []

    body = read_report(oid)
    assert body.splitlines()[0] == watcher.FAILURE_MARKER, body.splitlines()[:1]
    assert oid in body and "watcher.log" in body, "the report names the order and the log file"
    assert "--requeue %s" % oid in body, "the report gives the requeue command"
    assert not os.path.exists(claim), "the claim must be released"
    assert queued and "FAILURE" in queued[0], "the failure report goes out the report path"
    assert watcher.ledger_has(watcher.load_state(), oid), "the death is remembered as failed"
    return "dead session -> 0 reports before the grace, exactly 1 after, claim released"


# --- 2 ---------------------------------------------------------------------------------
def test_report_inside_grace_no_failure() -> str:
    tmp_env()
    oid = "WO-IN-GRACE"
    t0 = 2_000_000.0
    write_dead_claim(oid, t0)
    seed_started(oid, t0)
    watcher.queue_message = lambda text, reason="": "/tmp/fake-outbox.md"

    with Dead():
        watcher.detect_silent_deaths(cfg(), watcher.load_state(), now=t0 + 5)
        assert not os.path.exists(watcher.report_file(oid))
        # the agent writes its own report while the grace is still running
        with open(watcher.report_file(oid), "w") as fh:
            fh.write("DONE\n\n- project: x\n- branch: y\n")
        assert watcher.detect_silent_deaths(cfg(), watcher.load_state(), now=t0 + 30) == []
        watcher.detect_silent_deaths(cfg(), watcher.load_state(), now=t0 + 999)

    body = read_report(oid)
    assert body.startswith("DONE\n"), "a report inside the grace is left exactly as it was"
    assert not watcher.ledger_has(watcher.load_state(), oid), \
        "an order that reported is not recorded as failed"
    return "report inside the grace -> no failure report, the real report is untouched"


# --- 3 ---------------------------------------------------------------------------------
def test_ledger_blocks_repeat_block_and_logs() -> str:
    tmp_env()
    oid = "WO-LEDGER"
    o = order(oid)
    seed({
        "orders": {oid: watcher._hash(o["raw"])},
        "sessions": {oid: "ses_" + oid},
        "starts": {oid: {"status": "started", "attempts": 1, "session": "ses_" + oid,
                         "order": o}},
        "finished": {oid: time.time()},
        "ledger": {oid: {"status": "reported", "at": time.time(), "why": "it reported"}},
    })
    starts: list[str] = []
    watcher.start_agent = lambda cfg_, order_, files, inbox: (
        starts.append(order_["id"]) or "ses_new")

    st = watcher.load_state()
    routed = watcher.route_orders(st, [repost(oid)])
    assert routed["dispatch"] == [], "a ledger id must not be dispatchable: %r" % routed
    assert [x["id"] for x in routed.get("ledger") or []] == [oid], routed
    watcher.handle_orders(cfg(), st, routed["dispatch"], files=[])

    # and directly, the guard inside handle_orders must refuse it too
    watcher.handle_orders(cfg(), watcher.load_state(), [repost(oid)], files=[])
    assert starts == [], "a repeated block for a ledger id must start nothing, got %r" % starts
    with open(watcher.LOG) as fh:
        logged = fh.read()
    assert oid in logged and "ledger" in logged.lower(), logged[-500:]
    return "ledger id -> 0 starts, logged and ignored"


# --- 4 ---------------------------------------------------------------------------------
def test_requeue_clears_ledger_and_runs_once() -> str:
    tmp_env()
    oid = "WO-REQUEUE"
    o = order(oid)
    # the state a silent death leaves: a failure report, a dead session, the ledger
    with open(watcher.report_file(oid), "w") as fh:
        fh.write("%s\n\n- order: %s\n" % (watcher.FAILURE_MARKER, oid))
    seed({
        "orders": {oid: watcher._hash(o["raw"])},
        "sessions": {oid: "ses_" + oid},
        "starts": {oid: {"status": "failed", "attempts": 3, "session": "ses_" + oid,
                         "order": o}},
        "finished": {oid: time.time()},
        "ledger": {oid: {"status": "failed", "at": time.time(), "why": "no report"}},
    })
    starts: list[str] = []
    watcher.start_agent = lambda cfg_, order_, files, inbox: (
        starts.append(order_["id"]) or "ses_new")

    assert watcher.route_orders(watcher.load_state(), [repost(oid)])["ledger"], \
        "before requeue the block is still ignored"

    st = watcher.load_state()
    assert watcher.requeue_order(st, oid) is True, "requeue must find the record"
    after = watcher.load_state()
    assert not watcher.ledger_has(after, oid), "requeue must clear the ledger entry"
    assert not os.path.exists(watcher.report_file(oid)), "the failure report must be cleared"
    assert [x["id"] for x in watcher.route_orders(after, [repost(oid)])["dispatch"]] == [oid], \
        "after requeue the block is dispatchable again"

    # the very next pass starts it once...
    watcher.handle_orders(cfg(), watcher.load_state(), [], files=[])
    assert starts == [oid], "requeue must let the order run, once: %r" % starts
    # ...and the pass after that starts nothing more
    watcher.handle_orders(cfg(), watcher.load_state(), [], files=[])
    assert starts == [oid], "requeue must let it run exactly once: %r" % starts
    return "requeue -> ledger cleared, failure report cleared, exactly one run"


# --- 5 ---------------------------------------------------------------------------------
def test_ledger_survives_restart() -> str:
    tmp_env()
    oid = "WO-PERSIST"
    o = order(oid)
    seed({"orders": {}, "sessions": {}, "starts": {}})
    with open(watcher.report_file(oid), "w") as fh:
        fh.write("DONE\n\n- project: x\n- branch: y\n")

    watcher.handle_orders(cfg(), watcher.load_state(), [o], files=[])
    # a fresh process reading the file - a restart - still sees the entry
    on_disk = json.load(open(watcher.STATE))
    assert on_disk.get("ledger", {}).get(oid, {}).get("status") == "reported", on_disk.get("ledger")
    assert watcher.ledger_has(watcher.load_state(), oid)
    return "the ledger is in state.json and survives a restart"


# --- 6 ---------------------------------------------------------------------------------
def test_live_agent_is_not_a_silent_death() -> str:
    tmp_env()
    oid = "WO-ALIVE"
    t0 = 3_000_000.0
    write_dead_claim(oid, t0, pid=4242)
    seed_started(oid, t0)
    watcher._pid_alive = lambda pid: pid == 4242          # the agent is still running

    assert watcher.detect_silent_deaths(cfg(), watcher.load_state(), now=t0 + 10_000) == []
    assert not os.path.exists(watcher.report_file(oid))
    assert oid not in (watcher.load_state().get("deaths") or {})
    return "a live pid is never called a silent death"


def main() -> int:
    checks = [
        ("silent death reports once, after the grace", test_silent_death_one_report_after_grace),
        ("report inside the grace is not overridden", test_report_inside_grace_no_failure),
        ("a ledger id is ignored and logged", test_ledger_blocks_repeat_block_and_logs),
        ("requeue clears the ledger and runs once", test_requeue_clears_ledger_and_runs_once),
        ("the ledger survives a restart", test_ledger_survives_restart),
        ("a live agent is not a silent death", test_live_agent_is_not_a_silent_death),
    ]
    failed = 0
    for name, fn in checks:
        try:
            detail = fn()
            print("PASS  %-44s %s" % (name, detail))
        except Exception as e:  # noqa: BLE001
            failed += 1
            print("FAIL  %-44s %s" % (name, e))
    print("\n%d/%d proofs passed" % (len(checks) - failed, len(checks)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
