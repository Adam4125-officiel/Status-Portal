## Scheduled tasks (`scheduler.py`, `/admin/tasks`) — added 2026-08-21, v1.7.0

- **A new recurring job is a `scheduler.register()` call, not a fourth background
  thread.** This app had three hardcoded loops (health checks, resource polling, the
  Discord bot) before this existed, each with its cadence baked in. The framework is
  what stops a fourth appearing every time something needs a different schedule, an
  on/off switch or a "run it now" button.
- **The code owns the registry; the database owns the settings.** `register()` is
  called at import time by whichever module owns the job (`jellyfin_auth.py` is the
  first); the `scheduled_tasks` table holds only what an admin can change plus the
  outcome of the last run. Adding a task therefore needs **no migration** — the row
  is created lazily on first use via `INSERT OR IGNORE`, which is also what stops a
  restart resetting an admin's saved schedule back to the registry defaults.
- **A task is a plain callable returning a short message.** It signals "nothing to
  do, and that isn't a failure" by raising `scheduler.TaskSkipped` (recorded as
  `skipped`) and failure by raising anything else (recorded as `failed`, logged with
  its traceback). Don't add a base class; there is nothing for one to hold.
- **Every due task runs in its own short-lived thread**, so a slow or hung task
  delays only itself. The per-task lock is what keeps that bounded: a task never
  overlaps itself, so a hung task costs exactly one stuck thread rather than a new
  one every tick. "Run now" on a task that's already running reports `busy` and
  **records nothing** — the run in progress writes the real result, and stamping
  `last_run_at` for a run that never happened would corrupt the schedule.
- **A remote service being down is a failed run, but not a traceback.** Raise
  `scheduler.TaskUnavailable` (or just let a `requests` error propagate - `run_task()`
  walks the exception chain and recognises one either way). The run is still recorded
  as `failed`, because it is; only the logging changes, to one warning line carrying
  the reason. A traceback describes a fault in *this* code, and printing a chained one
  because Seerr answered 502 sends whoever reads the log hunting for a bug that isn't
  there - which is exactly what got reported (`docs/HISTORY.md` → "a 502 that read like
  a crash"). **A genuine bug in a task must still get its full traceback**; there's a
  test pinning that half, and it's the one that matters if this is ever "simplified".
- **A failed run still stamps `last_run_at`.** An interval schedule is measured from
  it, so leaving it alone on failure would make a permanently-failing task retry on
  every single scheduler tick instead of on its next scheduled run.
- **`next_run_at` is derived from the stored `last_run_at`, never tracked in
  memory.** That's what makes a daily task missed while the portal was down run on
  the next tick after it comes back, rather than silently skipping a day. A
  never-run *interval* task is due immediately; a never-run *daily* task is due at
  its next HH:MM (firing a 03:00 job the instant the portal first starts at 14:00
  would be surprising, not helpful).
- **`record_task_run()` and `update_task_schedule()` are `UPDATE` statements**, so
  they silently write nothing on a task whose row was never materialised. Both are
  reached through `scheduler.run_task()`/`scheduler.save_schedule()`, which ensure
  the row first — **don't call the `db.` functions directly from a route.** This bit
  twice while the framework was being written, both times as "the action appeared to
  work and recorded nothing".
- **Schedules are interval-or-daily, deliberately not cron.** Cron means either a
  new dependency in a project with five, or a hand-rolled parser with its own bug
  surface, and buys nothing the two modes cover. If cron is ever genuinely wanted,
  add it as a third `schedule_kind` rather than replacing these.
- **Daily times are UTC**, matching everything else this app stores, and the form
  says so. A schedule that silently shifted with daylight saving would be worse than
  one you convert once.
- `config.SCHEDULER_TICK_SECONDS` (30s) is the granularity floor — a task set to
  "every 5 minutes" can only be as punctual as the tick allows. It's a check
  interval, hence an env var; everything per-task is a DB row.
- **Three of this app's recurring jobs are deliberately *not* tasks, and must stay
  that way** — the health-check loop (switching it off from a browser would also
  switch off incident detection, which is the portal's whole job), resource polling
  (runs every 10s, faster than `SCHEDULER_TICK_SECONDS`, so the scheduler physically
  cannot drive it), and the Discord bot's refresh (lives inside discord.py's own
  asyncio loop). They are registered read-only via **`scheduler.register_loop()`** so
  `/admin/tasks` is still the complete answer to "what runs on a timer" instead of
  two thirds of it. `is_alive` is a **callable**, not a value — a thread that died
  since startup has to render as dead, and a value would freeze "alive" forever.
  `alive is None` means "never started in this process", which reads differently from
  "started and died" and must not be collapsed into it.
- **`tests/test_conventions.py::test_no_new_bare_background_loops` counts `while
  True:` per module against a named allow-list.** A fourth one fails the suite with
  instructions. If a new loop genuinely can't be a task, register it as a loop *and*
  add it there with the reason — don't just raise the number.
- **The update check and the status-history prune are tasks** (`update_check` in
  `updater.py`, `prune_status_history` in `app.py`), not hand-rolled "is it due yet"
  checks bolted onto the health-check loop, which is what both used to be. The prune
  in particular was tracked on a module-level float, so a portal restarted twice a
  day never pruned at all — deriving the schedule from the stored `last_run_at` is
  what fixes that, and is the general argument for the framework.
- **Automatic update checking has exactly one switch.** `/admin/about`'s checkbox
  writes the `update_check` task's own `enabled` flag (via `app._set_update_check_enabled()`,
  preserving the rest of the schedule — it's an on/off switch, not a reschedule), and
  `updater.update_check_enabled()` reads that row. Don't reintroduce a separate
  `update_check_enabled` setting beside it: the two would drift, and the About page
  would confidently contradict the Tasks page. The legacy setting is still read
  **once, at import**, purely to seed the new task's default so upgrading preserves an
  admin's existing "don't phone home" choice. `updater.py`'s import surface widened to
  include `scheduler` for the registration — still config/db-only, still no Flask, so
  the CLI is unaffected.
- **`PORTAL_UPDATE_CHECK_INTERVAL_SECONDS` is now a *default*, not a fixed interval** —
  it decides what the task's schedule starts as; after that the DB row wins and an
  admin changes it from `/admin/tasks` with no restart.
- **Each task card's schedule-editing form is collapsed by default**, behind a native
  `<details>` (same technique as the combined wizard's "Advanced settings" block, zero
  JS) - the info table (Last run/Result/Next run) and "Run now" stay visible either
  way, only the enabled-checkbox/schedule-kind/interval-or-daily/Save form collapses.
  The "Always-on background loops" section is deliberately left always-expanded -
  informational/read-only, nothing to collapse away from.
