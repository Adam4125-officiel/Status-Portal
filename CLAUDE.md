# Notes for future Claude Code sessions on this repo

This file is for context that survives between sessions — architecture decisions,
gotchas, and standing workflows that aren't obvious from reading the code cold. Keep
it updated as the project evolves; don't let it go stale.

## Read this part even if you read nothing else

This file is long. If you are working fast, or you are a smaller/faster model, read
*this* section and the lookup table below it, then jump straight to the section for
whatever you're touching. Do not skim the whole file — that's how rules get missed.

**The ten that have actually been broken before:**

1. New column on an existing table → add `_ensure_column()` in `init_db()`. `CREATE
   TABLE IF NOT EXISTS` is a no-op on a database that already exists.
2. New index → `CREATE INDEX IF NOT EXISTS` in `init_db()`'s list, same reason.
3. New `PORTAL_*` env var → **three files**: `config.py`, `.env.example`,
   `docker-compose.yml`. Missing from compose = silently ignored under Docker.
4. Never call slow/external I/O in a request handler. Background thread writes a
   cache; handlers only read it.
5. CSS/JS in a template → **always** `asset_url()`, never bare `url_for('static')`.
6. `_handle_incident_lifecycle`'s open branch is **level-triggered**. Never add a
   `previous_status != "down"` check.
7. Any status enumeration needs a `slow` entry, not just the four obvious ones.
8. New per-service field → **three places**: main form, combined wizard, Service
   defaults in Settings.
9. New module-level cache → reset it in `tests/conftest.py`, and clear it in that
   module's `clear_caches()`.
10. Entity picker → checkbox list, never `<select multiple>`.
11. `session["logged_in"]` is set in **one** function (`_start_admin_session`). The
    Jellyfin visitor session is a separate identity and must never write it.
12. A recurring job is a `scheduler.register()` call, not a fourth `while True` thread.

**Most of those are enforced by `tests/test_conventions.py`.** It runs in a fraction
of a second and its failure messages name the file and the fix. Run it early rather
than relying on having remembered the rule:

```
python -m pytest tests/test_conventions.py -q
```

A failure there is a real violation of a rule that cost someone a bug once — fix the
code, don't loosen the test. If you genuinely need an exception, add it explicitly
(with the reason) to the check itself, so the next person sees the decision.

**And the one habit that matters most:** run the full suite *and* the actual server
before calling anything done, and say plainly what you did and didn't verify. Most
bugs in `docs/HISTORY.md` passed their unit tests.

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

## If you're touching X, read Y first

This file is long enough that the real failure mode is not "the rule wasn't written
down", it's "the rule was written down three hundred lines away from what I was
editing". Look your change up here before you start; each row names the sections that
have bitten someone on exactly that change.

| What you're about to touch | Read before you start |
|---|---|
| Any DB schema change | *Conventions* → `_ensure_column()`; *Sessions/caching* → indexes go in `init_db()`'s list |
| A new per-service field | *Conventions* → "three places, not one" (main form, wizard, Service defaults) |
| A new setting of any kind | *Conventions* → the config split (env var vs DB row), and "a new `PORTAL_*` env var means three files" |
| Anything in `run_health_checks()` or a status value | *Conventions* → level-triggered `_handle_incident_lifecycle`, the `slow` tier, `_merge_api_health`/`_merge_dependency_health` |
| Anything a request handler calls | *Conventions* → no slow I/O in a request handler; *Monitoring architecture*; *Sessions/caching* |
| Login, sessions, cookies, CSRF | *Sessions, caching and DB performance*; *Two-factor authentication* |
| A new admin route | *Conventions* → CSRF is automatic under `/admin/`; `_require_totp()` for destructive actions only |
| A template's CSS/JS reference | *Conventions* → always `asset_url()`; *Sessions/caching* → `SEND_FILE_MAX_AGE_DEFAULT` is 30 days |
| Anything rendering a timestamp | *Conventions* → server-side UTC + `local-time` spans; *Sessions/caching* → ISO strings only |
| A new cache of any kind | *Sessions/caching* → module-owned `clear_caches()`, and reset it in `tests/conftest.py` |
| Pagination / "load more" | *Conventions* → `/api/incidents/more`, and `docs/HISTORY.md` → "four pagination bugs in sequence" |
| A new integration kind | *Conventions* → the `fetch_integration_status()` dispatch dict; per-integration timeouts |
| Comparing a version of anything | *Version checks* → `updater.parse_version` is 3-component semver; Servarr is 4-component |
| The Discord bot | *Discord bot* — the whole section, it is all load-bearing |
| The updater | *Self-update* — especially what rollback can and cannot do |
| Anything that shells out to the OS | *Conventions* → never live-invoke `control_host()`; `_restart_process()` has its own, narrower rule |
| Anything that ends in a restart | *Testing/verification habits* → mocks can't tell you the process came back; exercise it live once |
| The database restore | *Restoring the database* → the order is the safety machinery |
| A public-page section | *Public page layout* → add the key to `PUBLIC_SECTIONS` |
| Calling something "done" | *Testing/verification habits* — pytest alone has missed real bugs repeatedly |
| A rule you half-remember | `tests/test_conventions.py` — the checkable ones are enforced there, with the reasoning in each docstring |
| A new recurring background job | *Scheduled tasks* → register it, don't write another loop |
| A new admin page, or moving one in the nav | *Admin panel navigation* → pick a group, keep the route, three labels must agree |
| Anything touching visitor sign-in or `portal_user` | *Jellyfin-backed user accounts* — the whole section; the admin/visitor split is load-bearing |
| A new public (non-`/admin/`) POST route | *Conventions* → `_csrf_required_for()`; the exemption is a decision, not a default |
| Anything touching the theme | *The user account page* → three inputs, two implementations of the precedence; they must agree |
| Starting a multi-part batch of work | *Commit cadence* — one commit per completed fix, never one at the end |

Three things that are true no matter what you're touching:

1. **Run the full suite *and* the real server.** Most of the bugs recorded in
   `docs/HISTORY.md` passed their unit tests.
2. **Say what you actually verified.** This sandbox is Linux with no Windows, no real
   Jellyfin/*Arr, and no Discord gateway — "confirmed working" has to mean something
   narrower here, and saying which part is guesswork is more useful than a clean
   summary.
3. **The user tests things and finds what you missed.** Several rules in this file
   exist because they re-tested something and caught a gap the same session it
   shipped. Make what you changed easy for them to check.

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
- **Any new per-service field needs to be checked against three places, not one:**
  the main form (`admin_service_form.html`), the combined wizard
  (`admin_new_combined.html`'s `<details>` "Advanced settings" block), and — if the
  field is the kind of thing that's usually the same across most services (a
  threshold, a mode, a boolean toggle; not a unique value like `name`/`url`) —
  `service_default_*` in Settings → Service defaults (`_service_defaults()` +
  `admin_settings_general()`'s POST handler + the "Service defaults" section of
  `admin_settings.html`). Missing the wizard was a real bug once already
  (`docs/HISTORY.md` → "The combined wizard's missing field set" — `service_default_*`
  settings silently unreachable, `create_service()` falling back to hardcoded
  literals) and **missing the same two spots happened again** the very next session,
  this time for `run_target`/`show_run_target_public`/`show_dependencies_public` —
  caught by the user re-testing, not by anything in this file, which is why this
  bullet now names all three places explicitly instead of just two. When adding a
  field, grep for an existing sibling field (e.g. `slow_threshold_ms` or
  `api_health_mode`) and touch every spot that name appears in — that's the fastest
  way to find all three without relying on memory. The wizard's extra fields live in
  a collapsed-by-default `<details>` block; **collapsed is a CSS/visual state
  only — the inputs are still in the DOM and still submit**, which is why
  server-side pre-filling is enough on its own and no "open the advanced section"
  JS handler is needed. Naming gotcha: the service's own `auto_incident` checkbox
  and the integration's *different* `auto_incident` concept can't share one HTML
  name on one form, so the integration's is deliberately `check_auto_incident` in
  the template and mapped explicitly in the route.
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
- **Release notes come from the same response, not a second request.**
  `fetch_releases()` returns every release on the channel sorted by *parsed version*
  (never publish date - republishing an old release must not reorder the changelog);
  `fetch_latest_release()` is now just its first element. `check_for_update()` carries
  `release_notes` (everything strictly newer than what's running, newest first),
  `release_notes_omitted` and `current_notes` (the running version's own entry) into
  the cache the About page reads.
- **A release `body` is untrusted network input and must only ever be rendered through
  `richtext`.** It escapes first and then permits a deliberately tiny subset (bold,
  links, line breaks), so Markdown headings and bullets show as the literal characters
  the release author typed. That is the accepted trade - never swap in a real Markdown
  renderer here without one that is escaping-safe by construction, and never mark a
  body `|safe`. The CSS deliberately has **no `white-space: pre-wrap`**: `richtext`
  already turns every newline into a `<br>`, so preserving the newlines too would
  double every line break.
- **`MAX_RELEASE_NOTES` (20) caps how many intervening releases are rendered**, with
  the remainder reported as a count rather than silently dropped - a portal left
  un-updated for a long time must not render, or cache, a page of unbounded length.
- **A failed check must still leave the note fields present and empty.** The About
  page reads them unconditionally, so a network failure has to degrade to "couldn't
  check", not to a template error.
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

## Servarr version checks (`version_checks.py`, `/admin/integrations`)

- **`version_checks.parse_version()` is deliberately not `updater.parse_version()`, and
  this is the trap.** Servarr versions have **four** numeric components
  (`6.4.1.10545`) and the fourth - the build number - is the one that actually moves
  between releases. `updater.parse_version` keeps three and spends its fourth slot on a
  prerelease rank, so it parses `6.4.0.10540` and `6.4.0.10523` as the *identical*
  tuple and would report a months-old Radarr as up to date. There is a test asserting
  exactly that. The portal's own tags are semver with `-rc.N` and genuinely need the
  other function; these two formats must not share a parser.
- **Which app an integration is comes from the app's own `appName`**, never from the
  name the admin typed. `movies-4k` is still Radarr and something called `radarr`
  might not be; guessing from the label eventually checks the wrong project's releases
  and reports the wrong answer confidently.
- **`KNOWN_APPS` is a module constant and must stay one** - same argument as
  `updater.py`'s repo constant. Adding an app is a line there *plus* a check that its
  release tags really are plain version numbers: a project tagging releases
  `2024.10-hotfix` parses as `0.0.0` and would silently report "up to date" forever.
- **Results are persisted to the `settings` table, not held in a module cache.** This
  is a daily task, so an in-memory result would mean the admin page saying "not checked
  yet" for up to a day after every restart.
- **A run where *every* app failed is recorded as a failure, not a success** - it
  answered no question at all, and showing green for it hides the most likely cause.
  Per-app results are stored *before* that failure is raised, so the page still
  explains what went wrong instead of going blank. (Found by live-testing against a
  genuinely rate-limited GitHub, not by the unit tests.)
- **GitHub's unauthenticated API allows 60 requests/hour per IP, shared across
  everything this portal asks it** - the self-update check and these three. Fine at
  daily/6-hourly cadence; worth remembering before adding a fourth caller or shortening
  an interval.

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

## Discord DMs and Seerr approval alerts (`seerr_alerts.py`, `/admin/notifications/seerr`)

- **`seerr_alerts.py` exists because of the import graph, not by preference.** It needs
  `integrations` (to ask Seerr) and `discord_bot` (to deliver), and `discord_bot`
  already imports `integrations` — so it can't live in either without a cycle. A thin
  layer above both is the shape that works, and it's where the next "watch something,
  tell someone" job belongs too.
- **`discord_bot.send_dm()` is a different code path from everything else the bot
  sends.** Replies and message edits happen *inside* the bot's event loop; a DM is
  initiated from a scheduled task's thread, so it uses the same
  `asyncio.run_coroutine_threadsafe` bridge `stop()` does. `get_user()` then
  `fetch_user()`, cache-then-fetch, for the same reason `_edit_tracked_status_message()`
  does it: a cold gateway cache right after a restart otherwise looks identical to "no
  such user".
- **`dm_user_ids` is default-*closed*, unlike the bot's other three ID lists.** Those
  decide who may *ask* the bot for something already visible in the channel they asked
  from; this decides who it messages unprompted. Empty means nobody.
- **A bot can only DM someone who shares a server with it and allows DMs from server
  members.** This is Discord's rule and cannot be worked around. A valid user ID can be
  undeliverable; `send_dm()` reports that case explicitly (`Forbidden`) instead of as a
  generic failure, because the fix is a human action nobody would guess otherwise. Say
  this plainly in any UI that collects DM recipients.
- **The alert is edge-triggered and its state is persisted** (`seerr_notified_request_ids`
  in `settings`, capped at `MAX_REMEMBERED_IDS`). Same rule as the low-disk alert: a
  restart while requests are still pending must not re-announce all of them.
- **A request is only marked announced once a DM actually reached somebody.** A
  disconnected bot therefore means a *delayed* alert, not a swallowed one.
- **While DMs are off, what's pending is still remembered** — so switching them on
  announces what arrives next rather than emptying the whole existing queue into
  someone's inbox at once.
- **A failed poll must leave the stored count alone.** Overwriting a real count with 0
  would quietly tell the admin their approval queue was empty.
- **The count is admin-only, deliberately.** It's operational information about a
  queue, not a signal about whether anything is working, so it never reaches the public
  page.

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

## Media activity (`integrations.py`, `templates/sections/media.html`)

- **Four read-only views, one cache, one scheduled task** (`media_refresh`, 5 min):
  the Radarr/Sonarr calendar, Jellyseerr requests, qBittorrent downloads, and
  Prowlarr's per-indexer health. Request handlers only ever read
  `integrations.get_cached_media()`, which returns **copies** so a template iterating a
  list can't be tripped by the task replacing it mid-render.
- **Every source is independent.** One unreachable app leaves the other three showing
  real data, with its own entry in the cache's `errors` dict. Don't "simplify" this
  into a single try/except around the whole refresh.
- **A 404 from an *Arr app means "this app doesn't have that feature", not a failure.**
  Every `arr` integration is asked for both a calendar and an indexer list because
  which app it is isn't known here; Radarr/Sonarr answer the first and 404 the second,
  Prowlarr the reverse. Recording those 404s as errors made a perfectly healthy setup
  report "1 source(s) failed" (caught live). Anything *other* than a 404 is still
  reported, so a genuinely broken Prowlarr doesn't quietly show an empty list.
- **Which *Arr app a calendar entry came from is decided per item, not per app** — a
  Sonarr entry carries a `series` object, a Radarr entry doesn't. One request, and it
  can't get the answer wrong the way guessing from the integration's name could.
- **qBittorrent is the only integration that logs in rather than presenting a key**, so
  it has its own `username`/`password` columns and `fetch_integration_status()` routes
  it differently. `Referer` is **required** on the login POST — qBittorrent rejects it
  outright without one, as a CSRF defence for its own WebUI. The SID is deliberately
  not cached across calls: expiry, invalidation on a password change and a stale cookie
  looking exactly like wrong credentials are all real complications, for a request made
  once every few minutes from a background task. A blank username is a valid
  configuration ("Bypass authentication for clients on localhost"), not an error.
- **The blank-secret-means-keep rule now covers the qBittorrent password too**, not
  just `api_key` — forgetting it would silently break the integration on every
  unrelated edit (a rename, a URL change).
- **All four parts default to OFF, and the section is signed-in-only by default.**
  Unlike the resource cards, this says what is in (or heading into) the library and who
  asked for it. `media_requires_login` only *means* anything while Jellyfin sign-in is
  enabled — with no sign-in configured there is no such thing as a signed-in visitor,
  so enforcing it would hide the section from everybody rather than restrict it. Same
  shape and same reasoning as `report_requires_login`.
- **`eta == 8640000` is qBittorrent's "unknown"**, not 100 days. Rendering it literally
  gives a nonsense countdown.

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

## Restoring the database (`/admin/about/restore-db`, `db.py`)

- **The order of operations is the safety machinery and is not rearrangeable**:
  stage the upload to a temp file (live database untouched) → validate → snapshot the
  *current* database → atomic replace → restart. A refusal at any point must leave the
  live database byte-identical and take no snapshot; there are tests asserting exactly
  that for junk files, foreign databases, bad zips and zip bombs.
- **Validation is three checks, and the third is the one people forget.** The SQLite
  header, then `PRAGMA integrity_check`, then **`RESTORE_REQUIRED_TABLES` must all be
  present**. The first two only prove "a valid SQLite database" — which a Jellyfin
  library, a browser cookie store or an *Arr database all also are, and restoring one
  of those silently wipes the portal and leaves it unable to start. Validation opens
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
  the public report form included, a 64 MB body allowance. The hook that sets it
  **must be registered before `_check_csrf`**, because `_check_csrf` reads
  `request.form`, which parses the body and would 413 a perfectly good upload before
  the view ever runs.
- **`_prune_db_safety_backups()` reads `KEEP_DB_SAFETY_BACKUPS` inside the function**,
  not as a default argument — a default arg binds at def time and silently ignores a
  monkeypatched constant, the same trap that made `updater._prune_backups` pass for the
  wrong reason once.

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

## Admin quick wins, service→VM/host mapping, service dependencies, low disk alert (`app.py`, `db.py`, `monitoring.py` — added 2026-08-12)

- **Test-notification button** (`/admin/settings`) calls `notifications.notify()`
  directly with a canned payload — the exact same dispatch path as a real
  incident/maintenance alert, not a separate code path, so it actually confirms
  the whole chain (webhook URL, ntfy topic, bot permissions) rather than just "is
  this URL reachable." Since `notifications.notify()` is deliberately
  fire-and-forget (catches and logs every failure, never raises), the route can't
  report true success/failure per channel — it just confirms it was sent, and the
  admin checks their channel, which is the entire point of the button.
- **Manual DB backup** (`GET /admin/settings/backup-db` — GET, not POST, since it
  has no side effects) zips a snapshot of `instance/portal.db` via
  `db.backup_to_file()`, which uses SQLite's own `Connection.backup()` API rather
  than a raw file copy — a plain copy of a live database file being actively
  written by the background health-check thread could catch a torn/partial write
  mid-transaction; `Connection.backup()` can't. Deliberately kept separate from
  `updater.py`'s own pre-update backup machinery, which backs up app *code* for
  rollback — a different concern from an on-demand DB export.
- **Dark mode now defaults to `prefers-color-scheme`** when no explicit choice has
  been saved yet (previously always defaulted to dark regardless of the OS
  setting). Both `static/js/theme.js` *and* the FOUC-prevention inline script in
  `base.html`'s `<head>` needed the same fix — missing the inline one would have
  left a light-OS visitor with no saved choice flashing dark before `theme.js`
  corrected it a moment later. An explicit toggle click still writes to
  `localStorage` and wins forever after that, in both places.
- **`services.run_target`** (`''` / `'host'` / `'vm:<name>'`) records which
  machine a service actually runs on — the portal's own host (`platform.node()`)
  or a detected Hyper-V VM by name (no stable numeric VM id exists to reference
  instead — see `monitoring.get_vm_snapshot()`). Shown on the admin service
  list/edit form always; shown on the **public** card only if
  `services.show_run_target_public` is also checked (off by default — this is
  the one piece of this batch that can reveal infrastructure detail to
  visitors, so it's opt-in per service, not on by default like the rest of the
  admin-only fields it started as). **Does not affect this service's computed
  status either way** — purely informational, for faster troubleshooting ("this
  service is down, which machine do I go check").
- **Service dependencies** (`service_dependencies` join table) let a service be
  marked as depending on one or more others; if any of them is down, the
  dependent shows `degraded` instead of lying (`operational`) or falsely showing
  `down`. `_merge_dependency_health(status, dependency_statuses)` mirrors
  `_merge_api_health()`'s exact shape and is called in the same spot in
  `run_health_checks()` — same non-destructive precedence (only ever raises
  `operational`/`slow` to `degraded`, never overrides an already-worse
  `down`/`maintenance`), preserving the "one status feeds both display and
  incident lifecycle" invariant documented above under
  `_handle_incident_lifecycle`. **Direct dependencies only, not transitive** — a
  deliberate scope line: resolving a chain would need real cycle detection and
  topological ordering that nothing here needs yet, and a one-hop-only merge
  can't infinite-loop even if two services are configured to depend on each
  other, so only literal self-dependency is filtered out (in
  `db.set_service_dependencies()`), not full cycle detection. Dependency status
  is read from a **fixed pre-loop snapshot** (`status_by_id`, built once at the
  top of `run_health_checks()`), not a live re-query mid-loop — otherwise whether
  a dependency's fresh-this-cycle status is visible would silently depend on
  iteration order (already processed vs. not yet, within the same cycle). Caps
  dependency staleness at one check interval, same as everything else here
  already tolerates. **Deliberately kept separate from the VM/host mapping
  above** — asked and confirmed 2026-08-12: a service's mapped VM/host being off
  does *not* cascade into that service's status. Linking them would need reading
  live Hyper-V VM state, unverifiable in this sandbox beyond mocked tests, and
  was scoped out as a bigger feature for later if it turns out to actually be
  wanted, rather than bundled in here. Same opt-in-per-service public visibility
  as run_target above: `services.show_dependencies_public` (off by default)
  shows "Depends on: X, Y" on the public card, resolved from
  `db.get_service_dependencies()` in `_enrich_services()`.
- **Low disk space alert** (`monitoring.evaluate_low_disk()` +
  `app._check_low_disk_space()`) extends the already-cross-platform per-disk
  `percent`/`free_gb` (`_get_disk_snapshots()`) with an admin threshold, same
  settings-page shape as the high-load thresholds — but deliberately a
  **separate** check, not folded into `evaluate_high_load()`, since a disk can be
  completely idle and still be nearly full, a different failure mode from I/O
  throughput. **Notification-only, no incident** — incidents are inherently tied
  to a service via the join table, and disk space isn't a service; a
  "serviceless incident" concept wasn't worth inventing just for this.
  **Edge-triggered**, not every cycle: fires once on crossing the threshold and
  once on recovery, never repeatedly while it stays low — a raw "still low"
  notification every `CHECK_INTERVAL_SECONDS` would spam forever. **State is
  persisted via `db.get/set_low_disk_alert_state()`** (the `settings` table,
  namespaced `lowdisk_alert_state:<path>`), *not* an in-process dict — the first
  version of this feature used a module-level cache and was corrected the same
  day: this app is meant to survive its own restart cleanly (see the top-level
  convention on this), and a restart while a disk is still low must not
  re-send the notification just because the process forgot it was already low.
  Confirmed against a real running server: threshold set low enough to trip on
  this sandbox's actual disk usage, a real HTTP webhook receiver standing in for
  Discord, `python app.py` killed and restarted against the same database — the
  notification fired exactly once per disk across both runs, never a second
  time after restart.
- **`_enrich_services()`** (used by both `index()` and `/api/status`, so the JSON
  API gets these fields too, same as every other enriched field it already
  returns) computes `run_target_label`/`dependency_names` only when the
  corresponding `show_*_public` flag is set — the raw `run_target` column and
  `service_dependencies` rows are otherwise never resolved into public-facing
  values, so nothing leaks for a service that hasn't opted in. `_run_target_label()`
  is a small standalone helper (`app.py`) mirroring the inline Jinja logic already
  in `admin_services.html` — not deduplicated into one shared place since one side
  is Python and the other is a template, and the duplication is two short
  `if`/`elif` branches.
- **`run_target`/`show_run_target_public`/`show_dependencies_public` also needed
  `service_default_*` settings and wizard fields**, added in a same-day follow-up
  after the user caught the gap by testing rather than by anything catching it
  first — see the "Any new per-service field needs to be checked against three
  places" convention bullet above, which this incident is why exists in its
  current (three-place, not two) form.
- **Verification status**: full pytest suite (390 passing) plus live
  `python app.py` + `curl` smoke tests of every route above (login, settings save,
  backup download producing a real openable SQLite file, service create/edit with
  `run_target` and `depends_on`, dependency cascade confirmed by hand-computing
  the merge against a live DB, public-page rendering of "Runs on"/"Depends on"
  confirmed present only on a service that opted in and absent on one that
  didn't, `service_default_run_target`/`show_run_target_public`/
  `show_dependencies_public` confirmed pre-filling on both the plain "New service"
  form and the wizard). The low-disk restart fix specifically was verified
  against a real running server (not just pytest) — see above. VM mapping's
  dropdown is untestable beyond "renders correctly and defaults to empty" in
  this Linux sandbox — no real Hyper-V host to detect VMs from.
- **Workflow note**: this batch was pushed to a feature branch and opened as a PR
  rather than committed straight to `main`, specifically because it was
  user-requested as untested/pre-release (touches core status-computation logic
  — `run_health_checks()`, `_handle_incident_lifecycle`'s invariant — not just
  additive UI). Treat "push a branch + PR instead of main" as the default for a
  batch like this (new status-computation logic, not yet run against a real
  install) unless told otherwise, even outside the cloud-session no-tag-access
  fallback already documented under Release process below.

## Sessions, caching and DB performance (added 2026-08-19, v1.6.1)

- **`config.SECRET_KEY` must stay stable across restarts.** It used to fall back to
  `os.urandom()` per process when `PORTAL_SECRET_KEY` was unset, which invalidates
  every session cookie on every restart - and this app restarts routinely (the
  in-app updater re-execs itself, `/admin/system` has a restart button, systemd
  restarts on failure). That was the "a refresh randomly logs me out" bug.
  `_load_or_create_secret_key()` now persists a generated key to
  `instance/secret_key` at mode 0600. **Don't reintroduce a random default**, and
  don't move the file out of `instance/` - that path is gitignored *and* is the
  docker-compose volume mount, which is what makes the key survive a container
  recreate too. A read-only filesystem degrades to a per-process key rather than
  crashing (config.py is imported before logging is configured, so that path can't
  log - it's deliberately silent).
- **Admin sessions are permanent cookies with a server-side idle timeout, not
  browser-session cookies.** Flask's default (no `session.permanent`) emits a cookie
  that dies when the browser closes - so a desktop got logged out on every browser
  restart while a phone whose browser is never closed stayed logged in forever. That
  asymmetry *was* the "inconsistent across devices" report; the fix is
  `PERMANENT_SESSION_LIFETIME` + `session.permanent = True`, set in exactly one place
  (`_start_admin_session()`, which every login path calls).
- **`_enforce_session_timeout` must stay registered before `_check_csrf`.** Flask runs
  `before_request` hooks in registration order, and an admin POST on an expired
  session has to redirect to the login page - the CSRF token lives in the very
  session being cleared, so the other order turns "your session expired" into a bare
  400. If you add another `before_request` hook, mind where you define it.
- **The idle check is server-side (`session["last_seen"]`), not just the cookie's
  Max-Age.** A cookie's expiry attribute isn't covered by the signature, so a client
  that keeps sending an "expired" cookie would otherwise stay logged in forever. The
  cookie's own `Max-Age` is a fixed `config.SESSION_COOKIE_MAX_AGE_DAYS` backstop set
  at import - **don't make it track the DB setting**: Flask reads
  `app.permanent_session_lifetime` at cookie-set time, and mutating that per-request
  is a cross-thread global write under waitress. `admin_session_timeout_hours` (DB
  setting, live-editable) is clamped to it.
- **`status_history` is the only unbounded table, and every query against it runs on
  a public page load.** Two things keep it from becoming the app's slowest part, and
  both matter: `idx_status_history_service_checked` is deliberately
  `(service_id, checked_at, status)` - the third column is what makes it a *covering*
  index for the uptime aggregate (131ms -> 43ms; a 2-column version drops back to a
  table scan, and there's a test asserting `EXPLAIN QUERY PLAN` still says COVERING
  INDEX) - and `prune_status_history()` runs daily from the health-check loop.
  Before this, `get_uptime_percentage()` full-scanned the whole table *once per
  service* on every page load: ~1s for 17 services at 90 days of history, growing
  forever. Anything iterating over all services must use
  `db.get_uptime_percentages()` (one grouped query), never the single-service form
  in a loop.
- **Indexes go in `init_db()`'s explicit list, not inline in `CREATE TABLE`.** Same
  reasoning as `_ensure_column()`: `CREATE TABLE IF NOT EXISTS` is a no-op on an
  existing database, so an inline index would never appear on any real user's
  database. `CREATE INDEX IF NOT EXISTS` applies to both.
- **The database runs in WAL mode.** SQLite's default rollback journal makes readers
  and writers block each other database-wide, so the background health-check thread's
  writes stall every request handler that touches the DB (up to the 5s busy timeout,
  then "database is locked"). Combined with waitress's default of *4* request threads,
  that is what "the portal stops responding when the host is loaded" looks like -
  hence `config.WAITRESS_THREADS` (default 12) as well. WAL is a persistent property
  of the file, set once in `init_db()`; it can't be enabled on a network filesystem
  (SMB/NFS), which is why the switch is wrapped and logged rather than assumed.
- **`monitoring.start_background_refresh()` is no longer a no-op off Windows.** It now
  also owns the CPU sample: `psutil.cpu_percent(interval=0.2)` *sleeps* 0.2s, and it
  was being called from `get_resource_snapshot()` inside request handlers. The thread
  publishes `_CPU_CACHE` with the non-blocking `interval=None` form; handlers read it
  and fall back to a live blocking sample only when nothing has been published or the
  cache went stale (a dead thread must show a fresh number, not a frozen one).
  psutil's *first* `interval=None` call always answers 0.0, which is why
  `_refresh_cpu_cache()` won't publish a window shorter than
  `MIN_CPU_SAMPLE_WINDOW_SECONDS`.
- **`SEND_FILE_MAX_AGE_DEFAULT` is 30 days, which is only safe because `asset_url()`
  cache-busts every CSS/JS URL.** If you ever add a static reference that bypasses
  `asset_url()` (or `site_logo_version`), it will be cached for a month. The DB backup
  download passes `max_age=0` explicitly for the same reason - it's a point-in-time
  file, not a versioned asset.
- **`/admin/system` has *two* cache buttons and they are not redundant - don't merge
  them.** They act on different machines, and only one of them can act on a machine
  you don't control:
  - *Clear server-side cached data* clears each module's caches via its own
    `clear_caches()`, not by reaching into other modules' globals from `app.py` (add a
    cache -> add it to that module's `clear_caches()`/`cache_summary()`, not to a list
    in `app.py`). It also bumps `asset_cache_salt`, appended to `asset_url()`'s `?v=`,
    which is the *only* mechanism that reaches **other people's** browsers - it changes
    the URL they're asked for, so a stale copy can't answer. The salt is a settings row
    (a restart that forgot it would hand every browser back the URL it already cached)
    and is **random, not a timestamp**: two clicks in the same second would otherwise
    produce an identical "bust".
  - *Clear this browser's cache* only reaches the browser that clicked it, because
    that is all a web page can reach. It sends `Clear-Site-Data: "cache", "storage"`
    **and** re-does the work in JS, because Chrome/Edge only honor that header in a
    secure context and this portal is very often plain HTTP on a LAN/Tailscale. The
    JS half is what actually does the job in practice: `caches.delete()`, service
    worker unregistration, DOM storage, then `fetch(url, {cache: 'reload'})` over
    every asset (enumerated from the static directory by `_all_static_asset_urls()`,
    never a hand-maintained list). **Never add the `"cookies"` directive** - it would
    sign the admin out as a side effect of a cache action. The dark/light choice is
    carried through the wipe and put back deliberately; it's a preference, not stale
    data.

  Neither is behind `_require_totp()` - nothing is destroyed and nothing goes offline.
- **`_uptime_cache` and `_asset_salt` are module-level globals, so `tests/conftest.py`
  resets them** alongside `_integration_status_cache` and the Jellyfin cache. A new
  module-level cache needs the same treatment or it leaks across tests.
- **`local_time.js` is loaded once in `admin_base.html`, not per admin template.**
  `admin_about.html` had a `.local-time` span with no script behind it and silently
  rendered its raw UTC fallback forever. Timestamps handed to a `.local-time` span
  must be **ISO-8601 strings** - caches that stamp themselves with `time.time()`
  floats convert at the boundary (`monitoring._as_iso()`), because `local_time.js`
  leaves anything `new Date()` can't parse showing its fallback text.

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

## Admin panel navigation (`templates/admin_base.html`)

- **The nav is grouped by what an admin is trying to do** (Status · Content ·
  Monitoring · Notifications · System), not by when a feature was added. Group
  headings are `.admin-nav__group` spans, not nested lists — the nav is still a flat
  sequence of `<a>` tags, which is what keeps the `active` matching trivial.
- **Grouping is presentation only: no route ever moves for it.** Bookmarks are the
  reason. A page that belongs in a new group gets a new nav position and keeps its
  URL; if a genuinely new page is needed (as `/admin/notifications` was), add the
  route rather than relocating an existing one.
- **`.admin-nav` needs `overflow-y: auto`.** The grouped nav is ~966px tall and a
  laptop viewport is ~650–720px, so without it the bottom entries (About, Two-factor
  auth) are simply unreachable — invisible to every route-level test and obvious the
  moment you open a browser at a real window height.
- **A page's `<h1>`, its `{% block title %}` and its nav label must all match**, and
  the nav label is now also the thing that decides the other two when they disagree.
  Renaming a nav entry means checking all three (this is why "Discord Bot — Servers"
  became "Discord servers" in three places at once).
- **Every nav entry needs its own `active` key.** A sub-page sharing its parent's key
  (as `/admin/discord-bot/guilds` did) highlights the wrong entry the moment it gets
  its own line in the nav.

## Notification channels (`/admin/notifications`, `notifications.py`)

- **`notifications.channel_summary()` is the single list of what channels exist**, and
  it lives next to `notify()` on purpose: those are the two places a new channel has
  to appear, and keeping them adjacent is what stops a channel that sends perfectly
  from being invisible on the admin page, or vice versa. The admin route just renders
  whatever that function returns.
- **Email is the third channel, and it needs no new dependency** — `smtplib` and
  `email.message` are stdlib. It counts as configured only when host, from-address
  *and* at least one recipient are all present; a half-filled block reads as "not set
  up" rather than as a channel that fails on every send and fills the log with the same
  error on every service blip.
- **`send_email()` takes a `recipients` parameter** so per-user notifications can reuse
  it without going near `PORTAL_SMTP_TO`, which is specifically the admin alert list.
- **An unrecognised `PORTAL_SMTP_SECURITY` upgrades to STARTTLS, never downgrades to
  plaintext.** Failing to connect is a far better outcome than quietly sending
  credentials in the clear because someone typo'd the value.
- **The HTML body is built with `html.escape()`, not a Jinja template.**
  `notifications.py` is called from the background health-check thread, where there is
  no app or request context to render a template in — and this module deliberately
  doesn't import Flask. Plain text is set *first* because in `multipart/alternative`
  the last part is the preferred one.
- The channel status and "Send test" button **moved off `/admin/settings`** into this
  page. The test button still calls `notifications.notify()` with a canned payload —
  the real dispatch path, not a parallel one. It deliberately no longer refuses when
  nothing is configured: `notify()` with no channels is already a no-op and the button
  is disabled in the template, so the refusal was a third place to keep in step for no
  benefit.

## Jellyfin-backed user accounts (`jellyfin_auth.py`, `/admin/users`, `/login`) — added 2026-08-21, v1.7.0

A second identity alongside the single admin: visitors sign in with their Jellyfin
username and password. **Off by default** — enabling it puts a login form on the
public page, which has to be a deliberate choice.

- **The admin login is untouched, and the separation is structural, not a
  convention.** `session["logged_in"]` is written in exactly one function
  (`_start_admin_session`) and nothing in the visitor flow can reach it;
  `login_required` reads only `logged_in`, `user_login_required` reads only
  `portal_user`; separate lockout counters, separate timeouts, separate logout
  routes. **Four checks in `tests/test_conventions.py` enforce this** — if you find
  yourself wanting to share a helper between the two paths, that's the thing not to
  do. A signed-in Jellyfin user is a visitor with a name, not a lesser admin.
- **`_end_user_session()` pops its own keys and must never be `session.clear()`** —
  an admin who is also signed in as a Jellyfin user in the same browser must not be
  logged out of the admin panel by a visitor-side timeout. (`admin_logout` still
  clears everything, which is correct in that direction and was left alone.)
- **`jellyfin_admin` in the session is groundwork, read by nothing.** Being an
  administrator in Jellyfin says nothing about this portal. It's stored for the
  per-content visibility feature on the ROADMAP; if you start reading it, make that
  an explicit feature rather than an implicit privilege.
- **The cached user list can never authenticate anybody.** Jellyfin doesn't expose
  password hashes over its API, so there is no material to check a password against
  offline — accepting a username that merely appears in the cache would be no
  authentication at all. Sign-in is therefore **always** a live call to Jellyfin.
  This is the single most important thing not to "improve" here.
- **What the cache is actually for**: keeping already-signed-in visitors valid while
  Jellyfin is unreachable, and revoking the session of anyone since removed or
  disabled there. `_enforce_user_session` checks it on every request and **never
  contacts Jellyfin** — an outage must not sign anybody out.
- **An empty cache means "no information", not "no users".**
  `session_user_still_valid()` returns True when the list has never been synced;
  otherwise the very first sign-in, before any sync had run, would be thrown away on
  the next request. Likewise `sync_users()` refuses an empty user list from Jellyfin
  (far more likely a proxy answering with a 200 than a Jellyfin with zero users) and
  a failed sync leaves the previous list completely intact.
- **"Unreachable" must never be reported as "wrong password"**, and must not count
  towards the lockout. Telling someone their password is wrong when the server is
  merely down sends them off to reset a password that was fine; letting an outage
  fill the counter locks everyone out for five minutes after Jellyfin comes back.
- **Neither the password nor the Jellyfin access token goes in the session.** The
  Flask cookie is signed but *not* encrypted, so anything in it is readable by
  whoever holds it. The access token is used once — to revoke itself via
  `/Sessions/Logout` — which also stops the portal accumulating a dead device entry
  per sign-in in Jellyfin's own device list. `DEVICE_ID` is fixed for the same
  reason; don't randomise it.
- **Per-user portal access (`jellyfin_users.portal_allowed`) is the admin's own
  decision and must never be confused with Jellyfin's `is_disabled`.** One mirrors
  Jellyfin and is overwritten by every sync; the other is set here and **has to be
  carried across the sync**, because `replace_jellyfin_users()` is a full
  delete-and-reinsert — forget that and blocking someone silently un-blocks them
  within the hour. Carried by Jellyfin's stable user id, so a rename is not a way
  out of a block, while a genuinely new account reusing an old name starts allowed.
  Enforced in **two** places that must stay in step: `authenticate()` (refuses a new
  sign-in, with its own `not_allowed` reason distinct from `disabled`) and
  `session_user_still_valid()` (ends the session they're already in, on the next
  request — a block that waits for expiry isn't a block). Unknown user = allowed:
  absence of a row is absence of a decision, not a refusal.
- **The visitor sign-in control lives in the fixed `.page-actions` cluster in
  `base.html`**, sharing it with the theme toggle so the two can't overlap. It is
  rendered only on non-`/admin/` pages and never on the sign-in page itself.
  **`.topbar` reserves horizontal padding for that cluster** (and `body` carries a
  `no-visitor-controls` class when the cluster is just the theme toggle, so the
  reservation matches). Skip that and the topbar's right-hand text renders *behind*
  the button — which looks fine in every route-level test and is obvious the moment
  you open a browser. If you add anything else to the cluster, re-check the
  reservation at a phone width too.
- **`jellyfin_auth.py` owns every Jellyfin call involving identity; `integrations.py`
  stays read-only health/log checks.** That line is why `fetch_users()` lives here
  rather than next to `fetch_jellyfin_sessions()`. The *configuration* is still
  shared: the server URL and API key come from an ordinary Jellyfin integration row
  (`jellyfin_auth_integration_id` picks which, when there are several), so there is
  exactly one answer in the admin panel to "where is your Jellyfin". A stored id
  pointing at a deleted or disabled integration resolves to `None` and disables the
  feature rather than silently falling back to some *other* Jellyfin.
- **`config.JELLYFIN_AUTH_TIMEOUT_SECONDS` is separate from `integrations.TIMEOUT`
  on purpose** — same precedent as `BYPARR_TIMEOUT_SECONDS`, different pressure: a
  person is waiting on a sign-in, and a Jellyfin busy transcoding answers
  `/Users/AuthenticateByName` more slowly than `/System/Info`. Too low and a valid
  password looks like an outage.
- **The `/report` gate (`report_requires_login`) only takes effect while sign-in is
  enabled.** Otherwise enabling the default would make the report form unreachable
  behind a login that doesn't exist on every install that never set this up.
  **Known trade-off, stated in the admin UI rather than buried**: while Jellyfin is
  down, nobody who hasn't already signed in can sign in, so nobody new can report
  the outage — which is one of the times a status page matters most. The setting is
  the escape hatch; don't quietly remove it.
- **`_csrf_required_for()` replaced the bare `/admin/` prefix check.** Everything
  under `/admin/` still always applies. Beyond that: `/login` unconditionally (a
  cross-site POST to it is session fixation), and `/report` **only while somebody is
  signed in** — its exemption was justified by exercising no authenticated
  privilege, which stops being true once reports are attributable, while no-JS
  anonymous reporting keeps working on installs without sign-in. A new public POST
  route means a deliberate decision in that function; the convention test reads it.
- **`_safe_next_url()` exists because `/login` is reachable with no authentication
  at all** — an open redirect there is a phishing primitive. Anything not a
  single-slash relative path is discarded rather than sanitised.
- **Security note worth repeating to the user, not just the code**: enabling this
  publishes a Jellyfin login form wherever the portal is reachable. That's the
  actual risk of the feature, and it's why it's off by default.
- **What is and isn't verified**: every flow was exercised live against a *stand-in*
  Jellyfin HTTP server built from Jellyfin's documented response shapes — sign-in,
  wrong password, disabled account, outage behaviour, session revocation, restart
  survival — plus a real Chromium run of every new page. **No real Jellyfin instance
  exists in this sandbox**, so the exact response shapes of `/Users` and
  `/Users/AuthenticateByName` are unconfirmed against a running server. See
  `docs/HISTORY.md`.

## The user account page (`/account`) — added 2026-08-21, v1.7.0

A signed-in visitor's own page: what became of the reports they filed, and a couple
of personal settings. Reached by clicking the username in the sign-in chip.

- **Report visibility is scoped by `reporter_user_id` (Jellyfin's stable id), never
  by name.** `reporter_user` is kept alongside it for *display only*, because it has
  to stay readable for someone later removed from Jellyfin entirely.
  **`db.list_reports_for_user()` refuses a blank id inside the query function**, not
  in the route: every anonymous report has `reporter_user_id = ''`, so a caller that
  passed `""` would otherwise be handed every anonymous report in the database. Same
  for `count_unseen_replies()`/`mark_replies_seen()`. Don't move that guard out to
  the caller.
- **Report status wording on this page is aimed at the reporter**
  (`REPORT_STATUS_LABELS` in `app.py`), not at the admin triaging. "New" and
  "reviewed" are triage words that tell the person waiting nothing.
- **A report is a two-way thread (`report_messages`), not a field.** `author` is
  `'admin'` or `'user'`, and `seen` means "seen by the other party" — unambiguous
  because every message has exactly one intended reader. The old single `admin_reply`
  column on `problem_reports` is **legacy**: still there (nothing is ever dropped
  here), backfilled into the thread by an idempotent one-time `INSERT ... WHERE id
  NOT IN (...)` in `init_db()`, and read by nothing. Don't start writing to it again.
- **Messages are append-only.** The first version allowed editing a reply, which
  meant the other party could be looking at text that no longer existed. A
  conversation where earlier messages change under you is worse than one where a
  correction is just another message.
- **Both sides need an unread signal, or the conversation is one-directional in
  practice.** The user gets the dot on the sign-in chip; the admin's Reports nav
  badge counts `count_unread_problem_reports()` **plus** `count_unseen_user_messages()`
  — without that second term the admin would simply never learn anyone had answered.
  Each side's page marks the *other* side's messages read on open.
- **A user's reply deliberately does not reopen a closed report.** A status changing
  itself underneath the admin would be surprising; the badge is the signal, and
  reopening is their call.
- **The reporter's reply route checks ownership against the stored
  `reporter_user_id`**, and a report that isn't theirs answers *identically* to one
  that doesn't exist — "that exists but isn't yours" is itself information about
  other people's reports.
- **No extra rate limiting on replies, deliberately.** `/report`'s honeypot, timing
  check and global limit exist because it's open to anonymous visitors; every message
  here is attributable to a signed-in Jellyfin account the admin can block outright
  from `/admin/users`. If that ever stops being true, this needs revisiting.
- **An admin reply is visible to exactly one person** — that reporter's account page
  and nowhere else, since the report's own text was never public either. Replying to
  an anonymous report is allowed but warns that nobody can see it; the textarea is
  disabled for those rows.
- **Preferences live in `user_preferences`, deliberately not as more columns on
  `jellyfin_users`.** That table is rewritten wholesale by every sync, so anything
  stored there must be explicitly carried across `replace_jellyfin_users()` or it is
  silently wiped within the hour (`portal_allowed` is the one exception, and it
  predates this table — it's admin-owned, not user-owned). Keeping user-owned data
  out of `jellyfin_users` means the next preference can't fall into that trap.
- **The theme has three inputs and two implementations of the precedence — they must
  agree.** The inputs are this browser's `localStorage`, the signed-in user's account
  preference (`data-server-theme`, rendered onto `<html>` so a fresh device shows the
  right colours *before* any JavaScript runs), and the OS setting. The order is
  **localStorage → account preference → OS**, and it is implemented twice: in the
  inline anti-flash script in `base.html`'s `<head>` and again in
  `static/js/theme.js`. If those two ever disagree the page visibly changes colour a
  moment after load. Change both, together, or neither.
  - The trap that falls out of that order: saving "Light" on the account page would
    change every *other* device and visibly not the one you're sitting at, because
    this browser's local choice outranks it. `static/js/account.js` therefore syncs
    `localStorage` to the newly-saved value — **only immediately after a save**
    (`data-just-saved`), never on an ordinary visit, or it would quietly undo a local
    toggle later.
  - Saving `auto` **removes** the local override rather than leaving it, otherwise
    "auto" would keep whatever was last toggled on that device forever.
  - The floating toggle posts to `/account/theme` when signed in, so a choice made
    anywhere follows the user everywhere. That endpoint writes *only* the theme —
    `db.set_user_preferences()` takes named fields precisely so the toggle can't
    blank out anything else.
- **A signed-out visitor's theme behaviour is unchanged** from before this feature
  existed, and there's a test asserting it. This is the part most likely to regress
  unnoticed, since nobody testing while signed in would ever see it.

## Per-user notifications (`user_notify.py`, `/admin/notifications/users`, `/account`)

- **Nothing is ever sent from the request that triggered it.** An admin clicking
  "reply" writes one row to `notification_queue` and returns; the `user_notifications`
  scheduled task drains it. That's what stops a slow SMTP server making the admin panel
  hang, and it's the same rule every other outbound call here follows.
- **Matching a Jellyfin user to a Seerr user fails closed.** The only link followed is
  Seerr's own `jellyfinUserId`. Matching on email or username would eventually deliver
  one person's notifications to another, so an unmatched user is simply asked for their
  details instead. An unreachable Seerr is treated identically to "no link" — both
  correctly lead to asking.
- **`integrations.push_seerr_contact()` is the only call in this app that modifies
  another service.** One user, two contact fields, only from an explicit button press
  on that person's own account page, with the current Seerr values shown first. It must
  never become reachable from a sync or a background task.
- **The gating preference is checked at *delivery* time, not enqueue time**, so
  switching something off silences what's already queued too.
- **"Nowhere to send it" counts as delivered, not failed.** No contact details, or the
  preference switched off, will not be fixed by retrying in two minutes — and a row
  that can never succeed must not sit in the queue burning attempts.
- **Partial delivery counts as sent.** Retrying would re-deliver to the channel that
  already worked.
- **`DEFAULT_USER_PREFERENCES` must list every field `set_user_preferences()` can
  write.** A missing key reads back as `None`, is coerced to `0`/`""` when some *other*
  field is saved, and silently switches an on-by-default preference off. That happened
  while this was being written; `test_every_writable_preference_has_a_declared_default`
  now catches it.
- **Defaults: "things about my own reports" on, "anything about services I use" off.**
  The first is a reply to something the person started; the second is chatty.
- **The admin page never lists recipients.** The queue is addressed to a Jellyfin
  account; whose email or Discord ID that resolved to is that person's business.

## Keeping rules enforceable (`tests/test_conventions.py`)

- **When you add a rule to this file, ask whether it can be a test instead.** Prose in
  a 1200-line file is a request; a failing test is a fact. Anything checkable without
  running the app (a naming rule, a "these three files must agree" rule, an invariant
  expressible over the AST) belongs in `tests/test_conventions.py` *as well as* here —
  the prose explains why, the test makes forgetting it impossible.
- **No false positives, ever.** A check that fires on legitimate code is worse than no
  check at all, because the correct response to it becomes "ignore the convention
  tests", and that generalizes to the ones that matter. Encode real exceptions
  explicitly (see the uploaded-logo case in `test_templates_use_asset_url_for_css_and_js`)
  rather than watering the rule down until it never fires.
- **Failure messages are the interface.** Write them for someone who has *not* read the
  matching section here: say what is wrong, and what to do about it. That's the whole
  point — it delivers the rule at the moment it's being broken, instead of hoping it
  was read 900 lines earlier.
- **Prove a new check can fail.** Temporarily introduce the violation, watch the test
  go red, then revert. A convention test that silently can't fire is worse than none,
  because it reads as coverage.
- Static checks only. Anything needing a running app belongs in `tests/test_app.py`.
- **A `Stop` hook runs these checks automatically** (`.claude/settings.json` ->
  `.claude/check_conventions.sh`), so a violation surfaces at the end of the turn
  without anyone having to remember to run the suite — which is the same failure mode
  as the forgotten rule itself. It is silent when the checks pass. On a failure it
  asks once for a fix, and if it has *already* woken the model once
  (`stop_hook_active`), it downgrades to a message for the human instead of asking
  again — a violation that can't be fixed must never become a loop. To disable it
  temporarily, `/hooks`; to see what it does, read the script, it's 40 lines.

## Testing/verification habits (established over many sessions — keep following them)

- Run the full `pytest` suite *and* a live `python app.py` + `curl` smoke test of
  whatever routes actually changed before calling something done. Several real bugs
  (the integration-blocking page load, the maintenance-window timing bug, the 2FA
  enrollment `KeyError`) were only ever caught by actually running the server, never by
  unit tests alone.
- **For anything that depends on client-side JS (AJAX "load more" buttons, the
  favicon/logo actually rendering, console errors), `curl` alone isn't enough — drive a
  real browser.** A previous version of this file claimed Playwright/Chromium was
  pre-installed and told you not to run `playwright install`; that was wrong as of
  2026-08-19, when a fresh sandbox had neither. **Just install it** (the user has
  standing authorization to install tooling in this sandbox without asking):
  `pip install playwright && python -m playwright install --with-deps chromium`, ~1
  minute. Then drive it with `sync_playwright()`, hooking `console`, `pageerror` and
  `requestfailed` so an error can't pass unnoticed. This caught a real bug:
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
- **Never combine `curl -X POST` with `-L` against an admin route.** `-X` forces the
  method on the redirect too, so curl re-POSTs to the redirect target - which for every
  route here is a page whose form has a *different* CSRF token, giving a 400 that looks
  exactly like the action having failed when it actually succeeded (302) a moment
  earlier. Use `--data` without `-X` (curl then correctly switches to GET on 302), or
  drop `-L` and check the 302 directly.
- **Don't `pkill -f "python app.py"` from a Bash tool call.** `-f` matches full command
  lines, and the shell running the command contains that string, so it kills its own
  session. `kill $(lsof -t -i:5000)` targets the actual listener instead.
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

## Commit cadence — commit per fix, not per session

**Explicitly asked for on 2026-08-20**, after a session landed a whole batch (session
persistence + performance + a new admin control + docs) as one enormous commit.
Don't do that.

**Commit as soon as a single fix or feature is complete and its tests pass** — one
self-contained change per commit, with its own tests and its own doc/CLAUDE.md
updates included in that same commit. A session that fixes three bugs and adds a
button should produce four commits, not one.

"Complete" means the suite passes and the thing works, not that the whole batch is
done. Don't wait for the end of the session, don't wait for the user to confirm the
whole batch, and don't hold everything back for one tidy final commit — the tidy
final commit is the problem, not the goal.

Why this matters more here than the size of the diff suggests:

- **`git bisect` and `git revert` only work at commit granularity.** One 19-file
  commit spanning sessions, performance and a new feature can't be reverted to undo
  just the risky third of it — which, on a project where the user runs the release
  on a real home server and finds regressions by using it, is exactly the operation
  most likely to be needed.
- **The release notes write themselves** from `git log <previous-tag>..HEAD --oneline`
  (see the changelog step below). One giant commit collapses that into a single
  useless line.
- **Review is possible at all.** The PR convention below exists so a risky batch gets
  looked at; a single commit containing everything defeats it.

This does not change the *release* cadence — mid-session pre-releases stay coarse
(one `-rc.N` per completed batch, per the checkpoint rule below). Committing often
and releasing at checkpoints are separate things: commit at every completed fix, cut
a release when a batch is done.

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
