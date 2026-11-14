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
import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import bridge  # noqa: E402
import watcher  # noqa: E402

# tmp_env() stubs session_activity so no proof can touch the API by accident; the two
# proofs ABOUT that reader call the real one through this reference.
REAL_SESSION_ACTIVITY = watcher.session_activity
REAL_TURN_VERDICT = watcher._turn_verdict
REAL_AGENT_WINDOWS = watcher.agent_windows


def tmp_env(tmp=None) -> str:
    d = tempfile.mkdtemp(prefix="restart-resilience-")
    watcher.STATE = os.path.join(d, "state.json")
    watcher.INBOX = os.path.join(d, "inbox")
    watcher.LOG = os.path.join(d, "watcher.log")
    watcher.DOWNLOADS = os.path.join(d, "downloads")
    watcher.NEEDS_ORDER = os.path.join(d, "NEEDS-ORDER.md")
    watcher.REPORTS = os.path.join(d, "reports")
    # say-once bookkeeping and the down-service latch are process-wide: a proof must start
    # from neither, or the first proof's lines hide the second proof's
    watcher._SAID.clear()
    watcher._SERVICE_DOWN.clear()
    # the outbox too: a report finished from its file alone is queued for the planner, and a
    # test must never put anything in the real one
    bridge.OUTBOX = os.path.join(d, "outbox")
    os.makedirs(watcher.INBOX, exist_ok=True)
    os.makedirs(watcher.DOWNLOADS, exist_ok=True)
    # a test must never open, or close, a real window on the user's desktop, and must
    # never talk to the API: those stubs are the defaults, and each proof overrides
    # only the one it is about
    watcher.open_agent_terminal = lambda cfg_, oid, sid, directory: False
    watcher.session_activity = lambda cfg_, st_, now=None: []
    watcher._close_window = lambda pid, script: None
    # a proof about the views stubs this; the real reader (tmux + /proc) comes back here, so
    # one proof's stub can never decide another proof's answer
    watcher.agent_windows = REAL_AGENT_WINDOWS
    # the transcript probe: "nobody could say", which must always hold a slot.  A proof
    # that is about it overrides this.
    watcher._turn_verdict = lambda binp, sid: None
    return d


def cfg(**watch) -> dict:
    base = {"agent_active_seconds": 1800, "slot_seconds": 7200, "max_agents": 2,
            "resume_interrupted": True, "resume_max": 3, "resume_max_per_pass": 2,
            "resume_cooldown_seconds": 1800, "resume_max_age_seconds": 21600,
            "reap_idle_seconds": 7200, "launch_terminal": True, "ask_for_order": True,
            "start_retries": 0, "start_retry_delay": 0,
            # the free-memory floor is 1500 in production; 0 here, so no proof depends on
            # how much memory this machine happens to have.  The proof about it sets it.
            "min_free_mb": 0}
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
        done = st_.get("finished") or {}
        out = []
        for oid, sid in (st_.get("sessions") or {}).items():
            age = ages.get(oid)
            last = None if age is None else now - age
            out.append({"order": oid, "session": sid, "last": last,
                        "finished": oid in done,
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


def test_low_memory_holds_new_agents() -> str:
    """11-11: 246 MiB free, the background server died, every in-flight turn with it.

    `max_agents` counts agents; one `jest --runInBand` peaks near 1.8 GB, so two agents are
    fine or fatal depending on what they are running.  Below `min_free_mb` nothing new
    starts, the order is held unmarked, and `status` says which kind of wait it is.
    """
    tmp_env()
    real = watcher.free_memory_mb
    try:
        watcher.free_memory_mb = lambda: 400
        st = state()
        batch, held = watcher.dispatch_batch(cfg(min_free_mb=1500), st, [order("WO-400")])
        assert batch == [] and [o["id"] for o in held] == ["WO-400"], (batch, held)
        assert st["held"]["WO-400"], st["held"]
        assert st["memory"] == {"available_mb": 400, "floor_mb": 1500,
                                "at": st["memory"]["at"], "holding": ["WO-400"]}, st["memory"]
        assert "WO-400" not in st["orders"], "a held order stays unmarked, so a pass sees it"
        logtext = open(watcher.LOG).read()
        assert "holding WO-400: 400 MB free is under min_free_mb=1500" in logtext, logtext
        # status names the wait: memory, not a slot
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            watcher.report_status(cfg(min_free_mb=1500), st)
        out = buf.getvalue()
        assert "memory: 400 MB available (min_free_mb=1500) - below the floor" in out, out
        assert "WO-400  waiting for free memory" in out, out
        # memory frees: the next pass starts it, with no cap involved
        watcher.free_memory_mb = lambda: 3000
        batch, held = watcher.dispatch_batch(cfg(min_free_mb=1500), st, [order("WO-400")])
        assert [o["id"] for o in batch] == ["WO-400"] and held == [], (batch, held)
        assert st["memory"]["holding"] == [], st["memory"]
        # off by config, and never holding work on a reading that failed
        watcher.free_memory_mb = lambda: 1
        assert watcher.dispatch_batch(cfg(min_free_mb=0), st, [order("WO-401")])[0], \
            "min_free_mb=0 is the guard switched off"
        watcher.free_memory_mb = lambda: None
        assert watcher.dispatch_batch(cfg(min_free_mb=1500), st, [order("WO-402")])[0], \
            "an unreadable /proc/meminfo must never stop work"
        # a real reading, so the parser is exercised at least once
        watcher.free_memory_mb = real
        assert (watcher.free_memory_mb() or 0) > 0, "MemAvailable is read from /proc/meminfo"
    finally:
        watcher.free_memory_mb = real
    return ("under the floor nothing starts and the order waits unmarked, status says why, "
            "and it starts on the first pass after memory frees")


def test_a_probe_never_starts_a_second_service() -> str:
    """39 pid changes on record, 5 of them the watcher's: the probes were the other starters.

    Every ``opencode api`` call with the service down starts a client that tries to bring a
    service *up*, and a new service replaces the registered one - "managed service
    registration replaced; shutting down", 11-11.  One pass probed every session, so a down
    service was hit again and again: 187 ``Background service failed to start`` lines in
    five minutes that evening, 94 in two minutes on 11-13.
    """
    tmp_env()
    calls: list[list[str]] = []

    class Down:
        returncode = 1
        stdout = ""
        stderr = "Error: Background service failed to start"

    class Up:
        returncode = 0
        stdout = json.dumps({"data": {"time": {"updated": 1700000000000}}})
        stderr = ""

    real_run = watcher.subprocess.run
    try:
        watcher.subprocess.run = lambda cmd, **kw: (calls.append(cmd), Down())[1]
        errors: list[str] = []
        for sid in ("ses_1", "ses_2", "ses_3", "ses_4"):
            try:
                watcher._moved_at("/bin/true", sid)
            except Exception as e:
                errors.append(str(e))
        assert len(calls) == 1, \
            "four probes against a down service must not be four starters: %d" % len(calls)
        assert len(errors) == 4 and "service is down" in errors[3], errors
        logtext = open(watcher.LOG).read()
        assert logtext.count("is not answering") == 1, "one line per outage, not per probe"
        assert "no more calls may" not in logtext, logtext
        # the service answers again (the supervisor's check says the pid is alive): probes
        # may try again, and the recovery is said out loud
        watcher.service_recovered()
        watcher.subprocess.run = lambda cmd, **kw: (calls.append(cmd), Up())[1]
        assert watcher._moved_at("/bin/true", "ses_1") == 1700000000.0
        assert len(calls) == 2, calls
        logtext = open(watcher.LOG).read()
        assert "answering again" in logtext, logtext
    finally:
        watcher.subprocess.run = real_run
        watcher._SERVICE_DOWN.clear()
    return "a down service is probed once, not once per session, and recovery is noticed"


def test_a_replacement_says_who_did_it() -> str:
    """A pid change is the expensive event - every in-flight turn dies with the old server.

    Recording that it happened is not enough: the useful question is whether the watcher
    started it (it found no live server) or something else replaced it.  Only 5 of the 39
    changes on record were ours.
    """
    tmp_env()
    st = state()
    st["service"] = {"pid": 111, "started": time.time() - 10}
    change = {"from": 111, "to": 222}
    assert "by the watcher" in watcher.explain_service_restart(st, change)
    st["service"]["started"] = time.time() - 3600
    assert "by something else" in watcher.explain_service_restart(st, change)
    watcher.record_service_change(st, change, "by something else (an opencode client)")
    assert st["service"]["changes"][-1] == {"at": st["service"]["changes"][-1]["at"],
                                            "from": 111, "to": 222,
                                            "why": "by something else (an opencode client)"}
    for i in range(8):
        watcher.record_service_change(st, {"from": i, "to": i + 1}, "x")
    assert len(st["service"]["changes"]) == 5, "the ring keeps the last five"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        watcher.report_status(cfg(), st)
    out = buf.getvalue()
    assert "service: pid 111, last replaced" in out, out
    return "a replacement is attributed in the log and kept in the last five, and status shows it"


def test_status_shows_the_views_we_own() -> str:
    """Which window or tab each agent is in should not need pgrep and tmux ls.

    The answers were always in `agent_windows`, which the reap pass uses; `status` just never
    asked, so "who is on screen" was a separate investigation every time.
    """
    tmp_env()
    st = state(("WO-380", "ses_a", 60), ("WO-381", "ses_b", 3000))
    watcher.session_activity = make_activity({"WO-380": 60, "WO-381": 3000})
    watcher.agent_windows = lambda windows=None, s=None: [
        {"pid": 0, "sid": "ses_a", "script": "/x/a.sh", "tmux": "cb-wms-web-1234abcd:WO-380"},
        {"pid": 999, "sid": "ses_b", "script": "/x/b.sh", "tmux": None}]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        watcher.report_status(cfg(), st)
    out = buf.getvalue()
    assert "views: 2 open" in out, out
    assert "WO-380  tab cb-wms-web-1234abcd:WO-380" in out, out
    assert "agent last moved 1 min ago" in out, out
    assert "WO-381  window pid 999" in out, out
    assert "agent last moved 50 min ago" in out, out
    # nothing open: say so, rather than print nothing and leave it ambiguous
    watcher.agent_windows = lambda windows=None, s=None: []
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        watcher.report_status(cfg(), st)
    assert "views: none open" in buf.getvalue(), buf.getvalue()
    return "status names every tab and window we own, with how long its agent has been quiet"


def test_state_prunes_only_old_finished_orders() -> str:
    """188 KB of state.json over 45 orders, almost all of it start records nobody reads.

    `starts[<id>]` holds the parsed order (goal, acceptance, raw text), the inbox path and
    the downloaded files: what is needed to *prompt* an agent, which a finished order never
    does again.  The proof is also about what must NOT go: a live order's record, a recently
    finished one's, and the `finished`/`orders`/`sessions` marks that are still read.
    """
    tmp_env()
    st = state(("WO-500", "ses_a", 60))
    st["starts"]["WO-501"] = {"status": "started", "attempts": 1, "updated": time.time() - 9 * 86400,
                              "order": order("WO-501"), "files": [{"path": "/tmp/x"}],
                              "inbox": "/tmp/WO-501.md"}
    st["starts"]["WO-502"] = {"status": "started", "attempts": 1, "updated": time.time() - 86400,
                              "order": order("WO-502"), "files": [], "inbox": "/tmp/WO-502.md"}
    st["sessions"]["WO-501"] = "ses_old"
    st["finished"] = {"WO-501": time.time() - 9 * 86400, "WO-502": time.time() - 86400}
    st["orders"] = {"WO-501": "hash1", "WO-502": "hash2"}
    res = watcher.prune_state(st, days=7)
    assert res["orders"] == ["WO-501"] and res["bytes"] > 0, res
    assert "WO-501" not in st["starts"], "an old finished order's start record goes"
    assert "WO-502" in st["starts"], "a finished order from yesterday keeps its record"
    assert "WO-500" in st["starts"], "a live order is never touched"
    assert sorted(st["finished"]) == ["WO-501", "WO-502"], "the finished marks stay"
    assert st["orders"] == {"WO-501": "hash1", "WO-502": "hash2"}, \
        "the hash marks stay: a message still in the chat must not be dispatched twice"
    assert st["sessions"]["WO-501"] == "ses_old", \
        "the session mapping stays: it is how a leftover tab is recognised as ours"
    # idempotent, and a second pass over the same state changes nothing
    assert watcher.prune_state(st, days=7) == {"orders": [], "count": 0, "bytes": 0}
    # and it never touches a record that was never finished
    st["starts"]["WO-503"] = {"status": "started", "updated": 1, "order": order("WO-503")}
    assert watcher.prune_state(st, days=7)["count"] == 0
    return "old finished start records go; live, recent, finished and hash marks all stay"


def test_a_repeated_line_is_said_once() -> str:
    """312 ``could not read`` and 125 ``no longer in the chat`` lines: the same fact per pass.

    A log that repeats itself is a log nobody reads, and that is how a real failure gets
    missed.  Once per order per window - and after the window it speaks again, so a failure
    that lasts all day cannot hide behind its own first line.
    """
    tmp_env()
    for _ in range(20):
        watcher.log_once("unreadable-WO-1", "  could not read WO-1's session record: x",
                         every=1800)
    assert open(watcher.LOG).read().count("could not read WO-1") == 1
    watcher.log_once("unreadable-WO-1", "  could not read WO-1's session record: x",
                     every=1800, now=time.time() + 1801)
    assert open(watcher.LOG).read().count("could not read WO-1") == 2
    # different keys are independent: another order still gets its line
    watcher.log_once("unreadable-WO-2", "  could not read WO-2's session record: x")
    assert open(watcher.LOG).read().count("could not read WO-2") == 1
    return "a line about one order is said once per window, and again after it"


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


# --- the clock is not evidence: what the transcript says ---------------------------------
# 2026-11-11 13:44: five sessions held a cap of two and the queue stopped for 35 minutes.
# Only one of them was working.  A slot lasted an hour after any movement and a
# restart-resume moved the clock, so four killed agents sat on a full cap.

def test_a_dead_agent_frees_its_slot() -> str:
    tmp_env()
    ages = {"WO-320": 2000, "WO-321": 2100, "WO-322": 2200, "WO-323": 2300, "WO-324": 60}
    st = state(*[(oid, "ses_" + oid.lower(), age) for oid, age in ages.items()])
    watcher.session_activity = make_activity(ages)
    probed: list[str] = []

    def probe(a):
        probed.append(a["order"])
        return False          # every stale transcript ends in an idle marker: no turn

    act = watcher.session_activity(cfg(), st)
    assert [a["order"] for a in watcher.live_agents(act)] == ["WO-324"], "only the fresh one is 'working'"
    assert [a["order"] for a in watcher.slot_agents(act, cfg(), probe=probe)] == ["WO-324"]
    assert sorted(probed) == ["WO-320", "WO-321", "WO-322", "WO-323"], probed
    # the pass decisions use it too, so the queue moves without anybody freeing a slot
    watcher._turn_verdict = lambda binp, sid: "idle"
    batch, held = watcher.dispatch_batch(cfg(), st, [order("WO-325")])
    assert [o["id"] for o in batch] == ["WO-325"] and held == [], (batch, held)
    return "four stale sessions with no turn running free their slots; the queue moves"


def test_a_silent_suite_keeps_its_slot_with_evidence() -> str:
    tmp_env()
    st = state(("WO-330", "ses_a", 2400), ("WO-331", "ses_b", 2400))
    watcher.session_activity = make_activity({"WO-330": 2400, "WO-331": 2400})
    watcher._turn_verdict = lambda binp, sid: "working"   # the tests are running
    act = watcher.session_activity(cfg(), st)
    assert len(watcher.slot_agents(act, cfg(), probe=watcher.turn_probe(cfg()))) == 2
    batch, held = watcher.dispatch_batch(cfg(), st, [order("WO-332")])
    assert batch == [] and len(held) == 1, (batch, held)
    return "a silent session with a turn in flight keeps its slot: still no third agent"


def test_an_unreadable_probe_never_frees_a_slot() -> str:
    tmp_env()
    st = state(("WO-335", "ses_a", 2400))
    watcher.session_activity = make_activity({"WO-335": 2400})
    calls: list[str] = []

    def probe(a):
        calls.append(a["order"])
        return None           # the API did not answer: unknown is not "idle"

    act = watcher.session_activity(cfg(), st)
    assert len(watcher.slot_agents(act, cfg(), probe=probe)) == 1
    assert calls == ["WO-335"], calls
    return "a probe that cannot answer holds the slot: never free one on a guess"


def test_a_fresh_session_is_not_probed() -> str:
    tmp_env()
    st = state(("WO-340", "ses_a", 60))
    watcher.session_activity = make_activity({"WO-340": 60})
    called: list[str] = []

    def probe(a):
        called.append(a["order"])
        return "working"

    act = watcher.session_activity(cfg(), st)
    assert [a["order"] for a in watcher.slot_agents(act, cfg(), probe=probe)] == ["WO-340"]
    assert called == [], "a session that just moved is working: no request needed"
    # and once it goes quiet, the probe is what decides
    act2 = make_activity({"WO-340": 3000})(cfg(), st)
    assert [a["order"] for a in watcher.slot_agents(act2, cfg(), probe=probe)] == ["WO-340"]
    assert called == ["WO-340"], called
    assert act2[0].get("in_turn") is True, "the answer is kept on the activity for status"
    return "the probe is only asked about a session the clock cannot vouch for"


def test_a_resume_that_did_not_take_frees_its_slot() -> str:
    """2026-11-11 13:44 in miniature: resumes whose turns died within seconds.

    The resume moved the clock, so a slot lasted an hour and the queue stopped for 35
    minutes behind five sessions of which one was working.  What decides is whether a
    turn is running now, not who touched the session last.
    """
    tmp_env()
    st = state(("WO-360", "ses_a", 1200), ("WO-361", "ses_b", 1300))
    st["resume"] = {"WO-360": {"count": 2, "at": time.time() - 1200},
                    "WO-361": {"count": 2, "at": time.time() - 1300}}
    watcher.session_activity = make_activity({"WO-360": 1200, "WO-361": 1300})
    watcher._turn_verdict = lambda binp, sid: "idle"      # each resumed turn ended
    act = watcher.session_activity(cfg(), st)
    assert watcher.slot_agents(act, cfg(), probe=watcher.turn_probe(cfg())) == []
    batch, held = watcher.dispatch_batch(cfg(), st, [order("WO-362")])
    assert [o["id"] for o in batch] == ["WO-362"] and held == [], (batch, held)
    return "a resume whose turn died frees its slot: the clock it moved is not work"


def test_the_race_guard_holds_before_asking() -> str:
    """A prompt just accepted must not lose its slot to a transcript that lags behind."""
    tmp_env()
    st = state(("WO-365", "ses_a", 5))
    watcher.session_activity = make_activity({"WO-365": 5})
    called: list[str] = []

    def probe(a):
        called.append(a["order"])
        return False            # the transcript has not caught up with the prompt yet

    act = watcher.session_activity(cfg(), st)
    assert [a["order"] for a in watcher.slot_agents(act, cfg(), probe=probe,
                                                    now=time.time())] == ["WO-365"]
    assert called == [], "inside the grace period the clock is trusted, not the page"
    return "a session that moved seconds ago keeps its slot: the transcript can lag"


def test_a_started_order_drops_its_held_marker() -> str:
    tmp_env()
    st = state()
    st["held"] = {"WO-350": time.time()}
    started: list[str] = []
    watcher.start_agent = lambda c, o, f, i: (started.append(o["id"]) or "ses_new")
    watcher.handle_orders(cfg(), st, [order("WO-350")], files=[])
    assert started == ["WO-350"], started
    assert "WO-350" not in st["held"], st["held"]
    # and the file agrees: a save must not put the marker back out of the merge
    assert "WO-350" not in (json.load(open(watcher.STATE)).get("held") or {})
    # and the status never calls a started order one that is still waiting
    st["held"]["WO-351"] = time.time()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        watcher.report_status(cfg(), st)
    assert "WO-351  waiting for a free agent slot" in buf.getvalue(), buf.getvalue()
    assert "WO-350  waiting" not in buf.getvalue(), buf.getvalue()
    return "starting an order clears its held marker, and status stops contradicting itself"


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


def test_the_probe_asks_once_and_only_for_the_tail() -> str:
    """The probe is one 355-byte request per session, and never the whole page."""
    tmp_env()
    calls: list[str] = []

    def fake(binp, method, path, payload=None, timeout=60):
        calls.append(path)
        if "limit=1" not in path:
            raise AssertionError("the probe asked for more than one message: %s" % path)
        return {"data": [{"type": "idle"}]}

    watcher._PROBE_MEMO.clear()
    watcher.oc_api = fake
    assert REAL_TURN_VERDICT("bin", "ses_a") == "idle"
    assert REAL_TURN_VERDICT("bin", "ses_a") == "idle"          # a second pass in one go
    assert calls == ["/api/session/ses_a/message?limit=1"], calls
    # an idle marker means no turn; anything else is a turn
    watcher.oc_api = lambda *a, **k: {"data": [{"type": "assistant"}]}
    watcher._PROBE_MEMO.clear()
    assert REAL_TURN_VERDICT("bin", "ses_b") == "working"
    # ...unless it is parked on a human: alive, but nobody is working
    watcher.oc_api = lambda *a, **k: {"data": [{"type": "assistant", "content": [
        {"type": "text", "text": "Shall I use the staging key?"},
        {"type": "tool", "name": "question", "state": {"status": "running"}}]}]}
    watcher._PROBE_MEMO.clear()
    assert REAL_TURN_VERDICT("bin", "ses_parked") == "waiting"
    # a long shell is work, and it keeps its slot
    watcher.oc_api = lambda *a, **k: {"data": [{"type": "assistant", "content": [
        {"type": "tool", "name": "shell", "state": {"status": "running"}}]}]}
    watcher._PROBE_MEMO.clear()
    assert REAL_TURN_VERDICT("bin", "ses_shell") == "working"
    # and a truncated page (the CLI's habit on a big session) is unknown, never idle
    watcher._PROBE_MEMO.clear()
    watcher.oc_api = lambda *a, **k: {"raw": '{"data":[{"id":"msg_1","time":{"crea'}
    assert REAL_TURN_VERDICT("bin", "ses_c") is None
    return "one small request per session, and it tells work from a question"


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


# --- a turn parked on a person ---------------------------------------------------------

def test_a_view_just_launched_for_an_old_agent_is_kept() -> str:
    """`launch me the tab that did WO-130`: the tab a person asks for must survive.

    That agent's transcript may have been quiet for hours, but the view itself is seconds
    old and somebody wants to talk to it.  Closing a view needs both: an old view and a
    quiet agent.
    """
    d = tmp_env()
    st = state(("WO-365", "ses_a", 9000))
    act = make_activity({"WO-365": 9000})(cfg(), st)
    p = os.path.join(d, "ses_a.sh")
    with open(p, "w") as fh:
        fh.write("#!/bin/sh\n")                 # fresh, exactly as a launched tab is
    killed: list[int] = []
    watcher._close_window = lambda pid, script: killed.append(pid)
    wins = [{"pid": 111, "sid": "ses_a", "script": p, "tmux": None}]
    assert watcher.reap_windows(cfg(), st, act, windows=wins) == []
    assert killed == [], "the tab a person just asked for was closed under them"
    os.utime(p, (time.time() - 9000,) * 2)      # now the view is old too
    assert watcher.reap_windows(cfg(), st, act, windows=wins) == ["WO-365"]
    assert killed == [111], killed
    return "a view just launched for a long-quiet agent is kept; an old, quiet one goes"


def test_a_parked_turn_frees_its_slot() -> str:
    """WO-113 in miniature: a question waiting on an answer must not hold the queue.

    The turn is open, so the clock reads "working" - but nothing is computing, and the
    queue stopped behind it for ten minutes while 3 GB of RAM sat spare.  A parked turn
    is not work: it frees the slot, keeps its window, and says so in status.
    """
    tmp_env()
    st = state(("WO-340", "ses_a", 2400), ("WO-341", "ses_b", 2400))
    watcher.session_activity = make_activity({"WO-340": 2400, "WO-341": 2400})
    watcher._turn_verdict = lambda binp, sid: "waiting" if sid == "ses_a" else "working"
    act = watcher.session_activity(cfg(), st)
    slots = [a["order"] for a in
             watcher.slot_agents(act, cfg(), probe=watcher.turn_probe(cfg()))]
    assert slots == ["WO-341"], slots              # the parked one holds no slot
    batch, held = watcher.dispatch_batch(cfg(), st, [order("WO-342")])
    assert [o["id"] for o in batch] == ["WO-342"] and held == [], (batch, held)
    # and the pass records it as waiting for a person, not as work
    assert watcher.note_waiting(st, {"WO-340": "waiting", "WO-341": "working"}) == ["WO-340"]
    assert sorted(st["waiting"]) == ["WO-340"], st["waiting"]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        watcher.report_status(cfg(), st)
    assert "WO-340  waiting for you" in buf.getvalue(), buf.getvalue()
    assert "1 of 2 slots used" in buf.getvalue(), buf.getvalue()
    # the mark clears the moment it has been answered
    assert watcher.note_waiting(st, {"WO-340": "working", "WO-341": "working"}) == []
    assert st["waiting"] == {}, st["waiting"]
    return "a turn parked on a question frees its slot: the queue moves while a person thinks"


def test_a_parked_window_is_neither_reaped_nor_resumed() -> str:
    """That window is where the answer is typed, and a restart must not re-prompt it."""
    d = tmp_env()
    st = state(("WO-345", "ses_a", 9000))
    st["waiting"] = {"WO-345": time.time()}
    act = make_activity({"WO-345": 9000})(cfg(), st)
    p = os.path.join(d, "ses_a.sh")
    with open(p, "w") as fh:
        fh.write("#!/bin/sh\n")
    os.utime(p, (time.time() - 9000,) * 2)
    killed: list[int] = []
    watcher._close_window = lambda pid, script: killed.append(pid)
    assert watcher.reap_windows(cfg(), st, act, windows=[{"pid": 111, "sid": "ses_a",
                                                          "script": p}], force=True) == []
    assert killed == [], "the window it asked its question in must stay"
    watcher.oc_api, calls = stub_api({"ses_a": 9000})
    assert watcher.resume_stalled(cfg(), st, act, {"from": 1, "to": 2}) == []
    assert posts(calls) == [], calls
    return "a parked agent keeps its window and is never resumed: the reply goes to it"


# --- a finished order is not a worker --------------------------------------------------

def test_a_finished_order_holds_no_slot() -> str:
    """Once the work is done it is not a worker, however much its window is being poked.

    WO-122's window was still in use after ``watcher.sh done`` marked it finished, and
    its session counted against the cap that way - status read "3 of 2 slots used".
    """
    tmp_env()
    st = state(("WO-370", "ses_a", 30))          # just moved: the race guard would hold it
    st["finished"] = {"WO-370": time.time()}
    watcher.session_activity = make_activity({"WO-370": 30})
    act = watcher.session_activity(cfg(), st)
    assert act[0]["finished"] is True, act
    assert watcher.slot_agents(act, cfg(), probe=lambda a: "working") == []
    batch, held = watcher.dispatch_batch(cfg(), st, [order("WO-371")])
    assert [o["id"] for o in batch] == ["WO-371"] and held == [], (batch, held)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        watcher.report_status(cfg(), st)
    assert "0 of 2 slots used" in buf.getvalue(), buf.getvalue()
    return "a finished order's session is not a worker, even when its window is being poked"
  
  
def test_a_finished_order_is_not_busy_to_the_ask() -> str:
    """The ask path must not wait behind a finished order's window (WO-149).

    The order arrived without an ``acceptance`` line, so the watcher's job was to ask
    Claude to re-post it - and it never did: ``agent_busy`` read ``live_agents`` alone,
    and the finished WO-130 session was live only because the person was still typing in
    that window.  Every pass answered "WO-130 is still working", the ask was held, and a
    complete order stood still with both slots free.
    """
    tmp_env()
    st = state(("WO-380", "ses_a", 10))          # just moved: live, and not finished
    watcher.session_activity = make_activity({"WO-380": 10})
    assert watcher.agent_busy(cfg(), st).get("order") == "WO-380", st
    st["finished"] = {"WO-380": time.time()}
    assert watcher.live_agents(watcher.session_activity(cfg(), st)), "the window is still live"
    assert watcher.agent_busy(cfg(), st) == {}, watcher.agent_busy(cfg(), st)
    # a live *unfinished* agent is still busy, and the newest start is the one named
    st2 = state(("WO-381", "ses_b", 10), ("WO-382", "ses_c", 5))
    st2["finished"] = {"WO-380": time.time()}
    watcher.session_activity = make_activity({"WO-381": 10, "WO-382": 5})
    assert watcher.agent_busy(cfg(), st2).get("order") == "WO-382", st2
    # and with only finished agents live, the ask is free to go out
    st3 = state(("WO-383", "ses_d", 5))
    st3["finished"] = {"WO-383": time.time()}
    watcher.session_activity = make_activity({"WO-383": 5})
    plan = watcher.plan_attention(
        {"nudge": {"streak": 0, "at": 0}}, "here is a promise of work",
        {"dispatch": [], "incomplete": [({"id": "WO-384"}, ["acceptance"])],
         "duplicate": [], "ignored": []}, watcher.agent_busy(cfg(), st3), cfg())
    assert plan["action"] == "ask", plan
    return "a finished window is not 'working': the watcher asks Claude about the half order"


# --- the report that frees its own slot -------------------------------------------------

def test_a_report_file_finishes_the_order() -> str:
    """The order asks the agent to leave its report; the file appearing is the signal.

    Nothing in the loop can see the report land, so until now a finished order kept its
    slot until a person noticed and ran ``watcher.sh done``.
    """
    tmp_env()
    st = state(("WO-350", "ses_a", 600))
    st["waiting"] = {"WO-350": time.time()}
    act = make_activity({"WO-350": 600})(cfg(), st)
    verdicts = {"WO-350": "idle"}
    assert watcher.finish_reported_orders(cfg(), st, act, verdicts) == [], st["finished"]
    os.makedirs(watcher.REPORTS, exist_ok=True)
    with open(watcher.report_file("WO-350"), "w") as fh:
        fh.write("DONE\n- project: wms shadow\n")
    assert watcher.finish_reported_orders(cfg(), st, act, verdicts) == ["WO-350"]
    assert sorted(st["finished"]) == ["WO-350"], st["finished"]
    assert st["waiting"] == {}, "a finished order is not also waiting for a person"
    # and nothing is finished while the turn is still running
    st2 = state(("WO-353", "ses_c", 600))
    with open(watcher.report_file("WO-353"), "w") as fh:
        fh.write("DONE\n- project: x\n")
    assert watcher.finish_reported_orders(cfg(), st2, make_activity({"WO-353": 600})(cfg(), st2),
                                          {"WO-353": "working"}) == []
    return "the report file finishes the order, so the slot frees with nobody watching"


def test_a_report_file_never_sent_is_queued_for_the_planner() -> str:
    """WO-157: the file freed the slot while the planner was still waiting for the report.

    Its order was finished from ``reports/WO-157.md`` at 13:03 on 2026-11-13 - and the
    planner was saying *"WO-157 was posted earlier and I haven't seen its report"*, with an
    empty outbox, so nothing had even tried to deliver it.  A finish from the file *alone*
    now queues that file, which is the delivery the watcher already flushes.
    """
    tmp_env()
    # the agent sent its report the usual way as well: nothing to re-send
    st = state(("WO-390", "ses_a", 600))
    act = make_activity({"WO-390": 600})(cfg(), st)
    os.makedirs(watcher.REPORTS, exist_ok=True)
    with open(watcher.report_file("WO-390"), "w") as fh:
        fh.write("DONE\n- project: wms-api (docs/CLIENT-DEPLOYMENT.md)\n- commits: b3e9771\n")
    got = watcher.finish_reported_orders(cfg(), st, act, {"WO-390": "idle"},
                                         check=lambda oid, sid, moved: True)
    assert got == ["WO-390"], got
    assert watcher.outbox_files() == [], "a report its agent sent is never sent twice"
    # the same, except its agent never sent anything: the file is all the planner gets
    st2 = state(("WO-391", "ses_b", 600))
    act2 = make_activity({"WO-391": 600})(cfg(), st2)
    with open(watcher.report_file("WO-391"), "w") as fh:
        fh.write("DONE\n- project: wms-api\n- tests: 30 suites / 252 tests passed\n")
    got = watcher.finish_reported_orders(cfg(), st2, act2, {"WO-391": "idle"},
                                         check=lambda oid, sid, moved: False)
    assert got == ["WO-391"], got
    queued = watcher.outbox_files()
    assert len(queued) == 1, queued
    with open(queued[0]) as fh:
        raw = fh.read()
    assert "never sent by its agent" in raw, raw          # why the watcher is sending it
    body = bridge.read_queued(queued[0])
    assert body.startswith("DONE\n- project: wms-api"), body
    assert "252 tests passed" in body, body
    assert "its own report never reached you" in body, "the planner is told where it came from"
    # and it is queued once: the order is finished, so nothing runs this again
    assert watcher.finish_reported_orders(cfg(), st2, act2, {"WO-391": "idle"}) == []
    assert len(watcher.outbox_files()) == 1, "the same report was queued twice"
    return "a report that was written and never sent is queued once, and says where it came from"


def test_a_finished_order_drops_its_waiting_mark() -> str:
    """WO-130 and WO-148 sat in `waiting` for days after they were finished.

    A finish removes the mark, and a later pass re-added it: the verdict for a finished
    order's session still reads "parked".  Status hides it (finished wins the row), but
    reaping treats a parked order's view as one somebody is answering and never closes it.
    """
    tmp_env()
    st = state(("WO-395", "ses_a", 60))
    st["waiting"] = {"WO-395": time.time() - 3 * 86400}
    st["finished"] = {"WO-395": time.time() - 2 * 86400}
    assert watcher.note_waiting(st, {"WO-395": "waiting"}) == []
    assert st["waiting"] == {}, st["waiting"]
    # a live, unfinished order that parks still parks
    st2 = state(("WO-396", "ses_b", 60))
    assert watcher.note_waiting(st2, {"WO-396": "waiting"}) == ["WO-396"]
    assert sorted(st2["waiting"]) == ["WO-396"], st2["waiting"]
    return "a finished order never holds a waiting mark, so no view is treated as parked"


def test_a_report_in_the_transcript_finishes_the_order_too() -> str:
    """A report sent the usual way counts as well, and a template read never does."""
    tmp_env()
    st = state(("WO-351", "ses_a", 600), ("WO-352", "ses_b", 600))
    act = make_activity({"WO-351": 600, "WO-352": 600})(cfg(), st)
    report = ("DONE\n- project: order platform server\n- branch: feat/wms-shadow\n"
              "- commits: 2b9f688 (pushed)\n- tests: npm test 39 pass / 0 fail\n"
              "- notes: nothing outstanding\n")

    def fake(binp, method, path, payload=None, timeout=60):
        if "limit=6" not in path:
            raise AssertionError("the report scan asked for the wrong depth: %s" % path)
        # an agent that talks after it reports: the report sits three entries back, which
        # is exactly what WO-120 did (DONE at 15:51:37, two more messages, then idle)
        chat = [{"type": "idle"},
                {"type": "assistant", "content": [{"type": "text", "text": "and next?"}]},
                {"type": "assistant", "content": [{"type": "text", "text": "reading"}]}]
        if "ses_a" in path:                     # the report the agent typed
            chat.append({"type": "assistant", "content": [
                {"type": "tool", "name": "execute", "state": {
                    "status": "completed", "input": {"code": "const msg = `%s`" % report}}}]})
            chat += [{"type": "assistant", "content": [{"type": "text", "text": "ok"}]},
                     {"type": "assistant", "content": [{"type": "text", "text": "ok"}]}]
        else:
            # a *read* of the AGENTS.md template: that text is a tool's output, not a report
            chat.append({"type": "assistant", "content": [
                {"type": "tool", "name": "read",
                 "state": {"status": "completed",
                           "input": {"filePath": "/home/kilundi/.config/opencode/AGENTS.md"},
                           "output": "When you finish, report: DONE\n  - project: <name>\n"
                                     "  - branch: <branch>\n  - commits: <hashes>\n"}}]})
        return {"data": chat}

    watcher.oc_api = fake
    watcher._REPORT_SCAN.clear()
    got = watcher.finish_reported_orders(
        cfg(), st, act, {"WO-351": "idle", "WO-352": "idle"},
        check=lambda oid, sid, moved: watcher.reported_done(cfg(), sid, moved))
    assert got == ["WO-351"], got
    assert sorted(st["finished"]) == ["WO-351"], st["finished"]
    # the scan is a per-turn memo, not a per-pass one
    assert watcher.reported_done(cfg(), "ses_b", 600) is False
    return "a report three entries back finishes its order; reading the template does not"


# --- watcher.sh status -----------------------------------------------------------------

def test_a_stale_wait_is_not_reported_as_waiting() -> str:
    """An order the chat has moved past, or one already finished, is not "waiting"."""
    tmp_env()
    st = state()
    st["held"] = {"WO-390": time.time(), "WO-391": time.time(), "WO-392": time.time()}
    st["waiting"] = {"WO-393": time.time(), "WO-392": time.time()}
    st["finished"] = {"WO-392": time.time()}
    # this pass read WO-391 only: WO-390 has scrolled out, WO-392 is already finished, and
    # WO-393 is parked but has no session left (a parked order *with* a session keeps its
    # mark - the question is answered in its window)
    gone = watcher.prune_marks(st, [order("WO-391")])
    assert sorted(gone) == [("WO-390", "no longer in the chat"),
                            ("WO-392", "finished"),
                            ("WO-393", "no longer in the chat, and no session left to wait in")], gone
    assert sorted(st["held"]) == ["WO-391"], st["held"]
    assert st["waiting"] == {}, st["waiting"]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        watcher.report_status(cfg(), st)
    assert "WO-391  waiting for a free agent slot" in buf.getvalue(), buf.getvalue()
    assert "WO-390" not in buf.getvalue() and "WO-392" not in buf.getvalue(), buf.getvalue()
    # a parked order with a live session keeps its mark, so its window is not reaped
    st["waiting"]["WO-394"] = time.time()
    act = [{"order": "WO-394", "session": "ses_x", "last": time.time(), "finished": False,
            "live": True}]
    assert watcher.prune_marks(st, [order("WO-391")], act) == []
    assert sorted(st["waiting"]) == ["WO-394"], st["waiting"]
    return "a wait the chat has moved past is dropped, so the queue in status is real"


def test_a_removal_survives_the_save() -> str:
    """The save merges other processes' marks, and still lets a wait be pruned.

    It used to union every key, so a removal came straight back out of the file: the
    marker ``watcher.sh done`` popped was written again by the very next pass, and a
    finished order kept reading as one that was waiting for a slot.
    """
    tmp_env()
    st = state()
    st["held"] = {"WO-370": time.time(), "WO-371": time.time()}
    st["orders"] = {"WO-370": "h1"}
    watcher.save_state(st)
    # another process pops one wait, and marks an order its own reading found
    other = watcher.load_state()
    other["held"].pop("WO-370")
    other["orders"]["WO-372"] = "h2"
    watcher.save_state(other)
    on_disk = json.load(open(watcher.STATE))
    assert "WO-370" not in on_disk["held"], on_disk["held"]
    assert "WO-371" in on_disk["held"], on_disk["held"]
    assert on_disk["orders"].get("WO-372") == "h2", on_disk["orders"]
    return "a pruned wait stays pruned, and another process's marks are never reverted"


def test_status_says_what_is_running() -> str:
    tmp_env()
    st = state(("WO-380", "ses_a", 60), ("WO-381", "ses_b", 3000), ("WO-383", "ses_c", 1200))
    st["held"] = {"WO-382": time.time()}
    st["resume"] = {"WO-381": {"count": 3, "at": 1}}
    watcher.session_activity = make_activity({"WO-380": 60, "WO-381": 3000, "WO-383": 1200})
    watcher._turn_verdict = lambda binp, sid: "working" if sid == "ses_b" else "idle"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        watcher.report_status(cfg(), st)
    out = buf.getvalue()
    assert "2 of 2 slots used (max_agents=2)" in out, out
    assert "WO-380  just moved" in out, out
    assert "WO-381  working" in out, out
    assert "WO-383  idle" in out, out            # its turn ended: it holds no slot
    assert "WO-382  waiting for a free agent slot" in out, out
    assert "WO-381  resumed 3 time(s) - have a look at it" in out, out
    return "watcher.sh status shows the slots used, who is working, who is idle, who waits"


def main() -> int:
    checks = [
        ("a cap holds the third agent", test_slot_cap_holds_the_third),
        ("low memory holds instead", test_low_memory_holds_new_agents),
        ("a probe is not a starter", test_a_probe_never_starts_a_second_service),
        ("a replacement says who", test_a_replacement_says_who_did_it),
        ("a repeated line is said once", test_a_repeated_line_is_said_once),
        ("status names our views", test_status_shows_the_views_we_own),
        ("only old finished state goes", test_state_prunes_only_old_finished_orders),
        ("a finished order frees its slot", test_cap_frees_a_slot_when_one_finishes),
        ("a long suite keeps its slot", test_a_long_suite_keeps_its_slot),
        ("a dead agent frees its slot", test_a_dead_agent_frees_its_slot),
        ("a silent suite keeps its slot", test_a_silent_suite_keeps_its_slot_with_evidence),
        ("an unreadable probe holds", test_an_unreadable_probe_never_frees_a_slot),
        ("a fresh session is not probed", test_a_fresh_session_is_not_probed),
        ("a started order drops its held mark", test_a_started_order_drops_its_held_marker),
        ("a resume that died frees its slot", test_a_resume_that_did_not_take_frees_its_slot),
        ("the race guard holds", test_the_race_guard_holds_before_asking),
        ("a stale wait is dropped", test_a_stale_wait_is_not_reported_as_waiting),
        ("a removal survives the save", test_a_removal_survives_the_save),
        ("a restarted server is noticed", test_service_restart_is_noticed_across_a_watcher_restart),
        ("a dead server is started once", test_ensure_service_only_touches_a_dead_server),
        ("liveness comes from the record", test_activity_reads_the_record),
        ("one small record, not the page", test_liveness_reads_one_small_record_not_the_transcript),
        ("the probe asks once, small", test_the_probe_asks_once_and_only_for_the_tail),
        ("an unreadable record is unknown", test_an_unreadable_record_is_never_idle),
        ("resume needs a restart", test_resume_only_after_a_restart),
        ("resume is bounded", test_resume_is_bounded),
        ("resume skips working and finished", test_resume_skips_working_and_finished),
        ("resume takes the freshest first", test_resume_takes_the_freshest_first),
        ("a stale session is left alone", test_resume_too_old_is_left_alone),
        ("reaping closes quiet windows only", test_reap_closes_quiet_windows_only),
        ("a launched tab is kept", test_a_view_just_launched_for_an_old_agent_is_kept),
        ("a parked turn frees its slot", test_a_parked_turn_frees_its_slot),
        ("a finished order holds no slot", test_a_finished_order_holds_no_slot),
        ("a finished order is not busy", test_a_finished_order_is_not_busy_to_the_ask),
        ("a parked window stays, no resume", test_a_parked_window_is_neither_reaped_nor_resumed),
        ("a report file finishes the order", test_a_report_file_finishes_the_order),
        ("an unsent report is queued", test_a_report_file_never_sent_is_queued_for_the_planner),
        ("a finished order drops its mark", test_a_finished_order_drops_its_waiting_mark),
        ("a transcript report finishes too", test_a_report_in_the_transcript_finishes_the_order_too),
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
