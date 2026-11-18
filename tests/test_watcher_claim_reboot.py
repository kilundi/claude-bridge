#!/usr/bin/env python3
"""Prove the per-order claim is atomic, survives a reboot, and never doubles a session.

Runs against a throwaway copy of the state, a temp claims directory and a fake
``start_agent`` - it never touches the live watcher, its state, its inbox, its lock or its
claims, and it opens no terminal and connects to nothing.

    python3 tests/test_watcher_claim_reboot.py

Proves:
  1. two concurrent dispatches of the same order id, from two separate processes,
     start exactly one agent (the per-order claim, not the watcher lock)
  2. a claim written before a reboot, whose recorded pid and window are both gone, is
     replaced by exactly one agent on resume - and never two
  3. a claim whose starter died *mid-start* but whose window is still open is HELD, so
     the agent it already started is never doubled (rule 3: stale only when id AND
     window are both gone)
  4. a dispatch whose order id already has a report file is ignored and logged
  5. the claim created before a start already records the window and the start time
"""
from __future__ import annotations

import glob
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
    "goal": "prove the per-order claim starts exactly one agent",
    "dir": "/tmp",
    "branch": "",
    "save": "",
    "acceptance": "exactly one start",
    "model": "",
    "raw": "id: WO-TEST\ngoal: prove the per-order claim starts exactly one agent",
}

# a worker run as a *separate* process, so the two dispatches are genuinely racing.  It
# does NOT take the watcher lock: the point is that two watchers (a restart, a second
# pipeline, a resume racing a dispatch) still start one agent, which only the claim can do.
WORKER = r'''
import json, os, sys, time
sys.path.insert(0, os.environ["CB_DIR"])
import watcher
watcher.STATE = os.environ["CB_STATE"]
watcher.INBOX = os.environ["CB_INBOX"]
watcher.LOG = os.environ["CB_LOG"]
watcher.LOCK = os.environ["CB_LOCK"]
watcher.CLAIMS = os.environ["CB_CLAIMS"]
watcher.REPORTS = os.environ["CB_REPORTS"]
watcher.NEEDS_ORDER = os.environ["CB_NEEDS"]
STARTS = os.environ["CB_STARTS"]

def fake_start(cfg, order, files, inbox_path):     # the fake dispatcher: no agent, no tmux
    with open(STARTS, "a") as fh:
        fh.write(order["id"] + "\n")
    return "ses_" + order["id"]

watcher.start_agent = fake_start
watcher.open_agent_terminal = lambda cfg_, oid, sid, directory: False
watcher.session_activity = lambda cfg_, st_, now=None: []
watcher.agent_windows = lambda *a, **k: []          # no real tmux here
cfg = {"watch": {"auto_start": True, "min_free_mb": 0}}
order = json.loads(os.environ["CB_ORDER"])

st = watcher.load_state()          # both workers read "pending, no session" *before* racing
open(os.environ["CB_READY"], "w").close()
while not os.path.exists(os.environ["CB_GO"]):   # line up on the same instant
    time.sleep(0.002)
sid = watcher.ensure_started(cfg, st, order["id"])
print("started" if sid else "notstarted", flush=True)
'''


def tmp_env() -> str:
    d = tempfile.mkdtemp(prefix="watcher-claim-test-")
    watcher.STATE = os.path.join(d, "state.json")
    watcher.INBOX = os.path.join(d, "inbox")
    watcher.LOG = os.path.join(d, "watcher.log")
    watcher.LOCK = os.path.join(d, "watcher.lock")
    watcher.CLAIMS = os.path.join(d, "claims")
    watcher.REPORTS = os.path.join(d, "reports")
    watcher.NEEDS_ORDER = os.path.join(d, "NEEDS-ORDER.md")
    for p in (watcher.INBOX, watcher.CLAIMS, watcher.REPORTS):
        os.makedirs(p, exist_ok=True)
    # a proof must never open a real terminal window on the user's desktop, read a real
    # agent transcript, or see a real tmux window
    watcher.open_agent_terminal = lambda cfg_, oid, sid, directory: False
    watcher.session_activity = lambda cfg_, st_, now=None: []
    watcher.agent_windows = lambda *a, **k: []
    return d


def cfg() -> dict:
    return {"watch": {"auto_start": True, "start_retries": 2, "start_retry_delay": 0,
                      "min_free_mb": 0}}


def count_lines(path: str) -> int:
    if not os.path.exists(path):
        return 0
    with open(path) as fh:
        return len([ln for ln in fh if ln.strip()])


def seed(state: dict) -> None:
    with open(watcher.STATE, "w") as fh:
        json.dump(state, fh)


def pending_state() -> dict:
    """The state a reboot leaves: the order is known, but no session was recorded."""
    return {
        "orders": {ORDER["id"]: watcher._hash(ORDER["raw"])},
        "sessions": {},
        "starts": {ORDER["id"]: {"status": "pending", "attempts": 0, "order": ORDER,
                                 "inbox": os.path.join(watcher.INBOX, "WO-TEST.md"),
                                 "files": []}},
    }


class Dead:
    """Stub ``_pid_alive`` so no proof ever depends on a real pid or a real reboot."""

    def __init__(self, alive: bool = False) -> None:
        self.alive = alive

    def __enter__(self):
        self.real = watcher._pid_alive
        watcher._pid_alive = lambda pid: self.alive and pid == 4242
        return self

    def __exit__(self, *exc):
        watcher._pid_alive = self.real
        return False


# --- 1 ---------------------------------------------------------------------------------
def test_concurrent_dispatch_one_start() -> str:
    d = tmp_env()
    worker = os.path.join(d, "worker.py")
    with open(worker, "w") as fh:
        fh.write(WORKER)
    starts = os.path.join(d, "starts.log")
    go = os.path.join(d, "go")
    seed(pending_state())          # both workers read this same "no session" record

    env = dict(os.environ)
    env.update({
        "CB_DIR": ROOT, "CB_STATE": watcher.STATE, "CB_INBOX": watcher.INBOX,
        "CB_LOG": watcher.LOG, "CB_LOCK": watcher.LOCK, "CB_CLAIMS": watcher.CLAIMS,
        "CB_REPORTS": watcher.REPORTS, "CB_NEEDS": watcher.NEEDS_ORDER, "CB_STARTS": starts,
        "CB_ORDER": json.dumps(ORDER), "CB_GO": go, "CLAUDE_BRIDGE_HOME": d,
    })
    env.pop("WATCHER_LOCK_FD", None)

    procs = []
    for _ in range(2):
        ready = os.path.join(d, "ready.%d" % len(procs))
        env["CB_READY"] = ready
        procs.append(subprocess.Popen([sys.executable, worker], env=env,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
        deadline = time.time() + 30
        while not os.path.exists(ready) and time.time() < deadline:
            time.sleep(0.01)
        assert os.path.exists(ready), "worker did not reach the line-up"

    open(go, "w").close()          # release both dispatches at the same instant
    outs = []
    for p in procs:
        out, err = p.communicate(timeout=60)
        verdict = ""
        for line in (out or "").splitlines():
            if line.strip() in ("started", "notstarted"):
                verdict = line.strip()
        outs.append(verdict or ("rc=%s %s" % (p.returncode, (err or "").strip()[:200])))

    n = count_lines(starts)
    assert outs.count("started") == 1, "exactly one dispatch must start, got %r" % (outs,)
    assert outs.count("notstarted") == 1, "exactly one dispatch must be refused, got %r" % (outs,)
    assert n == 1, "two concurrent dispatches must start exactly one agent, got %d" % n
    return "two concurrent dispatches (no watcher lock) -> 1 started, 1 refused, %d start" % n


# --- 2 ---------------------------------------------------------------------------------
def test_stale_claim_after_reboot_one_replacement() -> str:
    d = tmp_env()
    starts: list[str] = []
    watcher.start_agent = lambda cfg_, order, files, inbox: (
        starts.append(order["id"]) or "ses_" + order["id"])
    # a claim from before the reboot: its pid is gone and its window is gone
    claim = os.path.join(watcher.CLAIMS, "%s.lock" % watcher.window_name(ORDER["id"]))
    with open(claim, "w") as fh:
        json.dump({"oid": ORDER["id"], "state": "started", "starter_pid": 999999,
                   "pid": 999999, "window": watcher.window_name(ORDER["id"]),
                   "session": "ses_before_reboot", "started": time.time() - 100}, fh)
    seed(pending_state())

    with Dead(alive=False):
        watcher.handle_orders(cfg(), watcher.load_state(), [], files=[])   # resume pass
    assert len(starts) == 1, "a stale claim must be replaced by exactly one agent, got %d" % len(starts)
    fresh = json.load(open(claim))
    assert fresh["state"] == "started" and fresh.get("session"), fresh

    # a second resume must start nothing: the replacement is now the claim's agent
    watcher.agent_windows = lambda *a, **k: [{"pid": 4242, "sid": fresh["session"],
                                             "script": "", "tmux": None}]
    with Dead(alive=True):
        watcher.handle_orders(cfg(), watcher.load_state(), [], files=[])
    assert len(starts) == 1, "the replacement must never be doubled, got %d" % len(starts)
    return "stale claim after reboot -> 1 replacement, then 0"


# --- 3 ---------------------------------------------------------------------------------
def test_starting_claim_with_open_window_is_held() -> str:
    d = tmp_env()
    starts: list[str] = []
    watcher.start_agent = lambda cfg_, order, files, inbox: (
        starts.append(order["id"]) or "ses_" + order["id"])
    name = watcher.window_name(ORDER["id"])
    claim = os.path.join(watcher.CLAIMS, "%s.lock" % name)
    with open(claim, "w") as fh:
        json.dump({"oid": ORDER["id"], "state": "starting", "starter_pid": 999999,
                   "pid": 0, "window": name, "started": time.time() - 100}, fh)
    # the starter died mid-start, but the window it opened is still there: an agent exists
    watcher.agent_windows = lambda *a, **k: [
        {"pid": 0, "sid": "ses_mid_start", "script": "", "tmux": "cb-repo:%s" % name}]
    seed(pending_state())

    with Dead(alive=False):
        watcher.handle_orders(cfg(), watcher.load_state(), [], files=[])
    assert starts == [], "a live window must hold the claim, not start a second agent"
    assert os.path.exists(claim), "the held claim must not be deleted"
    return "starting claim with a live window -> 0 starts (held)"


# --- 4 ---------------------------------------------------------------------------------
def test_reported_order_is_ignored() -> str:
    d = tmp_env()
    starts: list[str] = []
    watcher.start_agent = lambda cfg_, order, files, inbox: (
        starts.append(order["id"]) or "ses_" + order["id"])
    with open(os.path.join(watcher.REPORTS, "%s.md" % ORDER["id"]), "w") as fh:
        fh.write("# done\n")
    st = {"orders": {}, "sessions": {}, "starts": {}}

    watcher.handle_orders(cfg(), st, [ORDER], files=[])
    assert starts == [], "an order with a report on disk must never be dispatched"
    assert "reported" in st.get("starts", {}).get(ORDER["id"], {}).get("status", "") \
        or ORDER["id"] not in (st.get("starts") or {}), st.get("starts")
    with open(watcher.LOG) as fh:
        logged = fh.read()
    assert "reports/%s.md is already on disk" % ORDER["id"] in logged, logged[-500:]
    return "report on disk -> 0 starts, ignored and logged"


# --- 5 ---------------------------------------------------------------------------------
def test_claim_records_window_and_start_time() -> str:
    tmp_env()
    oid = "WO-CLAIMSHAPE"
    got, why = watcher.claim_order(oid, why="starting")
    assert got, why
    body = json.load(open(os.path.join(watcher.CLAIMS, "%s.lock" % watcher.window_name(oid))))
    assert body["state"] == "starting", body
    assert body["window"] == watcher.window_name(oid), body
    assert body["starter_pid"] == os.getpid(), body
    assert float(body["started"]) > 0, body
    return "claim at creation holds starter pid, window and start time"


def main() -> int:
    checks = [
        ("concurrent dispatch starts one", test_concurrent_dispatch_one_start),
        ("stale claim after reboot replaced once", test_stale_claim_after_reboot_one_replacement),
        ("mid-start claim with live window held", test_starting_claim_with_open_window_is_held),
        ("reported order is ignored", test_reported_order_is_ignored),
        ("claim records pid/window/start time", test_claim_records_window_and_start_time),
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
