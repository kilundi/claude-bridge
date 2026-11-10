#!/usr/bin/env python3
"""Prove the bridge always drives the right claude.ai tab, and that a new chat can be
remembered.

    python3 tests/test_chat_target.py

Everything here runs against fakes - no Chrome, no claude.ai, no messages sent. It covers
the behaviour behind ``ask-claude --new-chat`` and ``ask-claude --save-chat``:

  1. claude.ai/new is recognised as "a chat whose id has not settled yet"
  2. the tab the bridge drives is remembered, and preferred over a lookalike tab
  3. --save-chat looks at the driven tab exactly as it is (it must never navigate that
     tab back to the saved chat - doing so is what made a new chat impossible to
     remember: you saved the old chat again)
  4. a normal call drives the remembered tab and aims it at the saved chat, so a
     message can never land in a different chat because a second tab was open
  5. a chat saved as /new is left alone (aiming a page at /new would start a *different*
     empty chat on every call)
  6. with no claude.ai tab at all, a page is created on the saved chat
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import bridge  # noqa: E402

NEW = "https://claude.ai/new"
CHAT_A = "https://claude.ai/chat/aaaaaaaa-1111-2222-3333-444444444444"
CHAT_B = "https://claude.ai/chat/bbbbbbbb-5555-6666-7777-888888888888"

TABS: dict[str, dict] = {}
CREATED: list[str] = []
TMP_DIRS: list[str] = []


class FakeCDP:
    """A CDP connection to one fake tab: records navigations, reports page state."""

    def __init__(self, ws: str):
        self.id = ws.split("//")[-1]
        self.navigated: list[str] = []

    def call(self, method: str, params: dict | None = None):
        if method == "Page.navigate":
            self.navigated.append(params["url"])
            TABS[self.id]["url"] = params["url"]      # the tab really moves
        return {}

    def js(self, expr: str, timeout: float | None = None) -> str:
        return json.dumps({
            "ready": "complete", "composer": True, "target": self.id,
            "url": TABS[self.id]["url"],
        })

    # helpers for the assertions
    @property
    def url_now(self) -> str:
        return TABS[self.id]["url"]

    def state(self) -> dict:
        return json.loads(self.js(""))


def fake_create_page(url: str):
    CREATED.append(url)
    tab = {"id": "created", "type": "page", "url": url,
           "webSocketDebuggerUrl": "ws://created"}
    TABS["created"] = tab
    return tab


def setup(tabs: list[tuple[str, str]], target: str = "", page_url: str = CHAT_A) -> bridge.Bridge:
    """Point the bridge at a fake set of tabs: [(tab id, url), ...]."""
    TABS.clear()
    CREATED.clear()
    for tid, url in tabs:
        TABS[tid] = {"id": tid, "type": "page", "url": url,
                     "webSocketDebuggerUrl": "ws://" + tid}
    TMP_DIRS.append(tempfile.mkdtemp(prefix="chat-target-test-"))
    bridge.TARGET_PATH = os.path.join(TMP_DIRS[-1], "bridge-target.json")
    if target:
        bridge.save_target(target)
    bridge.ensure_chrome = lambda url=None: True
    bridge.CDP = FakeCDP
    bridge.create_page = fake_create_page
    bridge.Bridge._pages = lambda self: list(TABS.values())
    return bridge.Bridge(page_url)


# ------------------------------------------------------------------ the checks
def test_new_chat_url() -> str:
    assert bridge.is_new_chat_url(NEW)
    assert bridge.is_new_chat_url(NEW + "/")
    assert bridge.is_new_chat_url(NEW + "#prompt")
    assert not bridge.is_new_chat_url(CHAT_A)
    assert not bridge.is_new_chat_url("https://claude.ai/chat/new-thing")
    return "claude.ai/new (and /new/, /new#x) count as 'not settled yet'"


def test_target_is_remembered() -> str:
    setup([("t1", CHAT_A)])
    assert bridge.load_target() == ""
    bridge.save_target("t1")
    assert bridge.load_target() == "t1"
    bridge.save_target("")             # a closed tab clears it
    assert bridge.load_target() == ""
    return "the driven tab is remembered across calls"


def test_save_chat_looks_at_the_driven_tab() -> str:
    # t2 is the tab we drive; t1 is a second tab that happens to show the saved chat.
    # --save-chat must read t2 as it is, never navigate it (the old bug).
    b = setup([("t1", CHAT_A), ("t2", NEW)], target="t2", page_url=CHAT_A)
    c = b.connect(navigate=False)
    assert c.id == "t2", "must use the remembered tab, got %s" % c.id
    assert c.url_now == NEW, "must not navigate the driven tab, it is now %s" % c.url_now
    assert c.navigated == [], c.navigated
    return "--save-chat reads the driven tab (a new chat) without navigating it"


def test_normal_call_drives_the_remembered_tab() -> str:
    # both tabs show CHAT_A, we drive t2, and the saved chat is CHAT_B:
    # t2 must be the one aimed at CHAT_B, never t1.
    b = setup([("t1", CHAT_A), ("t2", CHAT_A)], target="t2", page_url=CHAT_B)
    c = b.connect()
    assert c.id == "t2", "must drive the remembered tab, got %s" % c.id
    assert c.navigated == [CHAT_B], c.navigated
    assert c.url_now == CHAT_B
    assert TABS["t1"]["url"] == CHAT_A, "the other tab must be left alone"
    return "a normal call drives the remembered tab and leaves lookalikes alone"


def test_no_navigation_when_already_there() -> str:
    b = setup([("t1", CHAT_A)], target="t1", page_url=CHAT_A)
    c = b.connect()
    assert c.navigated == [], "must not reload a page that is already the saved chat"
    return "already on the saved chat -> no navigation at all"


def test_saved_new_chat_is_left_alone() -> str:
    # After --new-chat the saved chat is /new and the tab has moved to /chat/<id>
    # (a message was sent). Aiming it at /new would start yet another empty chat.
    b = setup([("t1", CHAT_B)], target="t1", page_url=NEW)
    c = b.connect()
    assert c.id == "t1"
    assert c.navigated == [], "must not aim the tab at /new: %s" % c.navigated
    assert b.state(c)["url"] == CHAT_B
    return "a saved /new keeps using the open chat instead of starting a new one"


def test_creates_a_page_when_nothing_is_open() -> str:
    b = setup([], page_url=CHAT_A)
    c = b.connect()
    assert CREATED == [CHAT_A], CREATED
    assert c.id == "created"
    return "no claude.ai tab -> a page is created on the saved chat"


def test_exact_match_when_the_remembered_tab_is_gone() -> str:
    b = setup([("t1", CHAT_A)], target="gone", page_url=CHAT_A)
    c = b.connect()
    assert c.id == "t1", "must fall back to the matching tab, got %s" % c.id
    assert c.navigated == [], "it is already the saved chat: %s" % c.navigated
    assert bridge.load_target() == "t1", "and remember it from now on"
    return "remembered tab closed -> falls back to the matching tab, no navigation"


def main() -> int:
    checks = [
        ("claude.ai/new recognised", test_new_chat_url),
        ("driven tab remembered", test_target_is_remembered),
        ("--save-chat reads the driven tab", test_save_chat_looks_at_the_driven_tab),
        ("normal call drives the driven tab", test_normal_call_drives_the_remembered_tab),
        ("no needless navigation", test_no_navigation_when_already_there),
        ("saved /new left alone", test_saved_new_chat_is_left_alone),
        ("creates a page when none open", test_creates_a_page_when_nothing_is_open),
        ("stale tab falls back", test_exact_match_when_the_remembered_tab_is_gone),
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
