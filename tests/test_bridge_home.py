#!/usr/bin/env python3
"""Prove a second pipeline can run from the same code, sharing nothing with the first.

    python3 tests/test_bridge_home.py

``CLAUDE_BRIDGE_HOME`` moves the watcher's and the bridge's *data* - config, state, log,
lock, inbox, reports, outbox - to another directory. A second watcher started from it shares
nothing with the first: no lock (so neither can refuse the other), no queue, no state, no
chat. That is what lets video work run beside the coding loop without the coding loop ever
being interrupted by it. The code stays in one checkout; only the data moves.

The other half of that safety is the account: an account that has never been signed in
resolves to its own `claude.ai/new`, never to the other pipeline's chat. A video loop that
read the coding planner's chat would dispatch coding orders into the video session.

What is proved:

  1. CLAUDE_BRIDGE_HOME moves every data path to the new home
  2. the code stays where it is - only the data moves
  3. the second home is read from the second pipeline's own config
  4. with nothing set, every path is byte-for-byte today's
  5. an account with no chat yet never falls back to the coding chat
  6. the "bridge is down" advice names the right home

Everything runs against fakes: no Chrome, no claude.ai, no opencode (the paths are read in a
subprocess, because they are resolved once, at import).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import bridge  # noqa: E402
import watcher  # noqa: E402


def _paths(home: str | None) -> dict:
    """Import both modules in a fresh process with (or without) CLAUDE_BRIDGE_HOME."""
    code = (
        "import json,sys;"
        "sys.path.insert(0, %r);"
        "import watcher, bridge;"
        "print(json.dumps({"
        "'cfg': (watcher.load_config().get('watch') or {}).get('default_dir'),"
        "'w_home': watcher.HOME, 'w_state': watcher.STATE, 'w_config': watcher.CONFIG,"
        "'w_log': watcher.LOG, 'w_lock': watcher.LOCK, 'w_inbox': watcher.INBOX,"
        "'w_reports': watcher.REPORTS, 'w_downloads': watcher.DOWNLOADS,"
        "'w_needs': watcher.NEEDS_ORDER,"
        "'b_home': bridge.HOME, 'b_config': bridge.CONFIG_PATH,"
        "'b_target': bridge.TARGET_PATH, 'b_spare_pin': bridge.target_path_for('spare'),"
        "'b_outbox': bridge.OUTBOX, 'b_drafts': bridge.DRAFTS,"
        "'b_here': bridge.HERE, 'b_launcher': bridge.LAUNCHER,"
        "'account': bridge.ACCOUNT, 'port': bridge.PORT}))" % ROOT
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_BRIDGE_")}
    if home is not None:
        env["CLAUDE_BRIDGE_HOME"] = home
    p = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    if p.returncode != 0:
        raise AssertionError("subprocess failed: %s%s" % (p.stdout[-800:], p.stderr[-800:]))
    return json.loads(p.stdout.strip().splitlines()[-1])


# --- 1 ---------------------------------------------------------------------------------
def test_the_home_moves_every_data_path():
    home = tempfile.mkdtemp(prefix="video-home-")
    got = _paths(home)
    assert got["w_home"] == home, got["w_home"]
    moved = ["w_state", "w_config", "w_log", "w_lock", "w_inbox", "w_reports",
             "w_downloads", "w_needs", "b_config", "b_target", "b_spare_pin",
             "b_outbox", "b_drafts"]
    stray = {k: got[k] for k in moved if not got[k].startswith(home + os.sep)}
    assert not stray, "these did not move into the home: %s" % stray
    assert got["w_lock"] == os.path.join(home, "watcher.lock"), got["w_lock"]
    # two homes therefore cannot contend for one lock, which is the whole point
    other = _paths(tempfile.mkdtemp(prefix="other-home-"))
    assert other["w_lock"] != got["w_lock"], other["w_lock"]
    assert other["b_outbox"] != got["b_outbox"], other["b_outbox"]
    return "13 data paths follow the home, and a second home shares none of them"


# --- 2 ---------------------------------------------------------------------------------
def test_the_code_stays_where_it_is():
    got = _paths(tempfile.mkdtemp(prefix="video-home-"))
    assert got["b_here"] == ROOT, got["b_here"]
    assert got["b_launcher"] == os.path.join(ROOT, "start-bridge-chrome.sh"), got["b_launcher"]
    return "HERE and the launcher stay in the checkout - only the data moves"


# --- 3 ---------------------------------------------------------------------------------
def test_the_second_home_reads_its_own_config():
    home = tempfile.mkdtemp(prefix="video-home-")
    with open(os.path.join(home, "config.json"), "w") as f:
        json.dump({"watch": {"default_dir": "/tmp/the-video-home"},
                   "accounts": {"spare": {"profile": "/tmp/p", "port": 9223,
                                          "chat_url": "https://claude.ai/chat/spare"}},
                   "active_account": "spare"}, f)
    got = _paths(home)
    assert got["cfg"] == "/tmp/the-video-home", got
    assert got["account"] == "spare" and got["port"] == 9223, got
    return "the second pipeline reads the second config: account spare on port 9223"


# --- 4 ---------------------------------------------------------------------------------
def test_no_env_means_todays_paths():
    got = _paths(None)
    assert got["w_home"] == ROOT, got["w_home"]
    assert got["w_state"] == os.path.join(ROOT, "state.json"), got["w_state"]
    assert got["w_lock"] == os.path.join(ROOT, "watcher.lock"), got["w_lock"]
    assert got["w_log"] == os.path.join(ROOT, "watcher.log"), got["w_log"]
    assert got["b_config"] == os.path.join(ROOT, "config.json"), got["b_config"]
    assert got["b_outbox"] == os.path.join(ROOT, "outbox"), got["b_outbox"]
    # the pin file is the *account's* pin, which the live config decides; what matters here
    # is that it is not in some new home
    assert os.path.dirname(got["b_target"]) == ROOT, got["b_target"]
    assert os.path.dirname(got["b_spare_pin"]) == ROOT, got["b_spare_pin"]
    return "unset, every path is the one the coding loop has always used"


# --- 5 ---------------------------------------------------------------------------------
def test_an_unsigned_account_never_reads_the_coding_chat():
    """The one contamination that would matter: video work read from the coding planner."""
    chat_main = "https://claude.ai/chat/2999ac04-5f70-49d5-8925-fb516e7232ba"
    cfg = {"accounts": {"main": {"profile": "/p/main", "port": 9222, "chat_url": chat_main},
                        "spare": {"profile": "/p/spare", "port": 9223, "chat_url": ""}},
           "active_account": "spare"}
    spare = bridge.account_spec("spare", cfg)
    assert spare["port"] == 9223 and spare["chat_url"] == "", spare
    assert bridge.target_url(cfg, spare) == bridge.NEW_CHAT_URL, bridge.target_url(cfg, spare)
    assert chat_main not in bridge.target_url(cfg, spare)
    # and the coding account still resolves to its own chat
    main = bridge.account_spec("main", cfg)
    assert bridge.target_url(cfg, main) == chat_main, bridge.target_url(cfg, main)
    return "an unsigned account opens its own claude.ai/new - never the coding chat"


# --- 6 ---------------------------------------------------------------------------------
def test_the_advice_names_the_right_home():
    home, here = watcher.HOME, watcher.HERE
    try:
        watcher.HOME = here
        plain = watcher.bridge_down_advice()
        assert "CLAUDE_BRIDGE_HOME" not in plain, plain
        assert "bridge-up.sh" in plain, plain
        watcher.HOME = "/tmp/some-video-home"
        moved = watcher.bridge_down_advice()
        assert "CLAUDE_BRIDGE_HOME=/tmp/some-video-home" in moved, moved
        assert "bridge-up.sh" in moved, moved
    finally:
        watcher.HOME, watcher.HERE = home, here
    return "the coding loop gets the plain command; a second home gets its own"


def test_agent_watch_follows_the_home():
    """Following an agent has to follow the *right* loop: video agents are not coding ones."""
    home = tempfile.mkdtemp(prefix="video-home-")
    with open(os.path.join(home, "state.json"), "w") as f:
        json.dump({"sessions": {}, "starts": {}}, f)
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_BRIDGE_")}
    env["CLAUDE_BRIDGE_HOME"] = home
    p = subprocess.run([sys.executable, os.path.join(ROOT, "agent-watch.py"), "--once"],
                       env=env, capture_output=True, text=True)
    assert p.returncode != 0, "an empty home has nothing to follow: %s" % p.stdout
    out = p.stdout + p.stderr
    assert os.path.join(home, "state.json") in out, \
        "it read %s instead of the home: %s" % (home, out[-400:])
    return "the watcher reads the home's state, not the coding loop's"


def test_the_unit_follows_the_home():
    """A restart has to reach *this* pipeline's unit - one env var, never the other loop's."""
    def unit_of(home, cfg=None, extra=None):
        if cfg is not None:
            with open(os.path.join(home, "config.json"), "w") as f:
                json.dump(cfg, f)
        env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_BRIDGE_")}
        if home is not None:
            env["CLAUDE_BRIDGE_HOME"] = home
        env.update(extra or {})
        p = subprocess.run(["bash", os.path.join(ROOT, "watcher.sh"), "unit"],
                           env=env, capture_output=True, text=True)
        assert p.returncode == 0, p.stderr
        return p.stdout.strip()

    named = tempfile.mkdtemp(prefix="video-home-")
    assert unit_of(named, cfg={"watch": {"systemd_unit": "claude-bridge-video"}}) \
        == "claude-bridge-video", "the video home must restart its own unit"
    # a home that names no unit - and a home with no config at all - are the coding loop's
    assert unit_of(tempfile.mkdtemp(prefix="plain-home-"), cfg={"watch": {"interval": 30}}) \
        == "claude-bridge-watcher"
    assert unit_of(tempfile.mkdtemp(prefix="empty-home-")) == "claude-bridge-watcher"
    # with nothing set at all, nothing changed
    assert unit_of(None) == "claude-bridge-watcher"
    # and the explicit variable still wins outright (the unit file keeps using it)
    assert unit_of(named, cfg={"watch": {"systemd_unit": "claude-bridge-video"}},
                   extra={"CLAUDE_BRIDGE_UNIT": "elsewhere"}) == "elsewhere"
    return "a home names its own unit; unnamed, it is still claude-bridge-watcher"


def test_account_env_never_prints_an_empty_field():
    """The reader is `IFS=$'\t' read`, and a tab is IFS whitespace: one empty field shifts
    every later one.  An unsigned account used to hand the shell its NAME as the CHAT and an
    empty name, so bridge-up.sh fell back to "main" and opened the *coding* window."""
    home = tempfile.mkdtemp(prefix="video-home-")
    chat_main = "https://claude.ai/chat/2999ac04-5f70-49d5-8925-fb516e7232ba"
    with open(os.path.join(home, "config.json"), "w") as f:
        json.dump({"accounts": {
            "main": {"profile": "/p/main", "port": 9222, "chat_url": chat_main},
            "spare": {"profile": "/p/spare", "port": 9223, "chat_url": ""}},
            "active_account": "spare"}, f)
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_BRIDGE_")}
    env["CLAUDE_BRIDGE_HOME"] = home

    def env_line(*args):
        p = subprocess.run([sys.executable, os.path.join(ROOT, "bridge.py"), "--account-env",
                            *args], env=env, capture_output=True, text=True)
        assert p.returncode == 0, p.stderr
        return p.stdout.strip()

    line = env_line()                      # the active account: spare, signed in never
    fields = line.split("\t")
    assert len(fields) == 4 and all(fields), fields
    assert fields[3] == "spare" and fields[2] == bridge.NEW_CHAT_URL, fields

    # what a shell really sees - this is the assertion the bug would fail
    shell = 'IFS=$\'\\t\' read -r a_profile a_port a_chat a_name <<<"$1"; ' \
            'printf "%s|%s|%s" "$a_port" "$a_chat" "$a_name"'
    seen = subprocess.run(["bash", "-c", shell, "bash", line],
                          capture_output=True, text=True)
    assert seen.stdout == "9223|%s|spare" % bridge.NEW_CHAT_URL, seen.stdout

    # the signed-in account is untouched by any of this
    assert env_line("--account", "main").split("\t")[2] == chat_main
    return "an account with no chat yet still hands the shell four unshifted fields"


def main() -> int:
    checks = [
        ("home moves every data path", test_the_home_moves_every_data_path),
        ("code stays where it is", test_the_code_stays_where_it_is),
        ("second home reads its config", test_the_second_home_reads_its_own_config),
        ("no env means today's paths", test_no_env_means_todays_paths),
        ("unsigned account keeps its own chat", test_an_unsigned_account_never_reads_the_coding_chat),
        ("advice names the right home", test_the_advice_names_the_right_home),
        ("agent-watch follows the home", test_agent_watch_follows_the_home),
        ("the unit follows the home", test_the_unit_follows_the_home),
        ("account-env has no empty field", test_account_env_never_prints_an_empty_field),
    ]
    failed = 0
    for name, fn in checks:
        try:
            detail = fn()
            print("PASS  %-36s %s" % (name, detail))
        except Exception as e:  # noqa: BLE001
            failed += 1
            print("FAIL  %-36s %s" % (name, e))
    print("\n%d/%d proofs passed" % (len(checks) - failed, len(checks)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
