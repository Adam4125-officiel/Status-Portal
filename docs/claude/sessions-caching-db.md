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
- **Admin sessions are revocable through `admin_session_epoch`, and only three things
  rotate it: a password change, enabling 2FA and disabling 2FA.** A signed cookie
  can't be invalidated server-side on its own, so a copied one used to survive both
  logout and a password change. `_start_admin_session()` stamps the current epoch;
  `_enforce_session_timeout` pops the admin keys (`_ADMIN_SESSION_KEYS`, never
  `session.clear()`) of any session whose stamp doesn't match and redirects `/admin/`
  paths to the login page. It lives inside that hook rather than a new one because it
  has to run before `_check_csrf`, for the same reason the idle expiry does.
  `_rotate_admin_session_epoch()` re-stamps the browser making the change, so the
  admin who changed the password stays signed in. **Logout stays local on purpose**
  (other devices stay signed in). An unset setting and an unstamped session both read
  as `""`, which is why upgrading signed nobody out. **A database restore carries the
  live epoch into the restored file**: the backup's own epoch would sign out the
  admin doing the restore, and an older one would bring back revoked cookies.
- **`_start_admin_session()` clears the session before stamping it**, keeping only
  `_VISITOR_SESSION_KEYS`. That's what stops anything planted before authentication
  (a known CSRF token included) carrying into the admin session; the CSRF token is
  regenerated on the next render. A new session key that belongs to the *visitor*
  side has to be added to that tuple, or an admin login in the same browser will
  quietly drop it. Anything read from the session during login (`login_next`) must
  be read *before* the call.
- **First-run password setup is only accepted from a local client**
  (`_first_run_setup_allowed()`): loopback, private, link-local or Tailscale's
  100.64.0.0/10, which `ipaddress` doesn't count as private. Otherwise a fresh install
  (new Docker volume, reinstall) with a tunnel up belonged to whoever reached the login
  page first. Without `BEHIND_PROXY`, any forwarding header (`X-Forwarded-For`,
  `CF-Connecting-IP`, …) refuses outright, because cloudflared on the same host
  connects from 127.0.0.1 for every internet visitor. With it, `remote_addr` is
  ProxyFix's answer and is trusted. Only the *first-run* branch checks this; a normal
  login works from anywhere. When testing it, use real public addresses:
  `ipaddress` counts the documentation ranges (203.0.113.0/24, …) as private.
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
- **Every connection db.py opens sets `PRAGMA synchronous=NORMAL`, and a health check's
  result is written by `db.record_check_result()`.** `synchronous` is per-connection
  (WAL itself is persistent), so a new `sqlite3.connect()` in `db.py` goes through
  `_open_connection()` or sets it itself. `record_check_result()` writes the service
  state and its `status_history` row in one transaction; don't split them again.
- **`db.get_db()` returns one pooled connection per request, not a fresh connection
  per call** (added in the v1.8.2 performance batch). `app.py`'s first
  `before_request` hook calls `db.begin_request_scope()`, which opens a connection
  and caches it thread-local; every `get_db()` call on that thread returns the same
  connection (wrapped in `_PooledConnection`, whose `.close()` is a no-op) until
  `teardown_appcontext` calls `db.end_request_scope()`. This exists because opening
  a fresh connection to this schema costs ~380us (SQLite re-parsing the whole schema
  per connection) versus ~3us to reuse one already open, and a single public page
  load makes over a hundred `db.py` calls. Background threads (health-check loop,
  scheduler, Discord bot) never call `begin_request_scope()`, so they keep opening a
  fresh connection per call exactly as before this existed. `db.py` still imports no
  Flask - the scoping is a bare `threading.local()`, wired to the request lifecycle
  only from `app.py`.
  **Anything that replaces `DB_PATH` on disk (`os.replace()`/`os.rename()`) must call
  `db.end_request_scope()` first**, unconditionally, even if it looks like nothing in
  the current request could still be holding the file open.
  `db.restore_from_file()` does this. The reason is Windows-specific and was missed
  when this shipped: a connection this same thread opened - the pooled one, alive for
  the request's entire duration - can make `os.replace()` fail with `[WinError 5]
  Access is denied` if it's still open without delete-sharing at the moment of the
  replace. POSIX has no such failure mode (a rename succeeds regardless of open
  handles), which is why this passed the full test suite and manual testing in this
  Linux-only sandbox and only surfaced on the user's real Windows install
  (`docs/HISTORY.md` → "the restore that couldn't replace its own open file").
- **`monitoring.start_background_refresh()` is no longer a no-op off Windows.** It now
  also owns the CPU sample: `psutil.cpu_percent(interval=0.2)` *sleeps* 0.2s, and it
  was being called from `get_resource_snapshot()` inside request handlers. The thread
  publishes `_CPU_CACHE` with the non-blocking `interval=None` form; handlers read it
  and fall back to a live blocking sample only when nothing has been published or the
  cache went stale (a dead thread must show a fresh number, not a frozen one).
  psutil's *first* `interval=None` call always answers 0.0, which is why
  `_refresh_cpu_cache()` won't publish a window shorter than
  `MIN_CPU_SAMPLE_WINDOW_SECONDS`.
- **Everything under `/admin` and `/account` is sent `Cache-Control: no-store`**
  (`set_security_headers`), so neither the back button nor a shared machine can bring
  a signed-in page back after logout. It overrides `send_file()`'s own header too,
  which the backup and log downloads want anyway. Public pages and `/static/` are
  untouched.
- **`set_security_headers` also sets `base-uri 'self'`, `form-action 'self'` and
  `object-src 'none'`** (not covered by `default-src`), `Permissions-Policy` and
  `Cross-Origin-Opener-Policy: same-origin`. A form posting to another site would now be
  blocked by the browser; none does today.
- **`SEND_FILE_MAX_AGE_DEFAULT` is 30 days, which is only safe because `asset_url()`
  cache-busts every CSS/JS URL.** If you ever add a static reference that bypasses
  `asset_url()` (or `site_logo_version`), it will be cached for a month. The DB backup
  download passes `max_age=0` explicitly for the same reason - it's a point-in-time
  file, not a versioned asset.
- **`/admin/system` has one cache button: *Clear server-side cached data*.** It clears
  each module's caches via its own `clear_caches()`, not by reaching into other
  modules' globals from `app.py` (add a cache -> add it to that module's
  `clear_caches()`/`cache_summary()`, not to a list in `app.py`). It also bumps
  `asset_cache_salt`, appended to `asset_url()`'s `?v=`, which is the *only*
  mechanism that reaches **other people's** browsers - it changes the URL they're
  asked for, so a stale copy can't answer. The salt is a settings row (a restart that
  forgot it would hand every browser back the URL it already cached) and is **random,
  not a timestamp**: two clicks in the same second would otherwise produce an
  identical "bust". A per-browser "clear this browser's cache" page used to sit next
  to it and was removed: it only reached the browser that clicked it. Not behind
  `_require_totp()` - nothing is destroyed and nothing goes offline.
- **`_uptime_cache` and `_asset_salt` are module-level globals, so `tests/conftest.py`
  resets them** alongside `_integration_status_cache` and the Jellyfin cache. A new
  module-level cache needs the same treatment or it leaks across tests.
- **`local_time.js` is loaded once in `admin_base.html`, not per admin template.**
  `admin_about.html` had a `.local-time` span with no script behind it and silently
  rendered its raw UTC fallback forever. Timestamps handed to a `.local-time` span
  must be **ISO-8601 strings** - caches that stamp themselves with `time.time()`
  floats convert at the boundary (`monitoring._as_iso()`), because `local_time.js`
  leaves anything `new Date()` can't parse showing its fallback text.
