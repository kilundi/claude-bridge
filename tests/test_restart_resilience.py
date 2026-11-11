#!/usr/bin/env python3
"""Prove the loop survives the machine: a cap on agents, a server that comes back,
the turns a restart interrupted resumed, and the windows left behind closed.

    python3 tests/test_restart_resilience.py

This is the night of 2026-11-11 written down as proofs.  Five agents, a 6 GB box and
three JS test suites at once drove free memory to 246 MiB; the opencode background
server died, every in-flight turn died with it (silently), and the only reason the
server came back was a stampede of clients each starting one.  WO-108, WO-109, WO-110
and WO-111 all stopped mid-turn and nothing resumed them.

Offline: no Chrome, no opencode, no network - every transcript and process is faked.
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import watcher  # noqa: E402

# tmp_env() stubs session_activity so no proof can touch the API by accident; the two
# proofs ABOUT that reader call the real one through this reference.
REAL_SESSION_ACTIVITY = watcher.session_activity


def tmp_env(tmp=None) -> str:
    d = tempfile.mkdtemp(prefix="restart-resilience-")
    watcher.STATE = os.path.join(d, "state.json")
    watcher.INBOX = os.path.join(d, "inbox")
    watcher.LOG = os.path.join(d, "watcher.log")
    watcher.DOWNLOADS = os.path.join(d, "downloads")
    watcher.NEEDS_ORDER = os.path.join(d, "NEEDS-ORDER.md")
    os.makedirs(watcher.INBOX, exist_ok=True)
    os.makedirs(watcher.DOWNLOADS, exist_ok=True)
    # a test must never open, or close, a real window on the user's desktop, and must
    # never talk to the API: those stubs are the defaults, and each proof overrides
    # only the one it is about
    watcher.open_agent_terminal = lambda cfg_, oid, sid, directory: False
    watcher.session_activity = lambda cfg_, st_, now=None: []
    watcher._close_window = lambda pid, script: None
    return d


def cfg(**watch) -> dict:
    base = {"agent_active_seconds": 1800, "slot_seconds": 7200, "max_agents": 2,
            "resume_interrupted": True, "resume_max": 3, "resume_max_per_pass": 2,
            "resume_cooldown_seconds": 1800, "resume_max_age_seconds": 21600,
            "reap_idle_seconds": 7200, "launch_terminal": True, "ask_for_order": True,
            "start_retries": 0, "start_retry_delay": 0}
    base.update(watch)
    return {"watch": base}


def order(oid: str, directory: str = "/tmp") -> dict:
    return {"id": oid, "dir": directory, "branch": "main", "goal": "do the thing",
            "acceptance": "the thing is done", "raw": "id: %s" % oid}


def state(*agents) -> dict:
    """A state whose agents were started in the order given: (oid, sid, age)."""
    st: dict = {"orders": {}, "starts": {}, "sessions": {}, "files": []}
    for i, (oid, sid, age) in enumerate(agents):
        st["sessions"][oid] = sid
        st["starts"][oid] = {"status": "started", "attempts": 1, "session": sid,
                             "updated": time.time() + i * 0.001, "order": order(oid)}
    return st


def make_activity(ages: dict):
    """A session_activity() replacement: {oid: seconds since its transcript moved}."""
    def f(cfg_, st_, now=None):
        now = now or time.time()
        out = []
        for oid, sid in (st_.get("sessions") or {}).items():
            age = ages.get(oid)
            last = None if age is None else now - age
            out.append({"order": oid, "session": sid, "last": last,
                        "live": bool(last and age < 1800)})
        out.sort(key=lambda a: (st_.get("starts", {}).get(a["order"]) or {}).get("updated") or 0)
        return out
    return f


def stub_api(ages: dict):
    """Fake oc_api: a session record that last moved ``age`` seconds ago.

    Returns the function and the list of calls made, so a proof can assert that a
    resume was posted - and, just as importantly, that none was.
    """
    calls: list[tuple] = []

    def fake(binp, method, path, payload=None, timeout=60):
        calls.append((method, path, payload))
        if method == "POST":
            return {"ok": True}
        parts = path.split("/")
        sid = parts[3] if len(parts) > 3 else ""
        age = ages.get(sid)
        if age is None:
            raise RuntimeError("no such session: %s" % sid)
        ms = int((time.time() - age) * 1000)
        return {"data": {"id": sid, "outcome": "succeeded",
                         "time": {"created": ms - 60000, "updated": ms,
                                  "idle": ms + 7, "viewed": ms + 7}}}

    return fake, calls


def posts(calls) -> list:
    return [c for c in calls if c[0] == "POST"]


# --- how many agents may run at once ---------------------------------------------------

def test_slot_cap_holds_the_third() -> str:
    tmp_env()
    st = state(("WO-301", "ses_a", 60), ("WO-302", "ses_b", 60))
    watcher.session_activity = make_activity({"WO-301": 60, "WO-302": 60})
    started: list[str] = []
    watcher.start_agent = lambda c, o, f, i: (started.append(o["id"]) or "ses_new")
    batch, held = watcher.dispatch_batch(cfg(), st, [order("WO-303"), order("WO-304")])
    assert batch == [] and len(held) == 2, (batch, held)
    assert "WO-303" not in (st.get("orders") or {}), "a held order must stay unmarked"
    assert st["held"]["WO-303"] and st["held"]["WO-304"], st["held"]
    log = open(watcher.LOG).read()
    assert "holding WO-303" in log and "2/2 agents" in log, log
    # and no other path may start one either
    watcher.handle_orders(cfg(), st, [order("WO-303")], files=[])
    assert started == [], started
    return "2/2 slots full: the third order waits, unmarked, and is named in the log"


def test_cap_frees_a_slot_when_one_finishes() -> str:
    tmp_env()
    st = state(("WO-305", "ses_a", 60), ("WO-306", "ses_b", 60))
    watcher.session_activity = make_activity({"WO-305": 60, "WO-306": 60})
    assert not watcher.dispatch_batch(cfg(), st, [order("WO-307")])[0]
    watcher.finish_order(st, "WO-305")
    act = make_activity({"WO-305": 99999, "WO-306": 60})(cfg(), st)
    batch, held = watcher.dispatch_batch(cfg(), st, [order("WO-307")], activity=act)
    assert [o["id"] for o in batch] == ["WO-307"] and held == [], (batch, held)
    assert "WO-307" not in st["held"], st["held"]
    return "marking an order finished frees its slot at once, and the held order goes"


def test_a_long_suite_keeps_its_slot() -> str:
    tmp_env()
    st = state(("WO-310", "ses_a", 2400), ("WO-311", "ses_b", 2400))
    watcher.session_activity = make_activity({"WO-310": 2400, "WO-311": 2400})
    act = watcher.session_activity(cfg(), st)
    assert watcher.live_agents(act) == [], "40 minutes of silence is not 'working'"
    assert len(watcher.slot_agents(act, cfg())) == 2, "but it still holds its slot"
    batch, held = watcher.dispatch_batch(cfg(), st, [order("WO-312")])
    assert batch == [] and len(held) == 1, (batch, held)
    return "a 40-minute end-to-end run looks idle but keeps its slot: no third agent"


# --- noticing the server die, and bringing it back -------------------------------------

def test_service_restart_is_noticed_across_a_watcher_restart() -> str:
    tmp_env()
    pids = {"now": 1000}
    watcher.service_pid = lambda: pids["now"]
    st: dict = {}
    assert watcher.note_service(st) is None
    assert st["service"]["pid"] == 1000 and st["service"]["seen"], st
    watcher.save_state(st)
    st2 = watcher.load_state()
    assert watcher.note_service(st2) is None, "the same pid is not a restart"
    pids["now"] = 2000
    ch = watcher.note_service(st2)
    assert ch == {"from": 1000, "to": 2000}, ch
    assert st2["service"]["pid"] == 2000, st2
    assert watcher.note_service({}) is None, "the first pass ever is not a restart"
    return "the pid lives in state, so a restart is still seen after the watcher restarts"


def test_ensure_service_only_touches_a_dead_server() -> str:
    tmp_env()
    pids = {"now": 4242}
    alive = {"v": True}
    watcher.service_pid = lambda: pids["now"]
    watcher._pid_alive = lambda p: alive["v"]
    runs: list[list] = []

    class P:
        stdout, stderr = "  server up  ", ""

    orig = watcher.subprocess.run
    watcher.subprocess.run = lambda *a, **k: (runs.append(list(a[0])) or P())
    try:
        assert watcher.ensure_service(cfg(), {}) is False, "a live server must be left alone"
        assert runs == [], runs
        alive["v"] = False                       # the process is really gone
        st: dict = {}
        assert watcher.ensure_service(cfg(service_start_cooldown=60), st) is True
        assert runs[0][1:] == ["service", "start"], runs
        assert watcher.ensure_service(cfg(service_start_cooldown=60), st) is False
        assert len(runs) == 1, "one starter a minute - never a stampede"
        assert "the opencode background server was gone" in open(watcher.LOG).read()
    finally:
        watcher.subprocess.run = orig
    return "a live server is never touched; a dead one is started once, never in a herd"


# --- reading the session record --------------------------------------------------------

def test_activity_reads_the_record() -> str:
    tmp_env()
    st = state(("WO-390", "ses_a", 120), ("WO-391", "ses_b", 4000))
    watcher.oc_api, _ = stub_api({"ses_a": 120, "ses_b": 4000})
    act = REAL_SESSION_ACTIVITY(cfg(), st)
    assert [a["order"] for a in act] == ["WO-390", "WO-391"], act
    assert act[0]["live"] is True and act[1]["live"] is False, act
    assert abs((time.time() - act[0]["last"]) - 120) < 30, act[0]["last"]
    return "liveness comes from the session's own timestamps, not its outcome field"


def test_liveness_reads_one_small_record_not_the_transcript() -> str:
    tmp_env()
    st = state(("WO-401", "ses_a", 30),)
    watcher.oc_api, calls = stub_api({"ses_a": 30})
    REAL_SESSION_ACTIVITY(cfg(), st)
    paths = [c[1] for c in calls]
    assert paths == ["/api/session/ses_a"], paths
    # and the shape the CLI really returns for a big session: truncated JSON, which
    # oc_api hands back as {"raw": ...} - it must mean "unknown", never "idle"
    watcher.oc_api = lambda *a, **k: {"raw": '{"data":[{"id":"msg_1","time":{"crea'}
    act = REAL_SESSION_ACTIVITY(cfg(), st)
    assert act[0]["last"] is None and act[0]["live"] is False, act
    return "one small record per agent, so a 261 KB message page cannot blind us mid-way"


def test_an_unreadable_record_is_never_idle() -> str:
    tmp_env()
    st = state(("WO-395", "ses_gone", 100))

    def boom(*a, **k):
        raise RuntimeError("no such session")

    watcher.oc_api = boom
    act = REAL_SESSION_ACTIVITY(cfg(), st)
    assert act[0]["last"] is None and act[0]["live"] is False, act
    assert watcher.resume_stalled(cfg(), st, act, {"from": 1, "to": 2}) == [], act
    return "a session we cannot read is unknown: never resumed, never reaped on a guess"


# --- a turn that died with the server --------------------------------------------------

def test_resume_only_after_a_restart() -> str:
    tmp_env()
    st = state(("WO-320", "ses_a", 3000))
    act = make_activity({"WO-320": 3000})(cfg(), st)
    watcher.oc_api, calls = stub_api({"ses_a": 3000})
    assert watcher.resume_stalled(cfg(), st, act, None) == [], "a healthy server: idle is idle"
    assert posts(calls) == [], calls
    got = watcher.resume_stalled(cfg(), st, act, {"from": 1, "to": 2})
    assert got == ["WO-320"], got
    p = posts(calls)
    assert len(p) == 1 and p[0][1] == "/api/session/ses_a/prompt", p
    text = p[0][2]["text"]
    assert "git status" in text and "git log" in text, text
    assert "do NOT redo" in text and "ask_claude" in text, text
    assert st["resume"]["WO-320"]["count"] == 1, st["resume"]
    return "an idle agent is left alone; after a restart it is resumed once, git first"


def test_resume_is_bounded() -> str:
    tmp_env()
    st = state(("WO-330", "ses_a", 3000))
    act = make_activity({"WO-330": 3000})(cfg(), st)
    watcher.oc_api, calls = stub_api({"ses_a": 3000})
    c = cfg(resume_max=2)
    ch = {"from": 1, "to": 2}
    assert watcher.resume_stalled(c, st, act, ch) == ["WO-330"]
    assert watcher.resume_stalled(c, st, act, ch) == [], "the cooldown holds it back"
    st["resume"]["WO-330"]["at"] = 0
    assert watcher.resume_stalled(c, st, act, ch) == ["WO-330"]
    st["resume"]["WO-330"]["at"] = 0
    assert watcher.resume_stalled(c, st, act, ch) == [], "resume_max=2 is the limit"
    assert len(posts(calls)) == 2, calls
    return "one resume per cooldown, and never more than resume_max per order"


def test_resume_skips_working_and_finished() -> str:
    tmp_env()
    st = state(("WO-340", "ses_a", 30), ("WO-341", "ses_b", 3000), ("WO-342", "ses_c", 3000))
    st["finished"] = {"WO-341": time.time()}
    act = make_activity({"WO-340": 30, "WO-341": 3000, "WO-342": 3000})(cfg(), st)
    watcher.oc_api, calls = stub_api({"ses_a": 30, "ses_b": 3000, "ses_c": 3000})
    got = watcher.resume_stalled(cfg(), st, act, {"from": 1, "to": 2})
    assert got == ["WO-342"], got
    assert [p[1] for p in posts(calls)] == ["/api/session/ses_c/prompt"], calls
    return "a working agent is never interrupted, and a finished order is never resumed"


def test_resume_takes_the_freshest_first() -> str:
    tmp_env()
    ages = {"WO-350": 5000, "WO-351": 4000, "WO-352": 3000}
    st = state(("WO-350", "ses_a", 5000), ("WO-351", "ses_b", 4000), ("WO-352", "ses_c", 3000))
    act = make_activity(ages)(cfg(), st)
    watcher.oc_api, calls = stub_api(ages)
    got = watcher.resume_stalled(cfg(), st, act, {"from": 1, "to": 2})
    assert got == ["WO-352", "WO-351"], got
    assert "WO-350" not in st["resume"], "the third waits for a later pass"
    assert "more interrupted order" in open(watcher.LOG).read()
    return "on a reboot it resumes at most resume_max_per_pass, freshest first"


def test_resume_too_old_is_left_alone() -> str:
    tmp_env()
    st = state(("WO-355", "ses_a", 99999))
    act = make_activity({"WO-355": 99999})(cfg(), st)
    watcher.oc_api, calls = stub_api({"ses_a": 99999})
    got = watcher.resume_stalled(cfg(), st, act, {"from": 1, "to": 2})
    assert got == [], "a day-old session is not 'interrupted', it is over"
    assert posts(calls) == [], calls
    return "only a turn that was in flight recently is resumed (resume_max_age_seconds)"


# --- the windows left behind -----------------------------------------------------------

def test_reap_closes_quiet_windows_only() -> str:
    d = tmp_env()
    st = state(("WO-360", "ses_a", 30), ("WO-361", "ses_b", 9000))
    act = make_activity({"WO-360": 30, "WO-361": 9000})(cfg(), st)
    wins = []
    for sid, age, pid in (("ses_a", 30, 111), ("ses_b", 9000, 222),
                          ("ses_stranger", 9000, 333)):
        p = os.path.join(d, sid + ".sh")
        with open(p, "w") as fh:
            fh.write("#!/bin/sh\n")
        os.utime(p, (time.time() - age,) * 2)
        wins.append({"pid": pid, "sid": sid, "script": p})
    killed: list[int] = []

    def fake_close(pid, script):
        killed.append(pid)
        wins[:] = [w for w in wins if w["pid"] != pid]   # the window is gone now

    watcher._close_window = fake_close
    got = watcher.reap_windows(cfg(), st, act, windows=wins)
    assert got == ["WO-361"], got
    assert killed == [222], killed              # not the working agent, not a stranger
    assert not os.path.exists(os.path.join(d, "ses_b.sh")), "the script stayed behind"
    assert watcher.reap_windows(cfg(), st, act, windows=wins, force=True) == [], "reaped twice"
    assert watcher.reap_windows(cfg(reap_idle_seconds=1), st, act, force=True,
                                windows=[w for w in wins if w["sid"] == "ses_a"]) == []
    assert killed == [222], "force must still never close a working agent's window"
    return "a quiet agent's window goes, a working one never does, a stranger's never does"


# --- watcher.sh status -----------------------------------------------------------------

def test_status_says_what_is_running() -> str:
    tmp_env()
    st = state(("WO-380", "ses_a", 60), ("WO-381", "ses_b", 3000))
    st["held"] = {"WO-382": time.time()}
    st["resume"] = {"WO-381": {"count": 3, "at": 1}}
    watcher.session_activity = make_activity({"WO-380": 60, "WO-381": 3000})
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        watcher.report_status(cfg(), st)
    out = buf.getvalue()
    assert "1 working, 2 holding a slot (max_agents=2)" in out, out
    assert "WO-380  working" in out and "WO-381  slot" in out, out
    assert "WO-382  waiting for a free agent slot" in out, out
    assert "WO-381  resumed 3 time(s) - have a look at it" in out, out
    return "watcher.sh status shows working / slot / waiting / resumed at a glance"


def main() -> int:
    checks = [
        ("a cap holds the third agent", test_slot_cap_holds_the_third),
        ("a finished order frees its slot", test_cap_frees_a_slot_when_one_finishes),
        ("a long suite keeps its slot", test_a_long_suite_keeps_its_slot),
        ("a restarted server is noticed", test_service_restart_is_noticed_across_a_watcher_restart),
        ("a dead server is started once", test_ensure_service_only_touches_a_dead_server),
        ("liveness comes from the record", test_activity_reads_the_record),
        ("one small record, not the page", test_liveness_reads_one_small_record_not_the_transcript),
        ("an unreadable record is unknown", test_an_unreadable_record_is_never_idle),
        ("resume needs a restart", test_resume_only_after_a_restart),
        ("resume is bounded", test_resume_is_bounded),
        ("resume skips working and finished", test_resume_skips_working_and_finished),
        ("resume takes the freshest first", test_resume_takes_the_freshest_first),
        ("a stale session is left alone", test_resume_too_old_is_left_alone),
        ("reaping closes quiet windows only", test_reap_closes_quiet_windows_only),
        ("status says what is running", test_status_says_what_is_running),
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
