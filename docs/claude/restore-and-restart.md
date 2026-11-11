## Component restart controls (`app.py`, `discord_bot.py`)

- **`/admin/system` is deliberately separate from `/admin/resources`** — Resources is
  about the host machine's hardware, System is about this app's own process and
  components. Two restart targets: the whole app process, or just the Discord bot's
  connection.
- **Whole-app restart (`app._restart_process()`) uses `os.execv(sys.executable,
  [sys.executable] + sys.argv)`** — replaces the running process image in place (same
  PID), which works identically whether launched as `python app.py`, `python
  serve_waitress.py`, or either wrapped in a systemd unit/Task Scheduler entry, and
  needs **no supervisor process** unlike a fork+exit approach. Delayed by 1s on a
  background thread first (same shape as `monitoring.control_host()`) so the
  triggering HTTP response actually reaches the browser before the process image swaps
  out from under it.
- **Both targets go through `app._require_totp()`** — a full-app restart briefly takes
  the whole portal offline and a bot restart interrupts anyone mid-conversation with
  it. Same confirmation-panel UI pattern too (`static/js/admin_system_control.js`,
  mirroring `admin_host_control.js` — one confirm panel driving both trigger buttons
  via a `data-component` attribute instead of `data-action`).
- **A step-up panel is: trigger button → panel → 2FA code (when on) → Confirm. There is no
  word to type, and it must not come back.** All five actions behind `_require_totp()` (VM
  control, host restart/shutdown, app/bot restart, update, database restore) used to also ask for
  START / RESTART / UPDATE / REPLACE. The word was only ever checked in the browser - the
  server never saw it - so it added a second thing to type inside a thirty-second code and
  protected nothing the code does not. With 2FA off the panel is just trigger + Confirm, which is
  as much friction as the portal's other destructive buttons have. Opening the panel focuses the
  2FA field so the whole action is: click, type six digits, Enter
  (`test_the_step_up_panels_ask_for_the_2fa_code_and_no_word_to_type`; `docs/HISTORY.md` → "1.11.1").
- **`_restart_process()` and `control_host()` are not the same risk, and the rule
  distinguishing them was learned the hard way.** `control_host()` reboots or shuts
  down the *machine* and must never be live-invoked anywhere, full stop.
  `_restart_process()` only re-execs *this app's own process*, and testing it
  exclusively with mocks hid a real bug for months: under `python app.py` every
  restart died with "Address already in use" and the portal never came back
  (`docs/HISTORY.md` → "the restart that never came back"). A mocked test asserts that
  the route *called* the function — it cannot tell you the process comes back up.
  So: mocked tests remain the default and are what the route tests use, but **a change
  to restart behaviour must also be exercised live at least once** against a
  throwaway portal in this sandbox, checking the PID is unchanged and the port answers
  again. Never against anything the user depends on, and never `control_host()`.

- **A page that reloads itself must not reload a panel somebody is using.**
  `static/js/main.js` (the admin Resources & VMs page and the public status pages) reloads
  every `refresh_seconds`, and a reload closes any panel a script opened and empties what
  was typed into it - which is how the VM confirmation panel kept vanishing under a
  half-typed 2FA code. `main.js` now holds off, and restarts its countdown from the full
  interval, while an element marked **`data-holds-refresh`** is on screen or a text field
  has the focus. **Every `*-confirm` panel on a page that loads `main.js` must carry the
  attribute** - `tests/test_conventions.py` enforces it. Don't "fix" this by lengthening
  the interval or by making the panel survive a reload; the page is meant to stay live for
  everything *except* what is being typed into (`docs/HISTORY.md` → "1.11.1").

## Restoring the database (`/admin/about/restore-db`, `db.py`)

- **The order of operations is the safety machinery and is not rearrangeable**:
  stage the upload to a temp file (live database untouched) → validate → snapshot the
  *current* database → atomic replace → restart. A refusal at any point must leave the
  live database byte-identical and take no snapshot; there are tests asserting exactly
  that for junk files, foreign databases, bad zips and zip bombs.
- **Validation is four checks, and the third is the one people forget.** The SQLite
  header, then `PRAGMA integrity_check`, then **`RESTORE_REQUIRED_TABLES` must all be
  present**. The first two only prove "a valid SQLite database" — which a Jellyfin
  library, a browser cookie store or an *Arr database all also are, and restoring one
  of those silently wipes the portal and leaves it unable to start. Fourth, **the
  backup's `settings` must hold a non-empty `admin_password_hash`**: a database
  without one is a portal in first-run state, and restoring it would hand the admin
  account to whoever reached the login page next. Validation opens
  the file **read-only via a `file:...?mode=ro` URI**, so checking a file can never
  create or modify one.
- **The WAL sidecars must be deleted as part of the replace** (`db.restore_from_file`).
  The database runs in WAL mode, so `portal.db-wal` holds committed pages belonging to
  the *old* database; leaving it beside a new main file lets SQLite replay one file's
  journal into another, which is a corrupt database rather than a failed restore.
  Checkpoint first, replace, then remove both `-wal` and `-shm`.
- **The staged upload is written next to `instance/portal.db`, never in the system temp
  directory** — `os.replace()` is only atomic within one filesystem, and a 60 MB
  upload shouldn't be able to fill a small `/tmp`.
- **A zip's declared `file_size` is not trusted**: the extraction cap is enforced
  against bytes actually read. Member names are never joined to a path at all (the one
  member is streamed to a filename we chose), which is what makes zip-slip
  structurally impossible here rather than merely guarded against.
- **The two kinds of backup on the About page must stay visibly distinct.**
  `updater.py`'s are **application code**, for undoing a bad update, and hold no data;
  these are **database** snapshots and hold nothing else. Neither can restore the
  other, and the page says so. Don't merge the two lists.
- **`DB_RESTORE_MAX_BYTES` is applied per-request, not app-wide.** Flask 3.1 (hence
  `Flask>=3.1` in `requirements.txt`) allows `request.max_content_length` to be set for
  one request; raising `MAX_CONTENT_LENGTH` instead would hand every form on the site,
  the public report form included, the same large body allowance. The hook that sets
  it **must be registered before `_check_csrf`**, because `_check_csrf` reads
  `request.form`, which parses the body and would 413 a perfectly good upload before
  the view ever runs. **`MAX_EXTRACTED_DB_BYTES` (the cap on the database's own
  uncompressed size once extracted, a separate constant) must never be set equal to
  `DB_RESTORE_MAX_BYTES` again** - it was, for a while, and that silently defeated
  the entire point of accepting a zip: a real SQLite database routinely compresses
  well, so a database well over the *upload* cap can zip down to comfortably under
  it, only to have its *uncompressed* size rejected at extraction regardless. Caught
  live (`docs/HISTORY.md` → "the extraction cap that couldn't benefit from zip
  compression") - the fix keeps the two independent, with the extraction cap
  meaningfully larger than the upload cap.
- **The staged upload's SQLite sidecars must be cleaned up too.** Validating it opens
  the file with SQLite, and opening a WAL-mode database — which every backup of this app
  is — creates `-wal`/`-shm` beside it, even read-only. `os.replace()` moves only the
  main file, so without `_remove_sqlite_sidecars()` every restore *and every refusal*
  left two orphaned files in `instance/` forever. Found by looking at the directory
  after a release re-test; no route-level assertion would have noticed.
- **`_prune_db_safety_backups()` reads `KEEP_DB_SAFETY_BACKUPS` inside the function**,
  not as a default argument — a default arg binds at def time and silently ignores a
  monkeypatched constant, the same trap that made `updater._prune_backups` pass for the
  wrong reason once.
