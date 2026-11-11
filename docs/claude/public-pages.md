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
- **Overseerr's request payload embeds media by id and usually carries no title**, which
  is why that list once read "TMDB #438631". `fetch_seerr_detail()` resolves it from
  Seerr's own `/api/v1/{movie,tv}/{id}` and caches the answer in-process forever - it's
  immutable data keyed by a TMDB id, and twenty pending requests would otherwise mean
  twenty extra calls per refresh. A *failed* lookup is deliberately not cached, or a
  transient blip would pin "TMDB #..." until the next restart.
- **Status badges are coloured from a stable key** (`SEERR_*_STATUS_KEY`), never by
  matching the English label - a relabelling would otherwise silently turn every badge
  grey. Every code in both status maps needs a key, and a test asserts that.
- **A request's real availability lives in `media.status` or `media.status4k`,
  never both, and which one depends on `entry.is4k` — reading `status`
  unconditionally is a real bug, not a hypothetical one.** Seerr's own request-creation
  code (`server/entity/MediaRequest.ts`) explicitly leaves the tier that *wasn't*
  requested at `MediaStatus.UNKNOWN` forever (`status: !is4k ? PENDING : UNKNOWN,
  status4k: is4k ? PENDING : UNKNOWN`), and its own frontend (`RequestCard`) reads
  `media[is4k ? 'status4k' : 'status']` for exactly this reason. `fetch_seerr_requests()`
  used to read `media.status` unconditionally, which is why a 4K request showed
  "Unknown" no matter how long ago it was requested or how available it actually was —
  not a display bug, a genuinely wrong field read, confirmed against Seerr's own source
  rather than guessed at. This also silently broke the "something you requested has
  arrived" notification for 4K requests specifically, since `seerr_alerts.
  track_request_progress()` reads the same (now-fixed) `media_status` field from this
  function's output.
- **`SEERR_REQUEST_STATUS`/`SEERR_MEDIA_STATUS` must cover every code Seerr defines,
  not just the ones seen in casual testing.** `MediaRequestStatus` has a 5th value
  (`COMPLETED`) and `MediaStatus` has a 6th and 7th (`BLOCKLISTED`, `DELETED`) beyond
  what these maps originally listed — verified against `server/constants/media.ts`.
  Missing any of them silently fell back to "Unknown" for exactly the requests/media in
  that state, the same failure shape as the `is4k` bug above but simpler: a genuinely
  new/rare enum value maps to "Unknown" cleanly by design (the fallback exists on
  purpose), but a value the enum has *always* had must not.
- **The "Coming soon" window is `media_calendar_days`** (clamped 1-90). An open-ended
  calendar pull lets one long-running series fill the list indefinitely.
- **Use `.local-date` for something that happens on a day**, not `.local-time-short` -
  the latter renders hour:minute *only*, so a release date came out as a bare "03:00".
  Three classes now: `local-time` (full), `local-time-short` (clock only),
  `local-date` (date only).

## Public page layout (`templates/sections/`, `templates/public/`)

- **The public page is split in two kinds of thing.** `PUBLIC_SECTIONS` (announcements,
  services, incidents & maintenance) are blocks on the main page, reorderable by the
  admin. `PUBLIC_PAGES` (resources, VMs, Jellyfin activity, media, practical info) are
  pages of their own, reached from the shared `.page-nav` and summarised in one line on
  the main page. The main page answers "is it working?"; everything else is a click away.
- **Availability and content are two different questions, and the nav must only ask the
  cheap one.** Each entry in `PUBLIC_PAGES` has an availability predicate (settings
  only) *and* a context builder (which may poll the machine). Building the nav by
  calling every context builder meant every public page ran a full resource snapshot —
  200ms+, and worse when `monitoring`'s CPU cache is stale and `get_resource_snapshot()`
  falls back to a blocking sample. Jellyfin activity was paying for a disk and CPU poll
  it never displays, which is what made it look like it had hung. Anything touching the
  filesystem, the network or psutil belongs in the builder, never the predicate.
- **`_request_snapshot()` memoises the resource snapshot on `g`.** The main page wants
  it twice (high-load badge, resources summary) and it's the most expensive thing a
  public page does.
- **A page's context builder is the single gate, used by the page *and* the nav *and*
  the summary.** That is what stops a sub-page becoming a way around a `show_public_*`
  setting or `media_requires_login`: if the builder returns `None` the route 404s, the
  nav doesn't link it, and no summary mentions it — all three from the same call. Never
  render a sub-page from anything other than `_render_public_page()`.
- **A switched-off page 404s rather than rendering empty.** An empty page confirms the
  feature exists and is merely hidden; 404 is the same answer a visitor would get if it
  didn't exist.
- **Auto-refresh lives in `public_base.html`, not `index.html`.** The public page used
  to be one page that reloaded itself; after the split, putting the refresh script only
  on the main page would silently freeze the resources and VM pages.
- Each content block is its own partial under `templates/sections/<key>.html`, each
  owning its own "is there anything to show" guard, and the sub-page templates under
  `templates/public/` just include the matching partial — one copy of the markup, and
  the guard still applies wherever it's rendered. The topbar/status-hero/footer are page
  chrome, not content, and stay hardcoded — deliberately never made reorderable.
- `app._public_section_order()` reads the `public_layout_order` setting
  (comma-separated section keys) and is the *only* place that decides render order —
  `index.html` just does `{% include 'sections/' ~ key ~ '.html' %}` in a loop.
  **If you add another main-page section**, add its key to the `PUBLIC_SECTIONS` list in `app.py`
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

## Kiosk mode (`/kiosk`, `templates/kiosk/`, `static/js/kiosk.js`)

A full-screen display for a wall-mounted TV or a tablet left running: one view at a
time, rotating on a timer, no nav and no footer. Off by default.

- **A kiosk view is gated twice, and the second gate is the point.** `KIOSK_VIEWS`
  reuses `PUBLIC_PAGES`' `(predicate, context builder)` split, and `vms`/`resources`
  hand it `_vms_available`/`_resources_available` - the *same* predicates the public
  sub-pages use. Ticking "Virtual machines" in the kiosk settings while
  `show_public_vms` is off must not put VM names on a wall display; kiosk shows nothing
  a visitor couldn't already see on the public pages. A new view that surfaces anything
  currently behind a `show_public_*` setting must pass that setting's predicate too, not
  just its own checkbox. There's a test for both existing cases in `tests/test_kiosk.py`.
- **The predicate answers "may this be shown", the builder answers "is there anything
  to show".** A builder returning `None` means the view is skipped for that render -
  no announcements posted, no VMs detected. This is stricter than `/activity`, which
  deliberately renders an "idle right now" empty state: a page a visitor navigated to
  can afford to say "nothing here", a view that rotates onto a wall for twenty seconds
  cannot.
- **`/kiosk` and `/kiosk/views` both 404 while kiosk mode is off.** Not just the page:
  an ungated fragment endpoint would keep serving the display's contents to anything
  still polling it after the admin switched kiosk off. 404 rather than a redirect or an
  empty page, same as a switched-off public sub-page.
- **It refreshes by polling for a server-rendered HTML fragment, not by reloading.**
  `main.js`'s `window.location.reload()` is right for the public page and wrong here:
  it flashes the screen and throws the rotation back to the first view every cycle, so
  at a 60s refresh and a 20s rotation the later views are never reached at all.
  `/kiosk/views` is the same convention as `/api/incidents/more` and `/admin/logs/tail`
  - server-rendered HTML, never JSON, and the fragment the page includes on first load
  is the *same partial*, so a polled view can't look different from one that was there
  when the display was switched on. `applyFragment()` restores the view that was on
  screen before the swap and re-runs `window.applyLocalTimes()` over what arrived.
  **Don't "simplify" this back into a reload.**
- **No slow I/O, despite polling every refresh cycle.** The VM and Jellyfin data come
  from their background-refreshed caches and the resource snapshot reads `monitoring`'s
  CPU cache, exactly as the public sub-pages do. A new view that wants live outbound
  data needs a background cache first, like everything else here.
- **Each view is rendered to HTML in Python, then embedded with `|safe`** - Jinja's
  `include` shares the caller's context, and each view has its own. Merging all five
  contexts into one namespace happens to work today only because their keys don't
  collide, which isn't a property to depend on.
- **Rotation order is `KIOSK_VIEWS`' own, not the stored setting's.** The checkboxes
  say *which* views, not in what sequence. `_kiosk_selected_views()` therefore
  deliberately does **not** copy `_public_section_order()`'s "append any valid key
  missing from the stored value" behaviour: that list orders blocks that all show
  anyway, this one is an *inclusion* list, and silently adding a view nobody ticked
  would publish something to a wall display the admin never asked for.
- **`""` and unset are different values for `kiosk_views`.** `""` is an admin who
  unticked everything (and gets the Services fallback); `None` is an install that has
  never opened the page (and gets the defaults). `db.get_setting("kiosk_views", None)`
  is what keeps them apart - don't collapse it to `get_setting(key, "")`.
- **Services is the fallback and must stay always-available.** Every view excluded, or
  every ticked view currently empty, still shows Services rather than a blank screen on
  somebody's wall. That only works while `_kiosk_services_context()` can't return
  `None` - "no services configured yet" is itself an answer.
- **`kiosk_mode` suppresses `base.html`'s fixed `.page-actions` cluster.** That's the
  theme toggle and the sign-in chip, neither of which anyone presses on a TV. The
  consequence is that a kiosk browser has no theme control of its own and follows the
  ordinary rules (this device's `localStorage`, then the OS) - set it once from the
  main page on that same browser. `kiosk_mode` is undefined (and so falsy) everywhere
  else, but it's read from `base.html`, which *every* page extends, so a change there
  is a change to every page.
- **The *page* must never scroll; a *view* auto-scrolls when it doesn't fit.**
  `.kiosk-screen` is `100dvh` (not `vh`: a tablet's retracting toolbars make `vh`
  taller than the space actually available, which clips the progress bar off the
  bottom) with `body.kiosk` overflow hidden, and only `.kiosk-view__body` ever
  scrolls. Type is sized with `clamp()` against the viewport so one rule set covers a
  1080p television at four metres and a 10" tablet at arm's length.
- **A view too tall for the screen travels to the bottom and back within its own
  rotation slot**, on the fractions in `kiosk.js` (`SCROLL_HOLD_TOP`/`SCROLL_DOWN`/
  `SCROLL_HOLD_BOTTOM`, which must stay under 1 between them - the journey back up is
  the remainder, and is what puts the view back at the top before the rotation next
  reaches it).
  This is **self-gating and deliberately has no breakpoint**: it only fires when
  `scrollHeight - clientHeight` actually exceeds `SCROLL_MIN_OVERFLOW_PX`, so a
  television showing six services never moves while the same page on a 7" tablet does.
  The overflow is re-measured every frame, because a refresh can swap the contents
  underneath it and late-loading web fonts change the height of everything.
  `prefers-reduced-motion` gets two discrete cuts instead of a continuous scroll -
  **not** "no scrolling", which would leave the bottom of a long list permanently
  unreachable on exactly the screen where it matters.
- **A manual scroll hands control over for the rest of the slot** (`scrollTakenOver`,
  cleared by the next `show()`). The check compares against the position the animation
  last wrote (`__kioskScrollTop`), because assigning `scrollTop` fires `scroll` too and
  a naive listener would switch the animation off on its own first frame.
- **Rotation, the progress bar and the auto-scroll all run on one `requestAnimationFrame`
  loop reading one clock** (`viewStartedAt`, a timestamp - not a seconds counter).
  Rotation used to be checked on a 1s tick, which can only notice that 20 seconds have
  passed at the first tick *after* they have: every slot ran 20-21s and "20 seconds per
  view" quietly meant something else. Don't move any of the three back onto a timer -
  they must not be able to disagree about how far through the slot the display is.
  `.kiosk-progress__fill` therefore has **no CSS transition**, and
  `.kiosk-view__body` must keep `scroll-behavior: auto`; either one would ease towards
  a target that has already moved.
- **A short view is centred by auto margins on `.kiosk-view__title` and
  `.kiosk-view__body`, never `justify-content: center`** - that centring mode makes
  overflowing content unreachable past the top of a scroll box, while an auto margin
  collapses to zero as soon as the space runs out and the view goes back to
  top-aligned and scrollable.
- **A failed poll raises the "reconnecting" banner after two consecutive failures.**
  A display left running for weeks that still *looks* live while showing week-old data
  is the failure mode that matters here; one failed poll is a blip, and crying wolf on
  every one of those trains whoever walks past to ignore it.
- **The cursor is hidden by a 3-second idle timer, not a blanket `cursor: none`** - so
  the display can still be used when somebody walks up to it.
- **The link is on `/` only, not in the shared `.page-nav`.** The display mirrors that
  page's own status; in the nav it would follow a visitor onto every sub-page. It's
  hidden entirely when kiosk mode is off, because the route 404s in that state.
- **What is and isn't verified**: rotation through all five views (slots measured at
  5.98-6.04s against a 6s setting), the auto-scroll travelling the full height and
  returning to the top within one slot at 800x480, the polling refresh keeping both its
  place and its scroll position while bringing in changed data with zero page reloads,
  the stale banner raising and clearing, cursor hiding, and no page overflow at
  1920x1080, 1024x768 or 800x480 were all confirmed by driving a real Chromium against
  a live server, and **the user confirmed the whole feature stable end to end on their
  own portal (2026-09-04)**, which is what promoted it to the `v1.8.5` release. The
  **VMs view was rendered from injected fake VM data** - this sandbox is Linux with no
  Hyper-V, so what that proves is the template and the gating, not VM detection. The
  `prefers-reduced-motion` fallback was never exercised on a television browser either.
  See `docs/HISTORY.md`.

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
- **The report notification is sent with `allow_mentions=False`**, which adds
  `allowed_mentions: {"parse": []}` to the Discord webhook payload: the text is a
  stranger's, and "@everyone" in it must not ping the admin's whole server. It's a
  keyword on `notifications.notify()` so every other webhook post (incidents,
  maintenance, the Games Portal relay) stays byte-for-byte what it was, and the
  bot's announcement posts don't go through this path at all.
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
