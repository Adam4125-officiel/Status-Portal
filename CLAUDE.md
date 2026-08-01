# Notes for future Claude Code sessions on this repo

This file is for context that survives between sessions — architecture decisions,
gotchas, and standing workflows that aren't obvious from reading the code cold. Keep
it updated as the project evolves; don't let it go stale. See `ROADMAP.md` for open
feature ideas (this file is about *how the existing code works and how to work on
it*, not what's left to build).

## What this is

A personal Flask/SQLite status portal for a home server (Jellyfin, *Arr stack,
Jellyseerr, SMB...), meant to run via plain Python (`python serve_waitress.py` in
production), not Docker-first (Docker support exists as a secondary option). Single
admin, no user accounts. The user manages everything from `/admin` — they're
Python-comfortable but explicitly don't want to need Flask knowledge, so anything
routine (toggles, text, intervals that make sense to change often) belongs in the
DB-backed Settings pages, not a code edit.

## Conventions that matter (don't relitigate these)

- **`db.py` has no ORM, no migration framework.** Schema changes to a table that
  isn't brand-new need a `_ensure_column()` call at the end of `init_db()` — a real
  user database (`instance/portal.db`) has existed since mid-project, and
  `CREATE TABLE IF NOT EXISTS` is a silent no-op on a table that already exists, so a
  forgotten `_ensure_column()` call means every write touching the new column throws
  `sqlite3.OperationalError: no such column` in production. Brand-new tables are fine
  with plain `CREATE TABLE IF NOT EXISTS`.
- **Never call slow/external I/O directly inside a Flask request handler.** This bit
  the project once already: the public page used to call the Jellyfin/*Arr status
  checker synchronously on every load, and an unreachable integration added ~10s to
  every single page load (including every auto-refresh). Fix pattern, now standard:
  background thread polls and writes to a module-level cache dict
  (`app._integration_status_cache`-style); request handlers only ever *read* the
  cache. The one sanctioned exception is an explicit one-shot admin action the user
  knows will be slow (e.g. the "Check now" button) — never something that fires
  automatically or on every page load.
- **Config split**: secrets and things that behave like static deployment config
  (webhook URLs, bot tokens, check intervals) are env vars via `config.py` — nothing
  else should read `os.environ` directly, and changing them needs a restart. Routine
  admin-tunable toggles (site name, visibility checkboxes, command names) are DB
  `settings` rows, editable live from the browser, no restart needed. When adding a
  new setting, decide which bucket it belongs in based on that distinction, not
  convenience.
- **Optional heavy dependencies are never imported at module level.** `nvidia-ml-py`
  (GPU stats) and `discord.py` (bot) both follow this: lazy-imported only inside the
  function that needs them, wrapped so a missing package degrades to "feature
  disabled, log why" rather than crashing the whole app on import. Neither is in
  `requirements.txt`. If you add another optional integration, follow the same shape.
- **This app is meant to survive its own restart cleanly.** State that needs to
  persist across a process restart (maintenance-window progress, the Discord bot's
  tracked live-message id) lives in SQLite, never only in memory.
- **Status values aren't just `operational`/`degraded`/`maintenance`/`down`.**
  `slow` was added 2026-07-23 as a purely cosmetic tier (an otherwise-healthy
  response slower than a service's configured `slow_threshold_ms`) — it never
  opens/resolves an auto-incident on its own, and ranks between `maintenance` and
  `operational` in every overall-status precedence list. Any place one of these four
  original statuses is enumerated (badge colors, CSS class maps, Discord
  icon/label/presence-text tables, `compute_overall_status()` /
  `discord_bot._overall_status()`) needs a `slow` entry too — grep for `"degraded"`
  across the codebase before assuming you've found every status-aware spot.
- **A health-check response is "reachable" based on getting a response at all, not
  a 2xx.** Only a 5xx (or a connection failure/timeout, which stays `down`) counts
  against a service — a 401/403 login prompt or a 404 still means the server
  answered. This was a deliberate fix (2026-07-23) for services that gate their
  whole UI behind HTTP Basic Auth (e.g. Bazarr): pinging their `check_url` used to
  come back non-2xx and get misread as "degraded" even though the service was
  completely healthy.
- **`startup_grace_seconds` (per service) suppresses auto-incidents, not checks.**
  `app._within_grace_period(service)` gates the call to
  `_handle_incident_lifecycle()`/`_handle_integration_incident_lifecycle()` only —
  status and response time are still recorded on every cycle regardless, so the
  public page reflects reality during the grace window, it just won't open an
  incident over it. Measured from `app._APP_START` (process start), not from
  anything service-specific — good enough since services are expected to boot
  around the same time as the portal itself, not something to over-engineer.
- **`retry_count`/`retry_interval_seconds` (per service, added 2026-07-23) retry a
  'down' result inline before it's ever recorded, not after.** `app._check_service_status()`
  wraps `app._run_single_check()` — only a `down` outcome is retried (degraded/slow/
  operational are never worth retrying, and only `down` ever opens an auto-incident),
  the first non-down result wins immediately, and this blocks the background
  health-check thread for up to `retry_count * retry_interval_seconds` seconds for
  that one service — an intentional tradeoff (background thread, not a request
  handler), not a bug. `retry_count=0` (the default, and the value every
  pre-existing service gets via `_ensure_column`) preserves the exact original
  single-attempt behavior.
- **`_handle_incident_lifecycle()`'s open side must stay level-triggered, not
  edge-triggered — this already broke once (2026-07-23).** It used to require
  `previous_status != "down"` to open an incident. That's wrong: `services.status`
  is written to `"down"` every cycle regardless of the grace period (only the
  lifecycle *call* is suppressed during grace, not the status write), so a service
  whose downtime started during its own grace window would have `previous_status`
  already `"down"` the first time the call is ever actually made — no "fresh
  transition" would ever be detected again, and the service could stay down
  forever with zero incident. Fixed by opening whenever `new_status == "down"`,
  full stop, relying only on the existing `get_open_auto_incident_for_service()`
  idempotency guard (same fix applied to `_handle_integration_incident_lifecycle()`,
  same root cause via `_integration_status_cache`). **This was caught by live-testing
  against a real always-refusing HTTP server with real `time.monotonic()` timing —
  not by unit tests**, because every existing test called this function with a
  hand-picked `previous_status="operational"` and never exercised the
  grace-then-still-down sequence. If you touch either lifecycle function again, add
  a live/real-timing test alongside the mocked unit tests, not instead of them.
- **Timestamps are rendered server-side as UTC, converted to the visitor's local
  time client-side (added 2026-07-23).** Every public timestamp
  (`index.html`: announcements, incidents/updates, maintenance windows, and a
  service card's "upd. HH:MM" last-checked time) is wrapped as
  `<span class="local-time" data-utc="{iso}">{utc fallback text}</span>` (or
  `class="local-time-short"` for the compact service-card spot, which gets
  hour:minute only — no date/timezone-name clutter — instead of the full format);
  `static/js/local_time.js` finds every `.local-time[data-utc]`/
  `.local-time-short[data-utc]` element on load and overwrites its text with
  `Date.toLocaleString()` in the browser's own timezone. The UTC fallback text
  stays in the DOM for no-JS clients/JS failures — the server itself has no idea
  what timezone a visitor is in, so this has to happen client-side; don't try to
  guess/convert timezones server-side. **First pass (2026-07-23) missed the
  service-card timestamp** — caught by the user testing rc.2, not by review or
  tests — so when adding another timestamp to the public page, actually grep for
  `_at[` / `[:16]` / `[11:16]` across every template first rather than trusting
  that the obvious spots (incidents/announcements/maintenance) are the only ones;
  follow the same `data-utc` pattern for whatever turns up.
- **A many-to-many relationship (multi-service incidents/maintenance, added
  2026-08-01) gets a join table with its own per-row state, not a schema
  rewrite.** `maintenance_window_services`/`incident_services` are the source of
  truth for which service(s) a window/incident covers; the legacy single
  `service_id` column on `maintenance_windows`/`incidents` stays populated with
  the *first* selected service so every pre-existing single-service reader
  (auto-incident creation, RSS, the badge endpoints) keeps working with zero
  changes. A one-time backfill in `init_db()` seeds the join table for any row
  written before it existed (`WHERE id NOT IN (SELECT DISTINCT ... FROM
  <join_table>)` — idempotent, safe to re-run every startup). Each service in a
  multi-service maintenance window gets its *own* `pre_status`/
  `pre_manual_override` in the join table (a window covering 3 services needs 3
  independent restore points) — the legacy columns on `maintenance_windows`
  only mirror the primary service's snapshot, for cheap no-join reads. If you
  add another one-to-many-turned-many-to-many relationship later, follow this
  same shape rather than dropping the legacy column.
- **Every admin POST is CSRF-protected (added 2026-08-01) via a before_request
  hook in `app.py`, not per-route.** A per-session token
  (`app._get_csrf_token()`, registered as the `csrf_token()` Jinja global) is
  injected into every `<form method="POST">` by `static/js/csrf.js` reading a
  `<meta name="csrf-token">` tag `base.html` renders on every page — templates
  themselves never hand-embed the token, which is deliberate: hand-adding it to
  the ~16 templates with a POST form risked silently missing one. Any new
  admin route just needs to live under `/admin/` (the check is
  `request.path.startswith("/admin/")` + `method == "POST"`) — no per-route
  wiring required. The check is bypassed when `app.testing` is set, since the
  test client posts raw form dicts and never runs the injection JS; if you
  need to test the mechanism itself (not bypass it), see
  `test_csrf_protection_rejects_missing_or_wrong_token` in `tests/test_app.py`
  for the pattern (a client that does NOT set `TESTING`, extracting the real
  token from a GET response first).
- **Never interpolate a value from outside the portal's own admin into an
  inline JS event-handler attribute (e.g. `onsubmit="return confirm('...' +
  x + '...')"`).** This bit the project already: the Hyper-V VM-name
  confirmation dialog did exactly this, and Jinja's HTML-attribute escaping
  doesn't protect a value that the browser HTML-decodes and then hands to the
  JS engine a second time as code — a VM name containing a `'` (Hyper-V
  doesn't forbid it, and whoever can create/rename a VM on the host isn't
  necessarily the portal's own web-admin) could break out and inject script.
  Fixed by moving the value into a plain `data-*` attribute and reading it
  from a JS listener instead (`static/js/admin_vm_control.js`) — safe because
  it's then only ever used as a JS *string value*, never re-inserted into
  HTML or re-evaluated as code. The pre-existing `onsubmit="confirm('Delete
  {{ s.name }}?')"` pattern in `admin_services.html`/`admin_maintenance.html`
  is different/lower-risk (only the already-fully-privileged portal admin
  ever sets those names — self-XSS, no privilege gain) and was deliberately
  left alone, but don't copy that pattern for any value that can originate
  from outside the portal's own admin (an external API, another local
  account/service, etc.).
- **A background thread that can shell out to run a real OS command (host
  restart/shutdown, VM control, added 2026-08-01) must never be exercised for
  real in this sandbox, or against any environment you're not certain you're
  allowed to affect** — not even to see it "fail". `monitoring.control_host()`/
  `control_vm()` are unit-tested exclusively via a mocked `subprocess.run`;
  verify by reading the mocked call arguments, never by actually invoking the
  route live. `control_vm()` happens to safely no-op on non-Windows before
  reaching `subprocess` at all (its `os.name` guard is the very first line),
  so *that* one route is fine to hit live in this Linux sandbox — `control_host()`
  has no such guard (host restart/shutdown applies on both platforms on
  purpose) and must never be POSTed to outside of a mocked test.

## Monitoring architecture (`monitoring.py`) — background refresh added 2026-07-23

- The Windows-only, PowerShell/CIM-backed queries (Hyper-V VM list, CPU
  temperature, per-disk temperature + drive-letter-to-physical-disk mapping) are
  **polled by a background thread** (`monitoring.start_background_refresh()`,
  started from both `app.py` and `serve_waitress.py` at startup, same as
  `start_background_checker()`/`discord_bot.start()`) into a module-level cache
  (`_WINDOWS_CACHE`), not queried live inside a request handler — this is the same
  "never call slow I/O in a request handler" rule as the integration cache, just
  applied to local subprocess calls instead of outbound HTTP. `get_vm_snapshot()`
  and the new `_query_*()` functions stay live/directly-callable (that's what's
  unit-tested by mocking `subprocess.run`); `get_cached_vm_snapshot()` and
  `get_resource_snapshot()`'s `cpu_temp_c`/per-disk `temp_c`/`io` fields are the
  cache-reading wrappers request handlers should use instead.
- Per-disk temperature and I/O are **Windows-only** — correlating a mountpoint to a
  physical disk (needed for `psutil.disk_io_counters(perdisk=True)`'s
  `PhysicalDriveN` keys) uses `Get-Partition`'s drive-letter-to-disk-number mapping,
  which has no equivalent implemented here for Linux. The old aggregate
  (all-disks-combined) I/O reading was retired in favor of this — there's no
  system-wide I/O card anymore, only per-disk.
- **Unverified against real hardware** (this sandbox is Linux): CPU temp via the
  ACPI thermal zone WMI namespace and disk temp via
  `Get-StorageReliabilityCounter` are both well-known-unreliable on real Windows
  hardware — many systems return null/nothing through either, not a bug, a
  limitation of what Windows exposes without a third-party tool. Both degrade to
  `None` gracefully (same pattern as GPU detection with no NVIDIA card), but "shows
  nothing" on the user's actual box needs confirming there, not assumed to be a bug
  here.
- **Confirmed on real hardware (2026-07-23)**: CPU temp via the ACPI thermal zone
  returns nothing at all on the user's desktop (common — that WMI class is really
  meant for laptops/OEM systems with ACPI-exposed thermal zones, not enthusiast
  desktop boards reading sensors via the Super I/O/EC chip, which is what tools
  like HWiNFO do instead). `Get-StorageReliabilityCounter` also returned a literal
  `0` (not null) for one drive — `_query_windows_disk_details()` now treats `0` the
  same as "no reading" (a real drive is never 0°C) rather than displaying it as if
  it were a genuine reading. Neither of these swaps in a better data source (see
  `ROADMAP.md` → "More reliable CPU/disk temperature via HWiNFO" for the two
  options considered and why they weren't built yet) — don't assume this is fully
  fixed just because the obviously-wrong `0°C` display is gone.

## High-load indicator (`monitoring.evaluate_high_load()` / `integrations.evaluate_high_load()`)

- Split across two functions on purpose: `monitoring.evaluate_high_load(snapshot,
  thresholds)` is pure (no DB access — keeps `monitoring.py` DB-free, a constraint
  that predates this feature) and only compares system metrics (CPU/disk-I/O/network)
  against admin-configured thresholds. `integrations.evaluate_high_load(snapshot)`
  wraps it and layers in Jellyfin-derived signals (active transcode count via
  `/Sessions`, running scheduled tasks like trickplay generation via
  `/ScheduledTasks`, cached the same way `_integration_status_cache` works).
  `integrations.py` is the single place both `app.py` (public page) and
  `discord_bot.py` call this from — `discord_bot.py` can't import `app.py` (circular:
  `app.py` already imports `discord_bot.py`), so the shared logic had to live
  somewhere neither of them owns. If you add another cross-cutting signal both the
  web page and the bot need, this is probably where it belongs, not duplicated in
  both.
- **The public page can also show Jellyfin's running scheduled tasks directly**
  (`show_public_jellyfin_tasks` setting, added 2026-07-23) — separate from the
  high-load indicator above, this just renders
  `integrations.get_cached_jellyfin_activity()["running_tasks"]` (trickplay
  generation, library scans, etc.) as its own "Jellyfin activity" section whenever
  the list is non-empty, regardless of whether high-load's thresholds are also
  tripped. Reads the same background-refreshed cache — no extra polling added.

## Discord bot (`discord_bot.py`) — reworked 2026-07-22, read this before touching it again

- Uses a **slash command** (`discord.app_commands.CommandTree`), not a text/prefix
  command. An earlier version matched a literal `!status` in `on_message` — it
  worked, but needs Discord's privileged "Message Content" intent. Switched to slash
  commands on request + web research confirming this is Discord's own current
  guidance: prefix-command convenience isn't accepted as a reason to request that
  intent, and slash commands don't need any privileged intent at all. Setup no longer
  needs anything toggled in the Discord Developer Portal beyond inviting the bot.
- **`build_status_data(include)` must stay a pure function with zero `discord.py`
  import** — it returns a plain dict of sections. `build_embed(discord_module, data)`
  is the *only* function that touches `discord.Embed`/`discord.Color`, and it takes
  the already-imported module as a parameter rather than importing it itself. This is
  what keeps the module both unit-testable without the optional dependency installed
  and safely importable from `app.py` even when it isn't. Don't collapse these back
  into one discord.py-dependent function.
- Command registration happens in `StatusBot._register_command()` (called from
  `__init__`); syncing happens in `setup_hook()` — order matters, commands must exist
  in the local tree before `tree.sync()` runs. Guild-scoped sync (near-instant) if
  `PORTAL_DISCORD_BOT_GUILD_ID` is set; otherwise global sync (works everywhere, but
  can take up to ~1 hour to first appear).
- The tracked "live" status message (channel_id → message_id) lives in the
  `discord_status_messages` SQLite table, not memory, specifically so a restart keeps
  editing the same message instead of starting a new one. The refresh loop resolves
  the channel via `get_channel()` (cache) falling back to `fetch_channel()` (real API
  call) — the fallback matters because a cold gateway cache right after a restart
  would otherwise look identical to "channel deleted" and wrongly drop the tracked
  row.
- **Access control**: `discordbot_allowed_user_ids` (DB setting, comma/newline
  separated Discord user IDs) restricts who can invoke the slash command — checked
  inside the command callback itself (`allowed_user_ids()` / `normalize_user_ids()`),
  not via Discord's own per-guild command permissions UI, so it's portable across
  whatever server the bot is invited to without extra per-server setup. **Empty list
  = unrestricted (default)** — this was a deliberate choice so the command keeps
  working for anyone who hasn't configured it, at the cost of being open by default;
  the admin page calls this out explicitly rather than silently locking everyone out.
  An unauthorized attempt is logged to the console (with the offending user/id) and
  gets an ephemeral "not authorized" reply, without ever building the (heavier) status
  embed — the point is to stop both spam *and* the wasted work of building a response
  nobody authorized should see.
- **Server (guild) whitelist — added 2026-07-23**: `discordbot_guild_whitelist` (DB
  setting, same comma/newline-separated-IDs shape as the user allowlist —
  `allowed_guild_ids()`/`normalize_guild_ids()`, sharing `_parse_id_list()` with the
  user-allowlist functions) is a *different kind* of control than the user
  allowlist above: it doesn't just refuse a command, it makes the bot **leave** any
  server not on the list, via `StatusBot._enforce_guild_whitelist()` — called from
  both `on_guild_join` (the moment it's invited somewhere) and `on_ready` (every
  reconnect, looping `self.guilds`, so editing the whitelist to remove a server it's
  already in actually takes effect next reconnect, not just for future invites).
  Empty = unrestricted, same default-open convention as the user allowlist. This
  matters because presence/status updates are visible to a server just by the bot
  being in it, regardless of whether anyone there is authorized to run the slash
  command — refusing the command alone wouldn't stop that.
- **Verification status**: the old prefix-command version was confirmed working
  end-to-end by the user actually running it. The slash-command rewrite has *not*
  been re-confirmed against a real Discord server as of 2026-07-23 — message-building,
  the embed logic, and (unusually thoroughly for this module) the command callback's
  own authorization logic and the guild-whitelist leave behavior are all unit tested
  by actually constructing a `StatusBot` and invoking its real registered
  `command.callback(interaction)` / `on_ready()` / `_enforce_guild_whitelist()` with
  mocked Discord objects (see `_build_bot()`/`_make_interaction()`/`_make_guild()` in
  `tests/test_discord_bot.py`) — not just testing the helper functions around them. A
  real (fake-token) connection attempt was also smoke-tested against Discord's actual
  login endpoint to confirm the background thread behaves correctly and fails
  gracefully. What's still unverified: actual slash-command *registration/sync*
  against Discord's API, the restart-survives-editing behavior, and the guild
  whitelist's actual `guild.leave()` call against Discord's real API — all of which
  need a real bot/server to exercise. Don't assume "the bot" is confirmed working as a
  whole just because an earlier version of it was, or because one code path is now
  well-tested — ask what specifically was tested.
- `discord.py` is installed in this dev sandbox's Python environment for testing
  purposes even though it's not in `requirements.txt` (it's optional). If it's ever
  missing here and you need to verify code again: `pip install discord.py`.
- **Two slash commands now share one authorization gate (added 2026-08-01).**
  `/snapshot` (a short, one-shot plain-text reply — down services + whether
  any incident/maintenance is currently open, built by
  `build_snapshot_data()`/`build_snapshot_text()`, never tracked/edited like
  the main `/status` message) was added alongside the existing configurable
  main command. Both now call a shared `StatusBot._check_command_authorized()`
  (enabled-toggle → channel whitelist → user allow-list, in that order) instead
  of duplicating the checks — if you add a third command, call this helper
  too rather than re-inlining the checks. `_register_command()` guards against
  an admin naming their configurable command literally `snapshot` (falls back
  to `status`), since that would otherwise collide with the fixed command name
  at registration time and crash bot startup.
- **Channel whitelist (`discordbot_channel_whitelist`, added 2026-08-01)** is a
  *different kind* of control from the guild whitelist: it only refuses to
  reply in an unlisted channel (checked in `_check_command_authorized()`), it
  never makes the bot leave anything — a channel isn't something the bot can
  be a "member" of independently of its server. Same
  empty-means-unrestricted/comma-or-newline-separated-IDs convention as the
  user/guild allow-lists, sharing `_parse_id_list()`.
- **`discord_bot._state["guilds"]`** (added 2026-08-01) is a read-only snapshot
  of every server/channel the bot is currently in, populated straight from the
  gateway cache (`self.guilds`/`guild.text_channels` — no extra API calls) by
  `StatusBot._snapshot_guilds()`, called from `on_ready`, `on_guild_join`, the
  new `on_guild_remove` handler, and every `_refresh()` tick. Backs the new
  `/admin/discord-bot/guilds` admin page (server/channel list + the channel
  whitelist form) — same "reflects whatever the bot thread last reported"
  pattern `get_status()` already used for connection state, just extended to
  cover membership too. Like the rest of this module's Discord-API-dependent
  behavior, the actual gateway-cache correctness is unverified against a real
  server as of 2026-08-01 — only unit-tested via mocked `discord.Guild`/
  `discord.TextChannel` objects.

## Crash logging (`logging_setup.py`) — added 2026-08-01

- `logging_setup.init_logging()` configures Python's standard `logging` module
  (rotating file under `instance/logs/app.log`, same gitignored directory as
  the DB, plus the console) — called once from `app.py`/`serve_waitress.py`'s
  `__main__` blocks, **never at plain import time**, specifically so importing
  these modules under pytest doesn't create log files as a side effect. If you
  add a third entry point, call it there too, in the same spot (before
  `db.init_db()`).
- Every background-thread error path that used to `print(f"[tag] ...")`
  (health-check loop, `monitoring`'s Windows refresh, `discord_bot`) now goes
  through `logging.getLogger(__name__)` instead — `.exception(...)` inside an
  `except` block (auto-captures the traceback), `.info()`/`.warning()` for
  non-error diagnostics. Follow this pattern for any new print-style
  diagnostic rather than reintroducing bare `print()`.
- `threading.excepthook` is set to `logging_setup._log_thread_exception` —
  catches a background thread dying from something *outside* its own
  try/except (the health-check loop, monitoring refresh, and the Discord bot
  each already wrap their own loop bodies, but this is the safety net for
  anything that slips past that). Without this, a thread dying this way used
  to be completely silent — "services just stopped updating" with zero trace
  anywhere.
- **Tests that need to assert on logged output must use pytest's `caplog`
  fixture, not `capsys`** — `capsys` only captures direct `print()`/stdout
  writes, and with no logging configured during tests (see above), a bare
  `logging.Logger` call goes through Python's "handler of last resort" straight
  to *stderr* with no formatting, which `capsys.readouterr().out` won't see
  either. See `tests/test_monitoring.py`'s `test_vm_snapshot_logs_stderr_on_failure`
  or `tests/test_discord_bot.py`'s `test_start_logs_clearly_when_discord_py_missing`
  for the pattern (`with caplog.at_level("ERROR"): ...` then assert against
  `caplog.text`).

## Public page layout (`templates/sections/`) — added 2026-08-01

- Each of the 7 public-page content blocks (announcements, services,
  incidents & maintenance, practical info, resources, VMs, Jellyfin activity)
  is its own partial under `templates/sections/<key>.html`, each owning its
  own "is there anything to show" guard exactly as it did when inline in
  `index.html`. The topbar/status-hero/footer are page chrome, not content,
  and stay hardcoded at the top/bottom of `index.html` — they were
  deliberately never made reorderable.
- `app._public_section_order()` reads the `public_layout_order` setting
  (comma-separated section keys) and is the *only* place that decides
  render order — `index.html` just does
  `{% include 'sections/' ~ key ~ '.html' %}` in a loop over whatever list it's
  handed. **If you add an 8th section**, add its key to the `PUBLIC_SECTIONS`
  list in `app.py` (which doubles as the label lookup for the admin reorder
  UI) — `_public_section_order()` already appends any valid key missing from a
  stale stored value at the end, so an admin who saved a custom order before
  your new section existed still sees it (just at the bottom, not
  disappeared).
- The reorder UI on `/admin/settings` (`admin_layout_order.js`) is a plain
  up/down-button list, not drag-and-drop — deliberate, to stay dependency-free
  like every other admin-side JS file in this app (`admin_service_links.js`,
  `admin_maintenance_form.js`, etc.). Don't introduce a drag-and-drop library
  for this without discussing it first.

## Two-factor authentication (`twofactor.py`) — added 2026-08-01

- TOTP-based (Google Authenticator/Authy/1Password, etc.), off by default,
  never required — a single admin can opt in from `/admin/2fa`, and the page
  strongly recommends it (especially given the host power controls exist
  now) without forcing it, since some people explicitly don't want it. `pyotp`
  and `qrcode` are **required** dependencies (`requirements.txt`), not the
  lazy-imported-optional pattern used for `nvidia-ml-py`/`discord.py` — both
  are small pure-Python packages with no compiled extensions, and 2FA is a
  core always-available feature, not a rare/heavy integration.
- **The QR code is generated fully server-side as inline SVG** via `qrcode`'s
  `SvgPathImage` factory — deliberately avoids a Pillow/image-library
  dependency and avoids any third-party JS QR-rendering library (which would
  need loosening the CSP's `script-src` or vendoring a file — this app has no
  external JS dependencies anywhere else and that convention held here too).
- **Enrollment secret lives in the session, not the DB, until confirmed.**
  `GET /admin/2fa/enable` puts a freshly generated secret in
  `session["pending_totp_secret"]` and shows its QR/manual key; only a
  `POST` with a *verified* code from that same pending secret writes it to
  `db` (`admin_totp_secret`/`admin_totp_enabled`) — this stops an admin from
  ending up locked out by a secret they never actually got safely into their
  authenticator app. **Caught by live testing, not the unit tests**: the
  route used to crash with a bare `KeyError` if a `POST` ever arrived with no
  pending secret in the session (e.g. it expired between GET and POST, or a
  direct POST) — fixed by unconditionally ensuring a pending secret exists
  before rendering, instead of only doing so in the GET branch. If you touch
  this route again, keep a regression test for that specific "POST with
  nothing pending" case, not just the happy path.
- **Login becomes two steps when 2FA is enabled, still one step when it
  isn't.** `session["awaiting_totp"]` is set (server-side only, can't be
  forged by a client without the signed session's `SECRET_KEY`) once the
  password step succeeds; the *next* `POST` to `/admin/login` is read as a
  code instead of a password, without re-checking the password. The global
  login-lockout counter (`_login_state`) applies to wrong codes exactly like
  wrong passwords — deliberately **not** reset just for getting the password
  right, only once the whole two-step login actually succeeds, so a correct
  password doesn't buy an attacker unlimited unthrottled code guesses.
- **Step-up re-authentication for host restart/shutdown, added alongside
  this.** Even with an already-logged-in session, `/admin/resources/host-control`
  demands a *fresh* code when 2FA is enabled (checked in the route itself,
  same shared `twofactor.verify_code()`) — the reasoning: a stolen/replayed
  session cookie alone must not be enough to trigger the single most
  destructive action this app can take. Scoped deliberately narrow (host
  restart/shutdown only, not VM control, not other admin actions) — that's
  what was actually asked for; don't creep this onto other routes without
  discussing it first.
- **Resetting 2FA is a host-level action, not a web one, on purpose.**
  `twofactor.check_and_process_reset_flag()` looks for an empty file at
  `instance/RESET_2FA` on every hit of `/admin/login` (cheap
  `os.path.exists()`, no restart needed — takes effect on the very next page
  load) and, if found, wipes `admin_totp_secret`/sets `admin_totp_enabled=0`,
  then deletes the file itself (self-cleaning, one-shot — not a standing
  toggle someone could forget to unset). This is deliberately **not** a web
  UI button or an emergency backup code — either would just be another secret
  reachable purely over the web to protect. Creating a file requires actual
  filesystem access to the host, a meaningfully different trust boundary from
  knowing the admin password or holding a stolen session cookie. Routine
  self-service disabling (the admin still has their device, just wants it
  off) stays in the web UI at `/admin/2fa`, itself gated behind entering a
  current code — a hijacked session alone can't turn 2FA off either.

## Testing/verification habits (established over many sessions — keep following them)

- Run the full `pytest` suite *and* a live `python app.py` + `curl` smoke test of
  whatever routes actually changed before calling something done. Several real bugs
  (the integration-blocking page load, the maintenance-window timing bug, the 2FA
  enrollment `KeyError`) were only ever caught by actually running the server,
  never by unit tests alone.
- **When curl-smoke-testing a multi-request flow that depends on session state
  (login steps, flash messages, anything the server writes back via
  `Set-Cookie`), every request that should see or contribute to that state
  needs *both* `-b cookiejar` (send) *and* `-c cookiejar` (save the response's
  possibly-updated cookie) — `-b` alone silently reads a stale cookie and looks
  exactly like a real bug (a missing flash message, a "lost" session value)
  when it's actually just the session update from the previous request never
  having been saved.** Bit this exact session while testing 2FA enrollment
  2026-08-01 — the first symptom was indistinguishable from an actual
  server-side bug until re-checked with `-c` added.
- This sandbox is Linux. Hyper-V VM detection, Windows volume labels, CPU/disk
  temperature and per-disk I/O (all Windows-only, PowerShell/CIM-backed), real
  Jellyfin/*Arr/Jellyseerr instances (including the newer `/Sessions` and
  `/ScheduledTasks` fetchers behind the high-load indicator), and a real Discord
  gateway connection can't be fully exercised here. Say so explicitly rather than
  implying full verification — and if the user reports a bug in one of these areas,
  ask for the actual error text first (most of these paths now log real errors
  instead of swallowing them) rather than guessing blind again.
- Clean up after smoke testing: remove any `instance/portal.db` created during a
  test run, and any cookie jars, before finishing — don't leave a test admin password
  or fake data sitting in what could become the user's real database.
- **Never live-invoke anything that shells out to actually restart/shut down a
  machine (`monitoring.control_host()`), even in this sandbox, even just to
  "see what happens."** Verify exclusively via a mocked `subprocess.run` in
  pytest — see the bullet under "Conventions that matter" above. A "failed"
  real attempt is still an inappropriate action to take against an
  environment you don't own outright.
- **A `/code-review`-style security pass over a session's accumulated diff is
  worth running before a release that adds any new admin-facing control
  surface** (added a genuinely new class of risk to this codebase 2026-08-01:
  host/VM power controls) — it caught a real XSS bug
  (inline-onsubmit-with-untrusted-VM-name, see "Conventions that matter") that
  manual review and the existing test suite both missed. Doesn't replace the
  live `curl`/browser smoke testing above, but catches a different class of
  issue than either does alone.

## Release process

**Trigger**: the user says the session/work is done *and* that things are working —
e.g. "this session ends here", "that's it for today, everything works", "we're done,
thanks". Not every "looks good" or "great, that works" mid-session — those are just
confirmation of one change, not a signal to release. When genuinely unsure whether a
message means "wrap up the whole session" vs. "this one thing is fine, keep going",
ask rather than guessing — a release is a public, visible action.

Once the trigger fires: this is standing, pre-authorized (per the same blanket
authorization covering commit/push/shell commands for this project, granted
2026-07-22) — don't ask for confirmation again each time, just run the steps below.

**Mid-session checkpoint, not just end-of-session (added 2026-08-01)**: don't
wait only for the full "session's done, everything works" trigger above.
Whenever a *complete chunk of requested work* finishes — a whole batch of
asks handed over together, even if more requests come later in the same
session — proactively tell the user specifically what to test and how, and
cut a **pre-release** (`-rc.N`, marked prerelease on GitHub) covering that
chunk, rather than staying silent until the literal end of the session. This
is deliberately coarser than "after every individual step": a batch of many
related asks worked in one pass is one chunk, not one release per item —
confirmed with the user 2026-08-01 on a 16-item batch, where the agreed
cadence was one pre-release at the end of the whole batch, not per-item. The
stable-release trigger phrase above still governs promoting a pre-release (or
cutting a fresh full release) once the user actually confirms it works.

1. **Versioning**: `vMAJOR.MINOR.PATCH`, with an optional `-rc.N` suffix for anything
   not yet user-verified end-to-end (mark the GitHub release as a pre-release too).
   First tagged release is `v1.0.0` (2026-07-22). Bump MINOR for new features, PATCH
   for bug-fix-only changes, MAJOR only for an actual breaking change (should be
   rare — the whole `_ensure_column` schema policy exists specifically to avoid
   needing these). `v1.1.0` (2026-07-23) is the second, shipped as a **full
   release, not `-rc`**, by the user's explicit call — despite the Windows-only
   monitoring pieces (CPU/disk temp, per-disk I/O, VM detection), Jellyfin
   `/Sessions`/`/ScheduledTasks` parsing, and the Discord guild whitelist's
   `guild.leave()` call all still being unverified against the real thing (only
   unit-tested/mocked) at release time. That was a deliberate, informed tradeoff by
   the user, not an oversight — don't read "it's a full release" as "everything in
   it was end-to-end confirmed"; check the per-feature verification notes above for
   what actually was.
2. **Changelog**: build from `git log <previous-tag>..HEAD --oneline`, grouped
   informally into Added / Fixed / Changed, written as the release body. Keep it
   readable — a person, not a machine, reads this.
3. **Asset**: `git archive --format=zip -o status-portal-vX.Y.Z.zip HEAD` (or the new
   tag once created). `git archive` only includes tracked files, so it's already
   clean — no `.git`, no `instance/portal.db`, no `.env`, no `__pycache__` — without
   any manual exclusion list to maintain.
4. **Publish**:
   ```
   git tag vX.Y.Z
   git push origin vX.Y.Z
   gh release create vX.Y.Z status-portal-vX.Y.Z.zip --title "vX.Y.Z" --notes "<changelog>" [--prerelease]
   ```
