#!/usr/bin/env python3
"""Prove a work order can never be started twice by the watcher.

Runs against a throwaway copy of the state, a temp lock file and a fake
``start_agent`` - it never touches the live watcher, its state or its lock.

    python3 tests/test_watcher_single_start.py

Proves:
  1. two watcher processes started at the same instant -> exactly one lock
     holder and exactly one start of the test order
  2. a process killed between the mark and the start does not re-fire the order
     on restart (it goes to the retry path exactly once)
  3. a failing start_agent is retried at most twice (3 attempts total), then
     stops with status "failed"
  4. an order that already has a started session id in state is never re-started
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import watcher  # noqa: E402

ORDER = {
    "id": "WO-TEST",
    "goal": "prove the watcher starts an order at most once",
    "dir": "/tmp",
    "branch": "",
    "save": "",
    "acceptance": "exactly one start",
    "model": "",
    "raw": "id: WO-TEST\ngoal: prove the watcher starts an order at most once",
}

# a worker run as a *separate* process so the two starts are genuinely racing
WORKER = r'''
import json, os, sys, time
sys.path.insert(0, os.environ["CB_DIR"])
import watcher
watcher.STATE = os.environ["CB_STATE"]
watcher.INBOX = os.environ["CB_INBOX"]
watcher.LOG = os.environ["CB_LOG"]
watcher.LOCK = os.environ["CB_LOCK"]
STARTS = os.environ["CB_STARTS"]

def fake_start(cfg, order, files, inbox_path):
    with open(STARTS, "a") as fh:
        fh.write(order["id"] + "\n")
    return "ses_" + order["id"]

watcher.start_agent = fake_start
watcher.open_agent_terminal = lambda cfg_, oid, sid, directory: False
cfg = {"watch": {"auto_start": True, "start_retries": 2, "start_retry_delay": 0}}
order = json.loads(os.environ["CB_ORDER"])

while not os.path.exists(os.environ["CB_GO"]):   # line up on the same instant
    time.sleep(0.002)

fd = watcher.acquire_watcher_lock()
if fd is None:
    print("nolock", flush=True)
else:
    st = watcher.load_state()
    watcher.handle_orders(cfg, st, [order], files=[])
    time.sleep(1.0)        # hold the lock long enough for the other to fail
    print("lock", flush=True)
'''


def tmp_env() -> str:
    d = tempfile.mkdtemp(prefix="watcher-lock-test-")
    watcher.STATE = os.path.join(d, "state.json")
    watcher.INBOX = os.path.join(d, "inbox")
    watcher.LOG = os.path.join(d, "watcher.log")
    watcher.LOCK = os.path.join(d, "watcher.lock")
    os.makedirs(watcher.INBOX, exist_ok=True)
    # a test must never open a real terminal window on the user's desktop
    watcher.open_agent_terminal = lambda cfg_, oid, sid, directory: False
    return d


def cfg(delay: float = 0) -> dict:
    return {"watch": {"auto_start": True, "start_retries": 2, "start_retry_delay": delay}}


def count_lines(path: str) -> int:
    if not os.path.exists(path):
        return 0
    with open(path) as fh:
        return len([ln for ln in fh if ln.strip()])


def test_simultaneous_start() -> str:
    d = tmp_env()
    worker = os.path.join(d, "worker.py")
    with open(worker, "w") as fh:
        fh.write(WORKER)
    starts = os.path.join(d, "starts.log")
    go = os.path.join(d, "go")

    env = dict(os.environ)
    env.update({
        "CB_DIR": ROOT, "CB_STATE": watcher.STATE, "CB_INBOX": watcher.INBOX,
        "CB_LOG": watcher.LOG, "CB_LOCK": watcher.LOCK, "CB_STARTS": starts,
        "CB_ORDER": json.dumps(ORDER), "CB_GO": go,
    })
    env.pop("WATCHER_LOCK_FD", None)

    procs = [
        subprocess.Popen([sys.executable, worker], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(2)
    ]
    time.sleep(0.6)              # both are lined up on the go file
    open(go, "w").close()        # release them at the same instant
    outs = []
    for p in procs:
        out, err = p.communicate(timeout=60)
        lines = (out or "").strip().splitlines()
        outs.append(lines[-1] if lines else "rc=%s %s" % (p.returncode, (err or "").strip()[:200]))

    holders = outs.count("lock")
    losers = outs.count("nolock")
    n = count_lines(starts)
    assert holders == 1, "expected exactly one lock holder, got %r" % (outs,)
    assert losers == 1, "expected exactly one loser, got %r" % (outs,)
    assert n == 1, "expected exactly one start, got %d" % n
    return "two racing processes -> 1 lock holder, 1 loser, %d start" % n


def test_kill_between_mark_and_start() -> str:
    d = tmp_env()
    starts = os.path.join(d, "starts.log")
    calls: list[str] = []

    def fake_start(cfg_, order, files, inbox_path):
        calls.append(order["id"])
        with open(starts, "a") as fh:
            fh.write(order["id"] + "\n")
        return "ses_" + order["id"]

    watcher.start_agent = fake_start
    # exactly the state left behind when a process is killed after the mark+save
    # and before start_agent runs: seen marked, start record pending
    seed = {
        "orders": {ORDER["id"]: watcher._hash(ORDER["raw"])},
        "starts": {ORDER["id"]: {
            "status": "pending", "attempts": 0, "order": ORDER,
            "inbox": os.path.join(watcher.INBOX, "WO-TEST.md"), "files": [],
        }},
    }
    with open(watcher.STATE, "w") as fh:
        json.dump(seed, fh)

    st = watcher.load_state()
    new = [o for o in [ORDER] if (st.get("orders") or {}).get(o["id"]) != watcher._hash(o["raw"])]
    assert new == [], "the marked order must NOT be treated as new again"
    watcher.handle_orders(cfg(), st, new, files=[])   # a restart
    assert len(calls) == 1, "retry path should recover it exactly once, got %d" % len(calls)

    # a second restart must not fire it again
    st2 = watcher.load_state()
    watcher.handle_orders(cfg(), st2, [], files=[])
    assert len(calls) == 1, "must not re-fire after the retry, got %d" % len(calls)

    rec = json.load(open(watcher.STATE))["starts"][ORDER["id"]]
    assert rec["status"] == "started" and rec.get("session"), rec
    return "killed between mark and start -> 1 recovery start, then 0"


def test_retry_then_fail() -> str:
    d = tmp_env()
    calls: list[str] = []

    def boom(cfg_, order, files, inbox_path):
        calls.append(order["id"])
        raise RuntimeError("boom")

    watcher.start_agent = boom
    st = watcher.load_state()
    watcher.handle_orders(cfg(), st, [ORDER], files=[])
    assert len(calls) == 3, "expected 3 attempts (1 + 2 retries), got %d" % len(calls)

    rec = json.load(open(watcher.STATE))["starts"][ORDER["id"]]
    assert rec["status"] == "failed", rec
    assert rec["attempts"] == 3, rec
    assert "boom" in (rec.get("last_error") or ""), rec

    st2 = watcher.load_state()
    watcher.handle_orders(cfg(), st2, [], files=[])   # another pass
    assert len(calls) == 3, "must stop after the retry budget, got %d" % len(calls)
    return "failing start_agent -> 3 attempts, status failed, then stops"


def test_existing_session_not_restarted() -> str:
    tmp_env()
    calls: list[str] = []
    watcher.start_agent = lambda cfg_, order, files, inbox: (calls.append(order["id"]) or "ses_x")

    st = {
        "orders": {ORDER["id"]: watcher._hash(ORDER["raw"])},
        "sessions": {ORDER["id"]: "ses_existing"},
        "starts": {ORDER["id"]: {"status": "started", "attempts": 1,
                                 "session": "ses_existing", "order": ORDER}},
    }
    with open(watcher.STATE, "w") as fh:
        json.dump(st, fh)

    watcher.handle_orders(cfg(), watcher.load_state(), [ORDER], files=[])
    assert calls == [], "an order with a session id must never be started again"
    return "order with an existing session id -> 0 starts"


def main() -> int:
    checks = [
        ("simultaneous start", test_simultaneous_start),
        ("kill between mark and start", test_kill_between_mark_and_start),
        ("failing start retried then stopped", test_retry_then_fail),
        ("existing session not restarted", test_existing_session_not_restarted),
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
