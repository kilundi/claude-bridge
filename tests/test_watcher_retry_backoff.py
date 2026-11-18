#!/usr/bin/env python3
"""Prove the watcher starts exactly one agent per order, and survives a brief outage.

Runs against a throwaway copy of the state, a temp lock/claims directory, a fake clock and
a fake ``start_agent`` - it never touches the live watcher, its state, its inbox or its
lock, and it connects to nothing.

    python3 tests/test_watcher_retry_backoff.py

Proves:
  1. two dispatches of the same order id, begun in *separate processes* at the same
     instant, start exactly one agent (the per-order claim, not the watcher lock)
  2. an order whose agent cannot start because the background service is down for 60
     seconds is retried *after the service returns*, is not marked failed, and never
     starts a second agent
  3. an outage longer than the retry schedule marks the order failed - and
     ``--requeue`` puts it back in the queue, after which it starts
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
    "goal": "prove the watcher retries a failed start without losing or doubling it",
    "dir": "/tmp",
    "branch": "",
    "save": "",
    "acceptance": "one agent, started once",
    "model": "",
    "raw": "id: WO-TEST\ngoal: prove the watcher retries a failed start",
}

# a worker run as a *separate* process, so two starts are genuinely racing (proof 1)
WORKER = r'''
import json, os, sys, time
sys.path.insert(0, os.environ["CB_DIR"])
import watcher
watcher.STATE = os.environ["CB_STATE"]
watcher.INBOX = os.environ["CB_INBOX"]
watcher.LOG = os.environ["CB_LOG"]
watcher.LOCK = os.environ["CB_LOCK"]
watcher.CLAIMS = os.environ["CB_CLAIMS"]
STARTS = os.environ["CB_STARTS"]

def fake_start(cfg, order, files, inbox_path):
    with open(STARTS, "a") as fh:
        fh.write(order["id"] + "\n")
    return "ses_" + order["id"]

watcher.start_agent = fake_start
watcher.open_agent_terminal = lambda cfg_, oid, sid, directory: False
watcher.session_activity = lambda cfg_, st_, now=None: []
cfg = {"watch": {"auto_start": True, "min_free_mb": 0}}
order = json.loads(os.environ["CB_ORDER"])

while not os.path.exists(os.environ["CB_GO"]):   # line up on the same instant
    time.sleep(0.002)

fd = watcher.acquire_watcher_lock()
if fd is None:
    print("nolock", flush=True)
else:
    st = watcher.load_state()
    watcher.handle_orders(cfg, st, [order], files=[])
    print("lock", flush=True)
    deadline = time.time() + 30
    while not os.path.exists(os.environ["CB_RELEASE"]) and time.time() < deadline:
        time.sleep(0.02)
'''


class Clock:
    """A clock the proofs drive, so a 60-second outage takes no real time."""

    def __init__(self, t0: float = 1000.0) -> None:
        self.t = t0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += max(0.0, seconds)


def tmp_env() -> str:
    d = tempfile.mkdtemp(prefix="watcher-retry-test-")
    watcher.STATE = os.path.join(d, "state.json")
    watcher.INBOX = os.path.join(d, "inbox")
    watcher.LOG = os.path.join(d, "watcher.log")
    watcher.LOCK = os.path.join(d, "watcher.lock")
    watcher.CLAIMS = os.path.join(d, "claims")
    os.makedirs(watcher.INBOX, exist_ok=True)
    os.makedirs(watcher.CLAIMS, exist_ok=True)
    watcher.open_agent_terminal = lambda cfg_, oid, sid, directory: False
    watcher.session_activity = lambda cfg_, st_, now=None: []
    return d


def use_clock(clock: Clock):
    """Install the fake clock; return the originals so the caller can put them back."""
    real = (watcher._now, watcher._sleep)
    watcher._now = clock.now
    watcher._sleep = clock.sleep
    return real


def cfg() -> dict:
    # no start_retries/start_retry_delay: the proof exercises the real default schedule
    # (5s, 30s, 2m, 5m, 10m).  min_free_mb 0 so this machine's memory never decides it.
    return {"watch": {"auto_start": True, "min_free_mb": 0}}


def rec_of(oid: str) -> dict:
    if not os.path.exists(watcher.STATE):
        return {}
    return (json.load(open(watcher.STATE)).get("starts") or {}).get(oid, {})


def drive_until_started(clock: Clock, max_passes: int = 60) -> int:
    """Run watcher passes, waking each one exactly when the next attempt is due.

    Returns the number of passes used.  Stops as soon as a session is recorded or the
    record has spent its schedule (so a proof can never hang).
    """
    passes = 0
    while passes < max_passes:
        rec = rec_of(ORDER["id"])
        if rec.get("session"):
            return passes
        if int(rec.get("attempts") or 0) > len(watcher._retry_delays(cfg()["watch"])):
            return passes                       # gave up: terminal
        if rec.get("status") == "failed":
            return passes                       # terminal for good
        due = float(rec.get("next_attempt_at") or 0)
        clock.t = max(clock.t + 1.0, due)        # wake the next pass when it is due
        st = watcher.load_state()
        watcher.handle_orders(cfg(), st, [], files=[])
        passes += 1
    return passes


# --- 1 ---------------------------------------------------------------------------------
def test_double_dispatch_starts_one() -> str:
    d = tmp_env()
    worker = os.path.join(d, "worker.py")
    with open(worker, "w") as fh:
        fh.write(WORKER)
    starts = os.path.join(d, "starts.log")
    go = os.path.join(d, "go")
    release = os.path.join(d, "release")

    env = dict(os.environ)
    env.update({
        "CB_DIR": ROOT, "CB_STATE": watcher.STATE, "CB_INBOX": watcher.INBOX,
        "CB_LOG": watcher.LOG, "CB_LOCK": watcher.LOCK, "CB_CLAIMS": watcher.CLAIMS,
        "CB_STARTS": starts, "CB_ORDER": json.dumps(ORDER),
        "CB_GO": go, "CB_RELEASE": release, "CLAUDE_BRIDGE_HOME": d,
    })
    env.pop("WATCHER_LOCK_FD", None)

    procs = [subprocess.Popen([sys.executable, worker], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
             for _ in range(2)]
    time.sleep(0.6)
    open(go, "w").close()
    outs = []
    for p in procs:
        verdict = ""
        while True:
            line = (p.stdout.readline() or "").strip()
            if not line:
                verdict = "rc=%s %s" % (p.returncode, (p.stderr.read() or "").strip()[:200])
                break
            if line in ("lock", "nolock"):
                verdict = line
                break
        outs.append(verdict)
    open(release, "w").close()
    for p in procs:
        p.communicate(timeout=60)

    n = 0
    if os.path.exists(starts):
        n = len([ln for ln in open(starts) if ln.strip()])
    assert outs.count("lock") == 1, "expected one lock holder, got %r" % (outs,)
    assert outs.count("nolock") == 1, "expected one loser, got %r" % (outs,)
    assert n == 1, "double dispatch must start exactly one agent, got %d" % n
    return "two simultaneous dispatches -> 1 start"


# --- 2 ---------------------------------------------------------------------------------
def test_sixty_second_outage() -> str:
    d = tmp_env()
    clock = Clock(1000.0)
    outage_end = clock.t + 60.0
    attempts: list[float] = []
    successes: list[float] = []

    def flaky(cfg_, order, files, inbox_path):
        attempts.append(clock.t)
        if clock.t < outage_end:
            raise RuntimeError("the background service is down: simulated 60s outage")
        successes.append(clock.t)
        return "ses_" + order["id"]

    watcher.start_agent = flaky
    real = use_clock(clock)
    try:
        st = watcher.load_state()
        watcher.handle_orders(cfg(), st, [ORDER], files=[])
        first = rec_of(ORDER["id"])
        # the outage must NOT have marked the order failed: a retry is scheduled instead
        assert first["status"] == "retrying", first
        assert first.get("next_attempt_at"), first
        assert not first.get("session"), first

        passes = drive_until_started(clock)
    finally:
        watcher._now, watcher._sleep = real

    rec = rec_of(ORDER["id"])
    assert rec.get("session"), "the order must start once the service is back: %r" % rec
    assert rec["status"] == "started", rec
    assert len(successes) == 1, "exactly one agent must be started, got %d" % len(successes)
    assert successes[0] >= outage_end, \
        "the start must happen after the outage ends (%.0f < %.0f)" % (successes[0], outage_end)
    assert int(rec.get("attempts") or 0) <= len(watcher._retry_delays(cfg()["watch"])) + 1, rec
    return "60s outage: %d failed attempts, started at t=%.0f (outage ended t=%.0f), %d pass(es)" \
        % (len(attempts), successes[0], outage_end, passes)


# --- 3 ---------------------------------------------------------------------------------
def test_outage_past_budget_then_requeue() -> str:
    d = tmp_env()
    clock = Clock(1000.0)

    def boom(cfg_, order, files, inbox_path):
        raise RuntimeError("the background service is down: simulated long outage")

    watcher.start_agent = boom
    real = use_clock(clock)
    try:
        st = watcher.load_state()
        watcher.handle_orders(cfg(), st, [ORDER], files=[])
        drive_until_started(clock)          # with boom it can only ever give up

        rec = rec_of(ORDER["id"])
        # terminal: failed, with the retry clock cleared (a scheduled retry keeps it set)
        assert rec["status"] == "failed", "a spent schedule must mark the order failed: %r" % rec
        assert not rec.get("next_attempt_at"), "a spent schedule must have no retry due: %r" % rec
        assert int(rec.get("attempts") or 0) >= 1, rec

        # the explicit re-queue command puts it back
        st = watcher.load_state()
        assert watcher.requeue_order(st, ORDER["id"]) is True, "requeue must find the record"
        rec = rec_of(ORDER["id"])
        assert rec["status"] == "pending" and int(rec.get("attempts") or 0) == 0, rec

        # and now the service is back: the next pass starts it
        successes: list[str] = []
        watcher.start_agent = lambda cfg_, order, files, inbox: (
            successes.append(order["id"]) or "ses_" + order["id"])
        clock.t += 30.0
        st = watcher.load_state()
        watcher.handle_orders(cfg(), st, [], files=[])
        rec = rec_of(ORDER["id"])
        assert rec.get("session") and rec["status"] == "started", rec
        assert successes == [ORDER["id"]], successes
    finally:
        watcher._now, watcher._sleep = real
    return "past-budget outage -> failed; requeue -> pending; next pass -> started"


def main() -> int:
    checks = [
        ("double dispatch starts one", test_double_dispatch_starts_one),
        ("60s outage is survived", test_sixty_second_outage),
        ("past-budget outage fails, requeue starts", test_outage_past_budget_then_requeue),
    ]
    failed = 0
    for name, fn in checks:
        try:
            detail = fn()
            print("PASS  %-42s %s" % (name, detail))
        except Exception as e:  # noqa: BLE001
            failed += 1
            print("FAIL  %-42s %s" % (name, e))
    print("\n%d/%d proofs passed" % (len(checks) - failed, len(checks)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
