#!/usr/bin/env python3
"""Prove the bridge can drive more than one claude.ai account.

    python3 tests/test_accounts.py

One account = one Chrome profile - Chrome 136+ will not expose the DevTools port on the
default profile, so each account gets its own `--user-data-dir` and its own port, is signed
in once, and is reused from then on. What is proved here is the part behind
``ask-claude --switch-account`` / ``--account`` / ``--account-env`` and the account-aware
``--status``:

  1. a config that predates accounts reads as one account named `main`
  2. the active account decides profile, port, pins and chat; the environment still wins
  3. each account has its own pinned-tab file (and the old single pin still serves `main`)
  4. a switch delivers the outbox through the account it is leaving, first
  5. a switch writes both accounts down, and the old one keeps its own chat
  6. a fresh account is not switched to until it is signed in - config is untouched
  7. a switch never touches the work-order queue
  8. a report produced during a switch goes out exactly once
  9. switching back is symmetric: same chats, same pins, no loss
 10. `--status` names the account, the chat and the pinned tab
 11. `--account-env` is one tab-separated line a shell script can read
 12. `--switch-account` with no name lists the accounts and which window is up
 13. the watcher's own status names the planner it reports to
 14. the restart after a switch goes through `watcher.sh restart`

Everything runs against fakes: no Chrome, no claude.ai, no messages sent. The config, the
pinned-tab files, the outbox and the watcher's state file live in a temp directory.
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

# setup() stubs the switch's restart; this is the real one, for the proof about it
REAL_RESTART_WATCHER = bridge.restart_watcher

CHAT_MAIN = "https://claude.ai/chat/aaaaaaaa-1111-2222-3333-444444444444"
CHAT_SPARE = "https://claude.ai/chat/bbbbbbbb-5555-6666-7777-888888888888"

TMP = ""
UP_PORTS: set[int] = set()
OUTBOX: list[str] = []
DURING: dict = {"add": ""}
LOGGED: list[str] = []


class FakeCDP:
    def __init__(self, url: str):
        self.url = url

    def close(self) -> None:
        pass


def fake_flush(self, timeout=None):
    """Deliver what is queued, and let a proof drop a new report in mid-switch."""
    sent = []
    for f in list(OUTBOX):
        OUTBOX.remove(f)
        sent.append(f)
    if DURING["add"]:
        OUTBOX.append(DURING["add"])
        DURING["add"] = ""
    return {"sent": sent, "remaining": list(OUTBOX), "stopped": ""}


def setup(cfg: dict, *, signed_in: bool = True, chat: str = "") -> None:
    """A bridge that touches nothing real: temp config, temp pins, fake windows.

    The fake window is account-aware: it reports the chat of whatever port is current, so a
    proof cannot pass by reading one account's chat while switching another.
    """
    global TMP
    TMP = tempfile.mkdtemp(prefix="accounts-test-")
    bridge.CONFIG_PATH = os.path.join(TMP, "config.json")
    bridge.LEGACY_TARGET_PATH = os.path.join(TMP, "bridge-target.json")
    with open(bridge.CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=1)

    def url_now() -> str:
        if chat:
            return chat
        return CHAT_SPARE if bridge.PORT == bridge.DEFAULT_PORT + 1 else CHAT_MAIN

    bridge.target_path_for = lambda name: os.path.join(TMP, "bridge-target-%s.json" % name)
    bridge.port_alive = lambda port: port in UP_PORTS
    bridge.ensure_chrome = lambda url=None: True
    bridge.Bridge.flush_outbox = fake_flush
    bridge.Bridge.connect = lambda self, want_composer=True, navigate=True: FakeCDP(self.page_url)
    bridge.Bridge.state = lambda self, c: {"url": url_now(), "composer": signed_in,
                                           "ready": "complete", "target": "tab-%d" % bridge.PORT}
    bridge.Bridge._pages = lambda self: [{"id": "t1", "type": "page", "url": url_now(),
                                          "webSocketDebuggerUrl": "ws://t1"}]
    bridge.Bridge._ask_with = lambda self, c, text, timeout, images=None: "seeded: " + text
    bridge.restart_watcher = lambda: "restarted (systemd user unit, pid 4242)"
    OUTBOX.clear()
    DURING["add"] = ""
    UP_PORTS.clear()
    LOGGED.clear()
    bridge.use_account()          # resolve from this proof's config, not the last one's


def config(*accounts: tuple[str, str], active: str = "main", legacy: str = CHAT_MAIN) -> dict:
    return {"chat_url": legacy, "active_account": active,
            "accounts": {name: {"chat_url": chat} for name, chat in accounts}}


def read_config() -> dict:
    with open(bridge.CONFIG_PATH) as f:
        return json.load(f)


# ------------------------------------------------------------------ the checks
def test_a_legacy_config_reads_as_one_account() -> str:
    setup({"chat_url": CHAT_MAIN, "watch": {"max_agents": 2}})
    assert bridge.account_names() == ["main"], bridge.account_names()
    assert bridge.active_account() == "main"
    spec = bridge.account_spec()
    assert spec["chat_url"] == CHAT_MAIN, "the old top-level chat_url is main's chat: %s" % spec
    assert spec["profile"] == bridge.DEFAULT_PROFILE, spec
    assert spec["port"] == bridge.DEFAULT_PORT, spec
    # the single pin a pre-accounts bridge wrote is still main's pin
    with open(bridge.LEGACY_TARGET_PATH, "w") as f:
        json.dump({"target": "old-tab"}, f)
    bridge.use_account()
    assert bridge.load_target() == "old-tab", bridge.load_target()
    # ... until main pins a tab of its own
    bridge.save_target("new-tab")
    assert bridge.load_target() == "new-tab", bridge.load_target()
    assert json.load(open(bridge.target_path_for("main")))["target"] == "new-tab"
    return "a config without accounts is one account named main, legacy chat and pin included"


def test_the_active_account_decides_everything() -> str:
    setup(config(("main", CHAT_MAIN), ("spare", ""), active="spare"))
    assert bridge.account_names() == ["main", "spare"], bridge.account_names()
    assert bridge.active_account() == "spare"
    spec = bridge.account_spec()
    assert spec["name"] == "spare" and spec["port"] == bridge.DEFAULT_PORT + 1, spec
    assert spec["profile"].endswith("claude-bridge-chrome-spare"), spec
    assert spec["chat_url"] == "", "the spare has no chat yet: %s" % spec
    assert bridge.target_url() == bridge.NEW_CHAT_URL, bridge.target_url()
    # --account NAME for one call
    spec = bridge.use_account("main")
    assert bridge.ACCOUNT == "main" and bridge.PORT == bridge.DEFAULT_PORT, (bridge.ACCOUNT, bridge.PORT)
    assert bridge.target_url() == CHAT_MAIN
    assert bridge.PROFILE == bridge.DEFAULT_PROFILE
    return "the active account sets profile, port, pin and chat; --account changes it per call"


def test_the_environment_still_wins() -> str:
    setup(config(("main", CHAT_MAIN)))
    keep = dict(os.environ)
    try:
        os.environ["CLAUDE_BRIDGE_PORT"] = "9333"
        os.environ["CLAUDE_BRIDGE_PROFILE"] = "/tmp/some-other-profile"
        spec = bridge.use_account("main")
        assert bridge.PORT == 9333 and bridge.BASE.endswith(":9333"), (bridge.PORT, bridge.BASE)
        assert bridge.PROFILE == "/tmp/some-other-profile", bridge.PROFILE
        assert spec["port"] == bridge.DEFAULT_PORT, "the spec itself is unchanged: %s" % spec
    finally:
        os.environ.clear()
        os.environ.update(keep)
    return "CLAUDE_BRIDGE_PORT / CLAUDE_BRIDGE_PROFILE override the account, as before"


def test_pins_are_per_account() -> str:
    setup(config(("main", CHAT_MAIN), ("spare", CHAT_SPARE)))
    bridge.use_account("main")
    bridge.save_target("tab-on-main")
    bridge.use_account("spare")
    bridge.save_target("tab-on-spare")
    assert bridge.load_target() == "tab-on-spare", bridge.load_target()
    bridge.use_account("main")
    assert bridge.load_target() == "tab-on-main", "no crosstalk between accounts"
    files = sorted(f for f in os.listdir(TMP) if f.startswith("bridge-target"))
    assert files == ["bridge-target-main.json", "bridge-target-spare.json"], files
    return "each account has its own pinned tab; a pin on one is never read on the other"


def test_a_switch_delivers_the_outbox_first() -> str:
    setup(config(("main", CHAT_MAIN), ("spare", "")))
    UP_PORTS.add(9223)
    OUTBOX.extend(["outbox/old-report-1.md", "outbox/old-report-2.md"])
    seen: list[str] = []
    real_flush = fake_flush

    def watching_flush(self, timeout=None):
        # what the config says *at the moment we flush*: still the account we are leaving
        seen.append(read_config().get("active_account"))
        seen.append(",".join(OUTBOX))
        return real_flush(self, timeout)

    bridge.Bridge.flush_outbox = watching_flush
    res = bridge.switch_account("spare", timeout=5)
    assert seen[0] == "main", "the outbox must be flushed while main is still the account: %s" % seen
    assert seen[1] == "outbox/old-report-1.md,outbox/old-report-2.md", seen
    assert res["sent"] == ["outbox/old-report-1.md", "outbox/old-report-2.md"], res
    assert res["remaining"] == [] and OUTBOX == [], OUTBOX
    return "the outbox goes out through the account being left, before anything is written"


def test_a_switch_writes_both_accounts() -> str:
    setup(config(("main", CHAT_MAIN), ("spare", "")))
    UP_PORTS.add(9223)
    res = bridge.switch_account("spare", timeout=5)
    assert res["switched"] is True, res
    cfg = read_config()
    assert cfg["active_account"] == "spare", cfg
    assert cfg["chat_url"] == CHAT_SPARE, "the legacy key mirrors the active account: %s" % cfg
    assert sorted(cfg["accounts"]) == ["main", "spare"], cfg["accounts"]
    assert cfg["accounts"]["main"]["chat_url"] == CHAT_MAIN, \
        "the account we left keeps its own chat: %s" % cfg["accounts"]["main"]
    spare = cfg["accounts"]["spare"]
    assert spare["chat_url"] == CHAT_SPARE and spare["port"] == 9223, spare
    assert spare["profile"].endswith("claude-bridge-chrome-spare"), spare
    assert bridge.ACCOUNT == "spare" and bridge.PORT == 9223
    assert bridge.load_target() == res["target"] != "", res
    assert "watcher" in res and res["watcher"].startswith("restarted"), res
    return "a switch pins the new account's tab and leaves the old account's chat intact"


def test_a_fresh_account_waits_for_the_sign_in() -> str:
    setup(config(("main", CHAT_MAIN), ("spare", "")), signed_in=False)
    UP_PORTS.add(9223)
    res = bridge.switch_account("spare", timeout=5)
    assert res["switched"] is False and res["signed_in"] is False, res
    assert "sign in" in res["detail"], res
    assert read_config()["active_account"] == "main", "a switch that cannot finish changes nothing"
    assert bridge.ACCOUNT == "main" and bridge.PORT == bridge.DEFAULT_PORT, \
        "and this process is left where it was: %s" % (bridge.ACCOUNT, bridge.PORT)
    # the window is up with that profile: sign in there, run it again, and it finishes
    setup(config(("main", CHAT_MAIN), ("spare", "")), signed_in=True)
    UP_PORTS.add(9223)
    assert bridge.switch_account("spare", timeout=5)["switched"] is True
    return "a fresh account is brought up and the switch stops until it is signed in"


def test_a_switch_never_touches_the_queue() -> str:
    setup(config(("main", CHAT_MAIN), ("spare", "")))
    UP_PORTS.add(9223)
    watcher.STATE = os.path.join(TMP, "state.json")
    st = {"orders": {"WO-1": "hash"}, "starts": {"WO-1": {"status": "started"}},
          "sessions": {"WO-1": "ses_x"}, "finished": {}}
    with open(watcher.STATE, "w") as f:
        json.dump(st, f)
    before = open(watcher.STATE, "rb").read()
    bridge.switch_account("spare", timeout=5)
    after = open(watcher.STATE, "rb").read()
    assert before == after, "the switch must not touch state.json - no re-dispatch, no lost order"
    return "state.json is byte-for-byte identical across a switch"


def test_a_report_during_the_switch_goes_out_once() -> str:
    setup(config(("main", CHAT_MAIN), ("spare", "")))
    UP_PORTS.add(9223)
    OUTBOX.append("outbox/first.md")
    DURING["add"] = "outbox/written-during-the-switch.md"     # arrives mid-switch
    res = bridge.switch_account("spare", timeout=5)
    assert res["sent"] == ["outbox/first.md"], res
    assert res["remaining"] == ["outbox/written-during-the-switch.md"], res
    assert OUTBOX == ["outbox/written-during-the-switch.md"], OUTBOX
    # the next pass (or the next flush) delivers it, once, and never re-sends the first
    res2 = bridge.Bridge().flush_outbox()
    assert res2["sent"] == ["outbox/written-during-the-switch.md"], res2
    assert res2["remaining"] == [] and OUTBOX == [], res2
    return "a report produced during a switch is delivered exactly once, by the next flush"


def test_switching_back_is_symmetric() -> str:
    setup(config(("main", CHAT_MAIN), ("spare", "")))
    UP_PORTS.update({9222, 9223})
    bridge.switch_account("spare", timeout=5)
    spare_pin = bridge.load_target()
    assert spare_pin != "", spare_pin
    res = bridge.switch_account("main", timeout=5)
    assert res["switched"] is True and bridge.ACCOUNT == "main", res
    assert bridge.load_target() == res["target"] != "", (bridge.load_target(), res)
    cfg = read_config()
    assert cfg["active_account"] == "main" and cfg["chat_url"] == CHAT_MAIN, cfg
    assert cfg["accounts"]["main"]["chat_url"] == CHAT_MAIN, cfg["accounts"]
    assert cfg["accounts"]["spare"]["chat_url"] == CHAT_SPARE, cfg["accounts"]
    assert json.load(open(bridge.target_path_for("spare")))["target"] == spare_pin, \
        "the spare keeps the tab it pinned: both accounts keep their own"
    return "switching back returns to main's chat; both accounts keep their own chat and pin"


def test_status_names_the_account_chat_and_tab() -> str:
    setup(config(("main", CHAT_MAIN), ("spare", CHAT_SPARE), active="main"))
    UP_PORTS.add(9222)
    bridge.save_target("tab-1")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = bridge.main(["--status", "--json"])
    out = json.loads(buf.getvalue())
    assert code == 0, code
    assert out["account"] == "main" and out["port"] == 9222, out
    assert out["profile"] == bridge.DEFAULT_PROFILE, out
    assert out["target"] == "tab-1", out
    by_name = {a["name"]: a for a in out["accounts"]}
    assert by_name["main"]["active"] is True and by_name["main"]["window"] is True, out["accounts"]
    assert by_name["spare"]["active"] is False and by_name["spare"]["window"] is False, out["accounts"]
    assert by_name["spare"]["chat_url"] == CHAT_SPARE, out["accounts"]
    return "--status names the account, its chat, its port and the pinned tab"


def test_account_env_is_one_shell_line() -> str:
    setup(config(("main", CHAT_MAIN), ("spare", CHAT_SPARE), active="spare"))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert bridge.main(["--account-env"]) == 0
    fields = buf.getvalue().rstrip("\n").split("\t")
    assert len(fields) == 4, fields
    assert fields[0].endswith("claude-bridge-chrome-spare") and fields[1] == "9223", fields
    assert fields[2] == CHAT_SPARE and fields[3] == "spare", fields
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert bridge.main(["--account-env", "--account", "main"]) == 0
    fields = buf.getvalue().rstrip("\n").split("\t")
    assert fields[1] == "9222" and fields[2] == CHAT_MAIN and fields[3] == "main", fields
    return "--account-env prints PROFILE<TAB>PORT<TAB>CHAT<TAB>NAME for scripts"


def test_listing_accounts_shows_which_window_is_up() -> str:
    setup(config(("main", CHAT_MAIN), ("spare", ""), active="main"))
    UP_PORTS.add(9222)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = bridge.main(["--switch-account"])
    out = buf.getvalue()
    assert code == 0, code
    assert "active: main" in out, out
    assert "* main" in out and "window up" in out, out
    assert "spare" in out and "window down" in out, out
    assert "(none yet - a fresh chat opens on switch)" in out, out
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        bridge.main(["--switch-account", "--json"])
    rows = {a["name"]: a for a in json.loads(buf.getvalue())["accounts"]}
    assert rows["main"]["window"] is True and rows["spare"]["window"] is False, rows
    return "listing the accounts says which one is active and which window is up"


def test_the_watcher_status_names_the_planner() -> str:
    setup(config(("main", CHAT_MAIN), ("spare", ""), active="main"))
    st = {"orders": {}, "starts": {}, "sessions": {}, "files": []}
    cfg = {"watch": {"min_free_mb": 0}, "active_account": "spare",
           "accounts": {"main": {"chat_url": CHAT_MAIN}, "spare": {"chat_url": CHAT_SPARE}}}
    watcher.session_activity = lambda cfg_, st_, now=None: []
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        watcher.report_status(cfg, st)
    assert "planner: spare account -> %s" % CHAT_SPARE in buf.getvalue(), buf.getvalue()
    # a config with no accounts block says nothing at all about planners
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        watcher.report_status({"watch": {"min_free_mb": 0}}, st)
    assert "planner" not in buf.getvalue(), buf.getvalue()
    return "the loop's status names the planner it reports to (and only when accounts exist)"


def test_the_restart_goes_through_watcher_sh() -> str:
    setup(config(("main", CHAT_MAIN)))
    calls: list[list[str]] = []

    class FakeRun:
        returncode = 0
        stdout = "watcher restarted (systemd user unit, pid 4242)\n"
        stderr = ""

    class FakeSubprocess:
        @staticmethod
        def run(argv, **kw):
            calls.append(list(argv))
            return FakeRun()

    keep = bridge.subprocess
    bridge.subprocess = FakeSubprocess          # only inside bridge's own namespace
    try:
        line = REAL_RESTART_WATCHER()           # setup() stubs the switch's copy
    finally:
        bridge.subprocess = keep
    assert calls == [["bash", os.path.join(bridge.HERE, "watcher.sh"), "restart"]], calls
    assert line == "watcher restarted (systemd user unit, pid 4242)", line
    return "the post-switch restart is `watcher.sh restart`, which knows about the supervisor"


def main() -> int:
    checks = [
        ("legacy config = one account", test_a_legacy_config_reads_as_one_account),
        ("active account decides all", test_the_active_account_decides_everything),
        ("environment still wins", test_the_environment_still_wins),
        ("pins are per account", test_pins_are_per_account),
        ("switch flushes outbox first", test_a_switch_delivers_the_outbox_first),
        ("switch writes both accounts", test_a_switch_writes_both_accounts),
        ("fresh account waits for sign-in", test_a_fresh_account_waits_for_the_sign_in),
        ("switch never touches the queue", test_a_switch_never_touches_the_queue),
        ("report during switch goes out once", test_a_report_during_the_switch_goes_out_once),
        ("switching back is symmetric", test_switching_back_is_symmetric),
        ("status names account/chat/tab", test_status_names_the_account_chat_and_tab),
        ("account-env is one shell line", test_account_env_is_one_shell_line),
        ("listing shows which window is up", test_listing_accounts_shows_which_window_is_up),
        ("watcher status names the planner", test_the_watcher_status_names_the_planner),
        ("restart goes through watcher.sh", test_the_restart_goes_through_watcher_sh),
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
