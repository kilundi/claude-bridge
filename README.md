# Claude Web Bridge

Lets a **local agent** (opencode, like the one in this session) talk to **Claude in
your browser**, so the loop *"Claude plans → agent executes → agent reports back"*
can run without you copy-pasting between them.

It drives a **dedicated Chrome window** over the DevTools Protocol (CDP):

1. types your message into the claude.ai composer,
2. presses send,
3. waits for Claude's reply to finish streaming,
4. returns the reply text to the caller.

Nothing else on your normal Chrome is touched.

---

## Quick start

Needs: Linux with a desktop session, Chrome or Chromium, Python 3, and
[opencode](https://opencode.ai). Clone it to `~/claude-bridge` — the scripts expect that
path (or use `$HOME`).

```bash
git clone https://github.com/kilundi/claude-bridge ~/claude-bridge
cd ~/claude-bridge
cp config.example.json config.json     # your chat + model + a default directory
ln -s ~/claude-bridge/ask-claude ~/.local/bin/ask-claude

python3 tests/test_watcher_single_start.py   # 4 checks, touches nothing live
bash start-bridge-chrome.sh                  # the dedicated bridge window
ask-claude --save-chat                       # remember the conversation you opened
./watcher.sh start                           # begin watching it for work orders
```

Then keep reading for what each piece does.

`config.json` is yours and is git-ignored, as are `state.json`, `inbox/`, `downloads/`,
`outbox/`, `terminals/` and the logs — so real work orders, your chat URL and your model
choice never end up in a commit.

---

## One-time setup

1. Start the bridge window (it uses its own Chrome profile, so the debug port is
   allowed even on Chrome 156+):

   ```bash
   bash ~/claude-bridge/start-bridge-chrome.sh
   ```

2. In the **new window that opens**, sign in to claude.ai (Google SSO is fine) and
   open the conversation you want your agents to talk to.

3. Tell the bridge to remember that chat:

   ```bash
   ask-claude --save-chat
   ```

   (Saved to `~/claude-bridge/config.json` as `chat_url`.)

That's it. The profile keeps you signed in, so this is a one-time thing.

---

## Using it

From a shell:

```bash
ask-claude "DONE: parser fixed and pushed to feature/parser-fix. Tests green. What is the next move?"
ask-claude --read      # print Claude's last message, send nothing
ask-claude --status    # is the window up / signed in / streaming? (and which tab we drive)
ask-claude --open      # make sure the bridge window is running
ask-claude --probe     # DOM hints, for debugging selectors
ask-claude --new-chat  # start a fresh chat and make it the one we use (see below)
ask-claude --save-chat # remember the chat that is open in the bridge window
```

From an agent (opencode), the `claude-bridge` MCP server exposes a native tool:

| tool | what it does |
|------|--------------|
| `ask_claude(message, timeout_seconds?, images?)` | send a message, wait, return Claude's reply. If Claude is out of messages it returns `CLAUDE_UNAVAILABLE` in about a second (the message is still delivered). `images` attaches screenshots (absolute paths, or a directory) |
| `claude_availability()` | can Claude answer right now? and when the limit resets |
| `wait_for_claude(timeout_seconds?)` | block until Claude can answer again, then continue |
| `outbox()` | list reports saved while the bridge was unreachable |
| `flush_outbox(timeout_seconds?)` | deliver those saved reports now, oldest first |
| `bridge_up()` | bring the bridge up if it is down (window + watcher); safe to call when already up |
| `claude_status()` | report the bridge window state |
| `check_work()` | read the work order Claude last wrote in the chat |
| `fetch_claude_files(match?, timeout_seconds?)` | download the files Claude produced in the chat |
| `compact_conversation(timeout_seconds?)` | ask Claude for a compact handoff summary (see below) |
| `page_shot(url, out?, from_dir?, width?, height?, full_page?, flatten?, open?, expect?, wait_ms?, timeout_ms?, sign_in?)` | screenshot a page you are working on — the whole page at phone width by default — and hand the path to `ask_claude(images=...)` |

So an agent can literally call `ask_claude` when it finishes a task.

---

## When Claude cannot answer (usage limit)

Claude's free plan runs out ("You are out of free messages until 7:20 PM"). The
page still shows the composer, so a naive sender would post the message and then
wait for a reply that never comes - a 10-minute hang, which is exactly what
happened to a work-order agent at 18:32 one evening.

Now the bridge reads the limit notice off the page and tells you:

```bash
ask-claude --available          # exit 3 while Claude cannot answer
ask-claude --wait-available     # block until it can, then print
```

```
claude cannot answer right now: You are out of free messages until 7:20 PM
  the page says it resets at 7:20 PM (in ~28 min)
  exit code: 3
```

- `ask_claude` (and a plain `ask-claude "..."`) still **delivers** the message —
  so a finished report is waiting for Claude at the reset — but returns straight
  away with `CLAUDE_UNAVAILABLE` and the reset time instead of waiting. The agent
  is told not to resend.
- `claude_availability()` checks without sending; `wait_for_claude()` sleeps
  toward the reset time and wakes as soon as Claude answers again, so the loop
  can continue on its own.
- The watcher logs the limit once (`claude is out of messages until 7:20 PM -
  still reading, no replies until it resets`) and again when it lifts. It keeps
  **reading** while limited, so a work order written just before the limit is
  still picked up.
- It also **delivers the outbox** as soon as Claude can take it (see below), so a
  report that finished while the window was down goes out on the next pass.

### A report is never lost: the outbox

An agent can finish at any moment - including when the bridge window is closed,
Chrome is not running, or claude.ai is signed out. In those cases the message
cannot be delivered at all, so it is written to `~/claude-bridge/outbox/` instead
of being thrown away:

```
ask-claude "DONE: ..."          # window unreachable
claude cannot answer right now: the bridge window is not reachable
  nothing was sent - the report is saved in ~/claude-bridge/outbox/20261110-190633-done-....md
  it is delivered automatically once the bridge is back (or now with: ask-claude --flush)
  exit code: 3
```

- `ask-claude --outbox` (or the MCP `outbox`) lists what is waiting.
- `ask-claude --flush` (or the MCP `flush_outbox`) sends them now, oldest first.
- The **watcher** flushes the outbox on its next pass whenever Claude is
  reachable, so in practice it goes out by itself: bring the bridge back with
  `ask-claude --up` and the queued reports follow.
- Delivery is once only: if a queued report did reach the chat but no reply came
  (the limit again), it is counted as delivered and removed, never sent twice.

### The standing plan: compact when the session resets

The plan is: **when Claude's session resets, run the compact step**, so the long
thread can continue. `"compact_on_resume": true` in `config.json` (under `watch`)
makes the watcher do it for you — the moment the limit lifts it logs
`claude can answer again` and asks Claude for the compact handoff:

```
2026-11-10 19:20:05  claude can answer again - the loop is back to normal
2026-11-10 19:20:05    compact_on_resume: asking Claude for a compact handoff
```

The web UI has no `/compact`, so "the compact step" is the compact handoff
summary described below. You can run it by hand with `ask-claude --compact` (or
the MCP `compact_conversation`) — but not at the same time as the watcher, or
Claude receives the request twice.

---

## Bringing it back up (after a restart, or if the window was closed)

```bash
ask-claude --up                      # bridge window + watcher, then reports the state
bash ~/claude-bridge/bridge-up.sh    # exactly the same thing
```

Both are safe to run when it is already up — nothing is restarted, it is reused.

You do **not** have to open Chrome yourself, and you should not: a normal Chrome window uses
the default profile, which is not allowed to expose the debug port. An agent can bring it up
for you — say *"launch the bridge"* and it calls the `bridge_up` MCP tool.

- `ask_claude` (agent → Claude) already launches the window by itself if it is down, so the
  report-when-done direction heals on its own.
- The watcher (Claude → agent) deliberately does **not** open a window behind your back. It
  logs a line instead. Set `watch.auto_launch_bridge` to `true` in `config.json` if you would
  rather it opened the window itself.

---

## Automatic "report when done"

The rule is written into `~/.config/opencode/AGENTS.md`: when an agent finishes a
task, it sends a short **DONE report** to Claude and then takes on Claude's next
move. Suggested format:

```
DONE
- project: my-api
- branch: feature/parser-fix
- commits: f082d27, 9a1c2be (pushed)
- tests: build ok, lint ok, e2e ok
- notes: <anything odd / follow-ups>
NEXT: what should I take on?
```

If Claude cannot answer (the free plan's limit), `ask_claude` returns
`CLAUDE_UNAVAILABLE` and the report is still waiting in the chat for the reset —
see "When Claude cannot answer" above. Do not resend it: check with
`claude_availability`, or continue with `wait_for_claude`.

---

## Claude hands work back: the work-order queue

The chat is the queue in **the other direction** too. Claude in the browser cannot push
anything outward, so instead it *writes a work order into the chat* and the watcher picks
it up:

```
Claude writes a WORK-ORDER block in the chat
   -> watcher.py (polls every 30s) notices it
   -> saves it to ~/claude-bridge/inbox/<id>.md
   -> downloads any file cards Claude attached to ~/claude-bridge/downloads/
   -> starts an opencode session in the repository the order names
   -> that agent does the work, runs the checks, commits and pushes
   -> the agent reports DONE back with ask_claude, which posts into the chat
   -> Claude reads the report and writes the next work order
```

Start, stop and inspect it:

```bash
~/claude-bridge/watcher.sh start    # background watcher (auto-starts agents)
~/claude-bridge/watcher.sh status
~/claude-bridge/watcher.sh stop
~/claude-bridge/watcher.sh log      # follow the log

python3 ~/claude-bridge/watcher.py --once --dry-run   # one pass, start nothing
```

### What Claude writes

Both shapes work. The closing `>>>` is **optional** — Claude sometimes drops it, and the
block then ends at a blank line.

```
<<<WORK-ORDER
id: WO-102
dir: /home/you/projects/my-api
branch: feature/parser-fix
goal: what to build or fix
acceptance: what must be true when it is done
>>>
```

or a fenced ```` ```work-order ```` block. Fields:

| field | meaning |
|-------|---------|
| `id` | new each time; a repeated id with the same text is treated as already done |
| `dir` | the repository the agent works in (falls back to `watch.default_dir`) |
| `branch` | the branch the agent commits and pushes to |
| `goal` | what to do |
| `acceptance` | how we know it is done |
| `save` | optional path the result must be written to |
| `model` | optional `provider/id` override for that one job |

### A half-written order is asked back, never guessed at

The protocol is a template, not a memory test — a long chat gets summarised by the web
UI and things get forgotten. So the queue does not depend on Claude remembering:

* **A bare list of the same fields counts.** The `<<<WORK-ORDER ...>>>` wrapper is a
  convenience; a plain run of `id:` / `dir:` / `goal:` lines is read as an order too.
* **An order must carry `dir`, `goal` and `acceptance`.** Anything else is not dispatched.
  An agent with no directory, or no way to know it is finished, burns a whole turn
  guessing — so the watcher asks for exactly the missing lines instead, once, quoting the
  template back with the fields that *are* there.
* **A promise with no order behind it is not ignored.** If Claude's newest message reads
  as work it is about to do (`"I'm fixing the eleven findings in one small phase"`) and
  carries no order, the watcher sends one short message naming the template. Past-tense
  reports ("I've fixed …") and ordinary summaries never trigger this.
* **One id is one agent.** An id that already ran, re-posted with *different* text, is not
  started again — the watcher asks whether it is a new order (new id) or a quote.
* **Naming a file is enough.** If the order names `agent-prompt-*.md` and the card download
  failed, the file is resolved in `~/claude-bridge/downloads/` (tolerating a `" (1)"` copy)
  and handed to the agent by absolute path; if it really is not there, the order says so
  instead of the agent starting blind.

Each of those asks is sent **at most once per turn** (a turn ends when an order is
dispatched). If it still does not produce an order, the watcher stops asking and makes it
loud instead:

```bash
~/claude-bridge/watcher.sh status      # prints STALLED if a turn needs an order
cat ~/claude-bridge/NEEDS-ORDER.md     # what was asked, what Claude said, what to do
```

`NEEDS-ORDER.md` is written next to the bridge and deleted the moment an order is
dispatched. It is a human's cue, not another automated message: no agent is running while
it exists, and nothing is sent to Claude a second time. Knobs, all under `watch` in
`config.json`:

| setting | default | meaning |
|---------|---------|---------|
| `ask_for_order` | `true` | set `false` to never message Claude about a missing/half order (the loud flag still appears) |
| `ask_max_streak` | `1` | how many times to ask before going loud |
| `ask_grace_seconds` | `600` | how long to give Claude to answer the ask before going loud |
| `ask_min_seconds` | `120` | never two asks closer together than this |
| `ask_timeout` | `180` | how long the watcher waits for the reply to its ask (the reply is read on a later pass anyway) |
| `agent_active_seconds` | `1800` | an agent counts as "still working" (and the watcher holds its ask rather than stack a second agent on the same repo) if its transcript moved within this window |

### Files, and "save this to that directory"

Claude usually hands over *files* ("Agent prompt ...") rather than pasting text. The
watcher downloads them automatically, and an agent can fetch them on demand:

```bash
ask-claude --files                       # list the file cards in the chat
ask-claude --download                    # fetch all of them
ask-claude --download "integration 6"    # fetch one by title
ask-claude --work                        # show the work order in Claude's last message
```

If Claude asks for a file to be **saved in a specific directory**, that is just a normal
work order: the agent creates the file at that path (directories included) and copies any
attached file there. Claude may say it in the goal in plain words, or add a line
`save: /the/path`.

### Showing Claude a screenshot (images both ways)

Words describe a layout badly. The bridge can put a picture in the chat the same way the
paperclip does — it sets real files on the page's own `<input type=file>`, so claude.ai
uploads them exactly as if you had picked them:

```bash
# the phone layout breaks at 390px - ask with the picture attached
ask-claude --image ~/Pictures/phone.png "this breaks on a phone - what would you change?"

ask-claude --image ~/Pictures/Shots/ "the two newest runs, side by side"   # a directory = its images, newest first
ask-claude --image before.png --image after.png "which one is better?"     # repeat it
```

Up to **8 images of 20 MB** each (`png/jpg/jpeg/webp/gif/bmp`), checked before anything
touches the page: a wrong path, a non-image, or an oversized file is an error that says
which one and why, **not** a message that arrives without its screenshot.

**An agent can do it too** — that is how a UI review round-trips without you in the middle:

```python
# in the agent: take the shot it is talking about, then ask with it
await page.setViewportSize({"width": 390, "height": 844})
await page.screenshot({"path": "~/claude-bridge/shots/phone.png", "fullPage": True})
# ... then: ask_claude(message="the reservations table overflows at 390px",
#                      images=["~/claude-bridge/shots/phone.png"])
```

Write the picture somewhere durable: `/tmp` is a tmpfs on this machine and a
screenshot parked there can be gone before you attach it.

`ask_claude` takes the same thing over MCP: `images: ["/abs/path.png", ...]`. Images *from*
Claude are the other direction and already worked (`--download`, file cards).

#### Taking the shot in the first place

A viewport screenshot of a phone is nearly useless — a form is 3000 px tall and the part
that breaks is below the fold — so `page-shot` captures the **whole document**, at a phone
width, retina, with the app's own Playwright (nothing installed, and a throwaway browser
context so nobody gets signed out):

```bash
page-shot http://localhost:5173/items/new phone.png \
  --from ~/projects/myapp \
  --signin /signin \
  --fill '[data-testid="signin-email"]=you@example.com' \
  --fill '[data-testid="signin-password"]=secret' \
  --click '[data-testid="signin-submit"]' --ready '[data-testid="page"]' \
  --expect '[data-testid="page-title"]' \
  --open '[data-testid="section-toggle-catalogue"]' \
  --full --flatten
```

- `--expect` is the honest one: if the page you asked for never rendered, the tool says so
  (exit 3) rather than hand you a picture of a login screen.
- `--open` clicks accordions **that are closed** first, so a review sees the sections, and
  an already-open section is not toggled shut.
- `--flatten` puts sticky/fixed bars back in the flow. Use it with `--full`: otherwise a
  header paints halfway down the document and reads like a layout bug that is not there.
- `--viewport` (off by default) is for "what does the fold look like", not for reviews.

An agent gets the same thing as one tool call — `page_shot(url=..., flatten=true,
open=[...], sign_in={...})` — and hands the returned path straight to `ask_claude(images=...)`.

**The box is never somebody else's to send.** A message only ever contains what the bridge
itself typed. If the composer already holds something — a half-written thought, a screenshot
you pasted and did not send — the send is **refused** and says what is in the way:

```bash
ask-claude --composer     # what is in the box right now (draft text, attachments)
ask-claude --clear        # empty it; a draft's *text* is parked in drafts/ first
```

A refused **text** report is not lost: it goes to the outbox and is delivered by itself once
the composer is clear. A refused **image** ask is not queued (the outbox is text) and says
so, so nothing is ever promised that was not sent.

---

## Watching an agent work

An opencode session belongs to the **directory it was created in**. An agent working in a
repo you are not looking at is therefore invisible in whichever `opencode` window you have
open — the session is on the same server, just in another project's list.

**So the watcher opens the window for you.** When it starts an agent it also launches a
terminal in the order's directory, attached to that agent's session, titled
`<order> - <repo>`:

```
WO-105 - my-api         a qterminal window running:  opencode -s ses_e39bc84...
```

A work order is never silent background work any more. The window is opened *after* the
session is already running, and only as a nicety: if it cannot open (no display, no
terminal emulator installed) the order still goes ahead and the log says so.

Settings in `config.json`, under `watch`:

| setting | default | meaning |
|---|---|---|
| `launch_terminal` | `true` | open a terminal window per work order; `false` = background only |
| `terminal` | auto-detect | template to force one, e.g. `["qterminal", "-w", "{dir}", "-e", "{cmd}"]`. Placeholders: `{dir}`, `{cmd}` (command as one argument), `{file}` (launcher script) |

Each window runs a small generated script, `terminals/<session>.sh`, which `cd`s to the
repo, sets the window title and `exec`s `opencode -s <session>` — read it if you ever wonder
what a window is doing. Auto-detect takes the first of: qterminal, xfce4-terminal, kitty,
alacritty, wezterm, konsole, gnome-terminal, kgx, ptyxis, foot, tilix, mate-terminal,
lxterminal, xterm. What it launched is logged in `terminal.log`.

### Watching from where you already are

```bash
python3 ~/claude-bridge/agent-watch.py            # the agent running right now
python3 ~/claude-bridge/agent-watch.py WO-105     # by work-order id
python3 ~/claude-bridge/agent-watch.py --once     # where it got to, then exit
```

That prints each step as it happens (text, tool calls with the command or file, `:error` in
red) and exits when the agent stops. It only reads, so it is safe to leave running.

Inside the TUI itself, **Ctrl+X then l** is *"List all sessions"* and **Ctrl+O** opens
*"recent sessions and projects"*; or launch straight onto one with `opencode -s <session-id>`.
`opencode session list` is directory-scoped as well, so from the wrong directory an order is
simply not in the list.

---

## When the machine stops the agents (a restart, or running out of memory)

Every agent's turn runs inside **one process**: the opencode background server. When that
process dies, every in-flight turn dies with it - silently. On the night of 2026-11-11 it
happened for real:

```
03:51-04:04  free memory 4-6% of 6 GB (246 MiB at 03:52); journald "under memory pressure"
03:53:07     the desktop marks the agent windows unresponsive - the box is thrashing
03:53:08     every opencode client loses its event stream
03:53:31+    a stampede of clients each start "opencode serve --service"
03:55:41     the old server: "managed service registration replaced; shutting down"
04:03:46     the server that is still running today
```

Five agents (TUIs + their sessions), three JS test suites and Chrome on a 6 GB machine is
what did it. WO-108 through WO-111 all stopped **mid-turn**, nothing resumed them, and the
work only carried on when someone touched those windows. Both repositories were clean
afterwards - the agents commit each step - so an interruption costs a turn, not work.

The loop now expects this and deals with it:

* **Agents are capped (`max_agents`, default 2).** An order over the cap is left completely
  unmarked and picked up by a later pass, so nothing is dropped: it is merely the queue
  working. A slot belongs to a *running* agent, which a timestamp cannot tell you - the full
  web end-to-end suite is 48.8 minutes of silence and a turn killed with the background
  server is silent too, and they want opposite things. So a session quiet for less than
  `slot_probe_seconds` (2 min) is trusted to be working (that window is the race guard: a
  prompt has been accepted but its turn has not appeared yet); past it the watcher asks the
  session's own transcript whether a turn is running - the newest entry is an `idle` marker
  once a turn has ended, one 355-byte request - and frees the slot the moment it has ended.
  `slot_seconds` (an hour) is the backstop for a probe that cannot answer, never the normal
  rule, and `watcher.sh done <id>` frees a slot the moment a report lands.
  This is not theory: on 2026-11-11 five sessions held a cap of two for 35 minutes, because
  a restart-resume moved each clock and the old rule counted a clock. One of them was
  working.
* **The server comes back once, not in a stampede.** The watcher is the single owner: if the
  registered pid is gone it runs `opencode service start` itself (at most once a minute),
  and it never touches a live server.
* **The turns it interrupted are resumed.** A restart (noticed by the pid changing) makes
  the watcher re-prompt the agents that were stopped: *"your previous turn was interrupted
  ... `git status && git log` first ... do NOT redo committed work ... carry on, then report
  DONE"*. It never interrupts an agent that is working, never touches an order you have
  marked finished, and is bounded to `resume_max` per order with one per
  `resume_cooldown_seconds`, `resume_max_per_pass` at a time - a reboot must not fire up
  every agent that ever ran.
* **The windows left behind are closed.** An idle agent's window costs 150-250 MB for
  nothing. `watcher.sh reap` closes every agent window that is not working; on its own the
  watcher closes one once its agent has been quiet for `reap_idle_seconds` (2 h). A working
  agent's window is never closed, and neither is any window the watcher did not open.

```bash
~/claude-bridge/watcher.sh status        # what is running: working / slot / waiting / resumed
~/claude-bridge/watcher.sh reap          # close the windows of agents that are not working
~/claude-bridge/watcher.sh done WO-111   # it reported DONE: free its slot, stop resuming it
```

`done` is worth using: nothing in the loop can tell *"the agent reported DONE"* from *"the
agent is thinking"*, so telling it frees an agent slot now instead of two hours from now.

| setting | default | meaning |
|---|---|---|
| `max_agents` | `2` | how many agents may run at once; `0` = no cap |
| `slot_seconds` | `3600` | the backstop: a slot is freed after this even if a turn seems to be running |
| `slot_probe_seconds` | `120` | quiet for less than this, a session keeps its slot without being asked |
| `service_start_cooldown` | `60` | at most one `opencode service start` this often |
| `resume_interrupted` | `true` | resume the turns a server restart interrupted |
| `resume_max` | `3` | how many times one order may be resumed before it is left for a human |
| `resume_max_per_pass` | `2` | how many orders may be resumed in one pass |
| `resume_cooldown_seconds` | `1800` | never resume the same order twice inside this |
| `resume_max_age_seconds` | `21600` | a session quiet longer than this is over, not interrupted |
| `reap_idle_seconds` | `7200` | close an agent's window after this long of quiet |
| `agent_active_seconds` | `1800` | the transcript moving inside this window means "working" |

If you would rather systemd owned the server than the watcher, `contrib/opencode.service`
is a user unit for it (`systemctl --user enable --now opencode`); set `max_agents` and
`service_start_cooldown` with that in mind. The watcher's own supervision is the default
because it needs no sudo, and because one starter is what stops the stampede.

---

## Starting a new chat

A long thread fills up, so you want to continue in a fresh chat. The chat the
bridge talks to is whatever `config.json` says in `chat_url`, and by default a
fresh chat's address is not settled: `https://claude.ai/new` only becomes
`/chat/<id>` once the first message has been sent. That is why starting one by
hand used to fight you:

- opening a new chat in the bridge window and running any bridge command would
  navigate that tab **back** to the saved chat, so `--save-chat` saved the old
  chat again;
- and if several claude.ai tabs were open (a restored window, the launcher adding
  one), the bridge picked one at random, so a message could land in the wrong chat.

One command now does the whole switch, on the tab the bridge already drives:

```bash
ask-claude --new-chat                              # fresh chat, becomes the one we use
ask-claude --new-chat "context: <handoff summary>"  # fresh chat, seeded with your context
```

`--new-chat` opens a fresh chat in the bridge window, makes it the chat we use
and prints it. A new Claude has no memory of the old thread, so give it the
handoff summary as the message (or paste it yourself and re-run `--save-chat`).
`--save-chat` alone still means "remember the chat that is open in the bridge
window" - it reads the tab as it is and never navigates it. To move back to an
older chat (or to any chat you have a link to):

```bash
ask-claude --goto "https://claude.ai/chat/<id>" --save-chat
```

Details worth knowing:

- **The driven tab is remembered** (`bridge-target.json`, git-ignored). Extra
  claude.ai tabs are left alone and cannot capture your messages; if that tab is
  closed, the bridge falls back to the tab showing the saved chat.
- **`--status` shows it**: `url`, the `target` it drives, and the `tabs` it can see.
- **The queue carries over.** `state.json` keeps the orders this machine has
  already handled, so nothing re-dispatches just because the chat changed. An
  order is skipped only when its `id` **and** its text are byte-identical to one
  already seen - so if Claude in the new chat re-uses an id like `WO-101` with
  different text, it is still picked up.
- **The standing plan still applies.** With `watch.compact_on_resume: true`, the
  compact step runs when the limit lifts; see the section below.
- **Cheaper than it sounds**: starting the chat costs no Claude message if you
  send nothing.

---

## Long conversations and `/compact`

The user's habit for long threads is to run **`/compact`** on Claude so the
conversation can keep going once the context fills up.

Reality check on the web UI (verified by probing the live page):

- Typing `/` in claude.ai's composer opens a menu of **skills** (`deep-research`,
  `google-workspace`, `import-memory`, `morning`, `skill-creator`, `ui-ux-pro-max`, …)
  plus Doc / Slides / Design — there is **no `/compact` entry**.
- So `/compact` most likely belongs to **Claude Code (the CLI)**, not the browser chat.

How the bridge handles it:

- `ask-claude --slash <cmd>` types a slash command and selects it **only if the menu
  offers it**; otherwise it clears the box and refuses, so a stray `/compact` is never
  posted into the thread as a message.
- `ask-claude --compact`, the MCP tool `compact_conversation` and `Bridge.compact()`
  now do the fallback for you: they try `/compact` (which the web refuses), then ask
  Claude for the compact handoff below. So one call is enough.
- If a thread is getting long, the practical web-UI equivalent is to ask Claude for a
  compact summary and continue in a fresh chat (this is the text the fallback sends):

  ```bash
  ask-claude "Please write a compact handoff summary of this thread (goals, decisions, current state, next steps) so it can continue in a new chat."
  ```

  then carry it into a fresh chat with `ask-claude --new-chat "<the summary>"`.

---

## Files

| file | purpose |
|------|---------|
| `bridge.py` | the CDP driver + CLI (`ask-claude` is a thin wrapper around it) |
| `page-shot` | screenshot a running app with its own Playwright — whole page, phone width by default |
| `mcp_server.py` | MCP (stdio) server exposing `ask_claude` / `page_shot` / `claude_availability` / `wait_for_claude` / `claude_status` / `compact_conversation` / `bridge_up` / `check_work` / `fetch_claude_files` |
| `watcher.py` | polls the chat for work orders and starts agents on them |
| `watcher.sh` | start / stop / status the watcher |
| `agent-watch.py` | follow a running work-order agent live from any terminal |
| `terminals/` | the tiny per-session launcher script each agent window runs |
| `start-bridge-chrome.sh` | launches/reuses the dedicated bridge Chrome window |
| `bridge-up.sh` | after a restart: bridge window + sign-in check + watcher, in one go |
| `config.json` | remembered `chat_url` + the `watch` settings |
| `bridge-target.json` | the claude.ai tab the bridge drives (so extra tabs cannot steal a message) |
| `inbox/` | work orders Claude has handed over (`<id>.md`) |
| `downloads/` | file cards fetched from the chat |
| `drafts/` | the text of a draft `--clear` had to empty out of the composer (never sent, never lost) |
| `shots/` | screenshots taken by `page-shot` when no path was given |
| `state.json` | which orders and files have been handled (watcher) |
| `watcher.log` | what the watcher has done |
| `terminal.log` | output of the agent windows the watcher opened |

Wired into opencode in `~/.config/opencode/opencode.jsonc` under
`mcp.servers.claude-bridge`.

---

## Troubleshooting

- **"bridge Chrome is not reachable"** → `bash ~/claude-bridge/bridge-up.sh`
  (that starts the bridge window *and* the watcher; do **not** just open Chrome normally —
  the default profile is not allowed to expose the debug port).
- **"No composer … sign in"** → the bridge window is signed out; sign in there.
- **It replied but the text looks like the previous answer** → run `ask-claude --probe`
  and check the assistant-message selectors still match claude.ai's DOM; the
  selectors live in `ASSISTANT_SELECTORS`-style lists inside `bridge.py`.
- **You closed the bridge window** → just run `start-bridge-chrome.sh` again; the
  profile (and your sign-in) is kept in `~/.config/claude-bridge-chrome`.
- **A work order was picked up but the agent does nothing** → check the session's model
  call: `opencode api GET /api/session/<id>/message`. If it shows
  `provider.rate-limit` / *"Your free allowance for ':free' models is used up"*, the
  watcher is configured with a `:free` model whose allowance is gone — the session just
  retries forever and no work happens. Fix the model in `~/.claude-bridge/config.json`
  (`watch.model.id`, e.g. `deepseek-v4.1-flash` **without** the `:free` suffix, which runs
  on the plan/balance), then **restart the watcher** — `config.json` is read once at
  startup, so a running watcher keeps the old model. Repair the stuck session with
  `POST /api/session/<id>/model` + `POST /api/session/<id>/prompt` instead of cold-starting
  a duplicate. `ask-claude --available` will *not* reveal this: Claude can be answering
  fine while the agent's model is blocked.
