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
- **`/static/uploads/` is served with `Content-Security-Policy: sandbox`**
  (`set_security_headers`): an SVG logo can carry script, and the normal CSP allows
  inline script, so opened directly it would run on the portal origin. `<img>` and
  favicon use are unaffected.
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
- **Manual DB backup** (`POST /admin/settings/backup-db`, behind `_require_totp()`
  with a code field beside the button on the About page when 2FA is on — the backup
  holds the TOTP seed and every integration API key, so a session alone must not
  take it) zips a snapshot of `instance/portal.db` via
  `db.backup_to_file()`, which uses SQLite's own `Connection.backup()` API rather
  than a raw file copy — a plain copy of a live database file being actively
  written by the background health-check thread could catch a torn/partial write
  mid-transaction; `Connection.backup()` can't. Deliberately kept separate from
  `updater.py`'s own pre-update backup machinery, which backs up app *code* for
  rollback — a different concern from an on-demand DB export.
- **Scheduled DB backup** (`db_backup` task in `app.py`, daily 03:00 UTC) writes
  `instance/db_backups/portal-scheduled-<stamp>.db` and keeps the newest 7
  (`KEEP_SCHEDULED_DB_BACKUPS`). It shares the folder with the before-restore
  snapshots, so listing and pruning are **always per filename prefix**
  (`SCHEDULED_BACKUP_PREFIX` / `SAFETY_BACKUP_PREFIX`): never prune the folder as a whole.
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
  shows "Depends on: X, Y" on the public card, resolved in `_enrich_services()` by
  one grouped `db.list_dependencies_for_services()` call (only for services that
  opted in), not a query per service. The same goes for each maintenance window's
  services (`_attach_window_services()`) and each report's service name
  (`_attach_report_services()`). `test_the_public_page_query_count_does_not_grow_with_the_data`
  fails if a per-item query in a loop comes back.
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
  values. **That was not true of `/api/status` until 1.9.1**: it serialised whole
  `services` rows, raw `run_target` and every internal `check_url` included, for
  services that never opted in. `_api_service_view()` now publishes only the keys in
  `_API_SERVICE_KEYS` (an allow-list, so a new `services` column is **not** public by
  default) plus the raw `run_target` when `show_run_target_public` is set; `check_url`
  is never published. External dashboards read this endpoint, so no key may disappear;
  a test pins the key set, and adding a column means deciding here whether it is public.
  `_run_target_label()`
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
  additive UI). See "Never commit straight to `main`" under Release process below —
  as of 2026-08-30 this isn't a judgment call scoped to risky batches, it's the
  standing rule for every change.

## Admin panel navigation (`templates/admin_base.html`)

- **The nav is grouped by what an admin is trying to do** (Overview, then
  Status · Public content · Connections · Notifications · System), not by when a feature was added. Group
  headings are `.admin-nav__group` spans, not nested lists — the nav is still a flat
  sequence of `<a>` tags, which is what keeps the `active` matching trivial.
- **Grouping is presentation only: no route ever moves for it.** Bookmarks are the
  reason. A page that belongs in a new group gets a new nav position and keeps its
  URL; if a genuinely new page is needed (as `/admin/notifications` was), add the
  route rather than relocating an existing one.
- **`.admin-nav` needs `overflow-y: auto`.** The grouped nav is ~966px tall and a
  laptop viewport is ~650–720px, so without it the bottom entries (Security, Updates &
  backups) are simply unreachable — invisible to every route-level test and obvious the
  moment you open a browser at a real window height.
- **`/admin` is an overview, not a redirect** (`_overview_items()`): rows of
  crit/warn/ok, worst first, built only from the database, caches and settings - never a
  live call. A new "needs attention" signal is one more `add()` there. It has no
  trailing slash, so `_inject_admin_badges()` matches `/admin` explicitly as well as
  `/admin/...`. The Two-factor pages have no menu entry; Security stays highlighted.
  "About" is now "Updates & backups" (same `/admin/about` URL).
- **Under 720px the menu is a `<details>`** (`#admin_nav_menu`) whose summary shows the
  current page (from the `nav_labels` map in `admin_base.html`, keyed by `active`) and
  the badge total. It is rendered `open` and the summary is hidden above 720px, so
  without JS it is always visible; `admin_nav.js` closes it on narrow screens. A new
  admin page needs an entry in `nav_labels` too.
- **Admin tables on a phone:** `.col-secondary` cells are hidden under 720px and
  `.col-short` cells never break mid-word. Use them on new wide tables.
- **Auto-submitting a `<select>` in admin:** `data-autosubmit` (handled by
  `admin_autosubmit.js`, which uses `requestSubmit()`), never an `onchange` attribute.
- **A page's `<h1>`, its `{% block title %}` and its nav label must all match**, and
  the nav label is now also the thing that decides the other two when they disagree.
  Renaming a nav entry means checking all three (this is why "Discord Bot — Servers"
  became "Discord servers" in three places at once).
- **A sub-page reached only from its parent's own button gets no nav entry**, and sets
  `active` to its *parent's* key so the nav still shows where you are. Discord servers
  briefly had both a nav entry and an in-page button to the same place; the button won,
  because it sits next to the bot settings the page is about.
- **`.admin-table` is the wrapper `<div>`, never the `<table>` itself.** On a wrapper
  its `overflow-x: auto` scrolls a too-wide table; on the table it does nothing and the
  table drags the whole page sideways. The codebase had it both ways (nine pages one,
  five the other), which is why one page got a bug report and eight stayed broken.
  `tests/test_conventions.py` enforces it.
- **`.admin-main` needs `min-width: 0`.** It's a flex item, and a flex item's default
  `min-width: auto` means "never shrink below my content" — so a wide table pushed the
  panel and the page sideways no matter how many `overflow-x: auto` containers sat
  inside it. That one declaration is what makes all of them work.
- **`.led`'s glow is an `::after` at `inset: -6px`, so the dot paints wider than it
  measures.** It's suppressed inside `.admin-table` (`content: none`) because in a dense
  table it lands on the text beside it — or, when a cell is narrow enough to wrap, on the
  line beneath. That was the "green blob over the word yes" report. It stays on the
  public status page, where the cards are big and drawing the eye is the point.
- **Text from another system must be allowed to break mid-word** (`overflow-wrap:
  anywhere` on `.empty-state`, `.field-hint`, `.admin-table td`, `code`). An error like
  `HTTPConnectionPool(host='127.0.0.1', port=9)` has nothing to break at and pushes the
  page sideways on a phone.
- **Audit responsively by measuring, not by looking.** The script that found all of the
  above walks every admin page at 320/360/390/480/640/768/1024px and reports two things:
  any `.led` whose painted box (glow included) intersects a text rect, and
  `documentElement.scrollWidth > clientWidth`. Five separate causes, only visible one at
  a time.
- **The admin search bubble lives in `admin_base.html`, not per template** - same
  reasoning as `local_time.js` being loaded there: wiring it per page is how one page
  ends up without it. See *Admin search* below for the rest.
- **An unbalanced `<div>` can close a `<form>` early, and it hides well.** A stray
  `</div>` on the settings page put every field below it outside the form in the DOM,
  while the page still looked right and still saved correctly - an input's form owner is
  fixed at parse time (`docs/HISTORY.md` → "the stray `</div>`"). `tests/test_app.py`
  now counts divs on every rendered admin page. Note `html.parser` **cannot** catch this:
  it tokenises rather than building an HTML5 tree, so it counts `<form>` depth by tags
  alone and passes happily with the fault present.
- **Toggling a boolean in an admin table uses `.switch`** (`static/css/style.css` +
  `static/js/admin_toggle.js`), not a badge plus a separate Block/Allow button — that
  made the reader work out the current state from two elements written in different
  tones. The markup keeps a real focusable checkbox for keyboards and screen readers,
  and a hidden field carrying the value to *become*, so the same form works with JS
  (submit on change) and without it (a fallback button the script hides).

## Admin search (`admin_search.py`, `/admin/search`, the floating bubble)

- **The index is derived from the admin templates and must never become a
  hand-written list.** That is the entire design. A hand-maintained index of several
  hundred settings would be wrong within two sessions and wrong *invisibly* - a missing
  entry looks exactly like a setting that doesn't exist, which is worse than no search
  at all. `build_index()` reads the templates for every `<label>`, field hint and
  `name=`, so adding a setting adds it to the search and renaming one renames it, with
  one source of truth.
- **Never build the index by rendering the pages and reading the DOM.** It's the obvious
  shortcut and it's wrong: rendering `/admin/reports` marks messages as read and
  `/admin/logs` reads files off disk, so a search box would acquire side effects.
  Templates are inert. `admin_search.py` deliberately imports neither `app` nor `db`,
  and there's a test asserting it.
- **`PAGES` is the one hand-maintained part, and it's kept honest by two tests**: every
  `templates/admin_*.html` must be registered there or listed in `NON_PAGE_TEMPLATES`,
  and every endpoint must resolve through `url_for()`. The second caught a typo'd
  endpoint the moment it was written - without it that surfaces as a `BuildError` in
  front of the user, on a search.
- **Only the page-level entry carries `aliases`** (its own template/endpoint words).
  That's what makes "2fa" land on Two-factor auth rather than on the four step-up code
  prompts scattered around the panel, whose visible labels *do* contain "2FA" while the
  page's own label doesn't. Don't give every entry aliases - it would boost every
  control on that page equally and lose the distinction.
- **Results are a server-rendered HTML fragment** (`_admin_search_results.html`), same
  convention as `/api/incidents/more`, `/admin/logs/tail` and `/search/live`. This app
  has one JSON API (`/api/status`, for external consumers) and no client-side
  templating; a search box is not the place to start.
- **`?jump=` is an input's `name`, not an id**, and `admin_search.js` resolves it with
  `[name=...]` falling back to `getElementById`. Keyed that way so several hundred
  fields across twenty templates didn't each need an id added just to be linkable. On
  arrival it opens any enclosing `<details>` before scrolling - otherwise the page
  scrolls to something invisible and reads as broken.
- **Matching is AND across terms, not OR** - "disk alert" narrows rather than widens -
  and a query under two characters matches nothing, because every entry would match and
  that is a list rather than an answer.
- The index is cached on the templates' mtimes (like `asset_url()`), so an edited label
  shows up without a restart while a search does no disk I/O. It's in
  `admin_search.clear_caches()` and `tests/conftest.py` like every other module cache.

## Settings: three independent forms (`/admin/settings`)

- **Public page, Monitoring and Security are three forms with three handlers**
  (`admin_settings_general`, `admin_settings_monitoring`, `admin_settings_security`),
  each with its own anchor (the Security form itself now renders on `/admin/security`). **Each handler writes only its own keys.** An unticked
  checkbox is simply absent from a POST and reads as "off", so a handler that also
  wrote another form's keys would reset that form's boxes on every save. A new
  setting goes in the handler of the form that shows it; `test_each_settings_form_writes_only_its_own_keys`
  covers it. The service defaults live in the Monitoring form (rule 8 still applies).

## Security page (`/admin/security`)

- One page for password, 2FA status (the setup itself stays on `/admin/2fa`), session
  timeout, "Sign out all other sessions" (`_rotate_admin_session_epoch()`, behind
  `_require_totp()`), both API keys and a checks panel (`_security_checks()`).
- **Nothing it shows has a new handler.** The password form posts to `/admin/security`
  (`_change_admin_password()`, shared with the older POST to `/admin/settings`, which
  still works), the timeout to `/admin/settings/security`, the key buttons to their
  original routes, which return here when the form carries `back=security` (`_key_page()`,
  a whitelist, not a free `next`).
- The checks read the request, settings and the update cache only. HTTPS counts as seen
  from `request.is_secure` or an `X-Forwarded-Proto: https` header, because without
  `PORTAL_BEHIND_PROXY` the first is false behind a tunnel.

## Keeping your place when a form submits (`admin_scroll_restore.js`, `admin_flash.js`)

- **Every admin form submit remembers the page's scroll position and restores it
  after the reload.** Admin forms POST and redirect back to the same page, and a
  browser starts a fresh page at the top — so saving one field at the bottom of
  Settings threw you back to the top every time. Reported 2026-09-03 as "anytime I
  click a save button it brings me to the top of the page". One document-level
  `submit` listener covers every form, including any added later.
- **The restore is keyed on the path and expires in 10 seconds, and it is one-shot.**
  A restore must only happen on the page you submitted from, and only for the load
  that followed it — otherwise coming back to a page later, from somewhere else,
  silently dumps you halfway down it. A `back_forward` navigation is skipped too:
  the browser already restores scroll there, and fighting it is worse than doing
  nothing.
- **`form.submit()` bypasses the `submit` event entirely — use
  `form.requestSubmit()`.** `admin_toggle.js` submits on change, and with
  `.submit()` its flip would jump to the top of the page while every other form
  stayed put. If you add another script that submits a form programmatically, this
  is the trap.
- **Admin flash messages are pinned to the viewport (`.flash-stack`), and that is a
  consequence of the above, not decoration.** Once scroll is preserved, a message
  rendered at the top of a long page is a confirmation nobody ever sees. Successes
  auto-dismiss after 7s and anything can be clicked away; **errors deliberately stay**
  — an error that vanishes while you are still working out what it meant is worse
  than one you have to dismiss. Public pages keep the in-flow `.flash`: they are
  short, and nothing there restores scroll.
