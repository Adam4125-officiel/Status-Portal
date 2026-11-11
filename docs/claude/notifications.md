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
- **The recipient list is a DB setting (`smtp_recipients`), not an env var**, unlike the
  rest of the SMTP block. That split is deliberate and worth keeping straight: host,
  username and password are credentials and static deployment config; *who gets told* is
  a routine choice an admin changes without editing a file and restarting.
  `PORTAL_SMTP_TO` is still read as a fallback so an install configured before this moved
  keeps working — don't remove it.
- **`email_recipients()` must never raise**, because `notify()` never may: it runs on
  the background health-check thread, where an exception would take the whole cycle down
  over a notification. Reading a DB setting introduced a new way for that to happen, so
  the read is wrapped and falls back to the env var.
- **`send_email()` takes a `recipients` parameter** so per-user notifications can reuse
  it without going near `PORTAL_SMTP_TO`, which is specifically the admin alert list.
- **An unrecognised `PORTAL_SMTP_SECURITY` upgrades to STARTTLS, never downgrades to
  plaintext.** Failing to connect is a far better outcome than quietly sending
  credentials in the clear because someone typo'd the value.
- **The SMTP certificate is verified by default** (`ssl.create_default_context()` for
  both STARTTLS and SSL). `PORTAL_SMTP_VERIFY_TLS=false` is the explicit opt-out for a
  self-signed local relay. `build_email()` flattens line breaks in the subject to
  spaces, since a header can't hold them and `notify()` must never raise.
- **The HTML body is built with `html.escape()`, not a Jinja template.**
  `notifications.py` is called from the background health-check thread, where there is
  no app or request context to render a template in — and this module deliberately
  doesn't import Flask. Plain text is set *first* because in `multipart/alternative`
  the last part is the preferred one.
- **There are two email systems here and they are not the same thing - say so wherever
  either is edited.** `notifications.notify()` -> `send_email()` goes to the *admin
  alert list* (`smtp_recipients`) and deliberately ignores every per-user preference:
  an alert list a person could silence for themselves is not an alert list.
  `user_notify.deliver()` is the per-user path the `/account` checkboxes gate. An admin
  who puts their own address on the admin list - the obvious thing to do on a personal
  portal - therefore gets maintenance mail no matter what their account page says, which
  is exactly what got reported as a bug (`docs/HISTORY.md` -> "two email systems, no sign
  saying so"). Both pages now explain it; keep that text if you rework either page.
- **`user_notify.ADMIN_ALERTED_EVENTS` is why the same event doesn't arrive twice.**
  When an address is already on the admin alert list, `deliver()` skips the per-user
  *email* for an event the admin channel also sends - the admin copy wins, because it's
  the one that cannot be turned off per-person. Keep it scoped: only `maintenance`
  overlaps today, and a per-user Discord DM must **never** be suppressed this way (the
  admin webhook posts to a channel, a DM goes to a person - different destinations, not
  duplicates).
- The channel status and "Send test" button **moved off `/admin/settings`** into this
  page. The test button still calls `notifications.notify()` with a canned payload —
  the real dispatch path, not a parallel one. It deliberately no longer refuses when
  nothing is configured: `notify()` with no channels is already a no-op and the button
  is disabled in the template, so the refusal was a third place to keep in step for no
  benefit.

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
- **`prune_notification_queue()` removes given-up rows too**, after the same 30 days
  as delivered ones, aged by `created_at` since they have no `sent_at`. Before 1.9.1
  a row that hit `MAX_NOTIFICATION_ATTEMPTS` stayed forever. A row still being retried
  is never pruned, whatever its age.
- **`DEFAULT_USER_PREFERENCES` must list every field `set_user_preferences()` can
  write.** A missing key reads back as `None`, is coerced to `0`/`""` when some *other*
  field is saved, and silently switches an on-by-default preference off. That happened
  while this was being written; `test_every_writable_preference_has_a_declared_default`
  now catches it.
- **One preference column per channel per event, not one shared column per event
  (added 2026-08-27).** `notify_own_reports`/`notify_service_events`/`notify_requests`
  used to gate email *and* Discord DM together — someone couldn't ask for something by
  email without also getting a DM. Split into `notify_email_reports`/
  `notify_email_requests`/`notify_email_maintenance` and the Discord equivalents, plus a
  Discord-only `notify_discord_seerr_events` (approvals/declines/availability/issues, on
  by default — described in the UI as "Seerr events"). The three legacy columns stay
  (never dropped) but are read by nothing; a one-time backfill in `init_db()` — guarded
  by checking `PRAGMA table_info` for the new columns *before* calling `_ensure_column`
  on them, since the existing anti-join backfill idiom doesn't apply to new columns on
  an existing row — copies each legacy value into both of its new per-channel columns
  so nobody's existing choice is silently reset. `EVENT_CHANNEL_PREFERENCE` (replacing
  the old `EVENT_PREFERENCE`) maps each event to `{"email": col_or_None, "discord":
  col_or_None}`; `deliver()` gates each channel independently, and a channel mapped to
  `None` (email, for `seerr_event`) is never used regardless of contact info.
  `db.users_opted_into()` now takes `*fields` (OR'd) since maintenance broadcast spans
  two columns.
- **Defaults: "things about my own reports" on, "something I requested" on, "anything
  about services I use" off** (per channel now, not once for both). The first two are
  a reply to something the person started, or news about something they asked for, and
  almost always wanted; maintenance is chatty regardless of channel.
- **Admin management of a user's preferences reuses the visitor's own `account.html`,
  not a separate admin-only settings grid.** `/admin/users/<user_id>/account`
  (`admin_user_account()`) renders the same template in an `admin_viewing=True` mode —
  same `_save_account_prefs()` (app.py) the visitor's own POST uses, same
  `user_notify.adopt_seerr_contact()` the auto-fill/manual-import paths use — scoped to
  the path's `user_id` instead of `current_user()`. This is the deliberate alternative
  to maintaining two UIs for the same preferences. **The auto-fill-on-first-visit half
  of that sharing was missing until 2026-08-29** — the GET handler only ever called
  `adopt_seerr_contact()` via the manual "Use these details here" button, not on page
  load the way `user_account()` (the visitor's own route) does, so an admin always paid
  one extra click per user despite the docstring already claiming the two were unified.
  Fixed by copying `user_account()`'s exact "both fields still blank" guard block into
  `admin_user_account()`'s `GET` path — same idempotency guarantee, same flash message
  shape, just naming the target user instead of "your." If you touch one of these two
  auto-fill blocks, check the other didn't just drift again. The report thread is the
  one thing hidden in admin-viewing mode (`/admin/reports` already covers that);
  everything else user-facing (theme, contact, both notification tables, the Seerr
  account block) stays. `admin_users.html`'s username links there now; the inline per-row
  email/Discord-ID edit form is gone.
- **Seerr keeps email and Discord ID in two different places, and this is the trap.**
  Email is on the base `User` record (so it comes back with `/api/v1/user`); Discord IDs
  live on a per-user `UserSettings` sub-resource at
  `/api/v1/user/{id}/settings/notifications`. Reading only the user list therefore syncs
  email perfectly and Discord not at all — not a flaky field, the wrong API surface.
  `fetch_seerr_users(with_notification_settings=True)` does the extra per-user request;
  it's an N+1 and that's the accepted cost, since it runs hourly in a background task and
  only for users with a real Jellyfin link.
- **That N+1 must never run in a request.** `find_seerr_account()` used to do it on the
  first render of every `/account` and `/admin/users/<id>/account` page. Now display
  reads the `seerr_contacts` mirror and makes no call for anyone in it. Anything about
  to write to Seerr (`save_contact()`, the push route) passes `live=True`, which asks
  Seerr in at most two calls: the user list without settings, then the matched user's
  settings. A person the mirror doesn't know gets the same two-call lookup, written
  through to the mirror. `_invalidate_seerr_account_cache()` makes the next display ask
  Seerr again, or the page would show pre-push values until the next hourly sync.
- **It's `discordIds`, a list.** Current Seerr stores several per user; this portal sends
  to the first non-empty one. The older singular `discordId` is still read as a fallback.
- **Seerr's settings POSTs overwrite every field they read from the body**, so writing
  one field erases the rest — the user's PGP key, Telegram chat, Pushover tokens, quotas.
  `push_seerr_contact()` is therefore **read-modify-write on both endpoints**, and must
  stay that way. An earlier version sent only the changed field (and sent `email` to the
  notifications endpoint, which ignores it entirely). Verified against
  `seerr-team/seerr`'s `server/routes/user/usersettings.ts` — note the project was
  renamed from Jellyseerr to **Seerr** (`seerr-team/seerr`, docs.seerr.dev).
- **`push_seerr_contact()` validates `email`/`discord_id` before making any request, not
  after (added 2026-08-29).** Neither `account.html`'s free-text inputs nor
  `admin_user_contact()`'s form handling checked the shape of what they collected — a
  malformed value could reach Seerr's real settings and get read-modify-write'd
  straight over a previously-good one, with nothing catching it anywhere in the chain.
  `_EMAIL_RE` (a pragmatic `local@domain.tld` shape, not full RFC 5322) and
  `_DISCORD_ID_RE` (`\d{15,25}` — a Discord snowflake is currently 17-19 digits, with
  headroom for growth) both raise `ValueError` before either endpoint's GET/POST runs;
  every existing caller (`save_contact()`, `user_account_push_seerr_contact()`) already
  catches `ValueError` and reports it as a normal failure, so no caller needed to
  change. **A blank value is never rejected** — clearing a field is a legitimate
  action, distinct from a non-empty value that doesn't look right, so the check is
  `if email and not _EMAIL_RE.match(email)`, not `if not _EMAIL_RE.match(email)`.
  **Deliberately all-or-nothing**: an invalid `discord_id` blocks a valid `email` in
  the same call rather than pushing the good half and silently dropping the bad one —
  both fields are shown together in the one confirmation dialog before this is ever
  called, so partial application would contradict what was actually confirmed.
- **Which Seerr is used comes from `integrations.seerr_integration()`**, one function
  reading the `seerr_integration_id` setting, mirroring how the Jellyfin instance backing
  sign-in is chosen. It replaced three separate "first enabled one" pickers in
  `media_search`, `user_notify` and `seerr_alerts`, which could silently disagree — and
  which meant the Integrations page could diagnose one server while search used another.
- **Seerr owns contact details; this portal mirrors them.** `seerr_contacts` is a cache
  replaced wholesale by the `seerr_contact_sync` task, exactly like the Jellyfin user
  list and for the same two reasons: the delivery task must not call Seerr per message,
  and a Seerr outage must not stop notifications to people whose details are already
  known. A failed sync leaves the previous rows completely intact.
- **Only accounts Seerr itself has linked to a Jellyfin user are cached.** Same
  fail-closed rule as everywhere else here — matching on name or email would eventually
  give one person another's notifications.
- **Anything entered in this portal is written back to Seerr** via
  `user_notify.save_contact()`, the single path both the admin users page and the
  visitor prompt use. A failed write-back is *reported but not rolled back*: losing what
  somebody just typed because another service was unreachable is the worse outcome, and
  the local value is what delivery reads.
- **Contact fields are validated by one function, `user_notify.contact_errors()`**, used
  by `save_contact()` and `_save_account_prefs()` (the `/account` form, visitor and
  admin views). An invalid value gives an error message and that field is not saved; the
  rest of the form is. Discord IDs are digits only.
- **`/account` is four sections (Reports, Requests, Notifications, Preferences) around
  one settings form.** The form element (`id="account-prefs"`) is empty; the theme,
  contact and notification fields sit in their sections and join it with
  `form="account-prefs"`, so the Seerr and admin panels can be separate forms in the same
  section. A new notification checkbox is one table row in `account.html` plus its field
  in `_save_account_prefs()`; a channel that does not exist for an event is a dash, never
  a disabled box. The admin view (`admin_user_account`) uses the same template.
- **Signed-in visitors have no free-text contact box.** `_save_account_prefs()` never
  writes `contact`; `/report` attaches the stored one (if any) for a signed-in reporter
  and reads the posted box only for anonymous reports.
- **Visitor-facing messages never carry `{e}` from an outbound call**: use
  `integrations.describe_request_error(e)`; the raw text holds internal hosts and, for
  Bazarr, the API key in its query string.
- **The account page auto-fills from a linked Seerr account the first time it's opened
  with both fields still blank**, rather than waiting for the manual "Use these
  details here" button - `user_notify.adopt_seerr_contact(user_id, account)` is the one
  write both paths use. The guard is "both fields blank", checked on every page load,
  so it only ever fires once: as soon as either field has a value (typed, imported, or
  from this auto-fill itself), the guard no longer holds, and clearing a field back out
  does not bring the auto-fill back - a cleared field must stay cleared, not be
  silently refilled forever.
- **`contact_for()` prefers what was entered here over what Seerr last said.** That
  sounds backwards for a source-of-truth arrangement, and is deliberate: the two agree
  in the normal case because saving writes through, and the exception is a *failed*
  write-back — where the value the person actually typed is the better one to use.
- **The post-sign-in prompt is asked once and skipping is remembered**
  (`contact_prompt_dismissed`). Being asked the same question on every sign-in is how a
  prompt becomes something people learn to click past. An explicit `?next=` beats the
  prompt — somebody who followed a link and got bounced through sign-in should land
  where they were going.
- **A username is not an email address, and Seerr will hand you one.** Seerr fills a
  user's `email` field with their *username* when it imports a Jellyfin account that
  has none of its own, so `fetch_seerr_users()` must keep dropping anything that isn't
  address-shaped. Mirroring it verbatim put "zellowz_" in front of Gmail, which
  answered "553 not a valid RFC 5321 address" once per notification, per user, forever
  (`docs/HISTORY.md` → "the username that was posted as an email address"). Guarded at
  four layers on purpose - the source, `save_contact()`, `contact_for()` and
  `send_email()` - because each is reachable without the others.
- **`db.looks_like_email()` is deliberately permissive, and must stay that way.**
  It is what decides whether to *blank* a stored value (`_clean_non_email_contacts()`
  runs on every startup), so a stricter rule destroys data - an earlier draft required
  a dot in the domain and would have silently deleted a working `admin@nas` on a home
  LAN. It only has to catch what it exists to catch: bare usernames, which have no `@`
  at all. **`integrations._EMAIL_RE` is separate and stricter on purpose** - that one
  answers "will Seerr accept this" before a write to somebody else's service, where a
  refusal costs nothing. Don't collapse the two back together.
- **A notification about several things at once is one notification.**
  `db.process_maintenance_windows()` reports one event *per service* because that's the
  granularity the status flip works at - but `health._process_maintenance_and_notify()`
  groups those back up by `(window id, transition)` before sending anything. Looping
  straight over the events sent one message per service down every channel at once
  (Discord webhook, ntfy, admin email *and* each opted-in visitor's email and DM), so
  five ticked services meant five of everything (`docs/HISTORY.md` -> "One notification
  per service instead of one per window"). If you add another fan-out event sourced from
  a per-entity list, group it the same way - and remember `notifications.notify()` is
  three channels behind one call, so a duplication bug there is never confined to one.
- **`users_opted_into()` and `get_user_preferences()` must agree about a user with no
  preferences row.** Such a user follows the admin's configured defaults - that is what
  their own account page renders - so the broadcast query has to include them too.
  It didn't, and since `notify_email_announcements` ships on, announcement emails
  reached nobody who had never opened their account page while showing every one of them
  a ticked box.
- **Resolve a recipient's contact details through `user_notify.contact_for()`, never
  straight off the preferences row.** `contact_for()` also knows what Seerr holds for
  that person. `deliver()` was the one caller reading the row directly, so anyone whose
  details came only from Seerr had every automated notification dropped as "no contact
  details" while the admin's own "Send test email" button - which already used
  `contact_for()` - reached them fine. A test that passes while nothing arrives is close
  to undiagnosable from outside.
- **The admin page never lists recipients.** The queue is addressed to a Jellyfin
  account; whose email or Discord ID that resolved to is that person's business.
- **`send_direct(user_id, channel, subject, body)` (added 2026-08-28) is a deliberate
  second path around `EVENT_CHANNEL_PREFERENCE`, not a bug.** It backs the per-user
  "test notification" buttons (`/admin/users/<id>/test/discord`,
  `/admin/users/<id>/test/email`) *and* the free-text "Send a message" panel
  (`/admin/users/<id>/message`, `app.admin_user_message()`) the same way — both are
  one-to-one admin-initiated actions, not automated events, so they **bypass the
  recipient's channel preferences entirely** — a person who switched Discord off
  still needs to be reachable for an admin test or a direct message. It still
  resolves contact info through `contact_for()`, so it fails the same way `deliver()`
  does when there's genuinely nothing to send to. **The custom message keeps no
  history** (unlike the announcement send log) — scoped deliberately as a one-off,
  not something an admin needs to look back on later; if that ever changes, it's a
  new table, not retrofitting `announcement_sends` for an unrelated sender. **Runs
  synchronously in the request**, not queued — the same sanctioned one-shot-admin-
  action exception `admin_notifications_test()` already uses, bounded by
  `discord_bot.DM_TIMEOUT_SECONDS` (15s) and `config.SMTP_TIMEOUT_SECONDS` (10s) — and
  the point of doing it inline is that the admin gets the *real* failure back
  ("Discord refused the DM…"), not a queued row they'd have to go check on.
- **`seerr_email_enabled()` (setting `seerr_email_events_enabled`, added 2026-08-28,
  default off) suppresses only the email channel for Seerr-sourced events
  (`SEERR_SOURCED_EVENTS = {"request_update", "seerr_event"}`), never Discord.** Seerr
  already emails its own users directly about these, so this ships off - upgrading to
  this feature silences a duplicate rather than changing what a fresh install does.
  Checked by *event*, not by preference column, in `deliver()` right after `send_email`
  is computed - `seerr_event` already has no email preference column at all
  (`EVENT_CHANNEL_PREFERENCE["seerr_event"]["email"] is None`), so today this only has
  a visible effect on `request_update`, but a future email preference added for
  `seerr_event` would automatically be covered by the same switch with no second edit.
- **Admin-configurable notification defaults (`db.notification_defaults()`,
  `db.NOTIFICATION_TOGGLE_FIELDS`, added 2026-08-28) are new ground, not a pattern that
  existed before** — every other preference in this app has a fixed code-level default
  (`DEFAULT_USER_PREFERENCES`) only. **"Unconfigured" means no `user_preferences` row
  at all**, checked in `get_user_preferences()`'s `else` branch (no row) — a user who
  has saved *anything* already has a row and is permanently past the defaults, even if
  every value they saved happens to match. `adopt_seerr_contact()` is an instructive
  edge case: it creates a row (writing only 3 fields), so a user auto-filled from Seerr
  is "configured" for defaults purposes from that moment, even though they never saw a
  checkbox — this is `set_user_preferences()`'s existing "fill unset fields from
  `current`" behavior interacting correctly with the new defaults with zero
  special-casing, since `current` already comes from `get_user_preferences()`.
  Defaults are stored one `settings` row per toggle (`notify_default_<field>`), not a
  single JSON blob, so the admin panel can save one row at a time without a hidden
  dependency on submitting the whole form.
- **Override (`db.override_user_preference()`) is `default` and `override` — genuinely
  two different actions, not one setting with a checkbox.** Saving a default never
  touches an existing row, full stop; override is a separate, explicit, confirmed
  action (`static/js/admin_pref_override.js`, same `data-*`-attribute pattern as
  `admin_vm_control.js` — see the XSS note above for why that pattern and not an inline
  `onsubmit`) that runs one `UPDATE user_preferences SET <field>=?` with no `WHERE`,
  reaching every row that exists *right now*. It does not touch anyone who saves a row
  *after* the override runs — those people get the (possibly since-changed) default,
  not a retroactive guarantee of the override's value. `field` is checked against
  `NOTIFICATION_TOGGLE_FIELDS` before being interpolated into the `UPDATE` — it is a
  fixed set of known column names (8, as of the `notify_email_announcements` addition
  below), never raw form input, but the whitelist check stays regardless of source.
  The route re-reads `db.notification_defaults()[field]` for the value to apply rather
  than trusting anything posted alongside the button, so an override can never apply a
  value other than what's currently saved as the default.
