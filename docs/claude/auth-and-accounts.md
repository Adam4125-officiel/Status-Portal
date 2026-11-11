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
  `session["awaiting_totp"]` (the time the password was accepted; `_awaiting_totp()`
  drops it after 5 minutes and the login restarts at the password) is set (server-side only, unforgeable without the signed
  session's `SECRET_KEY`) once the password step succeeds; the *next* `POST` to
  `/admin/login` is read as a code instead of a password, without re-checking the
  password. The global login-lockout counter (`_login_state`) applies to wrong codes
  exactly like wrong passwords — deliberately **not** reset just for getting the
  password right, only once the whole two-step login actually succeeds, so a correct
  password doesn't buy an attacker unlimited unthrottled code guesses.
- **Step-up re-authentication** for the destructive actions goes through
  `app._require_totp()` — see the convention bullet above for the call pattern and
  which routes use it. Scoped deliberately narrow (host restart/shutdown, app/bot
  restart, self-update, and changing the admin password when 2FA is on — the user
  approved that last one explicitly; not VM control, not other admin actions); don't creep it onto
  other routes without discussing it first.
- **A TOTP code is deliberately *not* single-use.** It works for its whole
  `valid_window`, so logging in and then immediately doing a step-up can take the same
  code twice. A replay guard (record the accepted time step, refuse it again) was built
  during the 1.9.1 audit work and removed at the user's request before release. Don't
  reintroduce it without asking. The brute-force protection that *is* wanted is the
  shared `_login_state` counter, which `/admin/2fa/disable` now goes through too.
  Turning 2FA off goes through `twofactor.disable()`, used by both the admin page and
  the RESET_2FA flag.
- **`/admin/2fa/enable` is refused outright while 2FA is already on** (GET and POST,
  checked before anything touches a secret). Without that, a stolen session cookie
  could enrol the attacker's own authenticator over the admin's and then pass every
  step-up check. Switching device means disabling first, which needs a current code.
  `test_destructive_routes_go_through_require_totp` checks the refusal comes first.
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

- **The admin password form shares `_login_state`** (`_login_locked()` /
  `_register_login_failure()` / `_register_login_success()`), like `/admin/2fa/disable`.
- **Password recovery is host-level too:** an empty `instance/RESET_PASSWORD`, seen on a
  hit of `/admin/login`, erases `admin_password_hash`, rotates the session epoch and
  deletes itself (`app._check_and_process_password_reset_flag()`). The portal is then at
  first-run setup, which `_first_run_setup_allowed()` limits to the local network. It
  does not touch 2FA (that is `RESET_2FA`).

## Jellyfin-backed user accounts (`jellyfin_auth.py`, `/admin/users`, `/login`) — added 2026-08-21, v1.7.0

A second identity alongside the single admin: visitors sign in with their Jellyfin
username and password. **Off by default** — enabling it puts a login form on the
public page, which has to be a deliberate choice.

- **`/login` holds at most `USER_LOGIN_MAX_CONCURRENT` Jellyfin calls at once**
  (`_user_login_slots`, a third of the waitress pool, like `media_search._outbound_slots`).
  Past that the visitor gets "try again in a moment" and it is *not* counted as a
  failed sign-in. The lockout counter only moves after Jellyfin answers, so it cannot
  stop a simultaneous burst. A new semaphore like this is a module-level cache: reset
  it in `tests/conftest.py`.
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
  logged out of the admin panel by a visitor-side timeout. The same
  goes the other way: the admin idle expiry and `/admin/logout` call
  `_end_admin_session()` (the `_ADMIN_SESSION_KEYS` only), not `session.clear()`.
  `/logout` and `/admin/logout` are POST-only (buttons, not links; `/logout` is in
  `_CSRF_PROTECTED_PUBLIC_PATHS`).
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
  single-slash relative path is discarded rather than sanitised, and so is anything
  containing a backslash or a control character (`/\host` reads as `//host` in a
  browser). **Every `next` that ends in a redirect goes through it, the admin login
  included** — `/admin/login?next=` used to be honoured verbatim, which let a genuine
  portal link drop the admin on a look-alike page straight after a real login. The
  TOTP step re-checks the stored `login_next` at use, not only where it's stored.
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
