#!/usr/bin/env python3
"""Prove the launcher groups work by repository: one window, a tab per work order.

    python3 tests/test_one_window_per_repo.py

Five orders in one repo used to be five qterminal windows, all titled "... - projects".
qterminal 2.4.0 has no flag for opening a tab in a window that is already up, and no D-Bus
service to ask, so a repository's tabs live in a tmux session named after the directory
(`cb-<repo>-<hash>`) and the terminal window is opened only when the repository has none.

Also proven here: reaping closes a tab through its session, so it kills the agent's TUI
whether or not anybody is looking at it - including the tabs whose window was closed by
hand, which nothing used to sweep.

Offline: no display, no terminal, no tmux server - the emulator and tmux are fakes.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import watcher  # noqa: E402

PROJECTS = "/home/kilundi/projects"
WMS = "/home/kilundi/projects/warehouse-management-system"


class FakeTmux:
    """A tmux server in a dict: its sessions, each one's tabs, and who is watching."""

    def __init__(self, sessions=None, clients=None):
        self.sessions = {k: list(v) for k, v in (sessions or {}).items()}
        self.clients = dict(clients or {})       # slug -> how many terminals attached
        self.calls: list[list[str]] = []

    def __call__(self, *args, timeout=10):
        self.calls.append([str(a) for a in args])
        cmd, rest = str(args[0]), [str(a) for a in args[1:]]

        def opt(flag, default=None):
            return rest[rest.index(flag) + 1] if flag in rest else default

        if cmd == "list-sessions":
            return 0, "".join(s + "\n" for s in self.sessions)
        if cmd == "has-session":
            return (0, "") if opt("-t") in self.sessions else (1, "no such session")
        if cmd == "list-clients":
            return 0, "".join("/dev/pts/%d: fake (attached)\n" % i
                              for i in range(int(self.clients.get(opt("-t"), 0))))
        if cmd == "list-windows":
            return 0, "".join(w + "\n" for w in self.sessions.get(opt("-t"), []))
        if cmd in ("new-session", "new-window"):
            slug = opt("-s") or opt("-t")
            self.sessions.setdefault(slug, []).append(opt("-n") or "bash")
            return 0, ""
        if cmd == "kill-window":
            slug, name = opt("-t").split(":", 1)
            if name not in self.sessions.get(slug, []):
                return 1, "can't find window"
            self.sessions[slug].remove(name)
            return 0, ""
        if cmd == "kill-session":
            self.sessions.pop(opt("-t"), None)
            return 0, ""
        return 0, ""                              # select-window, set-option, ...

    def commands(self) -> list[str]:
        return [c[0] for c in self.calls]


class Spawner:
    """A terminal emulator that only writes down what it was asked to run."""

    def __init__(self):
        self.argv: list[list[str]] = []

    def Popen(self, argv, **kw):
        self.argv.append([str(a) for a in argv])
        return types.SimpleNamespace(pid=4242)

    def reset(self):
        self.argv = []

    def running(self) -> str:
        return " ".join(" ".join(a) for a in self.argv)


SPAWN = Spawner()
SERVER: FakeTmux


def tmp_env() -> str:
    """Every path this suite touches is a temp dir - and tmux is never the real one."""
    global SERVER
    d = tempfile.mkdtemp(prefix="one-window-per-repo-")
    watcher.STATE = os.path.join(d, "state.json")
    watcher.INBOX = os.path.join(d, "inbox")
    watcher.LOG = os.path.join(d, "watcher.log")
    watcher.TERMINAL_LOG = os.path.join(d, "terminal.log")
    watcher.TERMINALS_DIR = os.path.join(d, "terminals")
    os.makedirs(watcher.INBOX, exist_ok=True)
    # a test must never open a real window or talk to a real tmux server
    watcher.tmux_binary = lambda: "/usr/bin/tmux"
    SERVER = FakeTmux()
    watcher.tmux = SERVER
    watcher.subprocess = types.SimpleNamespace(Popen=SPAWN.Popen,
                                               DEVNULL=subprocess.DEVNULL)
    os.environ["DISPLAY"] = os.environ.get("DISPLAY") or ":0"
    SPAWN.reset()
    return d


def cfg(**watch) -> dict:
    base = {"launch_terminal": True, "tabs": True, "reap_idle_seconds": 7200,
            "opencode_bin": "/usr/bin/opencode"}
    base.update(watch)
    return {"watch": base}


def state(*agents) -> dict:
    st: dict = {"orders": {}, "starts": {}, "sessions": {}, "files": []}
    for i, (oid, sid) in enumerate(agents):
        st["sessions"][oid] = sid
        st["starts"][oid] = {"status": "started", "attempts": 1, "session": sid,
                             "updated": time.time() + i * 0.001,
                             "order": {"id": oid, "dir": PROJECTS}}
    return st


def script(sid: str, tab: bool = False) -> str:
    return os.path.join(watcher.TERMINALS_DIR, "%s%s.sh" % ("tab-" if tab else "", sid))


def write_script(sid: str, tab: bool = False, age: float = 9000) -> str:
    os.makedirs(watcher.TERMINALS_DIR, exist_ok=True)
    p = script(sid, tab)
    with open(p, "w") as fh:
        fh.write("#!/bin/sh\n")
    os.utime(p, (time.time() - age,) * 2)
    return p


def activity(ages: dict, st: dict) -> list[dict]:
    now = time.time()
    return [{"order": oid, "session": sid, "last": now - ages[oid], "finished": False,
             "live": False} for oid, sid in st["sessions"].items()]


# --- the window, and the tabs -----------------------------------------------------------------

def test_the_first_order_in_a_repo_opens_one_window_with_its_first_tab() -> str:
    tmp_env()
    assert watcher.open_agent_terminal(cfg(), "WO-1", "ses_a", PROJECTS) is True
    assert len(SPAWN.argv) == 1, SPAWN.argv
    assert "tab-ses_a.sh" in SPAWN.running(), SPAWN.running()
    # what that window runs: attach to the repository's tabs, or make the session with this
    # order as its first tab
    slug = watcher.tmux_session_name(PROJECTS)
    body = open(script("ses_a", tab=True)).read()
    assert "has-session -t %s" % slug in body, body
    assert "new-session -d -s %s -n WO-1 " % slug in body, body
    assert "'bash %s'" % script("ses_a") in body, body
    assert body.rstrip().endswith("attach -t %s" % slug), body
    assert "opencode -s ses_a" in open(script("ses_a")).read(), "the tab must run the agent"
    return "the first order in a repo opens one window, whose first tab is that order"


def test_a_second_order_in_the_same_repo_becomes_a_tab() -> str:
    """The complaint itself: no second window, a tab in the repository's window."""
    tmp_env()
    slug = watcher.tmux_session_name(PROJECTS)
    SERVER.sessions[slug] = ["WO-1"]              # its window is up, showing one tab
    SERVER.clients[slug] = 1
    assert watcher.open_agent_terminal(cfg(), "WO-2", "ses_b", PROJECTS) is True
    assert SPAWN.argv == [], "a second window was opened for the same repository"
    assert ["new-window", "-t", slug, "-n", "WO-2",
            "bash %s" % script("ses_b")] in SERVER.calls, SERVER.calls
    assert SERVER.sessions[slug] == ["WO-1", "WO-2"], SERVER.sessions
    assert "select-window" in SERVER.commands(), SERVER.calls
    return "a second order in the same repo is a tab: one window, not two"


def test_a_different_repo_gets_its_own_window() -> str:
    tmp_env()
    assert watcher.open_agent_terminal(cfg(), "WO-1", "ses_a", PROJECTS) is True
    assert watcher.open_agent_terminal(cfg(), "WO-2", "ses_b", WMS) is True
    assert len(SPAWN.argv) == 2, SPAWN.argv
    assert watcher.tmux_session_name(PROJECTS) != watcher.tmux_session_name(WMS)
    return "another repository is another window: grouping is by directory, not by order"


def test_a_window_closed_by_hand_comes_back_with_its_tabs() -> str:
    tmp_env()
    slug = watcher.tmux_session_name(PROJECTS)
    SERVER.sessions[slug] = ["WO-1", "WO-2"]      # the tabs live on, detached
    SERVER.clients[slug] = 0                      # nobody is watching them
    assert watcher.open_agent_terminal(cfg(), "WO-3", "ses_c", PROJECTS) is True
    assert SERVER.sessions[slug] == ["WO-1", "WO-2", "WO-3"], SERVER.sessions
    assert len(SPAWN.argv) == 1, "a window has to come back to show the tabs"
    assert "tab-ses_c.sh" in SPAWN.running(), SPAWN.running()
    return "a detached repository is re-attached, all its tabs intact, and gets the new one"


def test_tabs_off_still_opens_a_window_per_order() -> str:
    tmp_env()
    slug = watcher.tmux_session_name(PROJECTS)
    SERVER.sessions[slug] = ["WO-1"]
    SERVER.clients[slug] = 1
    assert watcher.open_agent_terminal(cfg(tabs=False), "WO-2", "ses_b", PROJECTS) is True
    assert len(SPAWN.argv) == 1, "tabs: false is one window per order, as it was"
    assert "ses_b.sh" in SPAWN.running() and "tab-ses_b.sh" not in SPAWN.running()
    assert SERVER.calls == [], "tabs: false must not touch tmux at all"
    return "tabs: false gives back exactly the old behaviour"


def test_the_session_name_is_one_per_repo_and_tmux_safe() -> str:
    tmp_env()
    assert watcher.tmux_session_name(PROJECTS) == watcher.tmux_session_name(PROJECTS + "/")
    assert watcher.tmux_session_name(PROJECTS) != watcher.tmux_session_name(WMS)
    ugly = watcher.tmux_session_name("/tmp/My Repo (v2): x.y")
    assert ":" not in ugly and "." not in ugly and " " not in ugly, ugly
    assert watcher.tmux_session_name("/") == "cb-root-%s" % watcher.tmux_session_name("/")[8:20]
    assert watcher.window_name("WO-124") == "WO-124"
    assert ":" not in watcher.window_name("WO:124.2"), watcher.window_name("WO:124.2")
    return "one stable, tmux-safe name per repository (a trailing slash is the same repo)"


# --- reaping the tabs ------------------------------------------------------------------------

def test_reap_closes_a_tab_and_the_window_with_the_last_one() -> str:
    tmp_env()
    slug = watcher.tmux_session_name(PROJECTS)
    st = state(("WO-1", "ses_a"), ("WO-2", "ses_b"))
    SERVER.sessions[slug] = ["WO-1", "WO-2"]
    for sid in ("ses_a", "ses_b"):
        write_script(sid, tab=True)
    got = watcher.reap_windows(cfg(), st, activity({"WO-1": 9000, "WO-2": 9000}, st))
    assert got == ["WO-1", "WO-2"], got
    assert SERVER.sessions == {}, "the last tab must take the repository's window with it"
    assert "kill-session" in SERVER.commands(), SERVER.calls
    assert not os.path.exists(script("ses_a", tab=True)), "the tab script stayed behind"
    assert not os.path.exists(script("ses_a")), "the launcher script stayed behind"
    return "quiet tabs are closed through tmux, and the last one closes the window"


def test_reap_sweeps_a_tab_nobody_can_see() -> str:
    """A window closed by hand orphans its agents; the session is what makes them findable."""
    tmp_env()
    slug = watcher.tmux_session_name(PROJECTS)
    st = state(("WO-9", "ses_z"))
    SERVER.sessions[slug] = ["WO-9"]              # no window, no client, no /proc entry
    write_script("ses_z", tab=True)
    got = watcher.reap_windows(cfg(), st, activity({"WO-9": 9000}, st))
    assert got == ["WO-9"], got
    assert SERVER.sessions == {}, SERVER.sessions
    return "a detached tab is still reaped: its session says the TUI is there"


def test_a_working_agent_keeps_its_tab() -> str:
    tmp_env()
    slug = watcher.tmux_session_name(PROJECTS)
    st = state(("WO-1", "ses_a"))
    SERVER.sessions[slug] = ["WO-1"]
    act = activity({"WO-1": 9000}, st)
    act[0]["live"] = True
    assert watcher.reap_windows(cfg(), st, act, force=True) == []
    assert SERVER.sessions[slug] == ["WO-1"], SERVER.sessions
    assert "kill-window" not in SERVER.commands(), SERVER.calls
    return "a tab whose agent is working is never closed, even by force"


def test_windows_of_another_kind_are_still_reaped() -> str:
    """An order started before tabs existed keeps its own window and its own kill."""
    tmp_env()
    st = state(("WO-1", "ses_a"))
    p = write_script("ses_a")                     # not a tab script: a plain window
    killed: list[int] = []
    watcher._close_window = lambda pid, s: killed.append(pid)
    got = watcher.reap_windows(cfg(), st, activity({"WO-1": 9000}, st),
                               windows=[{"pid": 777, "sid": "ses_a", "script": p,
                                         "tmux": None}])
    assert got == ["WO-1"] and killed == [777], (got, killed)
    return "a plain window (tabs: false, or opened before) is still closed the old way"


def main() -> int:
    checks = [
        ("the first order opens one window", test_the_first_order_in_a_repo_opens_one_window_with_its_first_tab),
        ("a second order becomes a tab", test_a_second_order_in_the_same_repo_becomes_a_tab),
        ("another repo, another window", test_a_different_repo_gets_its_own_window),
        ("a closed window comes back", test_a_window_closed_by_hand_comes_back_with_its_tabs),
        ("tabs off is the old behaviour", test_tabs_off_still_opens_a_window_per_order),
        ("one safe session name per repo", test_the_session_name_is_one_per_repo_and_tmux_safe),
        ("reap closes a tab, then the window", test_reap_closes_a_tab_and_the_window_with_the_last_one),
        ("reap sweeps a detached tab", test_reap_sweeps_a_tab_nobody_can_see),
        ("a working tab is kept", test_a_working_agent_keeps_its_tab),
        ("a plain window is still reaped", test_windows_of_another_kind_are_still_reaped),
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
