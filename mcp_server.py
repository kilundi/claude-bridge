#!/usr/bin/env python3
"""MCP (stdio) server exposing the Claude Web Bridge as tools.

Tools:
  ask_claude          - send a message to the Claude chat in the bridge Chrome and
                        return Claude's reply (blocks until the reply is complete; if
                        Claude is out of messages it returns CLAUDE_UNAVAILABLE at once)
  claude_status       - report the bridge window state (url, signed in, streaming)
  claude_availability - can Claude answer right now? (and when the limit resets)
  wait_for_claude     - block until Claude can answer again
  outbox              - list reports saved while the bridge was unreachable
  flush_outbox        - deliver those saved reports now
  compact_conversation- ask Claude for a compact handoff summary (the web has no /compact)
  check_work          - read the work order Claude last wrote in the chat
  fetch_claude_files  - download the file cards Claude produced in the chat

Protocol: JSON-RPC 2.0, one message per line on stdin/stdout (MCP stdio).
Nothing else must be written to stdout.
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bridge import (  # noqa: E402
    Bridge,
    ClaudeUnavailable,
    DEFAULT_TIMEOUT,
    extract_work_orders,
    outbox_files,
    parse_reset_time,
    start_bridge,
    target_url,
)

PROTOCOL = "2024-11-05"
SERVER = {"name": "claude-bridge", "version": "1.0.0"}

TOOLS = [
    {
        "name": "ask_claude",
        "description": (
            "Send a message to the planner Claude in the bridge Chrome window and return "
            "Claude's reply. Use this when a task is finished, or when you need the next "
            "move or a decision: send a clear status (repo, branch, commits, tests, "
            "blockers) and, if you want, ask what to do next. Blocks until Claude replies. "
            "If Claude cannot answer (out of free messages, or signed out) this returns "
            "immediately with CLAUDE_UNAVAILABLE and the reset time - your message is still "
            "delivered to the chat, so do NOT resend it; call claude_availability or "
            "wait_for_claude instead of retrying. If the bridge window is unreachable the "
            "message is saved in the outbox instead of being lost, and flush_outbox sends it "
            "later. Pass 'images' to show Claude a screenshot (a rendered UI, a phone-width "
            "layout, a broken page) - words cannot describe a layout problem as quickly as "
            "the picture can. The bridge refuses to send anything while somebody's own draft "
            "is in the composer; that comes back as an error naming the draft."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "The message / status to send to Claude."},
                "images": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Absolute paths of image files to attach to the message (png/jpg/jpeg/"
                        "webp/gif/bmp, up to 8 files of 20 MB each). A directory means the "
                        "images in it, newest first. Take the screenshot yourself first - e.g. "
                        "with Playwright: await page.setViewportSize({width: 390, height: 844}); "
                        "await page.screenshot({path: '/tmp/opencode/phone.png', fullPage: true}) "
                        "- then pass its path here with a question about what it shows."
                    ),
                },
                "timeout_seconds": {
                    "type": "number",
                    "description": "Max seconds to wait for the reply (default 600).",
                },
            },
            "required": ["message"],
        },
    },
    {
        "name": "claude_status",
        "description": (
            "Report the state of the Claude bridge window: page url, whether the composer "
            "is available (signed in), whether a reply is currently streaming, and whether "
            "Claude is out of messages (with the reset time)."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "claude_availability",
        "description": (
            "Check whether Claude can answer right now, without sending anything. Returns "
            "available true/false, the notice the page shows ('You are out of free messages "
            "until 7:20 PM'), the reset time, and how many seconds away it is. Use it before "
            "a long report, or after ask_claude returns CLAUDE_UNAVAILABLE."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "wait_for_claude",
        "description": (
            "Block until Claude can answer again (limit reset, or sign-in restored), then "
            "return. Use it when ask_claude says CLAUDE_UNAVAILABLE and you want to continue "
            "the loop automatically instead of stopping: it sleeps toward the reset time and "
            "wakes when Claude is back. Returns the final availability."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "timeout_seconds": {
                    "type": "number",
                    "description": "Give up after this many seconds (default 3600).",
                }
            },
        },
    },
    {
        "name": "compact_conversation",
        "description": (
            "Keep a long conversation going. The claude.ai web UI has no /compact (its "
            "slash menu only lists skills), so this asks Claude for a short compact handoff "
            "summary instead - the thing the user pastes into a fresh chat to continue. Use "
            "it when a long thread is running out of context or after a usage limit resets."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "timeout_seconds": {"type": "number", "description": "Max seconds to wait (default 300)."}
            },
        },
    },
    {
        "name": "bridge_up",
        "description": (
            "Bring the Claude bridge up when it is down - the dedicated Chrome window, the "
            "saved chat, and the work-order watcher - and report the result. Use this when "
            "you are told to 'launch the bridge', when ask_claude fails because the window "
            "is closed, or after a reboot. Safe to call when it is already up: nothing is "
            "restarted, it is reused."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "outbox",
        "description": (
            "List reports that were saved locally because the bridge window was not "
            "reachable, or Claude could not be signed in to. They are delivered "
            "automatically once the bridge is back - nothing is lost."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "flush_outbox",
        "description": (
            "Deliver the queued reports now, oldest first. Use it after bridge_up to make "
            "sure a report that finished while the bridge was down reaches Claude."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "timeout_seconds": {
                    "type": "number",
                    "description": "Max seconds to wait for each reply (default 600).",
                }
            },
        },
    },
    {
        "name": "check_work",
        "description": (
            "Read the chat for a work order from Claude - the planner writes them as a "
            "WORK-ORDER block (or a ```work-order fence). Use this when you have been told "
            "to 'check Claude for work', or when you want the latest instruction without "
            "sending a message. Returns each order's id, goal, repo directory, branch and "
            "acceptance test."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "fetch_claude_files",
        "description": (
            "Download the file cards Claude has produced in the chat (for example the "
            "'Agent prompt ...' files) into ~/claude-bridge/downloads and return their "
            "paths. Use this when Claude refers to a file it wrote instead of pasting it - "
            "the chat carries only text, but the files themselves can be fetched."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "match": {
                    "type": "string",
                    "description": "Only fetch cards whose title contains this text. Omit for all.",
                },
                "timeout_seconds": {
                    "type": "number",
                    "description": "Max seconds to wait for the downloads (default 60).",
                },
            },
        },
    },
]


def _write(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _content(text: str, is_error: bool = False) -> dict:
    out = {"content": [{"type": "text", "text": text}]}
    if is_error:
        out["isError"] = True
    return out


def _unavailable_text(e: ClaudeUnavailable) -> str:
    """One unmistakable line an agent can act on, without retrying blindly."""
    bits = ["CLAUDE_UNAVAILABLE: Claude cannot answer right now - %s"
            % (e.text or "reason unknown")]
    epoch = parse_reset_time(e.resets_at or e.text)
    if e.resets_at:
        away = " (~%d min away)" % max(1, round((epoch - time.time()) / 60)) if epoch else ""
        bits.append("It resets at %s%s." % (e.resets_at, away))
    if e.sent:
        bits.append("Your message WAS delivered to the chat and Claude will read it when it "
                    "can answer again - do NOT resend it.")
    elif e.queued:
        bits.append("Nothing was sent, but your report is saved in %s and will be delivered "
                    "automatically once the bridge is back - or call flush_outbox now."
                    % e.queued)
    else:
        bits.append("Nothing was sent.")
    bits.append("Call claude_availability to check, or wait_for_claude to continue "
                "automatically instead of retrying now.")
    return " ".join(bits)


def handle(req: dict):
    method = req.get("method")
    rid = req.get("id")

    if method == "initialize":
        proto = (req.get("params") or {}).get("protocolVersion") or PROTOCOL
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "protocolVersion": proto,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER,
            },
        }

    if method in ("notifications/initialized", "initialized", "notifications/cancelled"):
        return None

    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}

    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"resources": [], "templates": []}}

    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"prompts": []}}

    if method in ("logging/setLevel", "notifications/logging/setLevel"):
        return None if method.startswith("notifications") else {"jsonrpc": "2.0", "id": rid, "result": {}}

    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            if name == "ask_claude":
                message = (args.get("message") or "").strip()
                if not message:
                    raise ValueError("'message' is required")
                timeout = float(args.get("timeout_seconds") or DEFAULT_TIMEOUT)
                images = [str(p) for p in (args.get("images") or [])]
                try:
                    reply = Bridge(target_url()).ask(message, timeout=timeout, images=images)
                except ClaudeUnavailable as e:
                    return {"jsonrpc": "2.0", "id": rid, "result": _content(_unavailable_text(e))}
                return {"jsonrpc": "2.0", "id": rid, "result": _content(reply)}
            if name == "claude_availability":
                av = Bridge(target_url()).availability()
                if not av["available"]:
                    why = av["text"] or ("the bridge window is not signed in"
                                         if not av["signed_in"] else "Claude cannot answer")
                    extra = " (resets at %s)" % av["resets_at"] if av["resets_at"] else ""
                    return {"jsonrpc": "2.0", "id": rid, "result": _content(
                        "CLAUDE_UNAVAILABLE: %s%s - use wait_for_claude to continue later"
                        % (why, extra))}
                return {"jsonrpc": "2.0", "id": rid,
                        "result": _content("CLAUDE_AVAILABLE: " + json.dumps(av))}
            if name == "wait_for_claude":
                timeout = float(args.get("timeout_seconds") or 3600)
                av = Bridge(target_url()).wait_available(timeout=timeout)
                if av["available"]:
                    return {"jsonrpc": "2.0", "id": rid, "result": _content(
                        "CLAUDE_AVAILABLE: Claude is answering again - send your report now.")}
                return {"jsonrpc": "2.0", "id": rid, "result": _content(
                    "CLAUDE_UNAVAILABLE: still blocked after %.0fs - %s%s"
                    % (timeout, av["text"] or "unknown reason",
                       (" (resets at %s)" % av["resets_at"]) if av["resets_at"] else ""))}
            if name == "outbox":
                files = outbox_files()
                if not files:
                    return {"jsonrpc": "2.0", "id": rid, "result": _content(
                        "OUTBOX_EMPTY: no reports are waiting.")}
                return {"jsonrpc": "2.0", "id": rid, "result": _content(
                    json.dumps({"count": len(files), "outbox": files}, indent=1))}
            if name == "flush_outbox":
                timeout = float(args.get("timeout_seconds") or DEFAULT_TIMEOUT)
                res = Bridge(target_url()).flush_outbox(timeout=timeout)
                if not res["sent"] and res["stopped"]:
                    return {"jsonrpc": "2.0", "id": rid, "result": _content(
                        "CLAUDE_UNAVAILABLE: could not deliver the queued report(s) - %s"
                        % res["stopped"])}
                return {"jsonrpc": "2.0", "id": rid, "result": _content(
                    "delivered %d queued report(s); %d still waiting%s"
                    % (len(res["sent"]), len(res["remaining"]),
                       (" (%s)" % res["stopped"]) if res["stopped"] else ""))}
            if name == "claude_status":
                b = Bridge(target_url())
                c = b.connect()
                try:
                    st = b.last_message(c)
                finally:
                    c.close()
                return {"jsonrpc": "2.0", "id": rid, "result": _content(json.dumps(st, indent=1))}
            if name == "compact_conversation":
                timeout = float(args.get("timeout_seconds") or 300)
                res = Bridge(target_url()).compact(timeout=timeout)
                return {"jsonrpc": "2.0", "id": rid, "result": _content(res or "(compacted)")}
            if name == "bridge_up":
                return {"jsonrpc": "2.0", "id": rid, "result": _content(json.dumps(start_bridge(), indent=1))}
            if name == "check_work":
                b = Bridge(target_url())
                c = b.connect(want_composer=False)
                try:
                    st = b.last_message(c)
                finally:
                    c.close()
                if st.get("streaming"):
                    return {"jsonrpc": "2.0", "id": rid,
                            "result": _content("(Claude is still writing - try again shortly)")}
                orders = extract_work_orders(st.get("last") or "")
                if not orders:
                    return {"jsonrpc": "2.0", "id": rid,
                            "result": _content("(no work order in Claude's last message)")}
                return {"jsonrpc": "2.0", "id": rid, "result": _content(json.dumps(orders, indent=1))}
            if name == "fetch_claude_files":
                timeout = float(args.get("timeout_seconds") or 60)
                files = Bridge(target_url()).download(args.get("match") or None, timeout=timeout)
                return {"jsonrpc": "2.0", "id": rid, "result": _content(json.dumps(files, indent=1))}
            return {
                "jsonrpc": "2.0",
                "id": rid,
                "error": {"code": -32602, "message": f"unknown tool: {name}"},
            }
        except Exception as e:  # tools report errors in-band
            return {"jsonrpc": "2.0", "id": rid, "result": _content(f"error: {e}", is_error=True)}

    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"method not found: {method}"}}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        try:
            resp = handle(req)
        except Exception as e:  # never crash the loop
            resp = {"jsonrpc": "2.0", "id": req.get("id"), "error": {"code": -32603, "message": str(e)}}
        if resp is not None:
            _write(resp)


if __name__ == "__main__":
    main()
