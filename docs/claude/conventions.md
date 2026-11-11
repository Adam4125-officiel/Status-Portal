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
  `requirements.txt` — `discord.py` is pinned separately in `requirements-discord.txt`
  (added 2026-08-29, so the version is still reproducible for anyone who *does* want
  the bot without making it part of the base install); `nvidia-ml-py` has no such file
  yet since nothing else has needed a reproducible pin for it. If you add another
  optional integration, follow the same shape — a sibling `requirements-<name>.txt`
  if reproducibility matters, never a bare mention in a docstring.
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
  **Since only the status code matters, the check never reads the body**:
  `_run_single_check()` requests with `stream=True` and closes the response once the
  headers are in. So `elapsed_ms`, and the `slow_threshold_ms` comparison, measure
  time to the response headers, not to the last byte (before 1.9.1 it was the full
  download). A server that sends headers and then stalls its body is "reachable",
  consistent with the rule above.
- **The listen address is `config.HOST` (`PORTAL_HOST`, default `0.0.0.0`), the port
  `config.PORT`, in both `serve_waitress.py` and `python app.py`.** Never hard-code
  either. Set `127.0.0.1` when a tunnel on the same machine is the only way in; under
  docker leave `0.0.0.0` or the published port stops working.
- **A service stuck at ~2000 ms on a `localhost` check URL is almost always IPv4-only,
  not slow.** On Windows, `localhost` resolves to `::1` first, and a service listening
  only on `0.0.0.0` makes the connection wait ~2 s before falling back to `127.0.0.1`.
  The portal's measurement is correct - the delay is real - so don't "fix" it in the
  check code. The fix is in the service (listen on IPv6 too) or the check URL
  (`127.0.0.1` instead of `localhost`). Ask which before touching `_run_single_check()`
  (`docs/HISTORY.md` -> "The 2-second localhost").
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
- **`_refresh_integration_cache()` fetches in parallel and decides sequentially.** The
  network calls go through a `HEALTH_CHECK_WORKERS`-bounded pool, like the service
  checks, because one after another a few down integrations (10s per *Arr, 30s per
  Byparr) pushed the cycle past `CHECK_INTERVAL_SECONDS`. The cache writes and every
  `_handle_integration_incident_lifecycle()` call then run on the loop thread, in list
  order, exactly as before. Keep that split: parallelising the lifecycle calls too
  would make incident handling depend on thread scheduling.
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
  only, or `class="local-date"` for something that happens on a *day* rather than at a
  moment — picking the wrong one of these is how a release calendar came to show
  "03:00" with no date); `static/js/local_time.js` overwrites the text with `Date.toLocaleString()` in
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
  `admin_host_control`, `admin_system_restart`, `admin_update` and the password change
  in `admin_settings` — the actions where a stolen/replayed session cookie alone must
  not be enough. Adding a fourth
  destructive action means calling this helper, not re-inlining the check: three
  hand-maintained copies is three chances for one to quietly stop matching the
  others. (The login flow and `/admin/2fa` enable/disable are *not* step-up and
  correctly don't use it — those are primary auth and enrollment. `/admin/2fa/disable`
  does share `_login_state` through `_login_locked()`/`_register_login_failure()`/
  `_register_login_success()`, though: without it the form was an unthrottled
  code-guessing loop for anyone holding a session cookie.)
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
  admin (an external API, another local account/service, etc.). **This is now a
  convention test** (`test_no_template_values_inside_inline_event_handlers`): any
  `{{ }}` inside an `on*=` attribute fails unless it's an explicit entry in
  `_INLINE_HANDLER_EXPRESSIONS_ALLOWED`. The second real instance was a Jellyfin
  username in `account.html`'s "Send these details to Seerr" confirm, now a
  `data-confirm` attribute read by `static/js/account.js`.
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
- **A service's group is picked from the groups that exist, not retyped.** The control is the
  shared partial `templates/_group_picker.html` (service form *and* combined wizard include it,
  so the two cannot drift): a list of the existing groups plus "New group...", with the text box
  `name="group_name"` still the field that is submitted - so the route and `create_service()` saw
  no change, and without JavaScript the box is simply always visible. The list comes from the
  Jinja global `service_groups()` (= `db.list_service_groups()`), which the partial calls itself;
  don't thread a `groups=` argument through the routes, that is the forgotten-render-site bug.
  A group is not a table: it exists while a service is in it. **`db._canonical_group_name()`**
  (used by `create_service()` and `update_service()`) makes a typed name that matches an existing
  group ignoring case and stray spaces take that group's spelling, because the public page
  groups by exact string and "media" next to "Media" was two headings. The edited service is
  left out of that lookup on purpose, or a group's capitals could never be corrected.
- **Numeric settings, query parameters and form fields go through `db.parse_int()`,
  never `int(raw) if raw.isdigit() else ...`.** `str.isdigit()` accepts "²" and other
  Unicode digits that `int()` then rejects, and any length at all, which overflows
  SQLite's INTEGER. Both were unauthenticated 500s (a hostile `?offset=`), and a
  stored "²" broke the public page for everyone. `parse_int()` takes ASCII digits
  only and clamps to `[minimum, maximum]` (at most `db.MAX_SQLITE_INT`). A setting
  where 0 means something different from "the minimum", like the history retention,
  checks for 0 itself instead of clamping. Settings writes use
  `app._setting_digits()`, the same parser in the stored-string form. Form fields keep
  their own error behaviour: the search request's seasons/profile/tags are *refused*
  with the usual "didn't make sense" flash, and the service form's `depends_on`
  *skips* anything that isn't an id - only the parser changed.
- **An admin page's on-page `<h1>`, its `{% block title %}` and its nav label must all
  match** — they drift independently, and the `<h1>` is the one the user actually
  sees. Check all three when a page's scope or nav label changes
  (`docs/HISTORY.md` → "An admin `<h1>` drifting").
