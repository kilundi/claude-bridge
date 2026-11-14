# The bridge: what is left to improve

A working list. Each item says what it is, the evidence that put it here, and what closes it.
Ordered by what a failure costs: **a loop that loses or stalls work** outranks **a box that runs
out of memory**, which outranks **noise**.

---

## P1 — the loop loses or stalls work

### 1. A report file finishes an order, but nothing checks the planner ever saw it

- **Evidence.** `reports/WO-157.md` was written 11-13 12:57 and the order auto-finished 13:03,
  yet the planner says *"WO-157 was posted earlier and I haven't seen its report"*. The outbox is
  empty, so no delivery was even attempted. The watcher treats the report file as proof the *work*
  is done — correctly, that is what frees the slot — but the **planner** is still waiting, and the
  planner is what decides what happens next: the queue stops there. WO-158 was posted anyway,
  so this one cost a stalled order, not the whole loop.
- **Done** (`7b8bfeb`), by the signal the loop already had rather than a new ledger. An agent
  that sends a report calls `ask_claude`, and the watcher can see that call in the agent's own
  transcript — the same read that already finishes an order. So: a finish **from the report file
  alone** (no report in the transcript) queues that file for the planner through the outbox the
  watcher already flushes, marked *"[WO-x wrote this to reports/WO-x.md and its own report never
  reached you, so the watcher is sending it]"*. No new file format, no order id to invent, and a
  report the agent did send is never sent twice.
- **Why not a `delivered/<order>.json` ledger.** The bridge would have to know *which order* a
  message belongs to, and reports carry no order id (the AGENTS.md template has `project:`, not
  the id). Attributing by text hash fails the moment the file and the message differ by a word,
  which is the normal case. The transcript already pairs the two.
- **Proof.** `test_a_report_file_never_sent_is_queued_for_the_planner`: a sent report is not
  re-sent; an unsent one is queued exactly once, with the note, and the queue content is checked
  through `bridge.read_queued` (the same reader the flush uses). The real `outbox/` is redirected
  in the suite so a test can never deliver anything.

### 2. Publish the ask fix that is sitting uncommitted (WO-149)

- **Evidence.** `watcher.py` and `tests/test_restart_resilience.py` were changed 11-12 19:05 and
  never committed. The change makes `agent_busy` ignore **finished** orders: WO-130's finished
  window kept its transcript moving (a person was typing in it), so every pass answered "WO-130 is
  still working", the ask about an order block with no `acceptance` line (WO-149) was held
  forever, and a complete order stood still with both slots free.
- **Status.** Published in `7b8bfeb` at the person's instruction, with the provenance stated in
  the commit message. Green with it in the tree: **84/84** (86/86 after the two fixes above).

### 3. Nothing brings the loop back after a reboot

- **Evidence.** The machine rebooted 11-13 ~16:28 (the log ends 16:27 with the background service
  failing to start). The watcher has been down for 18 h since, no work order has been picked up,
  and there is no systemd user unit and no autostart entry for it.
- **Done.** `contrib/claude-bridge-watcher.service` (user unit, `Type=simple`,
  `Restart=on-failure`, `RestartSec=15`, `After/PartOf=graphical-session.target`,
  `Environment=DISPLAY=:0` so a restart can still open a window, `KillMode=process` so a watcher
  restart never kills an agent). Installed with `systemctl --user link` + `enable --now`; live
  since 11-14 11:05:41 and it picked up WO-158 within 13 seconds, opening the window
  `WO-158 - wms-web` (tmux session `cb-wms-web-e5557712`).
- **Also.** `watcher.sh run` (foreground, for the supervisor) and a supervisor-aware `status` /
  `stop`: with systemd owning the process there is no pid file, so `running()` alone answered
  "not running" while the loop was plainly working. `watcher.lock` being held now counts as
  running, `stop` points at `systemctl --user stop claude-bridge-watcher`, and a second
  `watcher.sh start` still refuses.
- **Note.** `Environment=DISPLAY=:0` fits this machine; a session on another display needs that
  line changed (`echo $DISPLAY`).

### 4. Two stale "waiting for you" marks outlive their orders

- **Evidence.** WO-130 (since 11-11 18:04) and WO-148 (since 11-12 17:56) are both finished and
  both still in `waiting`: a later pass re-adds the mark that the finish removed. The status row
  hides it (finished wins), but reaping treats them as parked and never closes their view, and a
  parked order is never asked about.
- **Done** (`7b8bfeb`). `note_waiting` skips finished orders and drops a mark that is already
  there. Proof `test_a_finished_order_drops_its_waiting_mark`: a finished order's mark goes and
  stays gone, and a live parked order still parks.

---

## P2 — the box runs out of memory

### 5. A free-memory floor before starting an agent

- **Evidence.** A 7.7 GB box, 1.2 GB available as I write this. One `jest --runInBand` peaks at
  1.8 GB. On 11-11 at 03:53 free memory reached ~246 MiB and the opencode server died with every
  in-flight turn in it; on 11-13 the same signature ("Background service failed to start") appears
  in the log right before the reboot at 16:28. The cap is `max_agents: 2`, which says nothing
  about how big those two happen to be.
- **Done.** `min_free_mb` (default **800**, `0` = off) is read from `MemAvailable` before any
  order starts; below the floor the order is held unmarked, the log says what it read, and
  `status` prints `memory: 412 MB available (min_free_mb=800) - below the floor: no new agent
  will start` and counts that order as `waiting for free memory`. An agent already running is
  never touched. A failed reading (`/proc/meminfo` unreadable) never holds work.
- **The number changed while building it.** The plan guessed ~1500, and on this box that held
  orders while one agent plus Chrome still left ~1.0-1.2 GB free — a stalled queue, which this
  list ranks *above* memory pressure. 800 is three times the ~246 MiB the background server
  actually died at, and `max_agents` bounds the total. It is one config value, so it can be
  raised without touching code; `status` shows the live reading on every call.
- **Proof.** `test_low_memory_holds_new_agents`: held below the floor with the line logged and
  the status rows checked, started on the first pass after memory frees, off at `0`, and
  never holding on an unreadable reading. The suite's own config sets `min_free_mb: 0`, so no
  other proof depends on how much memory this machine happens to have.

### 6. The background service is replaced — and killed — more often than it should be

- **Evidence.** The service pid changed several times a day (e.g. 861542 → 958611 → 4902 across
  one day and a reboot), the log holds 872 service lines, and the recurring
  "could not read WO-x's session record: Background service failed to start" plus CLI stack traces
  land in `watcher.log`. A replacement kills in-flight turns, which the resume logic then papers
  over — so it is invisible unless you count pids.
- **Done, and the answer was not the watcher.** Of the 39 pid changes on record, only 5 were
  the watcher's own start (`service.started`); the other 34 were *clients* — every
  `opencode api` call made while the service is down starts a client that tries to bring one
  up, and a new service replaces the registered one. The watcher probes every session every
  pass, so it *was* a herd of starters: 187 `Background service failed to start` lines in five
  minutes on 11-11, 94 in two on 11-13. Now the first failure stops the probing for
  `SERVICE_DOWN_MEMO` (120 s) — the supervisor's one start a minute is the only one — the
  recovery is logged, and repeated per-order lines collapse through `log_once`.
- **Observable replacement.** Each change is attributed (`by the watcher (it found no live
  server)` vs `by something else (an opencode client started its own...)`) and kept in the
  last five in `state.service.changes`; `status` prints the last one.
- **Proof.** `test_a_probe_never_starts_a_second_service` (four probes against a down service
  = one CLI call, one log line, recovery noticed) and `test_a_replacement_says_who_did_it`
  (attribution both ways, the five-deep ring, the status line).
- **Still open.** The remaining replacements are the CLI's own doing and cannot be fixed from
  here: the acceptance ("a full day with no unexplained replacement") is now *measurable* —
  `status` names the last one and who caused it — so the next day of work will say whether
  anything else is at play.

---

## P3 — noise and papercuts

7. **`is no longer in the chat` 125 times.** *Done.* `log_once` says it once per order per six
   hours, and the deeper cause is fixed too: a `waiting` mark (an agent parked on a question)
   is no longer dropped just because the order's message scrolled out of the chat — the question
   is answered in that agent's *window*, so the mark is what keeps the window from being reaped.
   It goes only when there is no session left to be parked in. The 312 `could not read ...
   session record` lines go through the same helper. Proofs: `test_a_repeated_line_is_said_once`,
   and `test_a_stale_wait_is_not_reported_as_waiting` now checks the reasons and the kept mark.
8. **`watcher.sh` has no `restart`.** *Done:*
   `start|stop|restart|status|log|reap|run|prune-state|done`. `restart` goes through
   `systemctl --user restart` when the unit owns the watcher — a plain stop+start would make
   systemd see its process vanish and start a second one.
9. **`status` never shows the views.** *Done:* `views: N open`, then one line per view (`tab
   <session>:<order>` or `window pid <n>`), with how long its agent has been quiet, from the same
   `agent_windows` the reap pass uses. Proof: `test_status_shows_the_views_we_own`.
10. **`state.json` keeps everything.** *Done:* `state_keep_days` (7) drops the whole start record
    of an order finished longer ago — the parsed order, its files and inbox path, which are only
    ever needed to *prompt* an agent. `finished`, the hash marks in `orders` and the session
    mapping stay, because they are still read. It runs every pass and on demand
    (`watcher.sh prune-state`). Proof: `test_state_prunes_only_old_finished_orders`.
    *Nothing is dropped yet:* all 40 finished orders are under four days old, so the file is
    still ~189 KB — the trimming starts next week, and the ring is in the proof.

Done, for the record: image attach, whole-page phone screenshots, one window per repository with a
tab per order, tabs reaped through their tmux session (which also sweeps the detached ones), a
report file that frees its own slot, a parked turn that frees its slot, and a view that is kept
when you ask for it.

---

*Person-specific state (unfinished orders, running containers, the dual-boot prerequisites) is
deliberately not in this file: it belongs in the session, not in a public repository.*
