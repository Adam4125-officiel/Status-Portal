## Cross-repo: gamesportal integration (`integrations.py`, `version_checks.py`, `user_notify.py`, `app.py` — added 2026-09-12)

Games Portal is a sibling project (same author/server, a Steam game request portal)
that plugs into this app two ways: as a normal health-check integration, and as a
consumer of this portal's notification machinery instead of building its own
Discord/email stack. Built against a contract agreed with the Games Portal session
directly (cross-session messaging), not guessed at from either side.

**Part A — health/version check, no new architecture needed:**
- `integrations.fetch_gamesportal_status()` is a new dispatch entry, same shape as
  every other fetcher: `GET <base_url>/health` with the standard `X-Api-Key` header
  (not Bazarr's query-param exception), always 200 with a valid key while the app is
  up (`{"status": "ok", "version": ..., "pending_requests": ...}`). A 401 for a
  missing/wrong key falls through `raise_for_status()` into the same
  `RequestException` branch every other fetcher uses - a real integration error, not
  a special "unreachable" case. `pending_requests` is surfaced as an informational
  `"warning"`-level issue when present and non-zero; it never affects
  `reachable`/degraded, since a queue of pending requests isn't something wrong with
  the app.
- `version_checks.DIRECT_APPS["gamesportal"]` points at
  `Adam4125-officiel/Games-Portal`, `/health`, and the `version` key - confirmed
  directly with the other session that its GitHub releases are v-prefixed
  (`vX.Y.Z[-rc.N]`, same shape this project's own tags use), so no special tag
  parsing was needed beyond what `parse_version()`/`_fetch_latest_release()` already
  do for every other app here.
- Added to both the integration form's kind dropdown and the combined wizard's, same
  as every other API-key-style kind (it needs no username/password the way
  qBittorrent does, so it fits the wizard unlike that one).

**Part B — notification delegation, two new authenticated public routes:**
- `POST /api/notify/admin` (`{"event", "subject", "body"}`) is a thin, authenticated
  front door onto the *existing* `notifications.notify()` admin fan-out (Discord
  webhook, ntfy, admin email list) - no new delivery code, same fire-and-forget
  semantics as the "Send test notification" button (a 200 means accepted and
  dispatched, not confirmed delivered). `event` is accepted but not used for
  anything here; delivery is `subject`/`body` straight into `notify()`.
- `POST /api/notify/user` (`{"jellyfin_user_id", "event", "subject", "body"}`) calls
  `user_notify.notify_user()` - queued for the existing `user_notifications`
  scheduled task to actually deliver, never sent from the request. An
  unmatched/unlinked Jellyfin user, or one with the relevant channel switched off,
  resolves through the normal `contact_for()`/`EVENT_CHANNEL_PREFERENCE` machinery
  exactly like any other event - "nowhere to send it" is reported as
  `{"status": "not_queued", ...}`, not an error. Per-user notifications being
  switched off portal-wide is checked *before* enqueueing (same reason, surfaced
  earlier) rather than silently queueing something that can never deliver.
- **`"gamesportal_request"` is a genuinely new event**, with its own preference
  columns `notify_email_gamesportal`/`notify_discord_gamesportal` (both default
  off, same reasoning as `notify_email_maintenance` - this is chatty by nature) -
  deliberately *not* folded into `notify_requests`/`notify_discord_requests`,
  which is Seerr's "something you requested" concept; a person may want one
  without the other. **Discord was initially left as `None` here** (a
  per-requester DM explicitly deferred, agreed on both sides during the first
  batch) and wired up as a same-day follow-up once the user asked for it - added
  2026-09-12, after the cross-repo test above had already shipped as `-rc.1`.
  Wired into `/account` (and therefore `/admin/users/<id>/account`, which shares
  the same template and save path) like every other per-channel toggle.
- **Both routes authenticate with one new static shared secret**
  (`NOTIFY_API_KEY_SETTING = "notify_api_key"`, a `settings` row, checked with
  `secrets.compare_digest` against the `X-Api-Key` header), generated and
  view/regenerated from a new "External notification API" panel on
  `/admin/notifications` - deliberately a DB setting rather than a `PORTAL_*` env
  var, since it's generated and shown by this app rather than typed in by the admin
  (same reasoning as `asset_cache_salt`). It is a *different* secret from the one
  Games Portal issues *to* this portal for its own `/health` check above - different
  direction, different secret, on purpose. Regenerating throws the old key away
  immediately; there is no grace period, since the only caller has to be handed the
  new value out of band anyway.
- **Deliberately NOT covered by the session-cookie CSRF mechanism.** Both routes are
  added to `tests/test_conventions.py`'s `public_post_exceptions` alongside
  `/report`, but for a different reason than that one: CSRF defends against a
  browser's *ambient* session credentials being exploited by another site, and there
  is no session here at all - a cross-site page cannot make a victim's browser send
  a custom `X-Api-Key` header it doesn't know, so the session-based CSRF concept
  doesn't apply to a machine-to-machine bearer secret. The key itself is the entire
  anti-abuse measure.
- The public page footer now shows the running version
  (`app._inject_portal_version()`, `config.VERSION_DISPLAY`) - unrelated to
  gamesportal itself, added in the same batch at the user's request.

**Coordination note, since this is the first two-repo feature this project has
had**: built via direct cross-session messaging with the Games Portal Claude Code
session (not by guessing at its side of the contract), with a human (the shared
author/admin of both projects) relaying model/version-bump instructions and
approving the one genuinely risky step - temporarily exposing each sandbox's dev
server to the public internet (via GitHub Codespaces port forwarding) so the two
could call each other for a real test, since neither sandbox can otherwise reach the
other. Do not expose a dev server that way, or generate/share a real credential
across a session boundary, without the user's explicit go-ahead each time - it was
treated as a "check with the user first" action here, not something authorized by
"be the orchestrator" alone.

**Verification status**: Part A confirmed with mocked-`requests` unit tests
(`tests/test_integrations.py`, `tests/test_version_checks.py`) plus a live
`GET /health` round trip against the other session's own dev instance, exposed
publicly the same way, from a real Games Portal build (not a stand-in). Part B
confirmed with unit/route tests (`tests/test_user_notify.py`) plus live `curl`
against a real running `python app.py`, including through the portal's own public
Codespaces URL: key generation and regeneration (old key rejected immediately
after), `/api/notify/admin` dispatching through the real `notifications.notify()`,
and `/api/notify/user` both queueing and (with a seeded test Jellyfin
user/preference row) actually attempting delivery.

**The full cross-repo round trip was then also confirmed for real**, driving Games
Portal's own `status_portal_client.py` against this portal's real `/api/notify/*`
(both public Codespaces URLs, real API keys, no stand-ins on either side): four
separate `/api/notify/admin` calls from the other session (a direct call bypassing
its app, two real requests created through its actual `/request` route, and a call
into its real `_deliver()` function) all landed, matched by server-log timestamp
against that session's own DB timestamps; and a real status change
(pending → approved) on an existing request, made through Games Portal's actual
admin route, correctly fired `/api/notify/user` with the exact expected payload,
which queued against the seeded test account and made a genuine SMTP delivery
attempt (failed only on this sandbox's lack of a configured mail server, not on
anything in the integration - the row is retried per the normal notification-queue
policy). One process error worth recording: a first log check during this testing
wrongly reported "nothing landed" because it read a *stale* log file left over from
a server restart partway through the session - corrected once checked against the
right file. Jellyfin-backed sign-in (`jellyfin_auth.py`) was **off** on the dev
instance used for all of this testing - `/api/notify/user` degrades correctly
either way (it just has nobody real to resolve against without it), but this is
worth checking on the actual production install before relying on
`notify_email_gamesportal`/`notify_discord_gamesportal` reaching anyone for real.
The Discord half specifically (added after this round of cross-repo testing, see
above) has only unit/route test coverage - not re-run against a live Games Portal
instance the way the email path was.
