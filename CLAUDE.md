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

- `ROADMAP.md` — open feature ideas, plus symptoms whose cause is still unknown.
  It carries only what is *left*: a shipped idea's write-up is deleted from it (an
  index line at the bottom is all that stays) precisely because this file and
  `docs/HISTORY.md` are the better record once code exists. This file is about *how
  the existing code works and how to work on it*, not what's left to build.
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
| Any DB schema change | *`docs/claude/conventions.md`: Conventions* → `_ensure_column()`; *`docs/claude/sessions-caching-db.md`: Sessions/caching* → indexes go in `init_db()`'s list |
| A new per-service field | *`docs/claude/conventions.md`: Conventions* → "three places, not one" (main form, wizard, Service defaults) |
| A new setting of any kind | *`docs/claude/conventions.md`: Conventions* → the config split (env var vs DB row), and "a new `PORTAL_*` env var means three files" |
| Anything in `run_health_checks()` or a status value | *`docs/claude/conventions.md`: Conventions* → level-triggered `_handle_incident_lifecycle`, the `slow` tier, `_merge_api_health`/`_merge_dependency_health` |
| Anything a request handler calls | *`docs/claude/conventions.md`: Conventions* → no slow I/O in a request handler; *`docs/claude/monitoring.md`: Monitoring architecture*; *`docs/claude/sessions-caching-db.md`: Sessions/caching* |
| Login, sessions, cookies, CSRF | *`docs/claude/sessions-caching-db.md`: Sessions, caching and DB performance*; *`docs/claude/auth-and-accounts.md`: Two-factor authentication* |
| A new admin route | *`docs/claude/conventions.md`: Conventions* → CSRF is automatic under `/admin/`; `_require_totp()` for destructive actions only |
| A template's CSS/JS reference | *`docs/claude/conventions.md`: Conventions* → always `asset_url()`; *`docs/claude/sessions-caching-db.md`: Sessions/caching* → `SEND_FILE_MAX_AGE_DEFAULT` is 30 days |
| Anything rendering a timestamp | *`docs/claude/conventions.md`: Conventions* → server-side UTC + `local-time` spans; *`docs/claude/sessions-caching-db.md`: Sessions/caching* → ISO strings only |
| A new cache of any kind | *`docs/claude/sessions-caching-db.md`: Sessions/caching* → module-owned `clear_caches()`, and reset it in `tests/conftest.py` |
| Pagination / "load more" | *`docs/claude/conventions.md`: Conventions* → `/api/incidents/more`, and `docs/HISTORY.md` → "four pagination bugs in sequence" |
| A new integration kind | *`docs/claude/conventions.md`: Conventions* → the `fetch_integration_status()` dispatch dict; per-integration timeouts |
| Comparing a version of anything | *`docs/claude/updater.md`: Version checks* → `updater.parse_version` is 3-component semver; Servarr is 4-component |
| The Discord bot | *`docs/claude/discord-bot.md`: Discord bot* — the whole section, it is all load-bearing |
| The updater | *`docs/claude/updater.md`: Self-update* — especially what rollback can and cannot do |
| A page that auto-refreshes, or a panel a script opens |  *`docs/claude/restore-and-restart.md`: Component restart controls* → `data-holds-refresh`; a reload empties what is being typed |
| Anything that shells out to the OS | *`docs/claude/conventions.md`: Conventions* → never live-invoke `control_host()`; `_restart_process()` has its own, narrower rule |
| Anything that ends in a restart | *`docs/claude/testing-and-workflow.md`: Testing/verification habits* → mocks can't tell you the process came back; exercise it live once |
| The database restore | *`docs/claude/restore-and-restart.md`: Restoring the database* → the order is the safety machinery |
| A public-page section | *`docs/claude/public-pages.md`: Public page layout* → add the key to `PUBLIC_SECTIONS` |
| A kiosk view, or anything under `/kiosk` | *`docs/claude/public-pages.md`: Kiosk mode* → the two-level gate, and why it polls instead of reloading |
| An announcement, or its window | *`docs/claude/announcements.md`: Announcements and their display window* → read-time filtering, and all five public consumers |
| Any admin template's markup | *`docs/claude/admin-panel.md`: Admin panel navigation* → an unbalanced `<div>` closes the `<form>`; the rendered-page test |
| The admin search, or a new admin page | *`docs/claude/admin-panel.md`: Admin search* → the index is derived, `PAGES` is the only hand-written part |
| Anything that sends a notification | *`docs/claude/notifications.md`: Notification channels* → the two email systems; *`docs/claude/notifications.md`: Per-user notifications* → group per-event, not per-entity |
| Calling something "done" | *`docs/claude/testing-and-workflow.md`: Testing/verification habits* — pytest alone has missed real bugs repeatedly |
| A rule you half-remember | `tests/test_conventions.py` — the checkable ones are enforced there, with the reasoning in each docstring |
| A new recurring background job | *`docs/claude/scheduler.md`: Scheduled tasks* → register it, don't write another loop |
| A new admin page, or moving one in the nav | *`docs/claude/admin-panel.md`: Admin panel navigation* → pick a group, keep the route, three labels must agree |
| Anything reading or exposing the log files | *`docs/claude/logging.md`: Reading the logs from the admin panel* → the name whitelist, and why every read is bounded |
| A script that submits a form itself | *`docs/claude/admin-panel.md`: Keeping your place when a form submits* → `requestSubmit()`, never `submit()` |
| Anything touching visitor sign-in or `portal_user` | *`docs/claude/auth-and-accounts.md`: Jellyfin-backed user accounts* — the whole section; the admin/visitor split is load-bearing |
| A new public (non-`/admin/`) POST route | *`docs/claude/conventions.md`: Conventions* → `_csrf_required_for()`; the exemption is a decision, not a default |
| Anything touching the theme | *`docs/claude/auth-and-accounts.md`: The user account page* → three inputs, two implementations of the precedence; they must agree |
| Anything in the search path | *`docs/claude/search-and-requests.md`: Unified search* → the one sanctioned live outbound call in a request handler |
| Anything that asks Seerr for something | *`docs/claude/search-and-requests.md`: Requesting through Seerr* → `is4k` is not optional, and a 2xx is not a success |
| Any call to Jellyfin | *`docs/claude/search-and-requests.md`: Talking to Jellyfin* → one header builder, and never a `X-Emby-*` header |
| The gamesportal integration, or `/api/notify/*` | *`docs/claude/gamesportal.md`: Cross-repo: gamesportal integration* → two different API keys, two different directions |
| The display-device API (`/api/device/summary`, `device_api.py`, Status-ESP, the Jelly Portal plugin) | *`docs/claude/device-api.md`: Display device API* → bounded by construction, keyed, never a second key check; a new field is an opt-in parameter, never a bigger default; `detail=full` is the one mode for a server, with its own ceilings |
| Starting a multi-part batch of work | *`docs/claude/testing-and-workflow.md`: Commit cadence* — one commit per completed fix, never one at the end |
| The user saying the session is over | *`docs/claude/testing-and-workflow.md`: Ending a session — and only then* — docs, release-if-stable, then delete every merged branch |

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

## Where each section went

`CLAUDE.md` was split on its `## ` headings; the text of each section is unchanged.

| Section | File |
|---|---|
| Conventions that matter (don't relitigate these) | `docs/claude/conventions.md` |
| Self-update (`updater.py`, `update.py`, `/admin/about`) | `docs/claude/updater.md` |
| Servarr version checks (`version_checks.py`, `/admin/integrations`) | `docs/claude/updater.md` |
| Monitoring architecture (`monitoring.py`) | `docs/claude/monitoring.md` |
| High-load indicator (`monitoring.evaluate_high_load()` / `integrations.evaluate_high_load()`) | `docs/claude/monitoring.md` |
| Discord bot (`discord_bot.py`) — read this before touching it again | `docs/claude/discord-bot.md` |
| Discord DMs and Seerr approval alerts (`seerr_alerts.py`, `/admin/notifications/seerr`) | `docs/claude/discord-bot.md` |
| Crash logging (`logging_setup.py`) | `docs/claude/logging.md` |
| Media activity (`integrations.py`, `templates/sections/media.html`) | `docs/claude/public-pages.md` |
| Announcements and their display window (`db.py`, `/admin/announcements`) | `docs/claude/announcements.md` |
| Public page layout (`templates/sections/`, `templates/public/`) | `docs/claude/public-pages.md` |
| Kiosk mode (`/kiosk`, `templates/kiosk/`, `static/js/kiosk.js`) | `docs/claude/public-pages.md` |
| Two-factor authentication (`twofactor.py`) | `docs/claude/auth-and-accounts.md` |
| Public "report a problem" form (`app.py`) | `docs/claude/public-pages.md` |
| Reading the logs from the admin panel (`/admin/logs`, `logging_setup.py`) | `docs/claude/logging.md` |
| Component restart controls (`app.py`, `discord_bot.py`) | `docs/claude/restore-and-restart.md` |
| Restoring the database (`/admin/about/restore-db`, `db.py`) | `docs/claude/restore-and-restart.md` |
| Custom logo / branding (`app.py`, `static/uploads/`) | `docs/claude/admin-panel.md` |
| Admin quick wins, service→VM/host mapping, service dependencies, low disk alert (`app.py`, `db.py`, `monitoring.py` — added 2026-08-12) | `docs/claude/admin-panel.md` |
| Sessions, caching and DB performance (added 2026-08-19, v1.6.1) | `docs/claude/sessions-caching-db.md` |
| Scheduled tasks (`scheduler.py`, `/admin/tasks`) — added 2026-08-21, v1.7.0 | `docs/claude/scheduler.md` |
| Admin panel navigation (`templates/admin_base.html`) | `docs/claude/admin-panel.md` |
| Admin search (`admin_search.py`, `/admin/search`, the floating bubble) | `docs/claude/admin-panel.md` |
| Keeping your place when a form submits (`admin_scroll_restore.js`, `admin_flash.js`) | `docs/claude/admin-panel.md` |
| Notification channels (`/admin/notifications`, `notifications.py`) | `docs/claude/notifications.md` |
| Jellyfin-backed user accounts (`jellyfin_auth.py`, `/admin/users`, `/login`) — added 2026-08-21, v1.7.0 | `docs/claude/auth-and-accounts.md` |
| The user account page (`/account`) — added 2026-08-21, v1.7.0 | `docs/claude/auth-and-accounts.md` |
| Per-user notifications (`user_notify.py`, `/admin/notifications/users`, `/account`) | `docs/claude/notifications.md` |
| Announcements pushed as notifications (`app.py`, `db.py` — added 2026-08-28) | `docs/claude/announcements.md` |
| Unified search (`media_search.py`, `/search`) | `docs/claude/search-and-requests.md` |
| Requesting through Seerr (`integrations.request_via_seerr()`) — read before touching it | `docs/claude/search-and-requests.md` |
| Talking to Jellyfin (`jellyfin_auth.auth_headers()`) — the 12.0 auth migration | `docs/claude/search-and-requests.md` |
| Cross-repo: gamesportal integration (`integrations.py`, `version_checks.py`, `user_notify.py`, `app.py` — added 2026-09-12) | `docs/claude/gamesportal.md` |
| Display device API (`device_api.py`, `GET /api/device/summary`) — added 2026-10-06, v1.10.0; extended in v1.11.0, v1.11.1 and v1.12.0 | `docs/claude/device-api.md` |
| Keeping rules enforceable (`tests/test_conventions.py`) | `docs/claude/testing-and-workflow.md` |
| Testing/verification habits (established over many sessions — keep following them) | `docs/claude/testing-and-workflow.md` |
| Commit cadence — commit per fix, not per session | `docs/claude/testing-and-workflow.md` |
| Ending a session — and only then | `docs/claude/testing-and-workflow.md` |
| Release process | `docs/claude/testing-and-workflow.md` |
