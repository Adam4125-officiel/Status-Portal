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
  completely healthy. **502 is a carve-out within that 5xx bucket (added
  2026-08-10):** `_check_status_for_response()` classifies a 502 as `down`, not
  `degraded` — unlike a 500/503/504 (the service itself is up but erroring/
  overloaded), a 502 means whatever's in front of it (reverse proxy, gateway)
  couldn't even reach it, functionally equivalent to a connection failure from a
  visitor's perspective. This composes for free with `retry_count`/
  `_handle_incident_lifecycle` since both are already keyed off `status == "down"`
  regardless of *why* — no separate code path was needed.
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
- **`.incident-bubble` must fully match `.service-card`'s treatment, not just
  look similar (tightened 2026-08-01).** The first version had the right
  background/border/radius/padding but was missing `.service-card`'s
  `display: flex; flex-direction: column; gap` and hover border-transition —
  close enough in a diff review to seem fine, but the user reported it back
  as "not reading as a card." If you touch either class, keep them in sync on
  purpose (or add a shared class) rather than letting them drift apart again
  from two independent-looking rulesets. The nested `.incident-updates`
  status-update list also got a left border + per-row dot for the same
  reason: a flat list under a card didn't read as "these updates belong to
  this incident" without an explicit hierarchy cue. When a visual bug report
  like this comes in again, render a quick Artifact preview using the app's
  actual CSS tokens before shipping the fix — confirmed the fix looked right
  before pushing it, rather than reasoning about CSS in the abstract.

- **A native `<select multiple>` service picker is a latent bug generator — added
  2026-08-10, replace it with a checkbox list instead of patching around it.**
  `admin_maintenance_form.html`'s multi-service `<select>` had
  `'selected' if s.id in selected_ids or not window` — normal operator precedence
  makes that `(s.id in selected_ids) or (not window)`, so on the "new" form
  (`window` is `None`) *every* option rendered pre-selected regardless of
  `selected_ids`. Combined with the field-hint's "ctrl/cmd-click to select more
  than one," an admin who ctrl-clicked the one service they actually wanted
  *deselected* it while leaving every other service selected — submitting
  "everything except" the intended service, exactly the bug report that led here.
  Fixed by replacing the `<select multiple>` in both the maintenance and incident
  forms with a checkbox list (`.checkbox-list`/`.field-check` in
  `static/css/style.css`) — `request.form.getlist("service_id")` reads identically
  from repeated checkboxes as from a multi-select, so this needed zero `app.py`
  changes, and a checkbox's `checked` state can't have this class of bug at all
  (no `or not window`-style fallback expression is needed). If another multi-select
  service/entity picker gets added later, use the checkbox-list pattern from the
  start rather than a native multi-select.
- **A "merge two status sources into one" feature (per-service `api_health_mode`,
  added 2026-08-10) must still produce a single final status that feeds both the
  public display and `_handle_incident_lifecycle`, never two independent
  decisions.** `app._merge_api_health(status, api_health_mode, integration_reachable)`
  folds a linked integration's cached reachability into the web-check's own
  `status` *before* `db.update_service_status_from_check`/
  `_handle_incident_lifecycle` are called in `run_health_checks()` — this
  preserves the level-triggered invariant documented below (the
  `_handle_incident_lifecycle` bullet), which already broke once from letting a
  status write and an incident-lifecycle decision see different values. Don't add
  a second "does the API health affect this?" branch anywhere else; extend the
  merge function instead.
- **An internal "load more" / pagination endpoint returns a server-rendered HTML
  fragment, not JSON (added 2026-08-10, `/api/incidents/more`,
  `/api/maintenance/history`).** This app's only actual JSON API is `/api/status`,
  meant for external consumption; there's no client-side templating anywhere else
  in the app, so an HTML-fragment endpoint (`render_template` on a partial like
  `sections/_incidents_fragment.html`, inserted via
  `insertAdjacentHTML('beforebegin', ...)` in `static/js/public_history.js`)
  matches the existing "server renders everything, small vanilla JS wires it up"
  convention instead of introducing a second, JSON-based rendering path just for
  this. Newly-inserted timestamps need `window.applyLocalTimes(document)`
  re-run (see `static/js/local_time.js`, which now exposes this instead of only
  running once as an unnamed IIFE) since they arrive after the page's own load
  event already fired.
- **`/api/incidents/more` paginates by the ids the client already has
  (`?seen=`), never by an offset or an id cursor, and never re-applies the
  initial view's `max_age_days` filter. All three of these were shipped broken
  in sequence on 2026-08-10, each caught live by the user rather than by the
  test suite — read this before "simplifying" it back.** A fourth, related bug
  in the same area was fixed 2026-08-11 — see item 4 below and the "fails
  closed" paragraph at the end of this bullet, both updated for it.
  1. **Re-applying `max_age_days` on "load more"** makes anything past the
     cutoff permanently unreachable: the initial page hides it, and the button
     hides it again. The whole point of the button is to reveal what the
     initial view hid. `api_maintenance_history()` drops the filter for the
     same reason.
  2. **A positional `OFFSET`** counted against the *age-filtered* initial query
     doesn't line up with an *unfiltered* continuation of it, so it skips past
     exactly the items being revealed.
  3. **An `id < cursor` cursor** cannot express "everything I'm not already
     showing" against a filtered view. Seeding from the oldest shown id skips
     anything hidden in an id-space *gap* (a still-open old incident can sit at
     a lower id than a newer resolved-and-hidden one); seeding from the newest
     shown id instead re-returned every already-visible item below it — which
     made the button re-append the entire visible list on every click, the
     symptom the user reported as "completely broken, it loads the same
     indefinitely."
  4. **The initial view's empty state didn't distinguish "no incidents exist"
     from "every incident is hidden by `max_age_days`"** (fixed 2026-08-11).
     With a filtered list of zero incidents, `sections/incidents.html`'s
     load-more button — the only way to reach items 1-3 above — lived entirely
     inside `{% if incidents %}`, so it silently disappeared right when it was
     most needed, and the page claimed "No incidents recorded. All clear."
     even though incidents existed. Fixed by having `index()` compute a
     separate `incidents_hidden` flag (`not incidents and bool(db.list_incidents(limit=1))`
     — an unfiltered existence check that only runs once the filtered list is
     already empty, so it adds no query in the common case) and giving the
     template a third branch: still no items to show, but a distinct message
     plus the same load-more button, reachable via `?seen=` with nothing in it
     yet (see the fails-closed paragraph below, which had to be narrowed for
     this to work).
  Excluding the shown ids is the only formulation that is simultaneously
  gap-free and duplicate-free, because it states the intent directly instead of
  approximating it with a position. The endpoint **fails closed** (empty
  response) only when `seen` is missing from the query string *entirely*, or
  oversized — a missing key is the real stale-client signal (see the
  `asset_url()` bullet below: an old cached `public_history.js` sending
  `?offset=` instead), and answering it with "page 1" is what turned that
  stale script into an infinite duplicator. A `seen` key that's *present but
  empty* (`?seen=`) is different and, since the 2026-08-11 fix above, legitimate
  — it's exactly what the real button sends when item 4's "all hidden" empty
  state is showing and nothing is on the page yet to list. The original version
  of this fix treated both cases as "fails closed" identically (there's a
  regression test from 2026-08-10 asserting exactly that), which was correct
  until item 4 introduced a real reason to send an empty-but-present `seen` —
  don't re-merge the two checks without re-breaking that case.
- **Every CSS/JS reference in a template must go through `asset_url()`
  (`app.py`), never a bare `url_for('static', ...)`** — it appends a
  `?v=<mtime>` cache-buster. Added 2026-08-10 after a real user-hit bug: this
  app's documented update process is "extract the release zip over your existing
  folder", which changes a JS file's *contents* but never its *URL*, so the
  browser kept serving the previous release's cached copy. A shipped
  `public_history.js` change (pagination switching parameters) was silently
  shadowed that way, leaving an old script talking to a new endpoint — the
  server ignored the obsolete parameter and re-returned the same page forever.
  Any future JS/CSS change has exactly the same exposure, so this is a
  project-wide rule, not a one-off patch. (`static/uploads/` logo URLs already
  carried their own `?v=`, which is where the pattern came from.)

- **New integration kinds are just a new entry in `integrations.py`'s
  `fetch_integration_status()` dispatch dict plus a matching fetcher function —
  no architectural change needed.** Bazarr/Tdarr/Byparr (added 2026-08-10)
  followed this exactly: `fetch_bazarr_status()`/`fetch_tdarr_status()`/
  `fetch_byparr_status()` each return the same `{"reachable", "version",
  "issues", "error"}` shape every other fetcher does. Bazarr differs from the
  Servarr-family pattern in one notable way — it expects its API key as a
  `?apikey=` query param, not an `X-Api-Key` header — confirmed against
  Bazarr's own source, not a real instance (see ROADMAP.md). Tdarr and Byparr
  have no API key concept of their own at all, so the shared `api_key` form
  field is simply unused by those two fetchers (left present on the form only
  for a consistent UI across all kinds).
- **Byparr's `/health` is genuinely slow, not flaky — it gets its own, longer,
  configurable timeout (`config.BYPARR_TIMEOUT_SECONDS`, env
  `PORTAL_BYPARR_TIMEOUT_SECONDS`, default 30s), added 2026-08-11 after a real
  user hit `Read timed out (read timeout=5)` against a reachable instance.**
  Checked against Byparr's own source (`src/endpoints.py`): `/health` doesn't
  just ping the process, it makes Byparr actually navigate to google.com and
  solve a real Cloudflare challenge before responding — there's no lighter
  endpoint documented anywhere to switch to instead, so `/health` was already
  the correct/only endpoint. The bug was purely the shared `TIMEOUT = 5`
  constant every other fetcher also uses for a plain fast REST call being far
  too short for this one specifically slow endpoint. If another integration
  kind turns out to have a similarly slow health check, give it the same
  treatment (its own `config.py` env var) rather than raising the shared
  `TIMEOUT` for everyone.
- **`service_default_*` settings (Settings → "Service defaults", added
  2026-08-10) are pre-fill-only, never live-cascading** — `app._service_defaults()`
  reads them and `admin_service_new`'s `GET` handler passes the result as an
  optional `defaults` context var to `admin_service_form.html` (already
  rendering every one of these fields for editing; `service is None` on the
  "new" form is what triggers using `defaults` instead of hardcoded
  fallbacks). Changing a default later never retroactively touches a service
  that already exists — `run_health_checks()` and the rest of the schema are
  completely unaffected by this setting, it only ever influences what a brand
  new "New service" form starts pre-filled with.
- **The combined "New service + status check" wizard (`/admin/new/combined`)
  used to be a completely separate, much smaller form from
  `admin_service_form.html` — that gap was a real bug, fixed 2026-08-11, and
  is worth understanding so it doesn't quietly reopen.** The wizard only ever
  rendered/submitted `name`/`icon`/`description`/`url`/`group_name` plus the
  integration fields — `service_default_*` settings were never reachable from
  it at all, so `db.create_service()` silently fell back to its own hardcoded
  literals (0, 5, `"off"`...) instead of what the admin had actually
  configured, and anyone who wanted retry/threshold/grace/API-health-mode set
  had to create the service via the wizard and then immediately go edit it.
  Fixed by giving `admin_new_combined.html` every field
  `admin_service_form.html` has (minus the extra-links repeater, which isn't
  on the plain "New service" form either — not a wizard-specific gap), and
  updating `admin_new_combined()`'s `POST` handler to build the same kind of
  `data` dict `admin_service_new()` does before calling `db.create_service()`.
  The new fields live inside a collapsed-by-default `<details>` "Advanced
  settings" block (`.form-panel details` in `style.css`) so the common case
  (name/URL/kind/API key, hit Create) doesn't get longer — **collapsed is a
  CSS/visual state only, the inputs are still part of the DOM and still
  submit normally**, which is exactly why pre-filling them server-side from
  `_service_defaults()` is enough on its own; nothing needs an "open the
  advanced section" JS handler for the defaults to actually reach
  `create_service()`. One naming gotcha if you touch this again: the service
  has its own `auto_incident` checkbox (open an incident when the *service*
  goes down) and the integration being created alongside it has a *different*
  `auto_incident` concept (open an incident when the *status check* fails) —
  both can't share the HTML `name="auto_incident"` on one `<form>` without
  colliding, so the integration's checkbox is deliberately named
  `check_auto_incident` in the template and mapped explicitly in the route.
- **An admin page's on-page `<h1>` and its `{% block title %}` can drift apart
  independently — check both, not just one (caught 2026-08-11).**
  `admin_incidents.html`'s `<title>` already correctly said "Incidents —
  Admin", but its `<h1>` still read "Incidents & maintenance" (stale from
  before Maintenance got split out into its own nav item/page), which is what
  the user actually saw and found confusing — the `<title>` tag is invisible
  unless you're looking at the browser tab. Fixed by changing the `<h1>` to
  just "Incidents", matching the nav label and the `<title>`. Every other
  `admin_*.html` page's `<h1>`/`block title`/nav label already matched
  3-for-3 when surveyed — this was the only mismatch found, not a systemic
  problem, but if a nav label or a page's scope ever changes again, check the
  on-page heading too, not just the `<title>` block.

## Self-update (`updater.py`, `update.py`, `/admin/about`) — added 2026-08-10

- **`VERSION` (a tracked file at the repo root) is the single source of truth, and
  bumping it is a required step of cutting a release** (see Release process below).
  It has to be a file rather than a literal in `config.py` because `updater.py`
  reads the *incoming* release's version straight out of an extracted zip without
  importing the new, not-yet-installed code. It has to be a tracked file rather
  than anything git-derived because `git archive` strips `.git` — a shipped zip has
  no git metadata at all, which is exactly why nothing in this project carried a
  version before now. `config.IS_GIT_CHECKOUT` (one `os.path.isdir(".git")` at
  import, no git subprocess) distinguishes a working tree from an extracted
  release; `config.VERSION_DISPLAY` appends `+dev` for the former. **`+dev` is a
  label only — never compare against it**, every version comparison uses
  `config.VERSION`.
- **`updater.py` is the one implementation; `update.py` and the admin route are
  both thin wrappers.** This was an explicit requirement, and it's what stops the
  CLI and the button drifting apart. `updater.py` imports only `config` and `db` —
  never `app.py` (which imports it) and never Flask, so the CLI works on a portal
  that won't start.
- **The repo URL is a module constant and must never become configurable** — not a
  DB setting, not an env var, not a CLI flag. A configurable update source is a
  "point this server at my code" primitive for anyone who can write a setting.
  There's a test asserting the constants and that certificate verification is never
  disabled; keep it.
- **`browser_download_url` is untrusted input, not a constant** — it arrives over
  the network from the API response, so `_validate_download_url()` checks scheme
  and host against `ALLOWED_DOWNLOAD_HOSTS` both before the request *and* on
  `response.url` after redirects have been followed.
- **Integrity verification protects the transfer, not the publisher.** The size and
  SHA-256 are checked against what the releases API declares, which comes from the
  same origin as the bytes — so this catches truncation/corruption/a mangling proxy
  and (via TLS) an in-transit substitution, but not a malicious release. Real
  publisher authenticity would need a detached signature against a key shipped with
  the app. Say this plainly whenever documenting the feature rather than letting
  "checksum verified" imply more. (Confirmed live 2026-08-10: GitHub *does* publish
  a `digest` field on release assets, so the SHA-256 path is the one that actually
  runs, not the size-only fallback.)
- **Which files get replaced comes from the release archive's own member list** —
  a whitelist by construction, and structurally incapable of containing
  `instance/`, `.env` or `static/uploads/` since all three are gitignored and
  releases are built with `git archive`. `PROTECTED_PREFIXES`/`PROTECTED_FILES`
  are a second, redundant check that **aborts the entire update** if one ever shows
  up, rather than skipping that entry — an archive containing one means the release
  was built wrong, which is not a condition to proceed through quietly.
- **Channel = GitHub release channel (stable/unstable), deliberately not a git
  branch.** A branch head has no version identity, so "are you behind", "what am I
  installing" and "roll back to what" all become unanswerable; a branch is also
  arbitrary mid-work code rather than something that was cut and tested. Stable =
  non-prerelease releases only; unstable = prereleases (`-rc.N`) too. The latest is
  picked by **parsed version, not publish date**, so republishing an old release
  can't look like an update. Channel is a DB setting (routine admin toggle); the
  check *interval* is an env var (`PORTAL_UPDATE_CHECK_INTERVAL_SECONDS`) — the
  standard config split, applied.
- **The About page reads a cache and never checks GitHub inline.** Same rule and
  same shape as `_integration_status_cache`. `refresh_update_cache_if_stale()` is
  called from the existing health-check loop rather than starting a second thread,
  and no-ops until its own (6h) TTL elapses so a 120s health-check interval doesn't
  become a GitHub call every 120s. "Check now" (`/admin/about/check`) is the
  sanctioned explicit-slow-action exception, like the integrations Check-now button.
  A failed check renders as "couldn't check" and affects nothing else on the page.
- **`perform_update()` runs synchronously inside the admin route.** That is the
  same sanctioned exception — an explicit one-shot action the admin knowingly
  triggered — not a violation of the no-slow-I/O rule. Don't "fix" it by moving it
  to a background thread; the admin needs the success/failure in the response.
- **What rollback can and cannot do — don't overstate this.** Every failure
  *before* the restart (download, verification, a file that won't replace part way
  through, a failed `pip install`) is rolled back automatically and in-process, and
  the portal keeps running what it already had. The failure *after* the restart
  cannot be: once `os.execv` replaces the process image nothing from the old
  version exists to detect a bad start. `write_pending_marker()` therefore only
  buys two things — the next *successful* start confirms and clears it, and a
  failed one leaves a record naming the exact backup to restore. Genuine automatic
  post-restart rollback needs a supervisor outside the process (systemd + a health
  check, or a wrapper), which this project deliberately doesn't ship because it
  would change how everyone launches the portal.
- **`update.py rollback --emergency` exists because of a bug found by live
  testing, not by review.** The first version imported `updater` at module level;
  a real end-to-end test (updating a throwaway install to an actual release whose
  `config.py` predated `APP_ROOT`) left a tree where `updater.py` no longer
  imported — so the designated recovery tool was itself broken by the update it
  was meant to undo. Fixed by making every `update.py` import lazy and adding a
  self-contained emergency path that reads only the `manifest.json` updater.py
  already wrote. That is **not** a second update implementation and must not grow
  into one — it only restores an existing backup.
- **`pip install` runs only when `requirements.txt` actually changed, and a failure
  rolls the update back** — restarting into code whose dependencies aren't
  installed just fails to start, and this is the last moment where something is
  still running that can undo it.
- **Windows file locking**: every file is written to a sibling temp file then
  `os.replace()`d (atomic; a crash mid-write leaves the old file intact). The
  rename can still fail on Windows while another process holds the destination
  open, hence `REPLACE_RETRY_ATTEMPTS` with backoff, and a whole-update rollback if
  it still fails. If even the rollback can't write, the error names the backup
  folder to restore by hand rather than claiming it was handled — there's a test
  for that specific case.
- **An update never deletes a file that a later release removed.** Only files
  present in the incoming archive are written; anything the old version shipped
  that the new one dropped just stays on disk. This is identical to the
  extract-the-zip-over-the-folder process that predates the updater, and it's
  harmless (Python only imports what's referenced, Flask only serves what's
  routed), but don't assume a post-update tree is byte-identical to a fresh
  extraction — it's a superset. Deleting the difference would mean trusting a
  computed file list to remove things, which is a much worse failure mode than
  leaving a stale file behind.
- **Nothing in `updater.py` may read a DB setting without going through
  `_read_setting()`.** `sqlite3.connect()` *creates* an empty file for a path
  that doesn't exist, so a bare `db.get_setting()` from the CLI would leave a
  zero-table `instance/portal.db` behind on a fresh install — which `init_db()`
  then has to cope with, and which looks exactly like a corrupted database.
  `_read_setting()` checks `os.path.isfile(db.DB_PATH)` first and falls back to
  the default. It also swallows read errors, because the CLI is the tool you
  reach for when things are broken, up to and including the database.
- **`update.py`'s output must stay pure ASCII.** An em dash in the header line
  raised `UnicodeEncodeError` on a Windows console using codepage 437 (found
  while writing the user's test instructions, before it ever shipped). This is
  the recovery tool — it must not be able to fail on a decorative character.
  `updater.py`'s `progress()` messages are subject to the same rule since the
  CLI prints them. There's a check for this in `tests/test_updater.py`.
- **Backups are pruned to `KEEP_BACKUPS` (5) after each successful update.**
  `_prune_backups()` reads the module constant *inside* the function rather than
  as a default argument value — a default arg binds at def time, which silently
  ignores a monkeypatched constant and made the pruning test pass for the wrong
  reason until it was fixed.
- **The About page's `list_backups()` is a local `os.listdir` + small JSON
  reads** — the same class of call as `asset_url()`'s `getmtime`, not the kind of
  slow outbound I/O the no-blocking-in-a-request-handler rule is about. Don't
  "fix" it into another cache.
- **Changing the channel must clear the update cache** (`admin_about_settings`
  does). Otherwise the page renders a "latest available" that was fetched for the
  *previous* channel right next to the newly-selected one — e.g. still showing
  the newest stable release seconds after switching to unstable.
- **`_inject_admin_badges()` also exposes `update_available`** for the nav's
  About badge. It reads the cache only (never triggers a check), so a miss or a
  failed check simply means no badge — exactly like having no unread reports.
- **Test-suite gotcha: `config.IS_GIT_CHECKOUT` is genuinely `True` when pytest
  runs from this repo**, so every update route and the About page's button
  correctly refuse. `tests/test_app.py` has an autouse fixture
  (`_update_test_environment`) that patches it to `False` and clears the update
  cache, so those tests stand in for a normal install. Without it a test can
  "pass" by hitting the git-checkout refusal rather than the behavior it meant to
  assert — if you add an update-route test, make sure it's actually reaching the
  code you think it is.
- **`config.ENABLE_INAPP_UPDATE` is an env var on purpose.** The risk it addresses
  is "someone compromised the admin panel"; a DB toggle that same attacker could
  flip from that same panel would address nothing. Same reasoning as
  `twofactor.RESET_2FA` being a host-level file. It defaults to **enabled** (the
  button was explicitly asked for), and the route checks it, not just the template
  — verified live that a valid 2FA code still gets refused when it's off.
- **Verification status (2026-08-10)** — in the Linux sandbox: real end-to-end
  updates against the actual GitHub API and real release zips, in throwaway
  installs (real SHA-256 verification, 83/90 files replaced,
  `instance/portal.db`/`.env`/`static/uploads/` confirmed byte-identical
  afterwards, a real rollback, a real `--emergency` rollback, and a re-run
  correctly no-opping as "up to date"). The About page, the channel form, "Check
  now", the step-up-2FA refusal and the kill-switch refusal were all exercised
  live against a running server.
- **Confirmed on the user's real Windows machine (2026-08-10), which is what
  closed out most of the list above.** They updated an installed `v1.5.0-rc.2`
  to `v1.5.0-rc.3` **entirely through the admin panel's button**, with waitress
  serving and the Discord bot connected at the time, then rolled back with the
  CLI. What that confirms, specifically:
  - **`os.execv` in-place restart works on Windows.** The process came back ~2s
    later, re-bound port 5000, and the Discord bot reconnected on its own. This
    was the single biggest unknown in the whole feature.
  - **`write_pending_marker()`/`check_pending_marker()` work end to end**: the
    restarted process logged "Update to 1.5.0-rc.3 completed - the app restarted
    successfully on the new version" and cleared the marker, which is the entire
    (limited) thing that mechanism was built to do.
  - **No Windows file-locking failure occurred** replacing 90 files while the
    server was live. Note the corollary: `REPLACE_RETRY_ATTEMPTS`/the
    retry-then-roll-back path therefore remains **unexercised in the wild** — it
    didn't need to fire, which is not the same as it having been proven to work.
  - **The browser-side typed-confirmation JS works** — reaching the update at all
    required typing `UPDATE` to enable the submit button.
  - `python update.py rollback` restored all 90 files on Windows.
- **Still not verified**: relaunch under Task Scheduler after `os.execv` (they ran
  `python serve_waitress.py` from PowerShell directly, so a supervisor's reaction
  to the in-place restart is still unknown); the Windows file-lock retry/rollback
  path (see above); and `pip install` actually running during an update, since no
  release so far has changed `requirements.txt`.
- **Cosmetic wart, deliberately left**: backup folder names are UTC
  (`20260810-171829-...`) while the console/app log shows local time, so on a
  UTC+2 machine the same update reads as 19:18 in the log and 17:18 in the folder
  name. Consistent with everything else in this app storing UTC, and the CLI's
  `list-backups`/`rollback` never require reading the timestamp by eye - but it
  does look like a mismatch if you're picking a backup by hand.

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
  at registration time and crash bot startup. **`build_snapshot_text()`
  originally rendered the incident count only** ("3 open incident(s)") —
  changed after user feedback that it was too vague to be useful, to full
  per-incident detail (title, description, status, service(s), start time,
  every update). Uses Discord's own markdown for readability rather than a
  flat wall of text: a bold title line per incident, everything else
  (start time/description/updates) as a `>` blockquote (consecutive `>`
  lines render as one continuous left-barred block in every Discord
  client — the same "nested detail under a title" hierarchy the public
  page's `.incident-bubble`/`.incident-updates` gives visually), and a
  blank line between separate incidents so multiple open ones don't run
  together. If you touch this again, keep both the multi-incident
  separation and the blockquote grouping — both were specifically
  requested fixes, not incidental formatting.
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
- **`stop()`/`restart()` (added 2026-08-10)** — the module was fully
  fire-and-forget before this: `start()`'s `_run()` closure discarded both the
  `threading.Thread` and the `discord.Client` instance, so nothing outside the
  module could ever command a running connection to shut down. Fixed by having
  `_run()` manage its own `asyncio` event loop explicitly
  (`loop.run_until_complete(runner())`, not the `client.run(...)` convenience
  wrapper) and stashing `client`/`loop`/`thread` in a module-level `_runtime`
  dict *before* the client starts connecting — that's what lets `stop()` call
  `asyncio.run_coroutine_threadsafe(client.close(), loop)` from a *different*
  thread (an admin route's request-handling thread, via `/admin/system`) and
  then `thread.join(timeout)` to know the connection is genuinely closed, not
  just "asked nicely." `restart()` is just `stop(); start()` — relies on
  `start()`'s new already-running guard (`if _runtime["client"] is not None:
  return`) so a `restart()` call can't race with the old connection still
  shutting down. Verification: unit-tested with a fake in-thread event loop
  standing in for a real discord.py connection (`_start_fake_bot_runtime()` in
  `tests/test_discord_bot.py`) — genuinely exercises the
  threading/asyncio wiring `stop()` depends on, but still never a real
  gateway connection; see ROADMAP.md's verification-against-real-instances note.
- **`_edit_tracked_status_message()` (added 2026-08-10, fixing a real
  reliability bug the user hit)** — `_refresh()`'s loop over tracked `/status`
  messages used to wrap `fetch_message()`/`msg.edit()` in a bare `except
  Exception: db.delete_discord_status_message(...)`. That treated *any*
  failure — a timed-out API call, a momentary network blip, anything — exactly
  like "the message was deleted by someone," immediately forgetting it and
  forcing a brand-new `/status` run to get it back, even though the message
  was still perfectly fine. Fixed by extracting the edit into its own method
  that only forgets the tracked row on `discord.NotFound` (genuinely gone) or
  `discord.Forbidden` (access revoked) — every other exception retries up to
  `REFRESH_RETRY_ATTEMPTS` times (`REFRESH_RETRY_DELAY_SECONDS` apart) and, if
  still failing, is logged and left alone: the row stays tracked, and the next
  scheduled `refresh_loop` tick tries again on its own. Don't collapse the
  `except (NotFound, Forbidden)` / `except Exception` branches back into one —
  that distinction *is* the fix; a bare catch-all forgetting the message on
  any failure is the exact bug this replaced.
- **`on_resumed()` (added 2026-08-11) — without it, the admin panel could get
  permanently stuck showing "not connected" for a bot that was fully working.**
  `on_disconnect()` sets `_state["connected"] = False` and fires for *any*
  dropped gateway connection, including an ordinary blip that discord.py
  resumes on its own without a fresh login. The catch: a resumed session only
  fires `on_resumed()`, never `on_ready()` again (`on_ready()` is for a fresh
  identify only) — so with no `on_resumed()` handler, nothing ever set
  `connected` back to `True` after the first disconnect+resume cycle, even
  though the bot kept dispatching events and responding normally the entire
  time (confirmed by the user: it kept answering the slash command and
  editing its tracked `/status` message while the admin panel insisted it
  was offline). Fixed by adding `on_resumed()` alongside `on_ready()`/
  `on_disconnect()`, setting `_state["connected"] = True` the same way
  `on_ready()` does — it deliberately does *not* redo the guild-whitelist
  enforcement or restart `refresh_loop` like `on_ready()` does, since a
  resume means the session context didn't actually change, just the
  connection dictionary. If you add another lifecycle-dependent piece of
  `_state`, remember `on_ready`/`on_resumed`/`on_disconnect` are the three
  events that matter, not just the first two.

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

## Public "report a problem" form (`app.py` — added 2026-08-10)

- **`GET/POST /report` is this app's first public POST route besides
  `/admin/login`** — deliberately separate from the admin-authored
  incident/maintenance system (a visitor telling the admin something looks
  wrong, not the admin recording a known outage). Because it's outside
  `/admin/`, it is **not** covered by the CSRF `before_request` hook — this is
  fine here (there's no authenticated session/privilege being exercised, so a
  cross-site submission achieves nothing an attacker couldn't already do by
  POSTing directly), but it does mean this route needs its *own* anti-abuse,
  not "just add CSRF."
- **Three independent anti-abuse layers, all process-global (not per-IP), same
  reasoning as `_login_state`**: a honeypot field (`website`, hidden via CSS
  positioning, not `display:none`/`type=hidden` which some bots skip — a
  filled honeypot silently "succeeds" without writing a row, so a bot gets no
  signal it was caught), a minimum-time-to-fill check
  (`session["report_form_rendered_at"]`, set on the `GET`, checked against
  `REPORT_MIN_SECONDS_TO_FILL` on `POST`), and a rate limit
  (`_report_state`/`_report_rate_limited()`/`REPORT_RATE_LIMIT` per
  `REPORT_RATE_WINDOW_SECONDS`, mirroring `_login_state`'s shape exactly). No
  external rate-limiting library was added just for this one route.
- **`problem_reports` is a brand-new table** (plain `CREATE TABLE IF NOT
  EXISTS` is fine per the schema-change convention above — nothing to
  retrofit). `service_id` is optional (a report can reference a specific
  service's card via `?service_id=N` pre-filling the form, or be general) and
  `ON DELETE SET NULL` so deleting a service later detaches rather than
  cascade-deletes any reports about it.
- **The admin nav's unread-count badge (`unread_reports_count`) comes from a
  `context_processor`** (`app._inject_admin_badges()`), not threaded through
  every admin route's `render_template` call by hand — same reasoning as
  `csrf_token()` being a Jinja global. Scoped to `request.path.startswith("/admin/")`
  so the `COUNT(*)` query never runs on a public page load.
- **"Create incident from this report"** (`/admin/reports/<id>/create-incident`)
  pre-fills a new incident's title/description from the report and marks the
  report resolved, then redirects to the incident's edit page rather than
  silently doing everything with no further admin input — the admin still
  gets a chance to adjust title/description/services before it goes live.
- **Open reports show directly on the public page's service cards, not just
  in `/admin/reports` (added 2026-08-10, requested after the first version
  shipped admin-only).** `db.count_open_reports_by_service()` returns a
  `{service_id: count}` map in one grouped query (not N+1 per-card queries),
  wired into `_enrich_services()` (shared by `index()` and `api_status()`) as
  `s["open_reports_count"]`. "Open" here means the same thing an incident's
  "open" does — anything not yet `resolved` (`new` or `reviewed`), a
  deliberately *broader* definition than the admin nav badge's unread-only
  (`new`) count from `count_unread_problem_reports()` — don't conflate the
  two or reuse one function for both. **Only a count is shown publicly, never
  the report's message/contact text** — a visitor-submitted free-text field
  is not something to echo back onto the public page. General reports
  (`service_id IS NULL`) aren't attributable to any one card and are excluded
  from the counts entirely, not folded into every service's total.
- **Per-service "show the Report a problem button" toggle
  (`services.show_report_button`, added 2026-08-11).** Same shape as
  `ignore_in_overall_status`/`auto_incident` — a plain `_ensure_column`
  retrofit, default `1` so every pre-existing service keeps showing the
  button exactly as before, opt-*out* per service rather than opt-in. Checked
  in `sections/services.html` (`{% if s.show_report_button %}` around the
  existing "Report ⚑" link) — purely cosmetic, hides the button on that one
  service's card only. **Deliberately does not touch the `/report` route
  itself** — a visitor who already has (or guesses) `/report?service_id=N`
  can still submit a report for a service with the button hidden. That was a
  conscious scope call (asked for as "hide the button," not "block
  reporting"), not an oversight — if per-service access control is ever
  wanted, it needs its own check inside `report_problem()`, not just a
  bigger template guard. No global/default-value setting was added either
  (no `service_default_show_report_button`) — purely per-service, matching
  what was actually asked for; don't add a defaults cascade for this without
  a real reason to.

## Component restart controls (`app.py`, `discord_bot.py` — added 2026-08-10)

- **`/admin/system` is a new page, deliberately separate from
  `/admin/resources`** — Resources is about the host machine's hardware,
  System is about this app's own process/components. Two restart targets:
  the whole app process, or just the Discord bot's connection.
- **Whole-app restart (`app._restart_process()`) uses `os.execv(sys.executable,
  [sys.executable] + sys.argv)`** — replaces the running process image in
  place (same PID), which works identically whether launched as `python
  app.py`, `python serve_waitress.py`, or either wrapped in a systemd
  unit/Task Scheduler entry, and needs **no supervisor process** unlike a
  fork+exit approach. Delayed by 1s on a background thread first (same shape
  as `monitoring.control_host()`) so the triggering HTTP response has a
  moment to actually reach the browser before the process image swaps out
  from under it.
- **Same step-up 2FA pattern as `admin_host_control()`** applies to both
  restart targets in `admin_system_restart()` — a stolen/replayed session
  cookie alone must not be enough to trigger either, given a full-app restart
  briefly takes the whole portal offline for everyone and a bot restart
  interrupts anyone mid-conversation with it. Same typed-confirmation UI
  pattern too (`static/js/admin_system_control.js`, mirrors
  `admin_host_control.js` almost exactly — one confirm panel driving both
  trigger buttons via a `data-component` attribute instead of `data-action`).
- **Never live-invoke `_restart_process()` for real in this sandbox or any
  shared environment** — same rule as `monitoring.control_host()`. Verify
  exclusively by mocking `app._restart_process()`/`discord_bot.restart()` in
  pytest and asserting the route called them; a live smoke test should exit
  the confirm-panel flow right before actually submitting, not click
  through it.
- See the Discord bot section above for `discord_bot.stop()`/`restart()`
  themselves — the restart route here is just the thin admin-facing wrapper
  around them (plus the equivalent whole-app path).

## Custom logo / branding (`app.py`, `static/uploads/` — added 2026-08-10)

- **This app's first file upload, ever** — confirmed zero prior upload code
  anywhere in the repo before this. Kept deliberately narrow to avoid
  needing to reason about a broad upload surface: the saved file is always
  named exactly `logo.<ext>` (never the visitor/admin-supplied original
  filename) under a dedicated `static/uploads/` directory, extension
  whitelisted (`png`/`jpg`/`jpeg`/`svg`/`webp`/`ico`) against
  `LOGO_ALLOWED_EXTENSIONS`, and `MAX_CONTENT_LENGTH` (already 2MB app-wide)
  caps the upload size — there's no path-traversal surface and at most one
  logo file ever exists on disk at a time. `static/uploads/` is gitignored
  the same way `instance/` is — it's runtime-created state, not tracked
  content.
- **`app._inject_branding()` (a `context_processor`) exposes
  `site_logo_filename`/`site_logo_version` to every template**, admin
  included, so both the public topbar (`index.html`, `report.html`) and
  `base.html`'s `<head>` (for the favicon `<link>`) can use it without every
  route threading it through by hand. `site_logo_version` is a cache-busting
  `?v=<mtime>` query param from a local `os.path.getmtime()` stat — not the
  kind of slow I/O the request-handler rule above is about, it's a local
  filesystem call same class as reading a DB row.
- **A re-upload in a different format removes the old file first**
  (`admin_settings_logo()` diffs the new filename against the stored
  `site_logo_filename` setting before saving) — otherwise switching from
  e.g. a `.png` to a `.svg` logo would leave the old file orphaned on disk
  forever, since the filename (and therefore the path) changes with the
  extension.
- Added a new `@app.errorhandler(413)` at the same time (this app had
  `MAX_CONTENT_LENGTH` set app-wide already, but nothing had ever hit it
  before file uploads existed) — without it, an oversized upload fell
  through to Flask's default unstyled error page instead of this app's own
  `error.html`.

## Testing/verification habits (established over many sessions — keep following them)

- Run the full `pytest` suite *and* a live `python app.py` + `curl` smoke test of
  whatever routes actually changed before calling something done. Several real bugs
  (the integration-blocking page load, the maintenance-window timing bug, the 2FA
  enrollment `KeyError`) were only ever caught by actually running the server,
  never by unit tests alone.
- **For anything that depends on client-side JS (AJAX "load more" buttons, the
  favicon/logo actually rendering, console errors), `curl` alone isn't enough —
  use the pre-installed Playwright/Chromium browser** (see the environment
  notes on `PLAYWRIGHT_BROWSERS_PATH`/`executablePath` — don't run `playwright
  install`) to actually click through the flow and check for console errors.
  This caught a real bug 2026-08-10: `report_problem()` never passed
  `site_name` to `report.html` at all, silently leaving the topbar brand text
  and page title blank — every route-level `pytest` test for that route only
  checked for form-field presence, never branding, and `curl` output looked
  fine since the HTML was syntactically valid, just missing content. A real
  browser screenshot caught it immediately. (One red herring encountered
  along the way: an `ERR_CONNECTION_RESET` console error from the sandbox
  having no egress to `fonts.googleapis.com` — unrelated to app code, don't
  chase it as if it were.)
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

0. **Bump `VERSION` first — this is now a required step, not a formality.** The
   `VERSION` file at the repo root must contain the exact version being released,
   *without* the leading `v` (`1.5.0`, `1.5.0-rc.1`), and must be committed before
   the tag is created, so the tagged commit — and therefore the `git archive` zip
   built from it — carries the right number. Nothing derives this automatically.
   Getting it wrong is not cosmetic now that self-update exists: `updater.py`
   compares the running `VERSION` against release tags, so a zip shipping a stale
   value makes an installed portal either believe it's already up to date (and
   refuse to install a real update) or offer an update it already has. Sanity check
   after tagging: `git show vX.Y.Z:VERSION` must equal `X.Y.Z`.
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
   any manual exclusion list to maintain. **The zip must always be attached to the
   release**: `updater.py` prefers a `.zip` asset and only falls back to GitHub's
   auto-generated zipball, which publishes neither a size nor a digest, so a
   release without the asset silently downgrades every updater's integrity check to
   TLS-plus-tag-pinning alone.
4. **Publish**:
   ```
   git tag vX.Y.Z
   git push origin vX.Y.Z
   gh release create vX.Y.Z status-portal-vX.Y.Z.zip --title "vX.Y.Z" --notes "<changelog>" [--prerelease]
   ```
   **If this step fails outright — no `gh` CLI, no direct GitHub API access, or
   `git push origin vX.Y.Z` itself rejected (added 2026-08-10, confirmed in a
   cloud/remote execution session):** this is normal for a session running in
   Anthropic's cloud infrastructure rather than directly on the user's own
   machine — those sessions' git credentials are scoped to push *branches*
   (confirmed: pushing a brand-new branch name succeeds) but not to create new
   *tags* (confirmed: `git push origin vX.Y.Z` gets a `403`, tags apparently
   sit outside the allowed ref pattern), and the GitHub MCP toolset available
   in that context may have no release-creation tool at all (only read tools
   like `list_releases`/`get_latest_release`/`get_tag` — checked via
   `ToolSearch` before concluding this, don't assume it's missing without
   checking). Don't just report failure and stop — fall back to: build the zip
   anyway (`git archive` still works locally, no push needed), push a **new
   branch** off the current one (this *does* work), and hand the user
   everything they'd need to finish the last step themselves: the exact tag
   name, target commit/branch, title, changelog body, prerelease flag, and the
   zip itself (e.g. via `SendUserFile`) — see this exact exchange for the
   template of what to hand back. A locally-run session (the user's own
   machine, not this cloud environment) usually has full `git`/`gh` access and
   can just complete every step directly — this fallback is specifically for
   when it can't, not a replacement for trying the real steps first.
