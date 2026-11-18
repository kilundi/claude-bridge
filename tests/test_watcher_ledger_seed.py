#!/usr/bin/env python3
"""Prove the startup seed freezes the reports already on disk into the ledger (WO-254).

Runs against a throwaway copy of the state, a temp claims/reports directory and a fake
``start_agent`` - it never touches the live watcher, its state, its inbox, its lock, its
claims or its reports, and it opens no terminal and connects to nothing.

    python3 tests/test_watcher_ledger_seed.py

Proves:
  1. the reports already on disk get ledger entries - ``failed`` for a FAILURE report,
     ``reported`` for a normal one - and the seed is idempotent
  2. the first pass seeds once and remembers it: a second start adds nothing to the ledger
  3. a repeated block for a seeded id whose report mtime is older than SELF_DONE_MAX_AGE
     is ignored and logged and starts nothing (without the seed it would be dispatched)
  4. an id with a live claim is not seeded; every other report still is
  5. ``requeue`` of a seeded id clears the entry and the failure report and runs it once
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
    "id": "WO-SEED",
    "goal": "prove the ledger seed covers the reports already on disk",
    "dir": "/tmp",
    "branch": "",
    "save": "",
    "acceptance": "a seeded id is never dispatched again",
    "model": "",
    "raw": "id: WO-SEED\ngoal: prove the ledger seed covers the reports already on disk",
}


def tmp_env() -> str:
    d = tempfile.mkdtemp(prefix="watcher-ledger-seed-test-")
    watcher.STATE = os.path.join(d, "state.json")
    watcher.INBOX = os.path.join(d, "inbox")
    watcher.LOG = os.path.join(d, "watcher.log")
    watcher.LOCK = os.path.join(d, "watcher.lock")
    watcher.CLAIMS = os.path.join(d, "claims")
    watcher.REPORTS = os.path.join(d, "reports")
    watcher.NEEDS_ORDER = os.path.join(d, "NEEDS-ORDER.md")
    for p in (watcher.INBOX, watcher.CLAIMS, watcher.REPORTS):
        os.makedirs(p, exist_ok=True)
    watcher.open_agent_terminal = lambda cfg_, oid, sid, directory: False
    watcher.session_activity = lambda cfg_, st_, now=None: []
    watcher.agent_windows = lambda *a, **k: []
    watcher._pid_alive = lambda pid: False
    watcher._SAID.clear()
    return d


def cfg() -> dict:
    return {"watch": {"auto_start": True, "start_retries": 2, "start_retry_delay": 0,
                      "min_free_mb": 0}}


def order(oid: str) -> dict:
    o = dict(ORDER)
    o["id"] = oid
    o["raw"] = "id: %s\ngoal: prove the ledger seed covers the reports already on disk" % oid
    return o


def repost(oid: str) -> dict:
    """The same id, posted again with different text - what re-dispatched WO-236/238/249."""
    o = order(oid)
    o["goal"] = "the same id, posted again with different text"
    o["raw"] = o["raw"] + "\n# posted again"
    return o


def write_report(oid: str, body: str = "DONE\n\n- project: x\n- branch: y\n") -> str:
    path = watcher.report_file(oid)
    with open(path, "w") as fh:
        fh.write(body)
    return path


def write_claim(oid: str, pid: int = 4242) -> str:
    path = watcher.claim_path(oid)
    with open(path, "w") as fh:
        json.dump({"oid": oid, "state": "started", "pid": pid, "starter_pid": pid,
                   "window": watcher.window_name(oid), "session": "ses_" + oid,
                   "started": time.time()}, fh)
    return path


def seed(state: dict) -> None:
    with open(watcher.STATE, "w") as fh:
        json.dump(state, fh)


# --- 1 ---------------------------------------------------------------------------------
def test_reports_on_disk_get_ledger_entries() -> str:
    tmp_env()
    good = write_report("WO-REP")
    bad = write_report("WO-FAIL", "%s\n\n- order: WO-FAIL\n" % watcher.FAILURE_MARKER)

    st = watcher.load_state()
    seeded = watcher.seed_ledger_from_reports(st)
    assert sorted(seeded) == ["WO-FAIL", "WO-REP"], seeded
    assert watcher.ledger_get(st, "WO-REP")["status"] == "reported", st.get("ledger")
    assert watcher.ledger_get(st, "WO-FAIL")["status"] == "failed", st.get("ledger")

    # idempotent: the second call adds nothing
    assert watcher.seed_ledger_from_reports(st) == [], "the seed must be idempotent"
    # and it never touched the report files
    assert os.path.exists(good) and os.path.exists(bad), "the seed must not move a report"
    assert open(good).read().startswith("DONE"), "the seed must not edit a report"
    return "2 reports -> ledger entries (reported + failed), idempotent, files untouched"


# --- 2 ---------------------------------------------------------------------------------
def test_first_pass_seeds_once_and_remembers() -> str:
    tmp_env()
    write_report("WO-FIRST")
    first = watcher.seed_ledger_once(watcher.load_state())
    assert first == ["WO-FIRST"], first
    on_disk = json.load(open(watcher.STATE))
    assert on_disk.get(watcher.LEDGER_SEEDED_KEY), "the seed marker must be persisted"
    assert watcher.ledger_has(watcher.load_state(), "WO-FIRST")

    # a fresh start (a new process reading the file) does not seed again - even a report
    # that appeared after the first pass is left to the normal routes, not re-seeded
    write_report("WO-SECOND")
    second = watcher.seed_ledger_once(watcher.load_state())
    assert second == [], "a second start must seed nothing, got %r" % second
    assert not watcher.ledger_has(watcher.load_state(), "WO-SECOND")
    return "first pass seeds and records the marker; a second start seeds nothing"


# --- 3 ---------------------------------------------------------------------------------
def test_old_report_block_is_ignored_and_logged() -> str:
    tmp_env()
    oid = "WO-OLD"
    path = write_report(oid)
    old = time.time() - 2 * watcher.SELF_DONE_MAX_AGE        # older than the 24 h rule
    os.utime(path, (old, old))

    # without the seed the block is dispatchable: this is the gap WO-254 closes
    assert [x["id"] for x in watcher.route_orders({}, [repost(oid)])["dispatch"]] == [oid], \
        "the old report alone would leave the id dispatchable (the seed is the fix)"

    st = watcher.load_state()
    watcher.seed_ledger_from_reports(st)
    watcher.save_state(st)
    st = watcher.load_state()
    st.setdefault("orders", {})[oid] = watcher._hash(order(oid)["raw"])   # a repeated block

    routed = watcher.route_orders(st, [repost(oid)])
    assert routed["dispatch"] == [], "a seeded id must not be dispatchable: %r" % routed
    assert [x["id"] for x in routed.get("ledger") or []] == [oid], routed

    starts: list[str] = []
    watcher.start_agent = lambda cfg_, order_, files, inbox: (
        starts.append(order_["id"]) or "ses_new")
    watcher.handle_orders(cfg(), watcher.load_state(), routed["dispatch"], files=[])
    # and the direct guard inside handle_orders refuses it too
    watcher.handle_orders(cfg(), watcher.load_state(), [repost(oid)], files=[])
    assert starts == [], "a seeded id must start nothing, got %r" % starts
    with open(watcher.LOG) as fh:
        logged = fh.read()
    assert oid in logged and "ledger" in logged.lower(), logged[-500:]
    return "old report -> seeded -> repeated block ignored and logged, 0 starts"


# --- 4 ---------------------------------------------------------------------------------
def test_live_claim_is_not_seeded() -> str:
    tmp_env()
    running = "WO-RUNNING"
    write_report(running)                     # the agent already left a report...
    write_claim(running)                      # ...but it is still on the order
    write_report("WO-DONE")                   # an ordinary finished order
    watcher._pid_alive = lambda pid: pid == 4242

    st = watcher.load_state()
    seeded = watcher.seed_ledger_from_reports(st)
    assert seeded == ["WO-DONE"], "a live claim must be left alone, got %r" % seeded
    assert not watcher.ledger_has(st, running), "a live claim must not be seeded"
    assert watcher.ledger_has(st, "WO-DONE")
    return "a live claim is skipped; every other report still seeds"


# --- 5 ---------------------------------------------------------------------------------
def test_requeue_of_seeded_id_runs_once() -> str:
    tmp_env()
    oid = "WO-REQ"
    write_report(oid, "%s\n\n- order: %s\n" % (watcher.FAILURE_MARKER, oid))
    st = watcher.load_state()
    watcher.seed_ledger_from_reports(st)
    watcher.save_state(st)
    assert watcher.ledger_get(watcher.load_state(), oid)["status"] == "failed"

    starts: list[str] = []
    watcher.start_agent = lambda cfg_, order_, files, inbox: (
        starts.append(order_["id"]) or "ses_new")
    assert [x["id"] for x in watcher.route_orders(watcher.load_state(), [repost(oid)])
            .get("ledger") or []] == [oid], "before requeue the block is still ignored"

    assert watcher.requeue_order(watcher.load_state(), oid) is True, "requeue must find it"
    after = watcher.load_state()
    assert not watcher.ledger_has(after, oid), "requeue must clear the seeded entry"
    assert not os.path.exists(watcher.report_file(oid)), "the failure report must be cleared"
    routed = watcher.route_orders(after, [repost(oid)])
    assert [x["id"] for x in routed["dispatch"]] == [oid], \
        "after requeue a seeded id is dispatchable again: %r" % routed

    watcher.handle_orders(cfg(), watcher.load_state(), routed["dispatch"], files=[])
    assert starts == [oid], "requeue of a seeded id must run it, once: %r" % starts
    watcher.handle_orders(cfg(), watcher.load_state(), [], files=[])
    assert starts == [oid], "requeue of a seeded id must run it exactly once: %r" % starts
    return "seeded failure report -> requeue -> ledger cleared, runs exactly once"


def main() -> int:
    checks = [
        ("reports on disk seed the ledger", test_reports_on_disk_get_ledger_entries),
        ("the first pass seeds once and remembers", test_first_pass_seeds_once_and_remembers),
        ("an old report's block is ignored and logged", test_old_report_block_is_ignored_and_logged),
        ("a live claim is not seeded", test_live_claim_is_not_seeded),
        ("requeue of a seeded id runs once", test_requeue_of_seeded_id_runs_once),
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
