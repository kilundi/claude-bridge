#!/usr/bin/env python3
"""Prove the bridge can put a screenshot in front of Claude, and only ever sends
what it typed itself.

    python3 tests/test_attach_images.py

Everything runs against a fake page - no Chrome, no claude.ai, no messages sent. The
fake is the composer: it reports what it holds (draft text, attached images, an
upload in progress) and records the CDP calls, so the order of "attach, then type,
then send" is checkable.

  1. --image takes files and directories (a directory means its images, newest first)
  2. a path that is not a usable image is an error that says why
  3. the limits (8 files, 20 MB) are enforced before anything is touched
  4. attaching sets real files on the page's own <input type=file> and waits for the
     upload to finish, not for the thumbnail to merely appear
  5. an upload that never finishes is a loud error, not a silent send
  6. send() attaches first, then types, then clicks send
  7. a composer holding somebody's draft is refused, never posted along with ours
  8. a refused text report is queued, so it still goes out once the box is clear
  9. images are never queued - the outbox is text, so the refusal says so
 10. --clear parks a draft's text in drafts/ before emptying the composer
 11. a plain ask with a clean composer touches nothing DOM- or file-related
 12. the MCP tool carries the same thing (schema + pass-through)
 13. the CLI really offers the flags
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import bridge  # noqa: E402

TMP: list[str] = []


def tmpdir(prefix: str) -> str:
    d = tempfile.mkdtemp(prefix=prefix)
    TMP.append(d)
    return d


def write_png(path: str, bytes_: int = 2048) -> str:
    """A real (tiny, valid-header) png file of roughly the size asked for."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n")
        fh.write(b"\x00" * max(0, bytes_ - 8))
    return path


class FakeCDP:
    """A CDP session to a fake composer page.

    ``upload_ticks`` is how many reads of the composer still report an upload in
    progress (claude.ai shows "Loading" plus a progressbar while it uploads);
    ``stuck_upload`` never finishes.
    """

    def __init__(self, ws: str = "ws://fake"):
        self.events: list[tuple] = []
        self.thumbs: list[str] = []
        self.text = ""
        self.upload_ticks = 0
        self.stuck_upload = False
        self.composer_found = True
        self.file_node = 42
        self.setfiles: list[str] = []
        self.selector = None
        self.reads = 0

    # --- what the page does -------------------------------------------------
    def call(self, method: str, params: dict | None = None):
        params = params or {}
        self.events.append(("call", method, params))
        if method == "DOM.getDocument":
            return {"root": {"nodeId": 1}}
        if method == "DOM.querySelector":
            self.selector = params.get("selector")
            return {"nodeId": self.file_node if self.selector == 'input[type="file"]' else 0}
        if method == "DOM.setFileInputFiles":
            self.setfiles = list(params["files"])
            self.thumbs = [os.path.basename(f) for f in self.setfiles]
            self.upload_ticks = max(self.upload_ticks, 3)
            return {}
        if method == "Input.insertText":
            self.text = params["text"]
            return {}
        return {}

    def _composer(self) -> dict:
        self.reads += 1
        loading = 0
        names = list(self.thumbs)
        if self.stuck_upload:
            loading = 1
        elif self.upload_ticks > 0:
            self.upload_ticks -= 1
            loading = 1
        if loading:
            names = ["Loading"] * len(self.thumbs)
        return {
            "found": self.composer_found,
            "thumbs": len(self.thumbs),
            "names": names,
            "uploading": loading,
            "loading": loading,
            "text": self.text,
            "sendDisabled": not (self.thumbs or self.text.strip()),
        }

    def js(self, expr: str, timeout: float | None = None) -> str:
        if expr is bridge.JS_COMPOSER_ATTACH:
            return json.dumps(self._composer())
        if expr == bridge.JS_REMOVE_ATTACHMENTS:
            self.events.append(("js", "remove-attachments", {}))
            n = len(self.thumbs)
            self.thumbs = []
            return "removed %d" % n
        if expr == bridge.JS_CLEAR_COMPOSER:
            self.events.append(("js", "clear-text", {}))
            self.text = ""
            return "cleared"
        if expr == bridge.JS_FOCUS_COMPOSER:
            self.events.append(("js", "focus", {}))
            return "focused"
        if expr == bridge.JS_COMPOSER_TEXT:
            return self.text
        if expr == bridge.JS_CLICK_SEND:
            self.events.append(("js", "click-send", {"text": self.text, "thumbs": list(self.thumbs)}))
            self.text = ""
            self.thumbs = []
            return "clicked-send"
        if expr == bridge.JS_MARK_OLD:
            return "marked:3"
        if expr == bridge.JS_STATE:
            return json.dumps({"composer": True, "total": 3, "streaming": False,
                               "url": "https://claude.ai/chat/fake"})
        return "{}"

    def sent(self) -> list[dict]:
        return [e[2] for e in self.events if e[1] == "click-send"]


def fresh(b: bridge.Bridge | None = None) -> tuple[bridge.Bridge, FakeCDP]:
    b = b or bridge.Bridge("https://claude.ai/chat/fake")
    c = FakeCDP()
    return b, c


# ------------------------------------------------------------------ the checks
def test_paths_take_files_and_dirs() -> str:
    d = tmpdir("images-")
    old = write_png(os.path.join(d, "old.png"))
    new = write_png(os.path.join(d, "new.png"))
    write_png(os.path.join(d, "ignore.txt"))
    os.utime(old, (1000, 1000))
    os.utime(new, (2000, 2000))

    got = bridge.image_paths([d])
    assert got == [new, old], got                      # newest first
    assert bridge.image_paths([old]) == [old], bridge.image_paths([old])
    assert bridge.image_paths([old, d]) == [old, new], bridge.image_paths([old, d])
    assert len(bridge.image_paths([old, old])) == 1, "the same file twice is one image"
    return "a directory means its images, newest first; duplicates collapse"


def test_paths_refuse_the_wrong_thing() -> str:
    d = tmpdir("images-")
    txt = write_png(os.path.join(d, "notes.txt"))
    empty = os.path.join(d, "empty")
    os.makedirs(empty)
    for bad, want in [(txt, "not an image"), (os.path.join(d, "gone.png"), "no such image"),
                      (empty, "no images in")]:
        try:
            bridge.image_paths([bad])
        except ValueError as e:
            assert want in str(e), (want, str(e))
        else:
            raise AssertionError("accepted %s" % bad)
    return "a wrong path says which path and why, instead of asking without a picture"


def test_limits_are_enforced() -> str:
    d = tmpdir("images-")
    files = [write_png(os.path.join(d, "s%d.png" % i)) for i in range(3)]
    old_max_files, old_max_bytes = bridge.MAX_ATTACH_FILES, bridge.MAX_ATTACH_BYTES
    try:
        bridge.MAX_ATTACH_FILES = 2
        try:
            bridge.image_paths(files)
            raise AssertionError("accepted 3 images over a limit of 2")
        except ValueError as e:
            assert "at once" in str(e), str(e)
        bridge.MAX_ATTACH_FILES = 8
        bridge.MAX_ATTACH_BYTES = 100
        try:
            bridge.image_paths([files[0]])
            raise AssertionError("accepted an oversized image")
        except ValueError as e:
            assert "MB" in str(e), str(e)
    finally:
        bridge.MAX_ATTACH_FILES, bridge.MAX_ATTACH_BYTES = old_max_files, old_max_bytes
    return "8 images / 20 MB are checked before the page is touched"


def test_attach_sets_files_and_waits_for_the_upload() -> str:
    b, c = fresh()
    p = write_png(os.path.join(tmpdir("images-"), "phone.png"))
    c.upload_ticks = 4                                # the page reports "Loading" first
    info = b.attach(c, [p], timeout=30)

    assert c.setfiles == [p], c.setfiles
    assert c.selector == 'input[type="file"]', c.selector
    assert info["uploaded"] == 1 and info["thumbs"] == 1, info
    assert info["names"] == ["phone.png"], info["names"]
    assert c.reads >= 5, "it must wait for the upload, not the thumbnail: %d reads" % c.reads
    assert not c.sent(), "attaching must never send"
    assert "Input.insertText" not in [e[1] for e in c.events], "no typing while attaching"
    methods = [e[1] for e in c.events if e[0] == "call"]
    assert methods[:4] == ["DOM.enable", "DOM.getDocument", "DOM.querySelector",
                           "DOM.setFileInputFiles"], methods
    return "real files on the page's own input, and it waits until the upload is done"


def test_stuck_upload_is_loud() -> str:
    b, c = fresh()
    c.stuck_upload = True
    p = write_png(os.path.join(tmpdir("images-"), "phone.png"))
    t0 = time.time()
    try:
        b.attach(c, [p], timeout=1.5)
        raise AssertionError("a stuck upload was accepted")
    except RuntimeError as e:
        msg = str(e)
        assert "did not finish uploading" in msg and "asked for: phone.png" in msg, msg
    assert time.time() - t0 < 12, "it must give up, not hang"
    assert not c.sent()
    return "an upload that never finishes fails with what it saw, and sends nothing"


def test_send_attaches_then_types_then_sends() -> str:
    b, c = fresh()
    p = write_png(os.path.join(tmpdir("images-"), "phone.png"))
    info = b.send(c, "the phone layout breaks here - what would you change?", images=[p])

    assert info["attached"]["names"] == ["phone.png"], info
    order = [(e[0], e[1]) for e in c.events]
    attach_at = order.index(("call", "DOM.setFileInputFiles"))
    assert ("call", "Input.insertText") in order, order
    type_at = order.index(("call", "Input.insertText"))
    send_at = order.index(("js", "click-send"))
    assert attach_at < type_at < send_at, order
    assert c.sent()[0]["text"].startswith("the phone layout"), c.sent()
    return "attach -> type -> send, in that order, for one message"


def test_a_draft_is_refused() -> str:
    b, c = fresh()
    c.text = "hold on, before you do that -"
    try:
        b.send(c, "DONE: nothing to report")
        raise AssertionError("it posted over somebody's draft")
    except bridge.ComposerBlocked as e:
        assert "--clear" in str(e) and "draft" not in str(e)[:0], str(e)
        assert "'hold on" in str(e) or "text (" in str(e), str(e)
    assert not c.sent(), "nothing may be sent when the box is not ours"
    assert ("call", "Input.insertText") not in [(e[0], e[1]) for e in c.events]
    return "a draft in the box stops the send, and the error names the way out"


def test_foreign_attachment_is_refused() -> str:
    b, c = fresh()
    c.thumbs = ["1794372233416_image.png"]            # somebody's pasted screenshot
    p = write_png(os.path.join(tmpdir("images-"), "phone.png"))
    try:
        b.send(c, "look at this", images=[p])
        raise AssertionError("it posted on top of somebody's attachment")
    except bridge.ComposerBlocked as e:
        assert "1794372233416_image.png" in str(e), str(e)
        assert "NOT attached" in str(e), str(e)
    assert c.setfiles == [], "it must not add its own files to a dirty composer"
    assert not c.sent()
    return "somebody's attachment is named in the refusal, and ours is not added"


def test_a_refused_text_report_is_queued() -> str:
    b, c = fresh()
    c.text = "one more thing..." 
    outbox = tmpdir("outbox-")
    old_outbox = bridge.OUTBOX
    bridge.OUTBOX = outbox
    try:
        try:
            b._ask_with(c, "DONE\n- project: wms-web\n- tests: 545 passed", 5)
            raise AssertionError("the report was accepted over a draft")
        except bridge.ComposerBlocked as e:
            assert e.queued, "the refused report must be kept"
            assert "saved in" in str(e), str(e)
        files = os.listdir(outbox)
        assert len(files) == 1, files
        body = open(os.path.join(outbox, files[0])).read()
        assert "545 passed" in body and "draft" in body, body
    finally:
        bridge.OUTBOX = old_outbox
    return "a text report refused by a draft is queued, so it still goes out later"


def test_images_are_never_queued() -> str:
    b, c = fresh()
    c.text = "one more thing..."
    outbox = tmpdir("outbox-")
    old_outbox = bridge.OUTBOX
    bridge.OUTBOX = outbox
    p = write_png(os.path.join(tmpdir("images-"), "phone.png"))
    try:
        try:
            b._ask_with(c, "here is the phone layout", 5, images=[p])
            raise AssertionError("accepted")
        except bridge.ComposerBlocked:
            pass
        assert os.listdir(outbox) == [], "a screenshot cannot be queued - do not pretend"
    finally:
        bridge.OUTBOX = old_outbox
    return "images are never left in the outbox pretending to be delivered later"


def test_window_down_with_images_says_so() -> str:
    b, _ = fresh()
    p = write_png(os.path.join(tmpdir("images-"), "phone.png"))
    old_connect = bridge.Bridge.connect
    outbox = tmpdir("outbox-")
    old_outbox = bridge.OUTBOX
    bridge.OUTBOX = outbox
    bridge.Bridge.connect = lambda self, *a, **k: (_ for _ in ()).throw(RuntimeError("no window"))
    try:
        try:
            b.ask("look at this", timeout=3, images=[p])
            raise AssertionError("accepted")
        except bridge.ClaudeUnavailable as e:
            assert "NOT sent" in str(e), str(e)
            assert not e.queued, e.queued
        assert os.listdir(outbox) == [], "images must not be queued"
        try:
            b.ask("a plain report", timeout=3)
            raise AssertionError("accepted")
        except bridge.ClaudeUnavailable as e:
            assert e.queued, "a text-only report is still kept"
        assert len(os.listdir(outbox)) == 1, os.listdir(outbox)
    finally:
        bridge.Bridge.connect = old_connect
        bridge.OUTBOX = old_outbox
    return "bridge down: text is kept, a screenshot is refused out loud"


def test_clear_parks_the_draft() -> str:
    b, c = fresh()
    c.text = "remind me to ask about the reservations screen"
    c.thumbs = ["shot.png"]
    drafts = tmpdir("drafts-")
    res = b.clear_composer(c, park_dir=drafts)
    assert res["was"]["thumbs"] == 1 and "reservations" in res["was"]["text"], res
    assert os.path.isfile(res["parked"]), res
    body = open(res["parked"]).read()
    assert "reservations" in body and "shot.png" in body, body
    assert c.thumbs == [] and c.text == "", "the composer must end up empty"
    assert "clear-text" in [e[1] for e in c.events if e[0] == "js"], c.events
    return "clearing parks the words in drafts/ and never silently loses them"


def test_a_clean_plain_ask_is_unchanged() -> str:
    b, c = fresh()
    info = b.send(c, "DONE\n- project: bridge\n- tests: ok")
    assert info["attached"] is None, info
    kinds = [e[1] for e in c.events]
    assert "DOM.setFileInputFiles" not in kinds, kinds
    assert "DOM.enable" not in kinds, "no DOM/file work on the plain path: %s" % kinds
    assert c.sent()[0]["text"].startswith("DONE"), c.sent()
    return "a normal report still just types and sends - nothing else is touched"


def test_mcp_carries_images() -> str:
    import mcp_server
    tool = [t for t in mcp_server.TOOLS if t["name"] == "ask_claude"][0]
    assert "images" in tool["inputSchema"]["properties"], tool["inputSchema"]
    assert "screenshot" in tool["description"].lower(), tool["description"]

    seen: dict = {}

    class StubBridge:
        def __init__(self, url=None):
            pass

        def ask(self, text, timeout=None, images=None):
            seen["text"], seen["timeout"], seen["images"] = text, timeout, images
            return "ok"

    old = mcp_server.Bridge
    mcp_server.Bridge = StubBridge
    try:
        p = write_png(os.path.join(tmpdir("images-"), "phone.png"))
        resp = mcp_server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "ask_claude",
                       "arguments": {"message": "look at this", "images": [p],
                                     "timeout_seconds": 12}},
        })
        assert seen["images"] == [p], seen
        assert seen["text"] == "look at this" and seen["timeout"] == 12.0, seen
        assert "ok" in resp["result"]["content"][0]["text"], resp
    finally:
        mcp_server.Bridge = old
    return "the agent tool takes images and hands them to the bridge"


def test_cli_offers_the_flags() -> str:
    out = subprocess.run([sys.executable, os.path.join(ROOT, "bridge.py"), "--help"],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    for flag in ("--image", "--clear", "--composer"):
        assert flag in out.stdout, "%s missing from --help" % flag
    return "ask-claude --image/--clear/--composer are real flags (checked with --help)"


def main() -> int:
    checks = [
        ("--image takes files and dirs", test_paths_take_files_and_dirs),
        ("a bad path says why", test_paths_refuse_the_wrong_thing),
        ("the limits hold", test_limits_are_enforced),
        ("attach waits for the upload", test_attach_sets_files_and_waits_for_the_upload),
        ("a stuck upload is loud", test_stuck_upload_is_loud),
        ("send attaches then types then sends", test_send_attaches_then_types_then_sends),
        ("a draft is refused", test_a_draft_is_refused),
        ("a foreign attachment is refused", test_foreign_attachment_is_refused),
        ("a refused report is queued", test_a_refused_text_report_is_queued),
        ("images are never queued", test_images_are_never_queued),
        ("window down: text kept, image refused", test_window_down_with_images_says_so),
        ("--clear parks the draft", test_clear_parks_the_draft),
        ("a plain ask is unchanged", test_a_clean_plain_ask_is_unchanged),
        ("the MCP tool carries images", test_mcp_carries_images),
        ("the CLI offers the flags", test_cli_offers_the_flags),
    ]
    failed = 0
    try:
        for name, fn in checks:
            try:
                detail = fn()
                print("PASS  %-40s %s" % (name, detail))
            except Exception as e:  # noqa: BLE001
                failed += 1
                print("FAIL  %-40s %s" % (name, e))
    finally:
        for d in TMP:
            shutil.rmtree(d, ignore_errors=True)
    print("\n%d/%d proofs passed" % (len(checks) - failed, len(checks)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
