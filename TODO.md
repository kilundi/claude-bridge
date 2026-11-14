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
- **Close with.** Before starting an agent, read `MemAvailable`; below a floor
  (`min_free_mb`, ~1500) do not start — hold the order, say so loudly in the log, show it in
  `status`, and let the next pass try again. An agent that is already running is never touched.
- **Proof.** A unit proof with a stubbed memory reader: below the floor nothing starts and the
  line is logged; at or above it the order starts; the held order starts on a later pass.

### 6. The background service is replaced — and killed — more often than it should be

- **Evidence.** The service pid changed several times a day (e.g. 861542 → 958611 → 4902 across
  one day and a reboot), the log holds 872 service lines, and the recurring
  "could not read WO-x's session record: Background service failed to start" plus CLI stack traces
  land in `watcher.log`. A replacement kills in-flight turns, which the resume logic then papers
  over — so it is invisible unless you count pids.
- **Close with.** Find out whether it is the watcher's own supervision or the CLI reaping it,
  then make a replacement calm and observable: never replace a *live* service, log one line when
  the pid changes, and record the reason. Acceptance: a full day of work with no unexplained
  replacement and no turn killed by one.

---

## P3 — noise and papercuts

7. **`is no longer in the chat` 125 times.** Say it once per order, not once per pass.
8. **`watcher.sh` has no `restart`.** I keep doing `stop` + `start` by hand.
9. **`status` never shows the views.** Which windows/tabs we own, their tab names and ages, would
   make "who is on screen" answerable without `pgrep` + `tmux ls`.
10. **`state.json` keeps everything.** 188 KB over 45 orders is fine, but a `prune` for orders
    finished more than a week ago (keeping `finished`, dropping their `starts` order text) would
    stop it growing without limit.

Done, for the record: image attach, whole-page phone screenshots, one window per repository with a
tab per order, tabs reaped through their tmux session (which also sweeps the detached ones), a
report file that frees its own slot, a parked turn that frees its slot, and a view that is kept
when you ask for it.

---

*Person-specific state (unfinished orders, running containers, the dual-boot prerequisites) is
deliberately not in this file: it belongs in the session, not in a public repository.*
