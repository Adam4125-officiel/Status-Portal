## Announcements and their display window (`db.py`, `/admin/announcements`)

- **An announcement's `starts_at`/`ends_at` are optional, and `''` means "no bound".**
  Both blank is what every announcement written before the window existed has, and what
  a new one gets unless the admin fills something in - so the default behaviour is still
  "published on save, shown until deleted". There's a test pinning that; don't let a
  form default or a JS prefill quietly opt new announcements into an expiry.
- **Whether one is showing is decided at read time, never by a stored flag a job flips.**
  `db.list_active_announcements()` filters in SQL. Three reasons, and they're the
  argument against "tidying" this into a scheduled task: nothing can fall out of sync,
  so a window that opened or closed while the portal was down needs no catching up;
  extending an expired announcement brings it straight back, where a flag would need the
  admin to switch it on again *and* would make "expired" indistinguishable from "an
  admin turned this off"; and it costs no task for something derivable from two
  timestamps.
- **Every public-facing consumer must use `list_active_announcements()`; only
  `/admin/announcements` uses `list_announcements()`.** That's the status page,
  `/api/status`, `/feed.xml`, the Discord embed and the kiosk - five separate call
  sites, each with its own test, because a missed one is a leak: a *scheduled*
  announcement is unpublished content, which is worse than an expired one lingering.
  The admin list deliberately shows everything, with a "Showing" column, since it's the
  only page that can edit a scheduled or expired one.
- **A notification is refused for an announcement that isn't currently showing**
  (`_announcement_send_block()`, checked in all three send paths). It would point people
  at something they can't see, in both directions.
- **The public card shows the end date when there is one**, under the posted date
  (`sections/announcements.html`, mirrored in `kiosk/announcements.html` - the two are
  near-identical copies, so keep them in step). Only when `ends_at` is set: "until
  (nothing)" is worse than no second line. `.announcement__time` is a flex column for
  this, with `nowrap` moved onto the individual spans - it's there to stop a timestamp
  breaking mid-date, not to keep both on one row.
- **A `datetime-local` value has no timezone, so `data-utc` needs an explicit `Z`.**
  Stored as `2099-01-01T00:00`, which JS parses as *local* time - so without the `Z`,
  `local_time.js` shifts every window time by the viewer's own offset. Invisible in this
  sandbox, whose browser and clock are both UTC (`docs/HISTORY.md` → "the window times
  that were an hour out"). Any future timestamp that comes from a `datetime-local` field
  has the same trap.

## Announcements pushed as notifications (`app.py`, `db.py` — added 2026-08-28)

- **Additive to the existing `announcements` table/feature, not a second concept.** An
  announcement is still just a public status-page banner (and still shows in the
  Discord bot's `/status` embed via `build_status_data()`) until an admin explicitly
  sends it — creating or editing one with no channel checked behaves exactly as it did
  before this existed, and there's a test (`test_creating_with_no_channels_checked_
  behaves_as_before`) pinning that. Deliberately *not* a standalone broadcast tool with
  its own history unrelated to the banners — asked and confirmed when this was
  designed, to avoid two overlapping "tell everyone something" concepts.
- **One shared implementation, three entry points.** `routes.admin_content._dispatch_announcement_send
  (aid, title, message, channels)` is what actually sends — the create form's "Publish
  and send" (checkboxes on the same form as title/message, not a separate page),
  the edit form's equivalent, and the list page's per-row "Send" (for re-sending: a
  typo fixed and re-sent, or sent by email first and Discord later) all call it. Don't
  reimplement the dispatch in any of the three routes.
- **Email fans out through the existing per-user queue, nothing new needed there.**
  `EVENT_CHANNEL_PREFERENCE["announcement"] = {"email": "notify_email_announcements",
  "discord": None}` — one more entry in the same dict `report_reply`/`maintenance`/etc.
  already use, gated by a `notify_email_announcements` column added to
  `user_preferences` (`_ensure_column`, default on — same reasoning as
  reports/requests: an announcement is something the admin chose to say to everyone,
  not routine per-service noise). `user_notify.notify_service_subscribers("announcement",
  title, message)` does the fan-out; nothing in `user_notify.deliver()` needed to change.
- **Discord is `None` for a different reason than `seerr_event`'s `None`.**
  `seerr_event`'s email slot is `None` because there's no email equivalent at all.
  `announcement`'s **discord** slot is `None` because the Discord half is one post to
  one configured channel (`discordbot_announcement_channel_id`, a plain text field on
  `/admin/discord-bot/guilds` — copy the ID from the channel table already on that
  page, the same pattern as the existing channel whitelist field, not a `<select>`
  populated from `guilds`), not a per-user DM — there is no per-user Discord
  preference to gate a channel post by.
- **`discord_bot.send_channel_message(channel_id, text)` (new) deliberately does not
  retry, unlike `_edit_tracked_status_message()`'s periodic retry loop.** Same
  `asyncio.run_coroutine_threadsafe` bridge and cache-then-`fetch_channel()`
  resolution as `send_dm()`/`_fetch_channel()`, but this is a discrete one-shot send
  with a caller-visible outcome (the send-history row) to record — retrying would
  just delay that outcome being known, unlike the periodic refresh loop where "try
  again next tick" is the right answer because there's no such outcome to report.
- **The Discord post runs on a one-shot background thread from the admin route**
  (`app._send_announcement_discord`), the same shape `_restart_process()` already
  uses for a delayed one-off action — not a `while True` loop, so it isn't subject to
  (and doesn't need adding to) `test_no_new_bare_background_loops`'s allow-list.
  **The send-history row is written before the Discord result is known**
  (`db.record_announcement_send()` returns immediately; the background thread calls
  `db.set_announcement_send_detail()` once Discord actually answers) — so the history
  page always reflects that a send happened, with `discord_detail == ""` read by the
  template as "sending…" until it's filled in.
- **`announcement_sends` is `ON DELETE SET NULL` on `announcement_id`, not CASCADE.**
  The send history is a record of what was sent and must outlive the announcement
  being deleted later — same reasoning `problem_reports.incident_id` uses for a
  deleted incident. It's a brand-new table, so a plain `CREATE TABLE IF NOT EXISTS` is
  correct with no `_ensure_column()` migration needed.
- **`db.create_announcement()` now returns the new row's id** (`cur.lastrowid`) —
  needed so "Publish and send" can attach the send-history row to the announcement
  that was just created in the same request. Every pre-existing caller already
  ignored the return value, so this is a strictly additive change.
- **`channels` is whitelisted server-side against exactly `("email", "discord")`**
  in every one of the three entry points, never trusted verbatim from
  `request.form.getlist("channels")` — the same "server re-checks what the UI merely
  hides" reasoning as the search page's admin-only request-configuration fields.
