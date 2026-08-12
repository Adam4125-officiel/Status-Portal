# Notes for future Claude Code sessions on this repo

This file is for context that survives between sessions — architecture decisions,
gotchas, and standing workflows that aren't obvious from reading the code cold. Keep
it updated as the project evolves; don't let it go stale.

**Two companion files:**

- `ROADMAP.md` — open feature ideas. This file is about *how the existing code works
  and how to work on it*, not what's left to build.
- `docs/HISTORY.md` — the narrative archive: how past bugs actually presented, what
  the broken versions did, and what has been verified against real hardware / a real
  Discord server / real instances, and when. Rules live here in `CLAUDE.md`; the
  stories behind them live there. Where a rule below exists because something broke,
  it links to the write-up rather than retelling it.

**When you fix something notable**: put the *rule* here (imperative, short — what a
future session must not get wrong) and the *story* in `docs/HISTORY.md` (what broke,
how it presented, how it was caught). That split is what keeps this file readable as
the project grows. Verification records — "confirmed working on real Windows on date
X" — always go in `docs/HISTORY.md`.

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
- **Never call slow/external I/O directly inside a Flask request handler.** Standard
  pattern: a background thread polls and writes to a module-level cache dict
  (`app._integration_status_cache`-style); request handlers only ever *read* the
  cache. The one sanctioned exception is an explicit one-shot admin action the user
  knows will be slow (the "Check now" button, `perform_update()`) — never something
  that fires automatically or on every page load. This rule exists because it already
  bit the project once, costing ~10s on every single page load
  (`docs/HISTORY.md` → "The synchronous integration check").
- **Config split**: secrets and things that behave like static deployment config
  (webhook URLs, bot tokens, check intervals) are env vars via `config.py` — nothing
  else should read `os.environ` directly, and changing them needs a restart. Routine
  admin-tunable toggles (site name, visibility checkboxes, command names) are DB
  `settings` rows, editable live from the browser, no restart needed. When adding a
  new setting, decide which bucket it belongs in based on that distinction, not
  convenience. **A new `PORTAL_*` env var means three files, not one**: `config.py`
  (read it), `.env.example` (document it), and `docker-compose.yml`'s `environment:`
  block — compose only auto-loads `.env` for substitution *into that file*, it does
  not inject it into the container, so anything missing from that list is silently
  ignored under Docker no matter how correctly the user set it.
- **Optional heavy dependencies are never imported at module level.** `nvidia-ml-py`
  (GPU stats) and `discord.py` (bot) both follow this: lazy-imported only inside the
  function that needs them, wrapped so a missing package degrades to "feature
  disabled, log why" rather than crashing the whole app on import. Neither is in
  `requirements.txt`. If you add another optional integration, follow the same shape.
- **This app is meant to survive its own restart cleanly.** State that needs to
  persist across a process restart (maintenance-window progress, the Discord bot's
  tracked live-message id) lives in SQLite, never only in memory.
- **Status values aren't just `operational`/`degraded`/`maintenance`/`down`.**
  `slow` is a purely cosmetic fifth tier (an otherwise-healthy response slower than a
  service's configured `slow_threshold_ms`) — it never opens/resolves an auto-incident
  on its own, and ranks between `maintenance` and `operational` in every
  overall-status precedence list. Any place one of the four original statuses is
  enumerated (badge colors, CSS class maps, Discord icon/label/presence-text tables,
  `compute_overall_status()` / `discord_bot._overall_status()`) needs a `slow` entry
  too — grep for `"degraded"` across the codebase before assuming you've found every
  status-aware spot.
- **A health-check response is "reachable" based on getting a response at all, not a
  2xx.** Only a 5xx (or a connection failure/timeout, which stays `down`) counts
  against a service — a 401/403 login prompt or a 404 still means the server answered.
  **502 is a carve-out within that 5xx bucket**: `_check_status_for_response()`
  classifies it as `down`, not `degraded`, because it means whatever's in front of the
  service couldn't reach it at all. Both decisions have real bug reports behind them
  (`docs/HISTORY.md` → "Basic-auth services misread as degraded", "502 split out").
- **`startup_grace_seconds` (per service) suppresses auto-incidents, not checks.**
  `app._within_grace_period(service)` gates the call to
  `_handle_incident_lifecycle()`/`_handle_integration_incident_lifecycle()` only —
  status and response time are still recorded on every cycle regardless, so the
  public page reflects reality during the grace window, it just won't open an
  incident over it. Measured from `app._APP_START` (process start), not from
  anything service-specific — good enough since services are expected to boot
  around the same time as the portal itself, not something to over-engineer.
- **`retry_count`/`retry_interval_seconds` (per service) retry a 'down' result inline
  before it's ever recorded, not after.** `app._check_service_status()` wraps
  `app._run_single_check()` — only a `down` outcome is retried (degraded/slow/
  operational are never worth retrying, and only `down` ever opens an auto-incident),
  the first non-down result wins immediately, and this blocks the background
  health-check thread for up to `retry_count * retry_interval_seconds` seconds for
  that one service — an intentional tradeoff (background thread, not a request
  handler), not a bug. `retry_count=0` (the default, and the value every pre-existing
  service gets via `_ensure_column`) preserves the exact original single-attempt
  behavior.
- **`_handle_incident_lifecycle()`'s open side must stay level-triggered, not
  edge-triggered.** Open whenever `new_status == "down"`, full stop, relying only on
  the `get_open_auto_incident_for_service()` idempotency guard — never on a
  `previous_status != "down"` transition check. Same for
  `_handle_integration_incident_lifecycle()`. This is the single easiest invariant in
  the codebase to "tidy" back into a bug: it already broke exactly that way once, and
  the failure mode is a service staying down forever with zero incident ever opened
  (`docs/HISTORY.md` → "must become level-triggered" for why the transition check
  cannot work). **If you touch either lifecycle function, add a live/real-timing test
  alongside the mocked unit tests, not instead of them** — the mocked tests are what
  missed it the first time.
- **Timestamps are rendered server-side as UTC, converted to the visitor's local time
  client-side.** Every public timestamp is wrapped as
  `<span class="local-time" data-utc="{iso}">{utc fallback text}</span>` (or
  `class="local-time-short"` for the compact service-card spot, which gets hour:minute
  only); `static/js/local_time.js` overwrites the text with `Date.toLocaleString()` in
  the browser's own timezone. The UTC fallback text stays in the DOM for no-JS
  clients. The server has no idea what timezone a visitor is in — don't try to
  guess/convert server-side. **When adding another timestamp to the public page, grep
  for `_at[` / `[:16]` / `[11:16]` across every template** rather than trusting that
  the obvious spots are the only ones; a first pass already missed one this way
  (`docs/HISTORY.md` → "The missed service-card timestamp").
- **A many-to-many relationship (multi-service incidents/maintenance) gets a join
  table with its own per-row state, not a schema rewrite.**
  `maintenance_window_services`/`incident_services` are the source of truth for which
  service(s) a window/incident covers; the legacy single `service_id` column on
  `maintenance_windows`/`incidents` stays populated with the *first* selected service
  so every pre-existing single-service reader (auto-incident creation, RSS, the badge
  endpoints) keeps working with zero changes. A one-time backfill in `init_db()` seeds
  the join table for rows written before it existed (`WHERE id NOT IN (SELECT DISTINCT
  ... FROM <join_table>)` — idempotent, safe to re-run every startup). Each service in
  a multi-service maintenance window gets its *own* `pre_status`/`pre_manual_override`
  in the join table (a window covering 3 services needs 3 independent restore points);
  the legacy columns on `maintenance_windows` only mirror the primary service's
  snapshot, for cheap no-join reads. If you add another
  one-to-many-turned-many-to-many relationship later, follow this same shape rather
  than dropping the legacy column.
- **Every admin POST is CSRF-protected via a before_request hook in `app.py`, not
  per-route.** A per-session token (`app._get_csrf_token()`, registered as the
  `csrf_token()` Jinja global) is injected into every `<form method="POST">` by
  `static/js/csrf.js` reading a `<meta name="csrf-token">` tag `base.html` renders on
  every page — templates never hand-embed the token, deliberately: hand-adding it to
  the ~16 templates with a POST form risked silently missing one. Any new admin route
  just needs to live under `/admin/` (the check is
  `request.path.startswith("/admin/")` + `method == "POST"`) — no per-route wiring.
  The check is bypassed when `app.testing` is set, since the test client posts raw
  form dicts and never runs the injection JS; to test the mechanism itself see
  `test_csrf_protection_rejects_missing_or_wrong_token` in `tests/test_app.py`.
- **Step-up 2FA goes through `app._require_totp(message, endpoint)`, never a
  hand-rolled copy.** It returns `None` when the caller may proceed and a
  ready-to-return redirect when it must not, so every call site reads
  `blocked = _require_totp(...); if blocked: return blocked`. Used by
  `admin_host_control`, `admin_system_restart` and `admin_update` — the three actions
  where a stolen/replayed session cookie alone must not be enough. Adding a fourth
  destructive action means calling this helper, not re-inlining the check: three
  hand-maintained copies is three chances for one to quietly stop matching the
  others. (The login flow and `/admin/2fa` enable/disable are *not* step-up and
  correctly don't use it — those are primary auth and enrollment.)
- **Never interpolate a value from outside the portal's own admin into an inline JS
  event-handler attribute** (e.g. `onsubmit="return confirm('...' + x + '...')"`).
  Jinja's HTML-attribute escaping does not protect a value the browser HTML-decodes
  and then hands to the JS engine a second time as code. Put it in a `data-*`
  attribute and read it from a JS listener instead (`static/js/admin_vm_control.js` is
  the reference implementation). This was a real XSS via Hyper-V VM names
  (`docs/HISTORY.md` → "VM-name XSS"). The pre-existing
  `onsubmit="confirm('Delete {{ s.name }}?')"` pattern in the admin list templates is
  different and lower-risk (only the already-fully-privileged portal admin sets those
  names — self-XSS, no privilege gain) and was deliberately left alone, but **don't
  copy that pattern** for any value that can originate from outside the portal's own
  admin (an external API, another local account/service, etc.).
- **A background thread that can shell out to run a real OS command (host
  restart/shutdown, VM control) must never be exercised for real in this sandbox, or
  against any environment you're not certain you're allowed to affect** — not even to
  see it "fail". `monitoring.control_host()`/`control_vm()` are unit-tested
  exclusively via a mocked `subprocess.run`; verify by reading the mocked call
  arguments, never by actually invoking the route live. `control_vm()` happens to
  safely no-op on non-Windows before reaching `subprocess` at all (its `os.name` guard
  is the very first line), so *that* one route is fine to hit live in this Linux
  sandbox — `control_host()` has no such guard (host restart/shutdown applies on both
  platforms on purpose) and must never be POSTed to outside of a mocked test.
- **`.incident-bubble` must fully match `.service-card`'s treatment, not just look
  similar** — including `display: flex; flex-direction: column; gap` and the hover
  border-transition, not only background/border/radius/padding. Keep them in sync on
  purpose (or add a shared class) rather than letting two independent-looking rulesets
  drift. The nested `.incident-updates` list keeps its left border + per-row dot for
  the same reason: a flat list under a card doesn't read as "these updates belong to
  this incident" without an explicit hierarchy cue. For a visual bug report, render a
  preview using the app's actual CSS tokens before shipping rather than reasoning
  about CSS in the abstract (`docs/HISTORY.md` → "didn't read as a card").
- **Use a checkbox list, never a native `<select multiple>`, for any
  service/entity picker.** `.checkbox-list`/`.field-check` in `static/css/style.css`
  are the shared styles; `request.form.getlist(...)` reads identically from repeated
  checkboxes as from a multi-select, so this costs no route changes. A `<select
  multiple>` here shipped a bug where an admin submitted "everything except the
  service they picked" (`docs/HISTORY.md` → "The `<select multiple>` service picker").
- **A "merge two status sources into one" feature (per-service `api_health_mode`) must
  still produce a single final status that feeds both the public display and
  `_handle_incident_lifecycle`, never two independent decisions.**
  `app._merge_api_health(status, api_health_mode, integration_reachable)` folds a
  linked integration's cached reachability into the web-check's own `status` *before*
  `db.update_service_status_from_check`/`_handle_incident_lifecycle` are called in
  `run_health_checks()`. This is what preserves the level-triggered invariant above,
  which already broke once from letting a status write and an incident-lifecycle
  decision see different values. Don't add a second "does the API health affect this?"
  branch anywhere else; extend the merge function instead.
- **An internal "load more" / pagination endpoint returns a server-rendered HTML
  fragment, not JSON** (`/api/incidents/more`, `/api/maintenance/history`). This app's
  only actual JSON API is `/api/status`, meant for external consumption; there's no
  client-side templating anywhere else, so an HTML-fragment endpoint
  (`render_template` on a partial like `sections/_incidents_fragment.html`, inserted
  via `insertAdjacentHTML('beforebegin', ...)` in `static/js/public_history.js`)
  matches the existing "server renders everything, small vanilla JS wires it up"
  convention. Newly-inserted timestamps need `window.applyLocalTimes(document)` re-run
  (see `static/js/local_time.js`) since they arrive after the page's load event fired.
- **`/api/incidents/more` paginates by the ids the client already has (`?seen=`),
  never by an offset or an id cursor, and never re-applies the initial view's
  `max_age_days` filter.** Excluding the shown ids is the only formulation that is
  simultaneously gap-free and duplicate-free, because it states the intent directly
  instead of approximating it with a position. Four separate bugs were shipped here in
  sequence, each caught live by the user rather than by the test suite — **read
  `docs/HISTORY.md` → "four pagination bugs in sequence" before touching this or
  "simplifying" it back.** Two invariants that are easy to break silently:
  - The endpoint **fails closed** (empty response) only when `seen` is missing from
    the query string *entirely*, or oversized. A `seen` key that is *present but
    empty* (`?seen=`) is legitimate — it's what the button sends from the "all hidden"
    empty state. Don't re-merge those two checks.
  - The "all hidden by `max_age_days`" empty state needs its own template branch with
    the load-more button still present (`index()`'s `incidents_hidden` flag). Putting
    the button inside `{% if incidents %}` hides it exactly when it's needed most.
- **Every CSS/JS reference in a template must go through `asset_url()` (`app.py`),
  never a bare `url_for('static', ...)`** — it appends a `?v=<mtime>` cache-buster.
  The documented update process is "extract the release zip over your existing
  folder", which changes a file's *contents* but never its *URL*, so without this the
  browser keeps serving the previous release's copy. This has already silently
  shadowed a shipped fix (`docs/HISTORY.md` → "Stale cached JS"); every future JS/CSS
  change has the same exposure, so this is a project-wide rule, not a one-off patch.
- **New integration kinds are just a new entry in `integrations.py`'s
  `fetch_integration_status()` dispatch dict plus a matching fetcher function** — no
  architectural change needed. Every fetcher returns the same
  `{"reachable", "version", "issues", "error"}` shape. Two existing quirks worth
  knowing: Bazarr expects its API key as a `?apikey=` query param, not an `X-Api-Key`
  header (confirmed against Bazarr's source, not a real instance); Tdarr and Byparr
  have no API key concept at all, so the shared `api_key` form field is simply unused
  by those two (left on the form only for a consistent UI).
- **A genuinely slow integration endpoint gets its own `config.py` env var, not a
  raise of the shared `TIMEOUT`.** `config.BYPARR_TIMEOUT_SECONDS`
  (`PORTAL_BYPARR_TIMEOUT_SECONDS`, default 30s) is the reference case: Byparr's
  `/health` solves a real Cloudflare challenge before responding, so the shared
  `TIMEOUT = 5` every other fetcher uses for a plain REST call timed out against a
  perfectly healthy instance (`docs/HISTORY.md` → "Byparr's `/health` timeout").
- **`service_default_*` settings (Settings → "Service defaults") are pre-fill-only,
  never live-cascading** — `app._service_defaults()` reads them and the `GET` handlers
  for both "New service" forms pass the result as an optional `defaults` context var
  (`service is None` is what triggers using `defaults` instead of hardcoded
  fallbacks). Changing a default later never retroactively touches a service that
  already exists; `run_health_checks()` and the schema are completely unaffected.
- **Both "new service" entry points must stay field-complete.** `/admin/new/combined`
  (the wizard) and `/admin/services/new` should offer the same service fields and
  build the same kind of `data` dict before calling `db.create_service()` — the wizard
  having a smaller field set was a real bug where `service_default_*` settings were
  silently unreachable and `create_service()` fell back to hardcoded literals
  (`docs/HISTORY.md` → "The combined wizard's missing field set"). The wizard's extra
  fields live in a collapsed-by-default `<details>` "Advanced settings" block;
  **collapsed is a CSS/visual state only — the inputs are still in the DOM and still
  submit**, which is why server-side pre-filling is enough on its own and no "open the
  advanced section" JS handler is needed. Naming gotcha: the service's own
  `auto_incident` checkbox and the integration's *different* `auto_incident` concept
  can't share one HTML name on one form, so the integration's is deliberately
  `check_auto_incident` in the template and mapped explicitly in the route.
- **An admin page's on-page `<h1>`, its `{% block title %}` and its nav label must all
  match** — they drift independently, and the `<h1>` is the one the user actually
  sees. Check all three when a page's scope or nav label changes
  (`docs/HISTORY.md` → "An admin `<h1>` drifting").

## Self-update (`updater.py`, `update.py`, `/admin/about`)

- **`VERSION` (a tracked file at the repo root) is the single source of truth, and
  bumping it is a required step of cutting a release** (see Release process below).
  It has to be a file rather than a literal in `config.py` because `updater.py` reads
  the *incoming* release's version straight out of an extracted zip without importing
  the new, not-yet-installed code. It has to be a tracked file rather than anything
  git-derived because `git archive` strips `.git` — a shipped zip has no git metadata
  at all. `config.IS_GIT_CHECKOUT` (one `os.path.isdir(".git")` at import, no git
  subprocess) distinguishes a working tree from an extracted release;
  `config.VERSION_DISPLAY` appends `+dev` for the former. **`+dev` is a label only —
  never compare against it**, every version comparison uses `config.VERSION`.
- **`updater.py` is the one implementation; `update.py` and the admin route are both
  thin wrappers.** This was an explicit requirement, and it's what stops the CLI and
  the button drifting apart. `updater.py` imports only `config` and `db` — never
  `app.py` (which imports it) and never Flask, so the CLI works on a portal that
  won't start.
- **The repo URL is a module constant and must never become configurable** — not a DB
  setting, not an env var, not a CLI flag. A configurable update source is a "point
  this server at my code" primitive for anyone who can write a setting. There's a test
  asserting the constants and that certificate verification is never disabled; keep it.
- **`browser_download_url` is untrusted input, not a constant** — it arrives over the
  network from the API response, so `_validate_download_url()` checks scheme and host
  against `ALLOWED_DOWNLOAD_HOSTS` both before the request *and* on `response.url`
  after redirects have been followed.
- **Integrity verification protects the transfer, not the publisher.** The size and
  SHA-256 are checked against what the releases API declares, which comes from the
  same origin as the bytes — so this catches truncation/corruption/a mangling proxy
  and (via TLS) an in-transit substitution, but not a malicious release. Real
  publisher authenticity would need a detached signature against a key shipped with
  the app. Say this plainly whenever documenting the feature rather than letting
  "checksum verified" imply more.
- **Which files get replaced comes from the release archive's own member list** — a
  whitelist by construction, and structurally incapable of containing `instance/`,
  `.env` or `static/uploads/` since all three are gitignored and releases are built
  with `git archive`. `PROTECTED_PREFIXES`/`PROTECTED_FILES` are a second, redundant
  check that **aborts the entire update** if one ever shows up, rather than skipping
  that entry — an archive containing one means the release was built wrong, which is
  not a condition to proceed through quietly.
- **Channel = GitHub release channel (stable/unstable), deliberately not a git
  branch.** A branch head has no version identity, so "are you behind", "what am I
  installing" and "roll back to what" all become unanswerable; a branch is also
  arbitrary mid-work code rather than something that was cut and tested. Stable =
  non-prerelease releases only; unstable = prereleases (`-rc.N`) too. The latest is
  picked by **parsed version, not publish date**, so republishing an old release can't
  look like an update. Channel is a DB setting (routine admin toggle); the check
  *interval* is an env var (`PORTAL_UPDATE_CHECK_INTERVAL_SECONDS`) — the standard
  config split, applied.
- **The About page reads a cache and never checks GitHub inline.** Same rule and shape
  as `_integration_status_cache`. `refresh_update_cache_if_stale()` is called from the
  existing health-check loop rather than starting a second thread, and no-ops until
  its own (6h) TTL elapses so a 120s health-check interval doesn't become a GitHub
  call every 120s. "Check now" (`/admin/about/check`) is the sanctioned
  explicit-slow-action exception. A failed check renders as "couldn't check" and
  affects nothing else on the page.
- **`perform_update()` runs synchronously inside the admin route.** That is the same
  sanctioned exception — an explicit one-shot action the admin knowingly triggered —
  not a violation of the no-slow-I/O rule. Don't "fix" it by moving it to a background
  thread; the admin needs the success/failure in the response.
- **What rollback can and cannot do — don't overstate this.** Every failure *before*
  the restart (download, verification, a file that won't replace part way through, a
  failed `pip install`) is rolled back automatically and in-process, and the portal
  keeps running what it already had. The failure *after* the restart cannot be: once
  `os.execv` replaces the process image nothing from the old version exists to detect
  a bad start. `write_pending_marker()` therefore only buys two things — the next
  *successful* start confirms and clears it, and a failed one leaves a record naming
  the exact backup to restore. Genuine automatic post-restart rollback needs a
  supervisor outside the process (systemd + a health check, or a wrapper), which this
  project deliberately doesn't ship because it would change how everyone launches the
  portal.
- **`update.py rollback --emergency` is not a second update implementation and must
  not grow into one** — it only restores an existing backup, reading only the
  `manifest.json` `updater.py` already wrote. Every `update.py` import is lazy for the
  same reason: the recovery tool must still work on a tree the update broke
  (`docs/HISTORY.md` → "`update.py rollback --emergency`").
- **`pip install` runs only when `requirements.txt` actually changed, and a failure
  rolls the update back** — restarting into code whose dependencies aren't installed
  just fails to start, and this is the last moment where something is still running
  that can undo it.
- **Windows file locking**: every file is written to a sibling temp file then
  `os.replace()`d (atomic; a crash mid-write leaves the old file intact). The rename
  can still fail on Windows while another process holds the destination open, hence
  `REPLACE_RETRY_ATTEMPTS` with backoff, and a whole-update rollback if it still
  fails. If even the rollback can't write, the error names the backup folder to
  restore by hand rather than claiming it was handled — there's a test for that case.
  **This retry path has never actually fired in the wild** — see the verification
  records in `docs/HISTORY.md` before assuming it's proven.
- **An update never deletes a file that a later release removed.** Only files present
  in the incoming archive are written; anything the old version shipped that the new
  one dropped just stays on disk. This is identical to the extract-the-zip-over-the-
  folder process that predates the updater, and it's harmless (Python only imports
  what's referenced, Flask only serves what's routed), but don't assume a post-update
  tree is byte-identical to a fresh extraction — it's a superset. Deleting the
  difference would mean trusting a computed file list to remove things, which is a
  much worse failure mode than leaving a stale file behind.
- **Nothing in `updater.py` may read a DB setting without going through
  `_read_setting()`.** `sqlite3.connect()` *creates* an empty file for a path that
  doesn't exist, so a bare `db.get_setting()` from the CLI would leave a zero-table
  `instance/portal.db` behind on a fresh install — which `init_db()` then has to cope
  with, and which looks exactly like a corrupted database. `_read_setting()` checks
  `os.path.isfile(db.DB_PATH)` first and falls back to the default. It also swallows
  read errors, because the CLI is the tool you reach for when things are broken, up to
  and including the database.
- **`update.py`'s output must stay pure ASCII.** An em dash raised
  `UnicodeEncodeError` on a Windows console using codepage 437. This is the recovery
  tool — it must not be able to fail on a decorative character. `updater.py`'s
  `progress()` messages are subject to the same rule since the CLI prints them.
  There's a check for this in `tests/test_updater.py`.
- **Backups are pruned to `KEEP_BACKUPS` (5) after each successful update.**
  `_prune_backups()` reads the module constant *inside* the function rather than as a
  default argument value — a default arg binds at def time, which silently ignores a
  monkeypatched constant and made the pruning test pass for the wrong reason until it
  was fixed.
- **The About page's `list_backups()` is a local `os.listdir` + small JSON reads** —
  the same class of call as `asset_url()`'s `getmtime`, not the kind of slow outbound
  I/O the no-blocking-in-a-request-handler rule is about. Don't "fix" it into another
  cache.
- **Changing the channel must clear the update cache** (`admin_about_settings` does).
  Otherwise the page renders a "latest available" that was fetched for the *previous*
  channel right next to the newly-selected one.
- **`_inject_admin_badges()` also exposes `update_available`** for the nav's About
  badge. It reads the cache only (never triggers a check), so a miss or a failed check
  simply means no badge — exactly like having no unread reports.
- **Test-suite gotcha: `config.IS_GIT_CHECKOUT` is genuinely `True` when pytest runs
  from this repo**, so every update route and the About page's button correctly
  refuse. `tests/test_app.py` has an autouse fixture (`_update_test_environment`) that
  patches it to `False` and clears the update cache, so those tests stand in for a
  normal install. Without it a test can "pass" by hitting the git-checkout refusal
  rather than the behavior it meant to assert — if you add an update-route test, make
  sure it's actually reaching the code you think it is.
- **`config.ENABLE_INAPP_UPDATE` is an env var on purpose.** The risk it addresses is
  "someone compromised the admin panel"; a DB toggle that same attacker could flip
  from that same panel would address nothing. Same reasoning as
  `twofactor.RESET_2FA` being a host-level file. It defaults to **enabled** (the button
  was explicitly asked for), and the route checks it, not just the template.

## Monitoring architecture (`monitoring.py`)

- The Windows-only, PowerShell/CIM-backed queries (Hyper-V VM list, CPU temperature,
  per-disk temperature + drive-letter-to-physical-disk mapping) are **polled by a
  background thread** (`monitoring.start_background_refresh()`, started from both
  `app.py` and `serve_waitress.py` at startup) into a module-level cache
  (`_WINDOWS_CACHE`), not queried live inside a request handler — the same
  "never call slow I/O in a request handler" rule as the integration cache, applied to
  local subprocess calls. `get_vm_snapshot()` and the `_query_*()` functions stay
  live/directly-callable (that's what's unit-tested by mocking `subprocess.run`);
  `get_cached_vm_snapshot()` and `get_resource_snapshot()`'s `cpu_temp_c`/per-disk
  `temp_c`/`io` fields are the cache-reading wrappers request handlers should use.
- Per-disk temperature and I/O are **Windows-only** — correlating a mountpoint to a
  physical disk (needed for `psutil.disk_io_counters(perdisk=True)`'s
  `PhysicalDriveN` keys) uses `Get-Partition`'s drive-letter-to-disk-number mapping,
  which has no equivalent implemented here for Linux. The old aggregate
  (all-disks-combined) I/O reading was retired in favor of this — there's no
  system-wide I/O card anymore, only per-disk.
- **CPU temp (ACPI thermal zone WMI) and disk temp (`Get-StorageReliabilityCounter`)
  are both well-known-unreliable on real Windows hardware** — many systems return
  null/nothing through either. Both degrade to `None` gracefully. Confirmed on the
  user's actual desktop that CPU temp returns nothing at all, and that one drive
  reported a literal `0` (now treated as "no reading", since a real drive is never
  0°C). Neither swaps in a better data source — see `ROADMAP.md` → "More reliable
  CPU/disk temperature via HWiNFO", and `docs/HISTORY.md` for what was actually
  observed. Don't assume this area is fully fixed.

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
  somewhere neither of them owns. If you add another cross-cutting signal both the web
  page and the bot need, this is probably where it belongs, not duplicated in both.
- **The public page can also show Jellyfin's running scheduled tasks directly**
  (`show_public_jellyfin_tasks` setting) — separate from the high-load indicator, this
  just renders `integrations.get_cached_jellyfin_activity()["running_tasks"]` as its
  own "Jellyfin activity" section whenever the list is non-empty, regardless of
  whether high-load's thresholds are also tripped. Reads the same
  background-refreshed cache — no extra polling added.

## Discord bot (`discord_bot.py`) — read this before touching it again

- Uses a **slash command** (`discord.app_commands.CommandTree`), not a text/prefix
  command, because a prefix command needs Discord's privileged "Message Content"
  intent and a slash command needs no privileged intent at all
  (`docs/HISTORY.md` → "The prefix-command → slash-command rewrite"). Setup needs
  nothing toggled in the Developer Portal beyond inviting the bot.
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
  would otherwise look identical to "channel deleted" and wrongly drop the tracked row.
- **Three access controls, three different kinds of thing.** All use the same
  comma/newline-separated-IDs shape via `_parse_id_list()`, and all treat **empty as
  unrestricted** (a deliberate default-open choice so the command keeps working for
  anyone who hasn't configured it — the admin page calls this out explicitly):
  - `discordbot_allowed_user_ids` — *refuses the command* for an unlisted user,
    checked inside the command callback itself rather than via Discord's per-guild
    command permissions UI, so it's portable across whatever server the bot is invited
    to. An unauthorized attempt is logged with the offending user/id and gets an
    ephemeral reply, without ever building the (heavier) status embed.
  - `discordbot_channel_whitelist` — *refuses to reply* in an unlisted channel. Never
    makes the bot leave anything; a channel isn't something a bot is a "member" of
    independently of its server.
  - `discordbot_guild_whitelist` — makes the bot **leave** any server not on the list,
    via `StatusBot._enforce_guild_whitelist()`, called from both `on_guild_join` and
    `on_ready` (so editing the list to remove a server it's already in takes effect on
    the next reconnect, not just for future invites). This is stronger than the other
    two on purpose: presence/status updates are visible to a server just by the bot
    being in it, regardless of whether anyone there could run the command.
- **Both slash commands share one authorization gate.** `/snapshot` (a short, one-shot
  plain-text reply — down services plus full detail of every open incident, built by
  `build_snapshot_data()`/`build_snapshot_text()`, never tracked/edited like the main
  `/status` message) and the configurable main command both call
  `StatusBot._check_command_authorized()` (enabled-toggle → channel whitelist → user
  allow-list, in that order). **If you add a third command, call this helper** rather
  than re-inlining the checks. `_register_command()` guards against an admin naming
  their configurable command literally `snapshot` (falls back to `status`), since that
  would collide with the fixed command name at registration time and crash startup.
- **`build_snapshot_text()`'s formatting is a requested fix, not incidental**: full
  per-incident detail (not just a count), a bold title line per incident, the rest as
  a `>` blockquote, and a blank line between separate incidents. Keep both the
  multi-incident separation and the blockquote grouping if you touch it
  (`docs/HISTORY.md` → "`build_snapshot_text()` was too vague").
- **`discord_bot._state["guilds"]`** is a read-only snapshot of every server/channel
  the bot is currently in, populated straight from the gateway cache (`self.guilds`/
  `guild.text_channels` — no extra API calls) by `StatusBot._snapshot_guilds()`, called
  from `on_ready`, `on_guild_join`, `on_guild_remove`, and every `_refresh()` tick.
  Backs the `/admin/discord-bot/guilds` page.
- **`stop()`/`restart()` exist so a running connection can actually be commanded to
  close** — `_run()` manages its own `asyncio` event loop explicitly and stashes
  `client`/`loop`/`thread` in a module-level `_runtime` dict *before* the client starts
  connecting, which is what lets `stop()` call
  `asyncio.run_coroutine_threadsafe(client.close(), loop)` from an admin route's
  thread and then `thread.join(timeout)`. `restart()` is just `stop(); start()`,
  relying on `start()`'s already-running guard so it can't race with the old
  connection still shutting down.
- **`_edit_tracked_status_message()` must keep its two exception branches separate.**
  Only `discord.NotFound` (genuinely gone) or `discord.Forbidden` (access revoked)
  may forget the tracked row; every other exception retries up to
  `REFRESH_RETRY_ATTEMPTS` times (`REFRESH_RETRY_DELAY_SECONDS` apart) and, if still
  failing, is logged and left alone — the row stays tracked and the next
  `refresh_loop` tick tries again. **Don't collapse the branches back into one bare
  `except`**: that distinction *is* the fix for a real bug where any transient network
  blip permanently forgot a perfectly healthy message
  (`docs/HISTORY.md` → "a bare `except` that forgot live messages").
- **`on_ready`, `on_resumed` and `on_disconnect` are all three load-bearing for
  `_state["connected"]`.** A resumed session fires only `on_resumed()`, never
  `on_ready()` again — without an `on_resumed()` handler the admin panel gets
  permanently stuck showing "not connected" for a bot that is working fine
  (`docs/HISTORY.md` → "the permanently stuck 'not connected' panel"). `on_resumed()`
  deliberately does *not* redo guild-whitelist enforcement or restart `refresh_loop`
  the way `on_ready()` does, since a resume means the session context didn't change.
  If you add another lifecycle-dependent piece of `_state`, remember it's these three
  events that matter, not just the first two.
- **What is and isn't verified against a real Discord server**: `/snapshot` (and
  therefore slash-command registration and the command handler) is confirmed working
  live. Still unconfirmed for real: the guild whitelist's actual `guild.leave()` call,
  the server/channel management page's gateway-cache snapshot, and the
  restart-survives-message-editing behavior for the tracked `/status` message — all
  unit-tested with mocked Discord objects only. Don't assume "the bot" is confirmed
  working as a whole because one code path is; ask what specifically was tested.
- `discord.py` is installed in this dev sandbox's Python environment for testing even
  though it's not in `requirements.txt`. If it's missing and you need to verify code:
  `pip install discord.py` (23 tests fail with `ModuleNotFoundError` without it).

## Crash logging (`logging_setup.py`)

- `logging_setup.init_logging()` configures Python's standard `logging` module
  (rotating file under `instance/logs/app.log`, same gitignored directory as the DB,
  plus the console) — called once from `app.py`/`serve_waitress.py`'s `__main__`
  blocks, **never at plain import time**, specifically so importing these modules
  under pytest doesn't create log files as a side effect. If you add a third entry
  point, call it there too, in the same spot (before `db.init_db()`).
- Every background-thread error path goes through `logging.getLogger(__name__)` —
  `.exception(...)` inside an `except` block (auto-captures the traceback),
  `.info()`/`.warning()` for non-error diagnostics. Follow this pattern for any new
  diagnostic rather than reintroducing bare `print()`.
- `threading.excepthook` is set to `logging_setup._log_thread_exception` — the safety
  net for a background thread dying from something *outside* its own try/except.
  Without it, a thread dying that way is completely silent: "services just stopped
  updating" with zero trace anywhere.
- **Tests that assert on logged output must use pytest's `caplog` fixture, not
  `capsys`** — `capsys` only captures direct `print()`/stdout writes, and with no
  logging configured during tests a bare `logging.Logger` call goes to *stderr* via
  Python's handler of last resort, which `capsys.readouterr().out` won't see either.
  See `tests/test_monitoring.py`'s `test_vm_snapshot_logs_stderr_on_failure` for the
  pattern.

## Public page layout (`templates/sections/`)

- Each of the 7 public-page content blocks (announcements, services, incidents &
  maintenance, practical info, resources, VMs, Jellyfin activity) is its own partial
  under `templates/sections/<key>.html`, each owning its own "is there anything to
  show" guard. The topbar/status-hero/footer are page chrome, not content, and stay
  hardcoded in `index.html` — deliberately never made reorderable.
- `app._public_section_order()` reads the `public_layout_order` setting
  (comma-separated section keys) and is the *only* place that decides render order —
  `index.html` just does `{% include 'sections/' ~ key ~ '.html' %}` in a loop.
  **If you add an 8th section**, add its key to the `PUBLIC_SECTIONS` list in `app.py`
  (which doubles as the label lookup for the admin reorder UI);
  `_public_section_order()` already appends any valid key missing from a stale stored
  value at the end, so an admin who saved a custom order before your new section
  existed still sees it (just at the bottom, not disappeared).
- **Partial-template naming has two rules, both in play.** `_*.html` at
  `templates/` root = a shared include used by *both* a public section and an admin
  page (`_resource_cards.html`, `_vm_table.html`, `_vm_table_admin.html`).
  `templates/sections/_*.html` = an AJAX fragment returned directly by a route, or its
  per-item partial (`_incidents_fragment.html`, `_incident_item.html`, and the
  maintenance equivalents). Put a new partial wherever its actual role says, and don't
  assume everything under `sections/` is a reorderable block — only the non-underscore
  files are.
- The reorder UI on `/admin/settings` (`admin_layout_order.js`) is a plain
  up/down-button list, not drag-and-drop — deliberate, to stay dependency-free like
  every other admin-side JS file. Don't introduce a drag-and-drop library without
  discussing it first.

## Two-factor authentication (`twofactor.py`)

- TOTP-based, off by default, never required — a single admin can opt in from
  `/admin/2fa`, and the page strongly recommends it (especially given the host power
  controls) without forcing it. `pyotp` and `qrcode` are **required** dependencies
  (`requirements.txt`), not the lazy-imported-optional pattern used for
  `nvidia-ml-py`/`discord.py` — both are small pure-Python packages with no compiled
  extensions, and 2FA is a core always-available feature, not a rare/heavy integration.
- **The QR code is generated fully server-side as inline SVG** via `qrcode`'s
  `SvgPathImage` factory — deliberately avoids a Pillow/image-library dependency and
  any third-party JS QR library (which would need loosening the CSP's `script-src` or
  vendoring a file; this app has no external JS dependencies anywhere).
- **Enrollment secret lives in the session, not the DB, until confirmed.**
  `GET /admin/2fa/enable` puts a freshly generated secret in
  `session["pending_totp_secret"]`; only a `POST` with a *verified* code from that same
  pending secret writes it to `db`. This stops an admin ending up locked out by a
  secret they never got safely into their authenticator app. **Keep the regression test
  for "POST with nothing pending"** — that case crashed with a bare `KeyError` and was
  caught by live testing, not the unit tests
  (`docs/HISTORY.md` → "2FA enrollment `KeyError`").
- **Login becomes two steps when 2FA is enabled, still one step when it isn't.**
  `session["awaiting_totp"]` is set (server-side only, unforgeable without the signed
  session's `SECRET_KEY`) once the password step succeeds; the *next* `POST` to
  `/admin/login` is read as a code instead of a password, without re-checking the
  password. The global login-lockout counter (`_login_state`) applies to wrong codes
  exactly like wrong passwords — deliberately **not** reset just for getting the
  password right, only once the whole two-step login actually succeeds, so a correct
  password doesn't buy an attacker unlimited unthrottled code guesses.
- **Step-up re-authentication** for the destructive actions goes through
  `app._require_totp()` — see the convention bullet above for the call pattern and
  which routes use it. Scoped deliberately narrow (host restart/shutdown, app/bot
  restart, self-update — not VM control, not other admin actions); don't creep it onto
  other routes without discussing it first.
- **Resetting 2FA is a host-level action, not a web one, on purpose.**
  `twofactor.check_and_process_reset_flag()` looks for an empty file at
  `instance/RESET_2FA` on every hit of `/admin/login` (cheap `os.path.exists()`, no
  restart needed) and, if found, wipes the secret, disables 2FA, then deletes the file
  itself (self-cleaning, one-shot). This is deliberately **not** a web UI button or an
  emergency backup code — either would just be another secret reachable purely over the
  web to protect. Creating a file requires actual filesystem access to the host, a
  meaningfully different trust boundary. Routine self-service disabling stays in the
  web UI at `/admin/2fa`, itself gated behind entering a current code — a hijacked
  session alone can't turn 2FA off either.

## Public "report a problem" form (`app.py`)

- **`GET/POST /report` is this app's only public POST route besides `/admin/login`** —
  deliberately separate from the admin-authored incident/maintenance system (a visitor
  telling the admin something looks wrong, not the admin recording a known outage).
  Because it's outside `/admin/`, it is **not** covered by the CSRF `before_request`
  hook — fine here (no authenticated session/privilege is being exercised, so a
  cross-site submission achieves nothing an attacker couldn't do by POSTing directly),
  but it does mean this route needs its *own* anti-abuse, not "just add CSRF".
- **Three independent anti-abuse layers, all process-global (not per-IP), same
  reasoning as `_login_state`**: a honeypot field (`website`, hidden via CSS
  positioning, not `display:none`/`type=hidden` which some bots skip — a filled
  honeypot silently "succeeds" without writing a row, so a bot gets no signal it was
  caught), a minimum-time-to-fill check (`session["report_form_rendered_at"]` vs
  `REPORT_MIN_SECONDS_TO_FILL`), and a rate limit
  (`_report_state`/`_report_rate_limited()`, mirroring `_login_state`'s shape). No
  external rate-limiting library was added for this one route.
- `problem_reports.service_id` is optional (a report can reference a specific service's
  card via `?service_id=N`, or be general) and `ON DELETE SET NULL`, so deleting a
  service later detaches rather than cascade-deletes reports about it.
- **The admin nav's unread-count badge comes from a `context_processor`**
  (`app._inject_admin_badges()`), not threaded through every admin route by hand —
  same reasoning as `csrf_token()` being a Jinja global. Scoped to
  `request.path.startswith("/admin/")` so the `COUNT(*)` never runs on a public page.
- **"Create incident from this report"** pre-fills a new incident's title/description
  and marks the report resolved, then redirects to the incident's *edit* page rather
  than silently doing everything — the admin still gets to adjust before it goes live.
- **Two different definitions of "open", don't conflate them.**
  `db.count_open_reports_by_service()` (the public per-card indicator) counts anything
  not yet `resolved` — i.e. `new` *or* `reviewed`. The admin nav badge's
  `count_unread_problem_reports()` counts only `new`. Both are correct for their spot;
  don't reuse one function for both.
- **Only a count is shown publicly, never the report's message/contact text** — a
  visitor-submitted free-text field is not something to echo back onto the public page.
  General reports (`service_id IS NULL`) aren't attributable to any one card and are
  excluded from the counts entirely, not folded into every service's total. The counts
  come from one grouped query wired into `_enrich_services()` (shared by `index()` and
  `api_status()`), not N+1 per-card queries.
- **`services.show_report_button` is cosmetic only, by design.** It hides the "Report
  ⚑" link on that one service's card (`sections/services.html`) and deliberately does
  **not** touch the `/report` route — a visitor who already has (or guesses)
  `/report?service_id=N` can still submit. That was a conscious scope call (asked for
  as "hide the button", not "block reporting"); if per-service access control is ever
  wanted it needs its own check inside `report_problem()`. No global default setting
  was added either — purely per-service.

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
  it. Same typed-confirmation UI pattern too (`static/js/admin_system_control.js`,
  mirroring `admin_host_control.js` — one confirm panel driving both trigger buttons
  via a `data-component` attribute instead of `data-action`).
- **Never live-invoke `_restart_process()` for real in this sandbox or any shared
  environment** — same rule as `monitoring.control_host()`. Verify exclusively by
  mocking `app._restart_process()`/`discord_bot.restart()` in pytest and asserting the
  route called them; a live smoke test should exit the confirm-panel flow right before
  actually submitting, not click through it.

## Custom logo / branding (`app.py`, `static/uploads/`)

- **This app's only file upload.** Kept deliberately narrow to avoid a broad upload
  surface: the saved file is always named exactly `logo.<ext>` (never the
  admin-supplied original filename) under a dedicated `static/uploads/` directory,
  extension whitelisted against `LOGO_ALLOWED_EXTENSIONS`, and `MAX_CONTENT_LENGTH`
  (2MB app-wide) caps the size. There's no path-traversal surface and at most one logo
  file ever exists on disk. `static/uploads/` is gitignored like `instance/` — runtime
  state, not tracked content.
- **`app._inject_branding()` (a `context_processor`) exposes
  `site_logo_filename`/`site_logo_version` to every template**, admin included, so both
  the public topbar and `base.html`'s `<head>` (favicon `<link>`) can use it without
  every route threading it through. `site_logo_version` is a cache-busting `?v=<mtime>`
  from a local `os.path.getmtime()` — not the kind of slow I/O the request-handler rule
  is about.
- **A re-upload in a different format removes the old file first**
  (`admin_settings_logo()` diffs the new filename against the stored setting before
  saving) — otherwise switching from a `.png` to an `.svg` would orphan the old file
  forever, since the path changes with the extension.
- `@app.errorhandler(413)` exists because of this feature — `MAX_CONTENT_LENGTH` was
  set app-wide long before anything could actually hit it. Without the handler an
  oversized upload falls through to Flask's default unstyled error page.

## Testing/verification habits (established over many sessions — keep following them)

- Run the full `pytest` suite *and* a live `python app.py` + `curl` smoke test of
  whatever routes actually changed before calling something done. Several real bugs
  (the integration-blocking page load, the maintenance-window timing bug, the 2FA
  enrollment `KeyError`) were only ever caught by actually running the server, never by
  unit tests alone.
- **For anything that depends on client-side JS (AJAX "load more" buttons, the
  favicon/logo actually rendering, console errors), `curl` alone isn't enough — use the
  pre-installed Playwright/Chromium browser** (see the environment notes on
  `PLAYWRIGHT_BROWSERS_PATH`/`executablePath` — don't run `playwright install`) to
  actually click through the flow and check for console errors. This caught a real bug:
  `report_problem()` never passed `site_name` to `report.html`, silently leaving the
  topbar brand text and page title blank — every route-level pytest test for that route
  only checked for form-field presence, and `curl` output looked fine since the HTML was
  syntactically valid, just missing content. (Red herring to ignore along the way: an
  `ERR_CONNECTION_RESET` console error from the sandbox having no egress to
  `fonts.googleapis.com` — unrelated to app code.)
- **When curl-smoke-testing a multi-request flow that depends on session state** (login
  steps, flash messages, anything the server writes back via `Set-Cookie`), every
  request needs *both* `-b cookiejar` (send) *and* `-c cookiejar` (save the response's
  possibly-updated cookie). `-b` alone silently reads a stale cookie and looks exactly
  like a real bug (a missing flash message, a "lost" session value).
- This sandbox is Linux. Hyper-V VM detection, Windows volume labels, CPU/disk
  temperature and per-disk I/O (all Windows-only, PowerShell/CIM-backed), real
  Jellyfin/*Arr/Jellyseerr instances, and a real Discord gateway connection can't be
  fully exercised here. Say so explicitly rather than implying full verification — and
  if the user reports a bug in one of these areas, ask for the actual error text first
  (most of these paths now log real errors instead of swallowing them) rather than
  guessing blind.
- Clean up after smoke testing: remove any `instance/portal.db` created during a test
  run, and any cookie jars, before finishing — don't leave a test admin password or
  fake data sitting in what could become the user's real database.
- **Never live-invoke anything that shells out to actually restart/shut down a machine
  (`monitoring.control_host()`), even in this sandbox, even just to "see what
  happens."** Verify exclusively via a mocked `subprocess.run` in pytest.
- **A `/code-review`-style security pass over a session's accumulated diff is worth
  running before a release that adds any new admin-facing control surface** — it caught
  a real XSS bug that manual review and the existing test suite both missed
  (`docs/HISTORY.md` → "VM-name XSS"). Doesn't replace live smoke testing; catches a
  different class of issue.

## Release process

**Trigger**: the user says the session/work is done *and* that things are working —
e.g. "this session ends here", "that's it for today, everything works", "we're done,
thanks". Not every "looks good" or "great, that works" mid-session — those are just
confirmation of one change. When genuinely unsure whether a message means "wrap up the
whole session" vs. "this one thing is fine, keep going", ask rather than guessing — a
release is a public, visible action.

Once the trigger fires: this is standing, pre-authorized (per the same blanket
authorization covering commit/push/shell commands for this project, granted
2026-07-22) — don't ask for confirmation again each time, just run the steps below.

**Mid-session checkpoint, not just end-of-session**: don't wait only for the full
"session's done, everything works" trigger. Whenever a *complete chunk of requested
work* finishes — a whole batch of asks handed over together, even if more requests come
later in the same session — proactively tell the user specifically what to test and
how, and cut a **pre-release** (`-rc.N`, marked prerelease on GitHub) covering that
chunk. This is deliberately coarser than "after every individual step": a batch of many
related asks worked in one pass is one chunk, not one release per item (confirmed with
the user on a 16-item batch, where the agreed cadence was one pre-release at the end of
the whole batch). The stable-release trigger above still governs promoting a
pre-release or cutting a fresh full release.

0. **Bump `VERSION` first — a required step, not a formality.** The `VERSION` file at
   the repo root must contain the exact version being released, *without* the leading
   `v` (`1.5.0`, `1.5.0-rc.1`), committed before the tag is created, so the tagged
   commit — and therefore the `git archive` zip built from it — carries the right
   number. Nothing derives this automatically. Getting it wrong is not cosmetic now
   that self-update exists: `updater.py` compares the running `VERSION` against release
   tags, so a zip shipping a stale value makes an installed portal either believe it's
   already up to date (and refuse a real update) or offer an update it already has.
   Sanity check after tagging: `git show vX.Y.Z:VERSION` must equal `X.Y.Z`.
1. **Versioning**: `vMAJOR.MINOR.PATCH`, with an optional `-rc.N` suffix for anything
   not yet user-verified end-to-end (mark the GitHub release as a pre-release too).
   First tagged release was `v1.0.0` (2026-07-22). Bump MINOR for new features, PATCH
   for bug-fix-only changes, MAJOR only for an actual breaking change (should be rare —
   the whole `_ensure_column` schema policy exists to avoid needing these).
2. **Changelog**: build from `git log <previous-tag>..HEAD --oneline`, grouped
   informally into Added / Fixed / Changed, written as the release body. Keep it
   readable — a person, not a machine, reads this.
3. **Asset**: `git archive --format=zip -o status-portal-vX.Y.Z.zip HEAD` (or the new
   tag once created). `git archive` only includes tracked files, so it's already clean —
   no `.git`, no `instance/portal.db`, no `.env`, no `__pycache__` — with no manual
   exclusion list to maintain. **The zip must always be attached to the release**:
   `updater.py` prefers a `.zip` asset and only falls back to GitHub's auto-generated
   zipball, which publishes neither a size nor a digest, so a release without the asset
   silently downgrades every updater's integrity check to TLS-plus-tag-pinning alone.
4. **Publish**:
   ```
   git tag vX.Y.Z
   git push origin vX.Y.Z
   gh release create vX.Y.Z status-portal-vX.Y.Z.zip --title "vX.Y.Z" --notes "<changelog>" [--prerelease]
   ```
   **If this step fails outright — no `gh` CLI, no direct GitHub API access, or
   `git push origin vX.Y.Z` itself rejected:** this is normal for a session running in
   Anthropic's cloud infrastructure rather than on the user's own machine. Those
   sessions' git credentials are scoped to push *branches* (confirmed: a brand-new
   branch name succeeds) but not to create *tags* (confirmed: `git push origin vX.Y.Z`
   gets a `403` — tags sit outside the allowed ref pattern), and the GitHub MCP toolset
   available there may have no release-creation tool at all (only read tools like
   `list_releases`/`get_latest_release`/`get_tag` — check via `ToolSearch` before
   concluding this). Don't just report failure and stop — fall back to: build the zip
   anyway (`git archive` works locally, no push needed), push a **branch**, and hand
   the user everything they need to finish the last step themselves: exact tag name,
   target commit/branch, title, changelog body, prerelease flag, and the zip itself
   (via `SendUserFile`). A locally-run session usually has full `git`/`gh` access and
   can complete every step directly — this fallback is for when it can't, not a
   replacement for trying the real steps first.
