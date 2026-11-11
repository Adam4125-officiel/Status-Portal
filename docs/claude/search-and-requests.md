## Unified search (`media_search.py`, `/search`)

- **This is the one place an outbound call happens inside a request handler, and it is
  a deliberate carve-out, not an exception that has crept in.** A search query isn't
  known until somebody types it, so there is nothing to pre-fetch into a cache. Do not
  "fix" it by adding a cache, and do not use it as a precedent for anything that *can*
  be background-refreshed.
- **What makes the carve-out acceptable is the safety machinery, so don't remove any of
  it**: `config.SEARCH_TIMEOUT_SECONDS` (6s, deliberately shorter than every other
  timeout here) so a slow Jellyfin can't hold a request thread; the two sources asked
  **in parallel** (Seerr on a helper thread, Jellyfin on the request's), so two slow
  backends cost one timeout of thread time rather than two; each source failing
  independently; and a distinct "search is unavailable right now" state that must never
  be collapsed into "nothing found" — one is a system problem, the other is an answer.
  Neither search fetcher touches the database, which is what makes the helper thread
  safe: the pooled connection is thread-local and must never cross threads.
- **Signed-in visitors only, plus a per-person rate limit and a concurrency cap.** Three
  separate reasons: the result set reveals the whole library, requesting is a write
  against Seerr that has to be attributable to a person, and a search box wired to two
  external APIs is a free denial-of-service amplifier. The limit is **per person**
  (keyed on the Jellyfin user id), unlike `_login_state` and `_report_state` which are
  process-global — those defend a route open to anonymous strangers, where a shared
  counter is the point; this one is already behind a sign-in, so a global counter
  would let one enthusiastic searcher lock everybody else out.
- **The limit lives server-side, never in the session.** It used to be a counter in
  the cookie, and replaying an older cookie reset it (160 searches against a limit of
  40). `media_search.outbound_call(user_id)` is now the one gate: a sliding window of
  `SEARCH_RATE_LIMIT` calls per `SEARCH_RATE_WINDOW_SECONDS`, plus a bounded semaphore
  of `MAX_CONCURRENT_OUTBOUND` (a third of `WAITRESS_THREADS`). Without the cap, a few
  slow searches could hold every request thread and take the status page down with
  them. Past the cap the caller gets the rate-limit message, and a busy refusal
  doesn't cost them allowance. **Every route that makes one of these calls goes
  through it**: `/search`, `/search/live`, `/search/detail`, `/search/request/configure`
  (which had no limit at all before) and `/search/request`. Checking, taking a slot and
  recording happen under one lock, and the slot is released on the semaphore that was
  acquired, because `clear_caches()` swaps it. The admin clear-caches button
  deliberately doesn't call `media_search.clear_caches()`, same as login/report
  throttling.
- **The Seerr search query must be percent-encoded, not form-encoded.** Seerr proxies
  search to TMDB, and TMDB rejects `+` with HTTP 400 *"Parameter 'query' must be url
  encoded. Its value may not contain reserved characters."* — so `params={"query": ...}`
  (requests' default, which uses `+` for a space) worked for single words and failed for
  every multi-word search. `_seerr_search_url()` builds it with
  `urlencode(quote_via=quote)`; don't "simplify" it back to `params=`. The diagnostic
  builds its URL the same way on purpose: when it didn't, it reported "both calls
  succeeded" while search was broken, because it happened to use a one-word query.
- **Anything the shared public nav renders must be passed by *every* route that renders
  it.** `search_enabled` was computed only by `index()`, so the Search tab existed on the
  Status page and nowhere else. `_render_public_page()` passes the same set.
- **A page on its own needs an empty state; a block on a shared page doesn't.** The
  Jellyfin activity partial guarded itself into rendering nothing when idle, which is
  right inside a long scroll and reads as broken as a whole page. It now says so.
- **Results appear while typing, from a server-rendered fragment.** `/search/live`
  returns `sections/_search_results.html` - the *same* partial the full page includes -
  so the incremental results and the submitted ones cannot drift apart. Same convention
  as `/api/incidents/more`: this app has one JSON API (`/api/status`, for external
  consumers) and no client-side templating.
- **The debounce and the three-character minimum are the mechanism; the rate limit is
  the backstop.** Both are enforced server-side too, because the client can't be trusted
  to. `static/js/search_live.js` aborts an in-flight request when a newer keystroke
  arrives - each one costs two outbound API calls, and only the newest question matters.
- **Deduping is by title *and year*.** The two sources share no ids (Jellyfin has its
  own, Seerr speaks TMDB), so the title is all there is to match on — and merging a 1984
  film into its 2021 remake would be worse than showing both.
- **A merged result must keep both ids.** Jellyfin's is what makes "Watch now"
  possible; Seerr's TMDB id is what makes a request possible. Dropping either silently
  disables one of the two actions on exactly the rows where both apply.
- **Jellyfin wins on whether something is actually present.** It has the file; Seerr's
  `mediaInfo.status` is an opinion about it.
- **A request is attributed to the matching Seerr user via Seerr's own `jellyfinUserId`,
  and the link is never guessed.** Same rule as per-user notifications. With no link the
  request still goes through (unattributed) and the UI says so — an unattributed request
  is a much smaller problem than one attributed to the wrong person.
- **"Search says Seerr is down, Integrations says it's up" is not a contradiction, and
  the timeout is not the cause.** The two facts come from different calls with different
  dependencies: `/api/v1/status` is served by Seerr itself, while `/api/v1/search` makes
  Seerr go out to **TMDB**, so a Seerr host with slow or blocked outbound internet fails
  the second and passes the first. The Integrations page also reads a **cache** refreshed
  on the health-check interval, so it can be showing something up to
  `CHECK_INTERVAL_SECONDS` old. Search already has the *longer* timeout of the two, so
  widening it fixes nothing — `integrations.diagnose_seerr()` (the "Diagnose search"
  button on `/admin/integrations`) runs both calls back to back and says which applies.
- **Search failures are described in words** (`describe_request_error()`), not as a bare
  "couldn't be reached" — timed out, refused the API key, answered HTTP 500 and
  couldn't connect have completely different fixes, and are logged at *warning*, not
  info.
- **A 409 from Seerr is "already requested", which is ordinary**, not an error worth
  alarming anyone with — someone pressed the button twice.
- **A TV request needs a season selection, and "all" is only the fallback** for a
  caller that doesn't pass specific ones — see request configuration below for where a
  real picker now exists. Seerr rejects a series request with no seasons at all.
- **Posters are TMDB images (`image.tmdb.org`), one scoped CSP `img-src` exception** —
  a scoped, named-host exception, not a general relaxation. `poster_path` was
  already threaded through `search_seerr()` → `media_search.merge()`; only the rendering was missing. A Jellyfin-only result (no
  Seerr/TMDB match) stays text-only rather than proxying Jellyfin's own image endpoint
  through a request handler — that would be exactly the live-outbound-I/O-in-a-handler
  problem this app avoids everywhere else.
- **The detail page and the results list share one action partial**
  (`templates/sections/_search_result_action.html`) — Watch now / Request / Already
  requested / In the library, rendered identically in both places by construction, not
  by discipline. `fetch_seerr_detail()` was extended (same forever-cache, no second
  call) to carry `overview`/`genres`/`runtime`/`vote_average`/`seasons` alongside the
  title/year it already resolved, since the detail page needs the same Seerr response
  fetch_seerr_pending()/fetch_seerr_requests() already fetch for title resolution.
- **The detail route (`/search/detail/<media_type>/<tmdb_id>`) trusts query-string
  `in_library`/`jellyfin_id`/`requested`, sourced from the results link, rather than
  re-deriving them.** There is no cheaper way to know a single TMDB id's Jellyfin/Seerr
  status without a second live search — a deliberate, low-stakes scope choice (stale
  display info, not anything the route writes), not an oversight.
- **Request configuration**: "Request this" opens
  `/search/request/configure` rather than submitting immediately. A season picker (all
  seasons pre-checked, matching the old hardcoded `"all"`) is shown to **every**
  signed-in visitor; root folder, quality profile and tags are shown - and honoured -
  **only when the browser is also signed in as the portal admin**
  (`session["logged_in"]`), since root folder values are real server filesystem paths.
  `integrations.fetch_seerr_{radarr,sonarr}_{servers,detail}()` read Seerr's
  `/service/radarr`/`/service/sonarr` endpoints and default to whichever server Seerr
  flags `isDefault` (a picker between servers is only shown when there's more than one -
  the common case has exactly one *Arr instance per kind). **The admin-only fields are
  re-checked server-side in `search_request()`** (stripped to `None` unless
  `session["logged_in"]`) - the configuration page not rendering them for a plain
  visitor is not itself an authorization boundary, and a forged POST must not be able
  to smuggle them in.

## Requesting through Seerr (`integrations.request_via_seerr()`) — read before touching it

Everything here exists because of one incident: two orphaned `media_request` rows in
Adam's Seerr with `mediaId` NULL and their `season_request` children intact, which broke
the profile page of the user they belonged to (`docs/HISTORY.md` → "the requests that
were never made"). This is the only code in the portal that creates anything in Seerr,
and the rules below are what stop it asking for something Seerr will half-do.

- **`is4k` must always be sent, and this is the least obvious rule in the file.** It is
  optional in Seerr's API and the portal used to omit it, which silently disabled
  Seerr's *own* duplicate protection for every request the portal made. Seerr's
  `MediaRequest.request()` dedupes by comparing the **incoming** value twice —
  `.where('request.is4k = :is4k', ...)` for "already requested", and
  `request.is4k === requestBody.is4k` when working out which seasons a previous request
  already covers — while the row it writes takes `false` from the column default. So
  `undefined` goes in and `false` is stored: every request the portal made was invisible
  to the dedupe of the next one, and asking twice produced two overlapping request rows
  instead of a 409. Verified against `seerr-team/seerr`'s own source, not deduced. If
  4K requesting is ever added, it becomes a real value — never an omission again.
- **A 2xx from `/api/v1/request` does not mean a request was created.** Seerr maps its
  `NoSeasonsAvailableError` to **HTTP 202** with `{"message": "No seasons available to
  request"}` and creates nothing, and `raise_for_status()` is blind to it — so the
  portal told visitors "Requested. You'll be notified when it arrives" about requests
  that did not exist. Only a **201 carrying an `id`** counts; anything else raises
  `integrations.SeerrRequestNotCreated` with Seerr's own message. **Don't reduce this
  back to `raise_for_status()`.**
- **A created request is checked for attached media, and a media-less one is logged at
  ERROR with its id.** `media_request.mediaId` is nullable in Seerr's schema (its
  `@ManyToOne(() => Media, ...)` sets `onDelete: 'CASCADE'` but never
  `nullable: false`), so a 201 can describe exactly the orphan that started this. The
  portal cannot stop Seerr writing that row, but it can refuse to call it a success and
  name the id, which is the difference between a quiet corruption and something
  findable. That log line is the point of the check — keep it.
- **`SeerrRequestNotCreated` is caught before the `RequestException`/`ValueError`
  branch in `media_search.request()`, and must stay separate from it.** "Seerr answered
  fine and made nothing" is a different thing from "Seerr is unreachable", and "try
  again in a moment" is wrong advice for the first.
- **Never send an empty season list.** `seasons: []` asks Seerr for nothing. The check
  is truthiness, not `is not None` — that distinction was the bug. Refused with a
  sentence in `media_search.request()` and again as a structural backstop in
  `request_via_seerr()`.
- **Numeric form fields are validated, never filtered.** `search_request()` used to
  build seasons with `if s.isdigit()`, which turns a mangled submission into a
  *partial* one — a series quietly requested with three of its five seasons and nothing
  anywhere saying so. Refuse the submission instead; the same applies to `profile_id`
  and `tags`.
- **Three layers stop a duplicate, and each covers what the others can't.** Deleting
  any one of them leaves a real gap: `static/js/search_request_submit.js` disables the
  submit button (the only layer that can stop a double-click's second POST ever being
  sent, since both are in flight before the server has written anything);
  `app._request_recently_submitted()` keys a short per-session window on
  `media_type:tmdb_id:seasons` (covers a refresh or a back-and-resubmit, and lets a
  *different* season selection through on purpose); and `is4k` restores Seerr's own
  409, which is the only one that reaches another device. A failed submission is
  forgotten immediately (`_forget_submitted_request()`) — the guard exists to stop a
  *successful* request happening twice, not to lock a title out because Seerr was down
  for one press.
- **The wire payload is worth asserting on directly.** These bugs all lived in what was
  actually sent and what came back, which a mocked `request_via_seerr` cannot see —
  `tests/test_search.py`'s `_capture_post`/`_SeerrResponse` exist for that, and the
  live smoke test in `docs/HISTORY.md` drove the real form against a stand-in Seerr that
  records every POST.

## Talking to Jellyfin (`jellyfin_auth.auth_headers()`) — the 12.0 auth migration

Jellyfin **12.0** (released 2026-09-08, straight after the 10.11.x line) turns
*legacy authorization* off by default, and ships a migration that turns it off on
existing installs too. Every authenticated call this portal made used one of the
mechanisms it stopped reading, so all seven of them would have returned 401 against a
12.0 server.

- **`jellyfin_auth.auth_headers(token=None)` is the only place a Jellyfin request's
  headers are built.** Every call site goes through it — `fetch_users`, the sign-in,
  the token revocation, `fetch_jellyfin_status`, `search_jellyfin`,
  `fetch_jellyfin_sessions`, `fetch_jellyfin_running_tasks`, and
  `version_checks._fetch_direct_version`. That last one is the reason this is a rule
  rather than a habit: it built its own header, lives in a different module from the
  other six, and was missed on the first pass of the migration.
- **The token goes inside the `Authorization` header, never in `X-Emby-Token`.** What
  12.0 still reads is the `Authorization` header with the `MediaBrowser` scheme
  (`Token="..."` inside it) and the **`ApiKey`** query parameter. What it ignores by
  default: the `X-Emby-Token`, `X-MediaBrowser-Token` and `X-Emby-Authorization`
  headers, the lowercase **`api_key`** query parameter, and the `Emby` scheme. Note
  the two query parameters differ only in case and one of them still works — that is
  a genuinely easy thing to misread.
- **This is not a 12.0-only form, so there is no version sniffing and no fallback.**
  Reading Jellyfin's own `AuthorizationContext.cs` at v10.6.4, v10.7.7, v10.8.13,
  v10.10.7, v10.11.11 and v12.0, `Token=` inside the `Authorization` header is the
  **first** branch every one of those versions checks. That is why the legacy headers
  were dropped outright rather than sent alongside: on every Jellyfin that ever read
  them, this header won anyway. Don't reintroduce them "just in case" — they are
  scheduled for removal entirely in a later release.
- **`/Users/AuthenticateByName` takes no token but still needs the header.** It is the
  one call that authenticates nobody, and 12.0 still wants the
  Client/Device/DeviceId/Version fields from the `MediaBrowser` scheme — which is also
  what keeps the portal a single named device in Jellyfin's device list (see
  `DEVICE_ID`). Calling `auth_headers()` with no argument is that case.
- **Two convention tests enforce this** (`tests/test_conventions.py`): no module may
  use a legacy header, and no module other than `jellyfin_auth.py` may contain the
  string `MediaBrowser Client=`. The second is the one that would have caught
  `version_checks.py`. Both are exact string checks with no heuristics, so neither can
  fire on innocent code.
- **All eight endpoints this portal uses survive 12.0 and none are deprecated** —
  `/System/Info`, `/System/ActivityLog/Entries`, `/Users`, `/Users/AuthenticateByName`,
  `/Sessions`, `/Sessions/Logout`, `/ScheduledTasks`, `/Items` — checked against the
  official `jellyfin-openapi-stable.json`, which now reports version `12.0.0`. If you
  add a Jellyfin call, check it against that document first: 12.0's stated policy is
  that **an endpoint absent from the OpenAPI spec must not be used by clients** and
  may be removed in any major release without warning.
- **`GetItems` changed behaviour in 12.0** — it now applies `recursive` when filters
  are requested, for requests that include `includeItemTypes`, so the same query can
  return a different set than it did on 10.11. `search_jellyfin()` already passes both
  `Recursive=true` and `IncludeItemTypes`, so it is unaffected; a *new* `/Items` caller
  is the thing to check.
- **One build covers Jellyfin 10.8 → 12.0, and there are three auth eras behind
  that.** Don't add version detection; the single header form is accepted by all of
  them, for a reason worth knowing:
  - **10.0 – 10.10** read `X-Emby-Authorization` *first* and fall back to
    `Authorization` only when it is empty. This is why dropping `X-Emby-Authorization`
    was necessary rather than merely tidy — had it been left in place alongside, these
    versions would have kept parsing the legacy header and the migration would have
    been untested on exactly the servers most people run.
  - **10.11** reads `Authorization` first, legacy still enabled.
  - **12.0** is the same code with legacy authorization off by default.
- **Verified against stand-ins, not a real server.** This sandbox has no Jellyfin, so a
  driver reproduces `AuthorizationContext.cs` for each of the three eras and runs every
  call the portal makes against all of them — plus a counter-check proving the old
  `X-Emby-Token` request is accepted by the first two and 401s on the third, so the
  stand-ins genuinely discriminate rather than passing everything. Endpoint
  availability comes from Jellyfin's controller source at 10.8.13/10.10.7 and the
  published OpenAPI document for 12.0. That validates the authorization mechanism and
  the endpoint surface; it says nothing about a live install's behaviour.
  **The supported range is stated in `README.md` — update it there if it changes.**
