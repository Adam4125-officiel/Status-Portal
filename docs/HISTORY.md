# Project history — resolved bugs, post-mortems and verification records

This is the archive half of `CLAUDE.md`. It holds the **narrative** behind decisions
that are already settled: how a bug actually presented, what the wrong version did,
what was tried, and what was confirmed working on real hardware and when.

`CLAUDE.md` holds the **rules**. If a past bug produced a standing invariant ("must
stay level-triggered", "always go through `asset_url()`"), that rule lives in
`CLAUDE.md` in imperative form and links back here for the story. Nothing in this
file is a rule you need to read before working — it's here so that when you *do*
need to know why a rule exists, or whether something was ever actually tested against
the real thing, the answer wasn't thrown away.

Read this when:

- You're about to change something `CLAUDE.md` warns you about, and you want to know
  what happens if you get it wrong.
- You're tempted to "simplify" something that looks over-engineered — it may have
  been simple once, and there may be four bugs' worth of reasons it isn't now.
- You need to know what has genuinely been verified against real Windows / a real
  Discord server / a real instance, versus only unit-tested here.

Rough chronological range: 2026-07-22 through 2026-09-05.

---

## Health checks and the incident lifecycle

### The synchronous integration check that blocked every page load (fixed 2026-07-23)

The public page used to call the Jellyfin/*Arr status checker synchronously on every
single load. One unreachable integration added ~10 seconds to every page load,
including every 60-second auto-refresh. This is the bug that produced the standing
"never call slow/external I/O inside a request handler" rule and the
background-thread-plus-module-level-cache pattern (`app._integration_status_cache`)
that every later feature copied.

### `_handle_incident_lifecycle()` had to become level-triggered (fixed 2026-07-23)

The open side used to require `previous_status != "down"` before opening an incident —
i.e. it looked for a fresh transition into `down`.

Why that was wrong: `services.status` is written to `"down"` on every cycle regardless
of the startup grace period (only the *lifecycle call* is suppressed during grace, not
the status write). So a service whose downtime began inside its own grace window
already had `previous_status == "down"` the very first time the lifecycle function was
actually invoked. No "fresh transition" would ever be detected again, and the service
could stay down indefinitely with no incident ever opened.

Fixed by opening whenever `new_status == "down"`, full stop, and relying solely on the
pre-existing `get_open_auto_incident_for_service()` idempotency guard. The same fix was
applied to `_handle_integration_incident_lifecycle()`, which had the identical root
cause via `_integration_status_cache`.

**How it was caught matters**: live-testing against a real always-refusing HTTP server
with real `time.monotonic()` timing. Not by unit tests — every existing test called the
function with a hand-picked `previous_status="operational"` and never exercised the
grace-then-still-down sequence at all.

### Basic-auth services misread as degraded (fixed 2026-07-23)

Services that gate their entire UI behind HTTP Basic Auth (Bazarr being the concrete
case) answered their `check_url` with a non-2xx login prompt. The checker treated
non-2xx as degraded, so a completely healthy service showed as degraded permanently.

Fixed by redefining "reachable" as *got a response at all*, rather than *got a 2xx*.

### 502 split out from the rest of the 5xx bucket (added 2026-08-10)

`_check_status_for_response()` classifies a 502 as `down` rather than `degraded`.
Reasoning: a 500/503/504 means the service itself is up but erroring or overloaded,
whereas a 502 means whatever sits in front of it (reverse proxy, gateway) could not
reach it at all — functionally identical to a connection failure from a visitor's
point of view.

This composed for free with `retry_count` and `_handle_incident_lifecycle`, since both
were already keyed off `status == "down"` regardless of *why*. No separate code path
was needed.

---

## Public page and templates

### The missed service-card timestamp (2026-07-23)

The first pass at client-side local-time conversion covered the obvious spots —
announcements, incidents and their updates, maintenance windows — and missed the
service card's compact "upd. HH:MM" last-checked time, which kept rendering as raw
UTC.

Caught by the user testing rc.2, not by review and not by tests. The lesson that
became a rule: when adding a timestamp to the public page, grep for `_at[`, `[:16]`
and `[11:16]` across *every* template rather than assuming the obvious spots are the
only ones.

### The `<select multiple>` service picker (fixed 2026-08-10)

`admin_maintenance_form.html` rendered its multi-service `<select>` options with:

```jinja
'selected' if s.id in selected_ids or not window
```

Normal operator precedence makes that `(s.id in selected_ids) or (not window)`. On the
"new" form `window` is `None`, so **every** option rendered pre-selected regardless of
`selected_ids`.

Combined with the field hint telling the admin to ctrl/cmd-click to select more than
one, an admin who ctrl-clicked the single service they actually wanted thereby
*deselected* it while leaving every other service selected — submitting "everything
except the one I picked". That was the literal bug report.

Fixed by replacing the `<select multiple>` in both the maintenance and incident forms
with a checkbox list (`.checkbox-list` / `.field-check` in `static/css/style.css`).
`request.form.getlist("service_id")` reads identically from repeated checkboxes as
from a multi-select, so this needed zero `app.py` changes — and a checkbox's `checked`
state cannot have this class of bug at all, because no `or not window`-style fallback
expression is needed in the first place.

### `/api/incidents/more` — four pagination bugs in sequence (2026-08-10 → 2026-08-11)

Every one of these was caught live by the user, none by the test suite. Read all four
before "simplifying" the `?seen=` design back into something that looks tidier.

**1. Re-applying `max_age_days` on "load more".** Makes anything past the cutoff
permanently unreachable: the initial page hides it, and then the button hides it
again. The entire purpose of the button is to reveal what the initial view hid.
`api_maintenance_history()` drops the filter for the same reason.

**2. A positional `OFFSET`.** Counted against the *age-filtered* initial query, an
offset doesn't line up with an *unfiltered* continuation of that query — so it skips
past exactly the items that were supposed to be revealed.

**3. An `id < cursor` cursor.** Cannot express "everything I'm not already showing"
against a filtered view:
- Seeding from the **oldest** shown id skips anything hidden in an id-space *gap* — a
  still-open old incident can sit at a lower id than a newer resolved-and-hidden one.
- Seeding from the **newest** shown id instead re-returned every already-visible item
  below it, which made the button re-append the entire visible list on every click.
  That's the symptom the user reported as "completely broken, it loads the same
  indefinitely."

**4. The empty state didn't distinguish "no incidents exist" from "all hidden by
`max_age_days`"** (fixed 2026-08-11). With a filtered list of zero incidents,
`sections/incidents.html`'s load-more button — the only route to items 1-3 above —
lived entirely inside `{% if incidents %}`, so it vanished at exactly the moment it
was most needed, and the page claimed "No incidents recorded. All clear." while
incidents did in fact exist.

Fixed by having `index()` compute a separate `incidents_hidden` flag
(`not incidents and bool(db.list_incidents(limit=1))` — an unfiltered existence check
that only runs once the filtered list is already empty, so it costs no query in the
common case), and giving the template a third branch: nothing to show, but a distinct
message plus the same load-more button.

**Why excluding the shown ids is the only correct formulation**: it is simultaneously
gap-free and duplicate-free, because it states the intent directly instead of
approximating it with a position.

**The fails-closed narrowing.** The endpoint returns empty when `seen` is missing from
the query string *entirely*, or is oversized. A missing key is the genuine
stale-client signal (an old cached `public_history.js` sending `?offset=` instead —
see the cache-busting bug below), and answering that with "page 1" is precisely what
turned the stale script into an infinite duplicator. A `seen` key that is *present but
empty* (`?seen=`) is a different case and, since fix 4 above, legitimate: it's exactly
what the real button sends when the "all hidden" empty state is showing and there's
nothing on the page yet to list. The original fix treated both cases identically as
fails-closed — correct until item 4 gave an empty-but-present `seen` a real meaning.
There is a regression test from 2026-08-10 asserting the original merged behavior;
don't re-merge the two checks without re-breaking case 4.

### Stale cached JS shadowing a shipped fix (2026-08-10)

The documented update process is "extract the release zip over your existing folder",
which changes a JS file's *contents* but never its *URL*. Browsers kept serving the
previous release's cached copy.

A shipped `public_history.js` change (the pagination parameter switch above) was
silently shadowed this way, leaving an old script talking to a new endpoint: the
server ignored the obsolete parameter and re-returned the same page forever. This is
what produced the project-wide `asset_url()` rule. (`static/uploads/` logo URLs
already carried their own `?v=`, which is where the pattern was borrowed from.)

### `.incident-bubble` didn't read as a card (tightened 2026-08-01)

The first version had the right background, border, radius and padding, but was
missing `.service-card`'s `display: flex; flex-direction: column; gap` and its hover
border-transition. Close enough in a diff review to look fine; the user reported it
back as "not reading as a card."

The nested `.incident-updates` status-update list got a left border and a per-row dot
for the same reason — a flat list under a card didn't communicate "these updates
belong to this incident" without an explicit hierarchy cue.

Process note that came out of this: for a visual bug report, render a quick preview
using the app's actual CSS tokens and confirm the fix looks right before pushing,
rather than reasoning about CSS in the abstract.

### An admin `<h1>` drifting from its `{% block title %}` (caught 2026-08-11)

`admin_incidents.html`'s `<title>` correctly said "Incidents — Admin", but its `<h1>`
still read "Incidents & maintenance", stale from before Maintenance was split out into
its own nav item and page. The `<h1>` is what the user actually saw and found
confusing; a `<title>` is invisible unless you're looking at the browser tab.

Every other `admin_*.html` page's `<h1>` / `block title` / nav label matched 3-for-3
when surveyed — this was the only mismatch, not a systemic problem.

---

## Security fixes

### VM-name XSS via an inline `onsubmit` handler (fixed 2026-08-01)

The Hyper-V VM-name confirmation dialog interpolated the VM name straight into an
inline `onsubmit="return confirm('...' + x + '...')"` attribute.

Jinja's HTML-attribute escaping does not protect a value that the browser
HTML-decodes and then hands to the JS engine a *second* time as code. A VM name
containing a `'` — Hyper-V doesn't forbid it, and whoever can create or rename a VM
on the host isn't necessarily the portal's own web admin — could break out and inject
script.

Fixed by moving the value into a plain `data-*` attribute and reading it from a JS
listener instead (`static/js/admin_vm_control.js`), where it is only ever used as a JS
*string value*, never re-inserted into HTML or re-evaluated as code.

Found by a `/code-review`-style security pass over the session's accumulated diff —
not by manual review and not by the existing test suite.

### 2FA enrollment `KeyError` on a POST with nothing pending (fixed 2026-08-01)

`/admin/2fa/enable` crashed with a bare `KeyError` if a POST arrived with no pending
secret in the session — e.g. the session expired between the GET and the POST, or a
direct POST. Fixed by unconditionally ensuring a pending secret exists before
rendering, rather than only doing so in the GET branch.

Caught by live testing, not by the unit tests, which only covered the happy path.

---

## Integrations

### Byparr's `/health` timeout (fixed 2026-08-11)

A real user hit `Read timed out (read timeout=5)` against a genuinely reachable Byparr
instance.

Checked against Byparr's own source (`src/endpoints.py`): `/health` doesn't just ping
the process — it makes Byparr navigate to google.com and solve a real Cloudflare
challenge before responding. There is no lighter endpoint documented anywhere to
switch to, so `/health` was already the correct and only choice. The bug was purely
that the shared `TIMEOUT = 5` constant, fine for every other fetcher's plain fast REST
call, was far too short for this one specifically slow endpoint.

### The combined wizard's missing field set (fixed 2026-08-11)

`/admin/new/combined` used to be a completely separate, much smaller form than
`admin_service_form.html`: it only rendered and submitted
`name`/`icon`/`description`/`url`/`group_name` plus the integration fields.

Consequence: the `service_default_*` settings were never reachable from the wizard at
all, so `db.create_service()` silently fell back to its own hardcoded literals
(`0`, `5`, `"off"`…) instead of what the admin had actually configured. Anyone who
wanted retry/threshold/grace/API-health-mode set had to create the service through the
wizard and then immediately go and edit it.

Fixed by giving `admin_new_combined.html` every field `admin_service_form.html` has
(minus the extra-links repeater, which isn't on the plain "New service" form either —
not a wizard-specific gap), and updating `admin_new_combined()`'s POST handler to build
the same kind of `data` dict `admin_service_new()` does before calling
`db.create_service()`.

---

## Discord bot

### The prefix-command → slash-command rewrite (2026-07-22)

An earlier version matched a literal `!status` in `on_message`. It worked, but needed
Discord's privileged **Message Content** intent. Switched to slash commands on request,
backed by web research confirming Discord's own current guidance: prefix-command
convenience is not accepted as a justification for that intent, and slash commands need
no privileged intent at all. Setup no longer requires anything toggled in the Developer
Portal beyond inviting the bot.

### `build_snapshot_text()` was too vague to be useful (changed 2026-08-01)

The original rendered only an incident *count* ("3 open incident(s)"). User feedback
was that this was useless — changed to full per-incident detail: title, description,
status, affected service(s), start time, and every update.

The formatting choices were also specifically requested, not incidental: a bold title
line per incident, everything else as a `>` blockquote (consecutive `>` lines render as
one continuous left-barred block in every Discord client, giving the same "nested
detail under a title" hierarchy the public page's `.incident-bubble` gives visually),
and a blank line between separate incidents so multiple open ones don't run together.

### `stop()`/`restart()` — closing the fire-and-forget gap (added 2026-08-10)

The module was entirely fire-and-forget before this: `start()`'s `_run()` closure
discarded both the `threading.Thread` and the `discord.Client` instance, so nothing
outside the module could ever command a running connection to shut down.

Fixed by having `_run()` manage its own `asyncio` event loop explicitly
(`loop.run_until_complete(runner())`, not the `client.run(...)` convenience wrapper)
and stashing `client`/`loop`/`thread` in a module-level `_runtime` dict *before* the
client starts connecting. That is what lets `stop()` call
`asyncio.run_coroutine_threadsafe(client.close(), loop)` from a *different* thread (an
admin route's request-handling thread) and then `thread.join(timeout)` to know the
connection genuinely closed rather than merely being asked nicely.

### `_edit_tracked_status_message()` — a bare `except` that forgot live messages (fixed 2026-08-10)

`_refresh()`'s loop over tracked `/status` messages used to wrap
`fetch_message()`/`msg.edit()` in a bare `except Exception:
db.delete_discord_status_message(...)`.

That treated *any* failure — a timed-out API call, a momentary network blip, anything
at all — exactly like "the message was deleted by someone": immediately forgetting it
and forcing a brand-new `/status` run to get it back, even though the message was
still perfectly fine. This was a real reliability bug the user hit.

Fixed by extracting the edit into its own method that only forgets the tracked row on
`discord.NotFound` (genuinely gone) or `discord.Forbidden` (access revoked).

### `on_resumed()` — the permanently stuck "not connected" panel (fixed 2026-08-11)

`on_disconnect()` sets `_state["connected"] = False` and fires for *any* dropped
gateway connection, including an ordinary blip that discord.py resumes on its own
without a fresh login.

The catch: a resumed session fires only `on_resumed()`, never `on_ready()` again
(`on_ready()` is for a fresh identify only). With no `on_resumed()` handler, nothing
ever set `connected` back to `True` after the first disconnect-and-resume cycle — so
the admin panel insisted the bot was offline while it kept answering the slash command
and editing its tracked `/status` message the entire time. Confirmed by the user.

### The restart button that did nothing, and the loop that couldn't answer (fixed 2026-09-03)

**Reported as one bug, and it was two.** The bot would go offline on its own after
running for a while; restarting it from `/admin/system` did nothing at all, and only
restarting the whole process brought it back.

**The half with a definite cause.** `stop()` schedules `client.close()` onto the bot's
own event loop and waits on the future, then joins the thread. Both of those have
timeouts, and both were being allowed to expire quietly: the `.result(timeout=…)` raise
was caught and logged, `thread.join(timeout)` simply returned with the thread still
alive, and `_runtime` was left pointing at the connection that never went away —
because the only place clearing it is `_run()`'s `finally`, and `_run()` had not
finished. `start()`, called immediately afterwards by `restart()`, begins with
`if _runtime["client"] is not None: return`. So the button ran, flashed "Discord bot
restarted", and did nothing, for as long as the process lived. Every ingredient of that
was visible in the code; nothing in the test suite covered a `stop()` whose thread
refuses to end, because the fake runtime the existing tests used always stopped when
asked.

The fix is that `stop()` must leave `_runtime` in a state `start()` can act on, always
— it now reports whether the connection genuinely ended, abandons a wedged one, and
schedules `loop.stop()` onto it so the abandoned thread tears itself down the moment it
unblocks. `_forget_runtime()` carries the identity check that makes abandonment safe: a
late-finishing abandoned run must not clear the runtime of the connection that replaced
it, or `stop()` would have nothing to command and `start()` would be free to open a
second connection on the same token.

**The half that is still a suspect, not a conclusion.** Why it dropped in the first
place is unproven. The leading candidate is that `_refresh()` did all its reading
synchronously inside a coroutine — a dozen SQLite queries, and (whenever any resource
toggle is on) `monitoring.get_resource_snapshot()`, which falls back to a *blocking*
0.2s psutil CPU sample when its cache is stale and walks every mountpoint with
`psutil.disk_usage()`, a call that can stall for seconds on a sleeping or disconnected
drive. That runs on the same event loop that answers Discord's heartbeat. All of it now
goes through `asyncio.to_thread()`, which is right regardless of whether it was the
cause. **What would confirm it** is `instance/logs/app.log` containing discord.py's own
`heartbeat blocked` or `Shard ID None has stopped responding to the gateway` warnings —
logging is configured globally, so if it happened, those lines are already there.

**Not reproducible here**: this sandbox has no Discord gateway. What *was* exercised
live, against a real running portal with a deliberately invalid token (so the bot
thread dies on its own), is the whole failure-visibility path — the System page
correctly reading "Its connection thread isn't running (offline for 24s) — nothing is
currently retrying. Last error: Improper token has been passed.", the watchdog task
detecting the dead thread and reporting "bot was not running; started it", and the
restart button returning a clean success. The wedged-loop case itself is covered by a
test that fails on the previous implementation, not by a live repro.

### Verification record — the bot fixes, real Discord server, 2026-09-03

The user ran v1.8.3-rc.1 against their own portal and their own Discord bot and
reported it working end to end, specifically calling out the watchdog. That covers
the parts this sandbox cannot: a real gateway connection, the watchdog acting on a
real bot, and the restart path against a connection that genuinely exists.

**What that does and does not settle.** It settles the restart path and the watchdog.
It does *not* settle why the bot was dropping in the first place — the heartbeat
starvation theory (see above) predicts exactly this outcome, but so would several
other things, and "it stopped happening" is not a cause. The theory is deliberately
kept written down rather than closed out, with the two log lines that would confirm
it, because the cheapest moment to test it is the next time it recurs; there is a
matching entry in `ROADMAP.md` → "Known issues to investigate" so it isn't only
recorded in an archive nobody reads until something breaks.

---

## Self-update

### `update.py rollback --emergency` exists because of a live-testing find (2026-08-10)

The first version of `update.py` imported `updater` at module level. A real end-to-end
test — updating a throwaway install to an actual release whose `config.py` predated
`APP_ROOT` — left a tree where `updater.py` no longer imported. The designated recovery
tool was itself broken by the update it existed to undo.

Fixed by making every `update.py` import lazy and adding a self-contained emergency
path that reads only the `manifest.json` `updater.py` already wrote. Found by live
testing, not by review.

### The em-dash `UnicodeEncodeError` (2026-08-10)

An em dash in `update.py`'s header line raised `UnicodeEncodeError` on a Windows
console using codepage 437. Found while writing the user's test instructions, before it
ever shipped. This is the recovery tool — it must not be able to fail on a decorative
character.

### `_prune_backups()` and default-argument binding (2026-08-10)

`_prune_backups()` reads the `KEEP_BACKUPS` module constant *inside* the function
rather than as a default argument value. A default arg binds at `def` time, which
silently ignores a monkeypatched constant — and made the pruning test pass for the
wrong reason until it was fixed.

### Verification record — sandbox, 2026-08-10

In the Linux sandbox: real end-to-end updates against the actual GitHub API and real
release zips, in throwaway installs. Real SHA-256 verification, 83/90 files replaced,
`instance/portal.db` / `.env` / `static/uploads/` confirmed byte-identical afterwards, a
real rollback, a real `--emergency` rollback, and a re-run correctly no-opping as "up
to date". The About page, the channel form, "Check now", the step-up-2FA refusal and
the kill-switch refusal were all exercised live against a running server.

Also confirmed live that day: GitHub *does* publish a `digest` field on release assets,
so the SHA-256 path is the one that actually runs, not the size-only fallback.

### Verification record — real Windows, 2026-08-10

The user updated an installed `v1.5.0-rc.2` to `v1.5.0-rc.3` **entirely through the
admin panel's button**, with waitress serving and the Discord bot connected at the
time, then rolled back with the CLI. Specifically confirmed:

- **`os.execv` in-place restart works on Windows.** The process came back ~2s later,
  re-bound port 5000, and the Discord bot reconnected on its own. This was the single
  biggest unknown in the whole feature.
- **`write_pending_marker()`/`check_pending_marker()` work end to end**: the restarted
  process logged "Update to 1.5.0-rc.3 completed - the app restarted successfully on
  the new version" and cleared the marker.
- **No Windows file-locking failure occurred** while replacing 90 files with the server
  live. Corollary: `REPLACE_RETRY_ATTEMPTS` and the retry-then-roll-back path remain
  **unexercised in the wild** — they didn't need to fire, which is not the same as
  having been proven to work.
- **The browser-side typed-confirmation JS works** — reaching the update at all
  required typing `UPDATE` to enable the submit button.
- `python update.py rollback` restored all 90 files on Windows.

**Still unverified** as of that date: relaunch under Task Scheduler after `os.execv`
(the user ran `python serve_waitress.py` from PowerShell directly, so a supervisor's
reaction to the in-place restart is unknown); the Windows file-lock retry/rollback
path; and `pip install` actually running during an update, since no release so far has
changed `requirements.txt`.

---

## Monitoring

### CPU and disk temperature on real hardware (confirmed 2026-07-23)

CPU temp via the ACPI thermal zone WMI namespace returns nothing at all on the user's
desktop. This is common rather than a bug: that WMI class is really meant for
laptops and OEM systems with ACPI-exposed thermal zones, not enthusiast desktop boards
that read sensors via the Super I/O / EC chip (which is what tools like HWiNFO do
instead).

`Get-StorageReliabilityCounter` also returned a literal `0` — not null — for one drive.
`_query_windows_disk_details()` now treats `0` the same as "no reading", since a real
drive is never 0°C, rather than displaying it as a genuine reading.

Neither of these swapped in a better data source. See `ROADMAP.md` → "More reliable
CPU/disk temperature via HWiNFO" for the two options considered and why neither was
built. Don't assume this area is fully fixed just because the obviously-wrong `0°C`
display is gone.

---

## Sessions and performance (v1.6.1, 2026-08-19)

### The random-logout / never-logout pair

Two separate reports that turned out to be two different bugs wearing one description
("session handling is inconsistent"):

**"A refresh sometimes logs me out mid-session."** `config.SECRET_KEY` fell back to
`"change-me-in-prod-" + os.urandom(8).hex()` whenever `PORTAL_SECRET_KEY` was unset —
a *different key every process start*. Flask signs the session cookie with it, so
every restart silently invalidated every logged-in session, and the next request
looked like a spontaneous logout. What made it feel random rather than
restart-shaped is how many things restart this app without the user thinking of it as
a restart: the in-app updater `os.execv`s itself, `/admin/system` has a restart
button, and a systemd/Task Scheduler unit restarts on failure. `.env.example` had
even documented the symptom ("without this, every restart logs you out of /admin") —
it was treated as expected behavior rather than a bug, which is why it survived this
long.

**"On other devices a single login lasts forever."** Nothing ever set
`session.permanent`, so Flask emitted a *browser-session* cookie: no `Max-Age`, so it
dies when the browser process exits. On a desktop that means being logged out
whenever the browser is closed; on a phone, whose browser is essentially never
closed, it means staying logged in indefinitely. Same code, opposite behavior, purely
by device habit — and no expiry anywhere on the server side to bound either.

The fix for the pair: persist a generated key to `instance/secret_key`, mark sessions
permanent with an explicit `Max-Age`, and add a server-side sliding idle timeout
(`session["last_seen"]`, default 12h, admin-configurable). Verified live: logged in,
killed and restarted `python app.py`, reused the same cookie jar — `200`, still signed
in, and `instance/secret_key` unchanged across both runs. The cookie jar showed a
30-day expiry instead of the previous `0` (session cookie).

### Refresh slowness: the suspected cause wasn't the cause

The report was "the client-side refresh cycle feels excessively slow", suspected to be
each refresh re-pinging every service synchronously. It wasn't: service checks have
always run in the background health-check thread, and `index()` only ever reads
`services.status` out of the database. Worth stating because it's the kind of guess
that's easy to "confirm" by finding *something* slow nearby and stopping there.

The measured cause was `db.get_uptime_percentage()`. It ran **once per service on
every public page load**, and each call was a full table scan of `status_history` —
a table with **no indexes at all** anywhere in the schema, which gains a row per
service per check *forever* and was never pruned. Benchmarked against a synthetic 90
days of history for 17 services (1.1M rows, 64 MB): **1042 ms per page load**, growing
without bound. On the user's real hardware (spinning disks, a loaded host) that is
comfortably seconds.

Three changes, each measured: one grouped `GROUP BY service_id` aggregate instead of N
queries materializing every row into Python; a **covering** index
`(service_id, checked_at, status)` — the `status` column is what lets SQLite answer
without touching the table at all, 131 ms → 43 ms, and a plain two-column index drops
straight back to a table scan; and a 60s TTL cache, since a 30-day uptime percentage
cannot meaningfully change between two page loads 60 seconds apart. Public page load
went from ~1 s to 13–45 ms locally.

Two smaller things found in the same pass: `psutil.cpu_percent(interval=0.2)` *sleeps*
0.2 s and was being called from inside request handlers (moved to the background
thread, which until then had been a no-op on non-Windows entirely), and
`GetVolumeInformationW` — which can block until a spun-down Windows drive spins up —
ran per disk per page load rather than once per device.

### `CLAUDE.md` said Playwright was pre-installed. It wasn't.

`CLAUDE.md` had instructed sessions to use a "pre-installed Playwright/Chromium
browser" and explicitly *not* to run `playwright install`. On 2026-08-19 neither
existed in the sandbox — no `playwright` module, no `ms-playwright` cache, no
chromium on `PATH` — so the browser-verification step documented as mandatory was
quietly skipped for a whole release instead.

Worth recording because of the shape of the mistake rather than the mistake itself: a
note about the *environment* went stale in a file whose staleness checks are all
aimed at the *code*. The corrected instruction is simply to install it
(`pip install playwright && python -m playwright install --with-deps chromium`, about
a minute) rather than to trust a claim about what's already there.

### "The portal stops responding when the host is loaded"

Raised as a separate, root-cause-unknown symptom. Two structural contributors were
confirmed by inspection, on top of the page-load cost above:

1. **SQLite was in `delete` (rollback journal) mode**, where readers and writers block
   each other database-wide. The background health-check thread writes every cycle
   (two writes per service), so any request touching the database waits on that lock —
   up to the 5 s busy timeout, then fails outright with "database is locked". Now WAL.
2. **waitress runs 4 request threads by default.** With requests that could each take
   a second or more, four concurrent visitors (or four auto-refreshing tabs) is
   enough to leave the portal answering nothing at all. Now `PORTAL_WAITRESS_THREADS`,
   default 12.

Neither was reproduced under real load — this is a diagnosis from the code, not a
confirmed fix. If it recurs, that's the thing to say: the remaining candidate is
simply the host being CPU-starved, which no amount of application tuning fixes.

---

## Scheduled tasks and Jellyfin user accounts (v1.7.0, 2026-08-21)

### The two silent no-op writes

Both bugs the test suite caught while the scheduler framework was being written were
the same shape, and neither would have raised anything. `record_task_run()` and
`update_task_schedule()` are `UPDATE` statements; a task whose row had never been
materialised (because nobody had opened its settings page yet) simply had nothing
updated. So "Run now" ran the task correctly, reported success, and recorded no
result at all — and saving a schedule appeared to save and changed nothing.

Both now go through `scheduler.run_task()` / `scheduler.save_schedule()`, which
ensure the row exists first. The lesson worth keeping: in a schema where rows are
created lazily, every `UPDATE` needs to know who guarantees the row.

### A convention test that fired on the comment explaining the convention

`test_the_visitor_session_helper_never_touches_admin_session_keys` checks that
`_end_user_session()` doesn't call `session.clear()`. Its first version searched the
unparsed function — including the docstring, which says *"Never session.clear() —
an admin who is also signed in..."*. The test failed on the sentence explaining why
it must not happen.

Fixed by stripping the docstring before inspecting. This is exactly the false
positive `CLAUDE.md` warns about: a check that fires on correct code teaches people
to ignore the file it lives in.

### What "fall back to the cached user list" could and couldn't mean

The original brief asked for authentication to "fall back to the cached user list"
when Jellyfin is unreachable. That turned out not to be implementable as stated, and
the reasoning is worth keeping because it will come up again:

Jellyfin does not expose password hashes over its API (and must not), so the cached
list contains no material to verify a password against. "Falling back" to it could
only mean accepting any username that appears in it — which is not degraded
authentication, it is none.

What was built instead: sign-in always requires a live answer from Jellyfin, and an
outage refuses *new* sign-ins while leaving every *existing* session working
(validated against the cache, never against Jellyfin). Nobody new gets in, everybody
already in stays in.

The awkward consequence, flagged rather than hidden: with `/report` gated behind
sign-in, a visitor who has never signed in cannot report an outage *during* that
outage. Hence `report_requires_login` being a setting an admin can turn off, and the
admin page saying so explicitly.

### First real-Jellyfin feedback on rc.1 (2026-08-21)

The integration itself worked first time against Adam's real Jellyfin — the part
that could only be guessed at in this sandbox (the `/Users` and
`/Users/AuthenticateByName` response shapes) turned out to be right. Three things
came back from actually using it:

**The sign-in link was invisible.** It was a plain text hyperlink in the topbar, in
the same faint monospace as "next refresh in 60s", and it scrolled away. Replaced
with a solid pill button in a fixed cluster shared with the theme toggle.

That change then produced a bug that only a browser could show: the fixed cluster
rendered *on top of* the topbar's right-hand text. Every route-level test passed,
because the HTML was perfectly correct — the elements just occupied the same
pixels. Fixed by reserving padding on `.topbar`, and verified with a scripted
bounding-rect overlap check at both desktop and phone widths rather than by
squinting at a screenshot. Worth remembering as the general shape: **route tests
cannot see layout at all**, and "it renders" is not "it's readable".

**"Report with the username" was already built.** Verified live before writing any
code — a signed-in user's report stored and displayed their Jellyfin username
correctly. It just wasn't obvious in a seven-column table. The genuine gap was
next door: "create incident from this report" dropped the reporter entirely, so the
one action an admin takes on a report was the one place the attribution vanished.

**Per-user blocking** was a new request, and its one real trap is worth recording
because it would have been silent: `replace_jellyfin_users()` is a full
delete-and-reinsert, so an admin-set flag that isn't explicitly carried across the
sync gets wiped on the next run — blocking someone, then finding them able to sign
in again an hour later, with nothing in any log to explain it.

### The account page, and two bugs only a screenshot could find (2026-08-21)

Adam's feedback after end-to-end testing rc.2 was that a report went into a black
hole — no way to see whether anyone had looked at it, what came of it, or to hear
anything back. Hence `/account`.

Almost all of it surfaces facts that already existed in the database. Worth
recording because it's a recurring shape in this project: the feature that felt
missing wasn't missing data, it was missing *visibility* of data the admin could
already see.

**The theme preference was the genuinely hard part**, and not for any reason visible
in the requirement. It has three inputs (this browser's `localStorage`, the account
preference, the OS) and — because of the anti-flash script — *two* independent
implementations of the precedence order. The failure modes are all quiet:

- If the inline script and `theme.js` disagree, the page loads in one colour and
  switches a moment later.
- If `localStorage` outranks the account preference (which it must — it's the more
  recent deliberate action on that device), then saving "Light" on the account page
  changes every other device and visibly *not* the one you're sitting at. That is
  the most confusing possible outcome, and it's the default behaviour unless
  something explicitly syncs the local value after a save.
- If "Auto" doesn't *remove* the local override, it keeps whatever was last toggled
  on that device forever, which is not what "auto" means.

All three were driven through a real browser across seven scenarios rather than
reasoned about.

**Two bugs a screenshot caught and the test suite could not**, both in the same
render: the admin reply textarea kept the browser's default white background,
unreadable in dark mode, because the shared input styling is scoped to `.field` —
a block-level form row, the wrong shape for a control sitting in a table cell. And
the reports table's timestamp was raw UTC, never having used the `local-time`
conversion every other timestamp in the app goes through. Both had passed every
route-level test, which asserts on content and cannot see colour or layout at all.

### One column, then a table: the report reply (2026-08-21)

The reply shipped in rc.3 as a single `admin_reply` column, which was exactly right
for what was asked ("let the admin answer") and exactly wrong the moment the next
request arrived ("let the user reply back"). Replacing it with a `report_messages`
thread was the obvious fix; two details were not:

- **The old column had to be backfilled, not abandoned.** rc.2 and rc.3 were running
  on a real server with real replies in that column. Dropping it would have silently
  deleted somebody's conversation on update. It's seeded into the thread by an
  idempotent one-time insert in `init_db()`, the same shape as the two
  multi-service backfills that predate it, and the column is left in place unread.
- **The admin needed an unread signal too.** The single-reply version only ever had
  one direction to notify, so only the user had a badge. Making it two-way without
  adding the admin's half would have produced a conversation where the admin never
  learns anyone answered — which is worse than no reply feature at all, because the
  user reasonably assumes their message was received.

Also worth noting for its own sake: editing was dropped in the move. The single
column allowed rewriting a reply in place, which meant the other side could be
looking at text that no longer existed. Append-only is the correct shape for a
conversation; a correction is just another message.

### Verification record — sandbox, 2026-08-21

Exercised against a **stand-in Jellyfin** — a small HTTP server implementing
`/Users`, `/Users/AuthenticateByName` and `/Sessions/Logout` from Jellyfin's
documented response shapes — because no real Jellyfin exists in this sandbox:

- user sync populating the cache (3 users, including a disabled one);
- sign-in with a correct password, a wrong password, and a disabled account;
- the short-lived access token being revoked immediately (observed arriving at the
  stand-in server's `/Sessions/Logout`);
- a signed-in visitor refused on `/admin`, `/admin/services`, `/admin/settings`,
  `/admin/users`, `/admin/tasks`, `/admin/resources`;
- authenticated reporting, with the reporter's Jellyfin username recorded on the row;
- **Jellyfin taken down mid-session**: the signed-in session survived repeated
  requests, a new sign-in was refused with the "can't reach Jellyfin" message rather
  than a password error, and a sync attempt during the outage failed while leaving
  all 3 cached users intact;
- a user removed from Jellyfin losing their session on the next request after a sync;
- **a real process restart** against the same database: the signed-in session and the
  task's schedule and last-run history both survived.

Also driven through a real Chromium (Playwright), which is what confirmed
`static/js/csrf.js` actually injects the token into the two new forms — `/login` is
CSRF-protected, so had it not, nobody could have signed in from a browser at all
while every curl-based test still passed. No console errors on any new page.

**Not verified**: any real Jellyfin instance. The response shapes of `/Users` and
`/Users/AuthenticateByName` come from Jellyfin's API documentation, not from
observing a running server, and the `Authorization` / `X-Emby-Authorization` header
pair is belt-and-braces for older builds rather than something confirmed necessary.

## Database restore, and the restart that never came back (v1.8.0, 2026-08-21)

### The restart that never came back

Found while live-testing the new database restore, which ends by re-executing the
process. The restore itself worked perfectly - the right data came back, the safety
snapshot was written - and then the portal simply did not return. No process, nothing
listening on 5000, and nothing in the app log. The only trace was two lines at the very
bottom of the console output:

```
 * Serving Flask app 'app'
Address already in use
Port 5000 is in use by another program.
```

The cause is a deliberate Werkzeug behaviour meeting a deliberate one of ours.
`werkzeug.serving.run_simple()` calls `srv.socket.set_inheritable(True)` and exports
the descriptor as `WERKZEUG_SERVER_FD`, so that its auto-reloader can hand the same
bound port to a child process. Marking the socket inheritable is exactly what stops
`os.execv` from closing it. But the re-executed process only *adopts* that descriptor
when the reloader is active (`WERKZEUG_RUN_MAIN`), which it never is here - the app
runs `debug=False`. So the new image tried to bind a port its own previous image was
still holding, failed, and exited.

Three things about this are worth remembering:

- **It was never a restore bug.** `_restart_process()` is used by `/admin/system`'s
  restart button and by the self-updater, both of which shipped long before this. Any
  install run as `python app.py` - which is what the README's Quick start tells you to
  do - has had a restart button that killed the portal, and a self-updater that
  installed the update and then took the portal down for good.
- **Production was never affected**, which is why nobody noticed. `serve_waitress.py`
  is the documented production entry point and waitress never marks its socket
  inheritable, so there the descriptor is closed at exec and the rebind succeeds.
- **The mocked tests could not have found it.** They assert that the route *called*
  `_restart_process()`, which was true and remained true throughout. Nothing short of
  actually restarting a real process and asking whether it came back would have caught
  this - and CLAUDE.md at the time said not to do that. That rule has been amended:
  `control_host()` (reboots the machine) stays never-live; `_restart_process()` (re-
  execs this app) now has to be exercised live at least once when restart behaviour
  changes.

The fix is `app._release_dev_server_socket()`: pop `WERKZEUG_SERVER_FD` from the
environment and close that descriptor immediately before `os.execv`. It is a no-op
under waitress, and any failure is swallowed - not being able to close a socket must
never be the reason a restart doesn't happen. Verified two ways: an isolated probe
that binds a port the way `run_simple` does, execs itself, and reports whether the
rebind succeeded (fails without the fix, succeeds with it), and a real end-to-end
restore showing the same PID answering HTTP 200 two seconds later.

### What the restore was tested against

A real backup produced by the existing "Download a backup" button, on a live server:
a service added *after* the backup was taken vanished on restore and one present in
the backup came back, proving the file was genuinely replaced rather than merged. The
refusal paths were exercised live too - a text file, and a valid SQLite database
containing an unrelated `movies` table - and in both cases the live database was
byte-unchanged and no safety snapshot was written.

Not tested against: a database large enough to approach the 64 MB cap, a genuinely
corrupt-but-openable database (the corruption test mangles bytes and SQLite rejects it
at open time rather than at `integrity_check`), and Windows, where `os.replace` over a
file another process holds open behaves differently than it does here.

### The extraction cap that couldn't benefit from zip compression (fixed 2026-08-30)

The gap flagged above ("not tested against: a database large enough to approach the
64 MB cap") turned out to hide a real bug, found live-testing a restore on the user's
actual database: a 140 MB `portal.db`, zipped by the existing "Download a backup"
button down to 30 MB - comfortably under the 64 MB upload cap - was refused at
restore with `"The database inside that zip is too large (140 MB)"`.

The cause: `MAX_EXTRACTED_DB_BYTES` (the cap on the database's own size once
extracted from the zip - the actual zip-bomb guard, checked against bytes genuinely
read during extraction, never the zip's declared size) had been set to the exact same
value as `DB_RESTORE_MAX_BYTES` (the cap on the raw upload). That's backwards from
what accepting a zip is *for*: the whole point of zipping the backup at all is to let
a database larger than the upload cap through via compression, and a real SQLite
database compresses well - `status_history` especially, being mostly repeated status
strings and near-identical timestamps. Reusing the same number for both caps meant
compression could never actually buy anything; a database whose *uncompressed* size
exceeded 64 MB was refused regardless of how small the upload itself was.

This was pre-existing since the restore feature shipped in v1.8.0 - not introduced by
whatever change was being tested when it was found - confirmed by checking the same
line out on `main` before any other changes that session.

Fixed by making the two genuinely independent: `DB_RESTORE_MAX_BYTES` (upload cap)
raised to 128 MB for headroom, `MAX_EXTRACTED_DB_BYTES` (uncompressed-size cap) set
independently to 512 MB. Verified with a synthetic zip mirroring the real case (a
highly-compressible ~140 MB payload, compressing to well under 1 MB) now succeeding,
and a 600 MB declared inner size still correctly refused - the zip-bomb guard
mechanism itself was never the problem, only the cap value.

### The restore that couldn't replace its own open file (fixed 2026-08-30)

Reported from a real Windows install, immediately after the extraction-cap fix above
let a restore actually reach the replace step for the first time:

```
Restore failed: [WinError 5] Access is denied:
'...\instance\restore-88857qr6.db' -> '...\instance\portal.db'
```

Running as administrator, which ruled out a plain permissions problem and pointed at
file locking instead. The cause was a second bug in the same v1.8.2 performance
batch: `db.get_db()` had just been changed (a few commits earlier, same session) to
return one pooled connection per request instead of a fresh connection per call (see
CLAUDE.md's "Sessions, caching and DB performance" section for the full mechanism).
That pooled connection is opened at the very start of the request by `app.py`'s first
`before_request` hook and stays open for the request's entire duration - including
through `admin_restore_db()`'s own call into `db.restore_from_file()`, which ends
with `os.replace(staged, DB_PATH)`.

On POSIX, a rename succeeds regardless of who has the destination file open - the old
inode just stays valid for whoever already had it open. Windows has no equivalent
guarantee: `os.replace()` (via `MoveFileExW`) can fail with `ERROR_ACCESS_DENIED` if
*anything* still holds the destination open without `FILE_SHARE_DELETE`, and nothing
about SQLite's default Windows VFS guarantees that flag is set. The pooled
connection - opened by this same thread, for this same request, specifically because
of the change that had just shipped - was exactly such a handle. This sandbox is
Linux-only, so the full pytest suite and every manual restore test that session
passed cleanly; the failure mode structurally cannot reproduce outside Windows.

Fixed by having `db.restore_from_file()` call `db.end_request_scope()`
unconditionally before doing anything else - releasing the calling thread's pooled
connection (a safe no-op if there wasn't one, e.g. called from the `update.py` CLI)
before the checkpoint-then-replace sequence runs. Verified two ways, since the actual
Windows failure can't be reproduced here: a direct test asserting `db.get_db()`
returns a different (i.e. genuinely released, not the stale closed one) connection
after `restore_from_file()` runs inside a request scope, and confirming that test
fails without the fix and passes with it - proving it actually catches the class of
bug being fixed, not just that the code executes.

**Lesson worth remembering**: this shipped in the very same batch that introduced
`get_db()`'s pooling in the first place, and the original review of that change did
consider the database-restore route specifically (see its own commit message) - but
only reasoned about *other requests'* stale connections, not about the *current*
request's own pooled connection blocking its *own* replace. A change that holds a
resource open for a request's full duration needs checking against everything that
same request does before it ends, not just against what runs elsewhere.

## The log page, and keeping your place (v1.8.4, 2026-09-03)

### The save button that always scrolled you to the top

Reported while testing the new log page: "each time I click on apply it brings me to
the top of the page — and that's a bug not only with that apply button but anytime I
click on something like a save button".

The second half is what mattered. This was never a log-page bug: **every** admin form
POSTs and redirects back to the same page, and a browser starts a fresh page at the
top, so saving one field at the bottom of Settings meant scrolling all the way back
down. It had been true since the admin panel existed, and had simply never been named
— a good illustration of why "the user tests things and finds what you missed" is in
`CLAUDE.md` as a standing note rather than a compliment.

Two things came out of the fix that are easy to get wrong later:

- **`form.submit()` does not fire the `submit` event.** `admin_toggle.js` submitted
  that way, so a document-level listener never heard about it and flipping a switch
  far down a table would have kept jumping to the top while every other form behaved.
  `form.requestSubmit()` is the version that fires the event.
- **Restoring the scroll position hides the confirmation.** "Settings updated."
  renders at the top of the document; keep the reader at the bottom and they see
  nothing at all. The flash had to become a pinned message in the same change — the
  two are one feature, not a fix plus a decoration.

### The scroll compensation that ran twice

The live log tail trims old entries off the top as new ones arrive. Doing that shifts
everything below upward, so someone reading further up would watch the text slide out
from under them — the JS therefore measures the height it removed and subtracts it
from `scrollTop`.

In Chrome that made it *worse*: the browser implements scroll anchoring and had
already done exactly that adjustment, so the text slid **down** by the trimmed height
on every single update. Relying on the browser instead wasn't an option either —
Safari doesn't implement scroll anchoring at all, so the same code would have been
correct in one browser and broken in the other. The fix is `overflow-anchor: none` on
`.log-view` plus the explicit JS compensation: one mechanism, same behaviour
everywhere.

Only visible in a real browser, and only while scrolled up with entries arriving. The
unit tests could not have seen it, and neither could a screenshot.

### Two bad assertions, one real bug, in the same test run

Worth recording because the failures looked alike. A live-tail browser check came back
with four failures: two were the test's own fault — marker text reused from an earlier
run so `count() == 1` matched twice, and an assertion that `scrollTop` must not change
when trimming means it *should* — and one was the genuine scroll-anchoring bug above.
The lesson is not "distrust the tests", it's that a failing check needs reading before
it is either believed or dismissed; three of those four looked equally like real bugs
at first glance.

### Why the log rotates daily rather than per run

The log page opened on entries from a month earlier, because rotation was size-based
(2 MB x 3 files) and this portal is quiet enough that that spans months. Rotating on
every start was the obvious alternative and is the wrong one here: this app restarts
*itself* — the in-app updater re-execs, `/admin/system` has a button, systemd restarts
on failure — so a crash-restart loop would blow through every retained file and delete
precisely the history that explains the crash. Daily rotation with a retention count
(`PORTAL_LOG_RETENTION_DAYS`, default 7) is time-anchored instead, and a
"portal starting" banner keeps individual runs findable inside a day's file.

### Verification record — v1.8.4, 2026-09-03

The user tested the whole thing end to end on their own portal and confirmed it
stable: the log page, the live tail's scroll behaviour, the download, and the
panel-wide scroll-position fix. In this sandbox the same behaviours were driven in
Chromium against a running portal at 320-1440px, appending to a real log file — but
"stable" here rests on their run, not that one.

## Kiosk mode (v1.8.5, 2026-09-04)

### Why it polls a fragment instead of reusing the page reload

The public page has refreshed itself with `window.location.reload()` since the
beginning, and the obvious way to build a rotating kiosk display was to leave that
alone and rotate on top of it. It doesn't work. A reload restarts the page, so the
rotation restarts with it — at the default 60s refresh and a 20s rotation, views three
and four are reached exactly never, and every view that *is* reached arrives with a
white flash, on a television, forever.

So `/kiosk/views` returns the rotating views as a server-rendered HTML fragment and
`kiosk.js` swaps it in without navigating, restoring the view that was on screen
before the swap. That is the same shape `/api/incidents/more` and `/admin/logs/tail`
already use, so it needed no new convention — only the discipline of having the page's
own first render `include` the *same* partial, so a polled view cannot drift from one
that was there at switch-on.

Confirmed in Chromium against a live server: the data underneath the display changed
(a service renamed directly in the database), the change appeared on screen, the
rotation stayed exactly where it was, and `performance.getEntriesByType('navigation')`
stayed at 1 — the page never reloaded.

### A test that reported a bug it had caused itself

The first version of that refresh check forced a later view on screen by toggling the
`kiosk-view--on` class from JavaScript, then asserted the view survived a poll. It
didn't: the display snapped back to Services, which looked exactly like the bug the
whole design existed to avoid.

It wasn't one. Toggling the class reaches the DOM but not `kiosk.js`'s own rotation
index, so the module still believed it was on view 1 and restored view 1 — correctly.
Rewritten to let the rotation advance on its own, the assertion passed. Worth recording
next to the log-page entry above, which makes the same point from the other side: a
failing check has to be read before it is either believed or dismissed.

### Two gates per view, because one would have been a way around a setting

`/kiosk` is public, and a wall display showing Hyper-V VM names is a different
disclosure from a status page showing service names. Gating the VMs view on its kiosk
checkbox alone would have meant an admin who had deliberately switched `show_public_vms`
off could publish exactly that data by ticking a box on a different page — the same
class of mistake the public sub-pages already guard against by putting the visibility
rule in one shared builder. So `vms` and `resources` hand `KIOSK_VIEWS` the *same*
predicates `/vms` and `/resources` use, and the settings form says out loud when a
ticked view is being held back by one of them.

### The small screen that showed the top third of a list (fixed 2026-09-04)

Reported against rc.1: "for small screens it should scroll down and up automatically
during these 20s". Correct — a view too tall for the screen just sat there showing its
first few rows, and on a 7" tablet that is most of them.

Each view now travels to the bottom and back within its own rotation slot, on fractions
that sum to 1 so it is back at the top when the rotation next reaches it. Two details
worth keeping:

- **It has no breakpoint and doesn't need one.** The trigger is whether
  `scrollHeight - clientHeight` actually exceeds a threshold, so "small screen" is
  measured rather than guessed — a television with six services never moves, the same
  page on a tablet does, and a television with forty services scrolls too, which a
  media query would have got wrong.
- **A manual scroll hands control over for the rest of the slot.** The naive version of
  that check switched the animation off on its own first frame, because assigning
  `scrollTop` fires a `scroll` event exactly like a human does. It compares against the
  position the animation last wrote instead.

### The one-second rotation tick that made "20 seconds" mean 20-to-21

Adding the auto-scroll meant tracking elapsed time as a timestamp rather than a seconds
counter, since a scroll that moves once a second reads as a fault. That immediately
exposed something the counter had been hiding: rotation was checked on a 1s
`setInterval`, which can only notice that 20 seconds have passed at the first tick
*after* they have. Every slot ran 20 to 21 seconds.

Small, and nobody would have filed it — but it surfaced as a test failure that looked
much worse than it was. A sampler sleeping for exactly the rotation interval drifted
against the slightly-longer real slots and reported a view appearing twice in a row,
which reads as a broken rotation. Rotation, the progress bar and the auto-scroll now
share one `requestAnimationFrame` loop and one clock; measured slots are 5.98-6.04s
against a 6s setting.

That test needed two corrections of its own before it was measuring anything, and both
are the same mistake in different clothes: sampling at a guessed cadence rather than
watching for transitions, and then treating the very first interval — which starts at
the test's first poll, not at the display's own slot start — as if it were a slot.
Third entry in this file about a check that had to be read carefully before being
believed or dismissed.

### Verification record — v1.8.5, 2026-09-04

Driven in a real Chromium against a live portal on a scratch database: rotation through
all five views and wrapping back round with slots measured at 5.98-6.04s against a 6s
setting, the auto-scroll travelling a 518px overflow and returning to the top inside one
20s slot at 800x480 while a 1920x1080 view that fits stayed still, the polling refresh
keeping both its place and its scroll position while bringing in changed data with zero
navigations, the "reconnecting" banner raising after two failed polls and clearing on
recovery, cursor hiding after idle, and no page overflow in either axis at 1920x1080,
1024x768 or 800x480, in both themes. Plus the full pytest
suite and `curl` against every new route.

**The VMs view was rendered from injected fake VM data.** This sandbox is Linux with no
Hyper-V, so `monitoring.get_cached_vm_snapshot()` returns an empty list and the view
would otherwise skip itself. What that screenshot proves is the template and the
two-level gating; it says nothing about VM detection, which remains Windows-only and
unverifiable here.

**The user then tested the whole feature end to end on their own portal and confirmed
it stable**, which is what promoted it from `-rc.2` to the `v1.8.5` release. That run
is the only evidence about real hardware in this entry: everything above it happened in
a Linux sandbox against a stand-in.

Two things remain reasoned about rather than observed even so. `prefers-reduced-motion`
on a television browser was never exercised — the discrete-cut fallback is written from
the spec, not from watching one do it. And the VMs view's *detection* half still has no
evidence behind it; if VM rows ever fail to appear on a real Hyper-V host, that path has
never actually run here.

The header of this file says the range stops at 2026-08-21; it now runs to 2026-09-04.

## Notifications, and the day one event became twenty emails (v1.8.6, 2026-09-05)

Reported by the user in one sentence: "when a scheduled maintenance starts, it will send
one e-mail per concerned service, so if i check 5 services, it will send 5 e-mails in a
row instead of one containing the 5 of them." They then asked whether Discord had the
same problem, and later confirmed ntfy did too. All three did, from one cause.

### One notification per service instead of one per window (fixed 2026-09-05)

`db.process_maintenance_windows()` returns one event **per service**, which is right:
that is the granularity the status flip and the per-service `pre_status` snapshot
operate on. `app._process_maintenance_and_notify()` looped straight over those events
and sent a full notification for each one.

So a window covering five services produced five `notifications.notify()` calls - and
`notify()` fans out to the Discord webhook, ntfy *and* email from the same call, which
is why one root cause presented on all three channels at once. It also queued five
per-user rows, each of which delivers both an email and a Discord DM, so an opted-in
visitor got five of each as well. Five ticked services, twenty-odd messages.

The fix groups the events back up by `(window id, transition)` before anything is sent.
The multi-service *incident* notification had been joining its service names since
multi-service support was added; maintenance simply never was.

Measured live afterwards, against a real server with a webhook receiver and a real SMTP
sink: a five-service window's start and end produced **four emails in total** (one admin
alert and one per-user copy, per transition) where the old code would have produced
twenty, plus exactly one Discord webhook post and one ntfy push per transition, each
naming all five services.

### "I didn't turn maintenance email on and got one anyway" - two email systems, no sign saying so

Reported in the same session. It turned out not to be a gating bug at all, and the
empirical check is worth recording because the conclusion was not the obvious one.

Set up on a live server with a real SMTP sink: a user whose `/account` page said
maintenance email was **off**, whose per-user queue was consequently **empty** - and who
still received "Maintenance started". The reason is that this app has two entirely
separate email paths and nothing anywhere said so:

- `notifications.notify()` -> `send_email()` goes to the **admin alert list**
  (`smtp_recipients`, Notifications -> Email). It sends every incident, maintenance and
  low-disk alert to everyone on that list and **ignores per-user preferences entirely**,
  by design - an alert list individual people could silence would not be an alert list.
- `user_notify.deliver()` is the per-user path, and is what the account page's
  checkboxes gate.

The user had put their own address on the admin list, which is the obvious thing to do
on a personal portal. Nothing in either page hinted that the checkbox did not govern it.
Both pages now say it plainly. The behaviour itself was left alone.

### The same event arriving twice (fixed 2026-09-05)

Following on from that, the user asked: "just check if i turn on both of them it doesn't
spam me twice (we never know)." It did. Verified live - one address, both paths on, two
emails seconds apart:

```
to=me@example.com  Subject: Maintenance started
to=me@example.com  Subject: Maintenance started: Jellyfin, Alpha
```

`deliver()` now skips the per-user **email** when that address is already on the admin
alert list and the event is one the admin channel covers (`ADMIN_ALERTED_EVENTS`, which
is deliberately just `maintenance`). The admin alert wins because it is the copy that
cannot be switched off per-person. Scoped narrowly on purpose: the admin channel sends
no report replies, announcements or "your request arrived", so those must still reach an
admin like anybody else, and a Discord DM is never suppressed - the admin webhook posts
to a *channel* while a DM goes to a *person*, so those two are different destinations
rather than duplicates.

### Two more bugs the audit turned up, both silent (fixed 2026-09-05)

Neither was reported; both were found by auditing the whole path after the above, and
both were confirmed empirically against a live database before being fixed.

**Unconfigured users were shown a promise nobody kept.** `get_user_preferences()`
reports the admin's configured defaults for a user with no `user_preferences` row - so
that user's own account page renders the box **ticked**. `users_opted_into()`, which is
what actually queues a broadcast, read only the `user_preferences` table, so such a user
was never queued. `notify_email_announcements` ships **on** by default, so in practice
announcement emails silently reached nobody who had never opened their account page.
The two functions now agree.

**The test button worked and real notifications didn't.** `deliver()` read
`notify_email`/`notify_discord_id` straight off the preferences row, while
`send_direct()` (the admin's per-user "Send test email" button) and
`needs_contact_details()` both resolve through `contact_for()`, which also knows about
the details Seerr holds. So someone whose address Seerr knew, but who had never typed it
into this portal, had every automated notification dropped as "no contact details" while
the admin's test message reached them without trouble. That combination is close to
undiagnosable from outside: the test says it works, and nothing ever arrives.

### What was and wasn't verified (2026-09-05)

Verified live, driving a real Chromium against a running server with a real SMTP sink
(`aiosmtpd`) and an HTTP receiver standing in for the Discord webhook and ntfy:

- Five services ticked on one window through the actual admin form -> one Discord post,
  one ntfy push, one admin email and one per-user email per transition, each naming all
  five services.
- The full preference matrix, by observing what the SMTP sink actually received: opted
  out -> never queued; Discord-only -> queued but no email; on both lists -> the
  duplicate suppressed; no preferences row -> follows the admin default.
- The reported "email despite the box being off" scenario, reproduced exactly and then
  explained.
- Both changed pages rendered in Chromium with no console, page or request errors.

Not verified: no real Discord gateway, so the per-user **DM** half of the fan-out is
still mocked-only, as it has always been in this sandbox. No real Jellyfin either, so
the signed-in visitor's own `/account` page was exercised through
`/admin/users/<id>/account`, which renders the same template through the same save path.

## Two ways a misbehaving external service looked like a portal bug (2026-09-05)

Both reported from the user's own `instance/logs/app.log`, on the same evening, shortly
after v1.8.6 went on. Neither was visible from the UI at all - the log was the only
place either showed up, which is a decent argument for the log page existing.

### The username that was posted as an email address (fixed 2026-09-05)

```
WARNING [notifications] Email to 1 recipient(s) failed: {'zellowz_': (553, b'5.1.3 The
recipient address <zellowz_> is not a valid RFC 5321 address...')}
WARNING [notifications] Email to 1 recipient(s) failed: {'saint boboniolo': (553, ...
```

Seerr fills a user's `email` field with their **username** when it imports a Jellyfin
account that has no email of its own. `fetch_seerr_users()` copied that verbatim into
`seerr_contacts`, `adopt_seerr_contact()` promoted it into `user_preferences` on the
person's first account-page visit, and delivery then handed a bare username to Gmail.
Once per notification, per affected user, indefinitely.

Worth noting for the record that **v1.8.6 made this more visible**: before it,
`deliver()` read the preferences row directly, so only a value that had been adopted or
typed was used. Fixing that to go through `contact_for()` (correctly - it was dropping
real notifications) also meant the Seerr-cached value became reachable. The underlying
bad data predates it either way.

Guarded at four layers, because each is reachable without the others: the source
(`fetch_seerr_users()` no longer caches a non-address), `save_contact()` (which now
validates for *every* user - the existing check lived in `push_seerr_contact()`, which
only ever runs for users with a Seerr link), `contact_for()` (a stored non-address reads
as no address, so nothing has to wait for a restart) and `send_email()` (which also
covers the admin recipient list - free text nobody was validating).

**The check is deliberately permissive**, and the near-miss is the interesting part. The
first draft required a dot in the domain, matching `integrations._EMAIL_RE`. That draft
would have *deleted* a working `admin@nas` or `root@localhost` during the startup
cleanup - perfectly deliverable on the kind of home LAN this portal runs on, where a
relay on the same machine is normal. Caught because three existing tests using dot-less
addresses went red. The rule now only has to catch what it exists to catch: bare
usernames, which have no `@` at all. `integrations._EMAIL_RE` stays strict and separate
because it answers a different question ("will Seerr accept this") where a refusal costs
nothing.

Already-stored values self-heal: `_clean_non_email_contacts()` runs on every startup,
blanks what isn't an address, logs what it cleared and by design becomes a no-op
afterwards. Verified against a database seeded with the exact reported values - cleared
on the next `init_db()`, a real address beside them untouched, second restart silent.

### A 502 that read like a crash (fixed 2026-09-05)

```
ERROR [scheduler] Scheduled task 'seerr_approvals' failed
Traceback (most recent call last):
  ...
requests.exceptions.HTTPError: 502 Server Error: Bad Gateway for url: https://...:5055/...
During handling of the above exception, another exception occurred:
  ...
RuntimeError: Could not read pending requests: 502 Server Error: Bad Gateway
```

Seerr, behind a Tailscale tunnel, answered 502 for a moment. The task did exactly the
right thing - refused to overwrite a real pending count with 0, and raised so the run
was recorded as failed - but `run_task()` logged it with `.exception()`, so a transient
hiccup at the other end produced a two-deep chained traceback. The user reported it as
an error they couldn't explain, having noticed nothing wrong as a user. They were right:
nothing was wrong, and the log said otherwise.

`scheduler.TaskUnavailable` now covers this. The run is **still recorded as failed** -
the work didn't happen and `/admin/tasks` must say so - and only the logging changes, to
one warning line carrying the reason. `run_task()` additionally walks the exception
chain for a `requests` error, so the tasks that simply let one propagate (the Jellyfin
and Seerr contact syncs) get the same treatment without each needing to know this
exists. **A genuine bug in a task still gets its full traceback**, which is the half
with a test on it, because it is the half that would quietly rot.

Verified by reproducing the exact reported call, with the same URL, and reading the
resulting log line.

## Expiring announcements, and two bugs found building them (v1.8.7, 2026-09-05)

### The window times that were an hour out (caught before shipping)

The admin list renders an announcement's window through the usual `.local-time` span,
and in this sandbox it looked perfect. It was wrong.

A `datetime-local` field submits `2099-01-01T00:00` - no offset - and that form is
parsed by JavaScript as **local** time, not UTC. So `local_time.js` was shifting every
window time by the viewer's own offset. Invisible here, because this sandbox's browser
and clock are both UTC, which is the whole reason it nearly shipped: the check that
found it was re-running the page in a Chromium context set to `Europe/Paris`, where
00:00 UTC has to render as 01:00 GMT+1 and was rendering as 00:00.

Fixed by appending an explicit `Z` to `data-utc`. Worth noting that the *maintenance*
list sidesteps this by not converting at all - it prints raw UTC text - so it was never
a model to copy, and any future timestamp sourced from a `datetime-local` field has the
same trap.

### The stray `</div>` (fixed 2026-09-05)

Found while building the settings filter, not by looking for it. The filter groups
fields by the `.form-panel` they sit in, and it kept finding seven of them out of
twenty-seven. Searching "timeout" - a setting that plainly exists - matched nothing.

`admin_settings.html` had one `</div>` too many, which closed the `<form>` element
early: from "Public page layout" downwards, every field was parsed as a child of
`<body>`.

It had presumably been there a long while and was completely invisible, because an
input's **form owner is fixed at parse time**. The fields still submitted, still saved,
and the page still looked right; only something reasoning about DOM *structure* could
see it.

The test for it is instructive. The first attempt used `html.parser` to track `<form>`
depth and check no input fell outside - and it **passed with the fault reintroduced**,
because `html.parser` tokenises rather than implementing HTML5 tree construction, so a
stray `</div>` doesn't affect its idea of form depth at all. What works is counting
`<div>` against `</div>` on the *rendered* page (Jinja conditionals make a
template-level count give false positives) across every admin page - all sixteen
balance at zero, so it's a real invariant rather than a number that happened to fit.

### The feature itself

Asked for as "an expiring system such as incidents and maintenance for announcements".
Two optional fields; blank means no bound, so an announcement with neither behaves
exactly as one did before - pinned by a test, because that is the property the whole
thing has to preserve on an existing install.

The design choice worth keeping is that **expiry is evaluated at read time**, not by a
scheduled job flipping a stored flag. Nothing can fall out of sync, a window that opened
or closed during downtime needs no catching up, and extending an expired announcement
brings it straight back rather than needing the admin to switch it on again - with a
flag, "expired" would also be indistinguishable from "an admin turned this off".

Sending a notification for an announcement that isn't currently showing is refused in
all three send paths: it would point people at something they can't see, in both
directions.

### The settings filter, and what it became

The first cut was an in-page filter box on `/admin/settings` alone. The user's response
was that it should be a bubble floating on every admin page, searching the whole panel -
which is the right shape, and the reason is that "which page is that setting on" is the
actual question. It was replaced rather than kept alongside: two search UIs on one page
is worse than either.

The property that needed verifying in a browser rather than reasoned about, while the
in-page filter existed: **hiding was visual only**, so saving while filtered saved the
whole form. Confirmed by ticking a checkbox, filtering it off screen, saving, and
checking it held - because the alternative failure mode silently resets every setting
not currently visible.

### The panel-wide search, and the index that can't drift

The design decision worth keeping is that **the index is derived from the admin
templates**, not hand-written. A hand-maintained list of several hundred settings would
be wrong within two sessions and wrong *invisibly*: a missing entry looks exactly like a
setting that doesn't exist. Deriving it means adding a setting adds it to the search.

The tempting alternative - render each admin page and read the DOM - is worse than it
looks. Rendering `/admin/reports` marks messages as read and `/admin/logs` reads files
off disk, so an index built that way would give a search box side effects. Templates are
inert.

The one hand-maintained part is `PAGES`, one row per page. Two tests keep it honest, and
the second earned its place immediately: `test_every_registered_endpoint_actually_
resolves` failed on the first run with `Could not build url for endpoint
'admin_clear_browser_cache'` - the real name is `admin_system_clear_browser_cache`.
Without that test the typo surfaces as a `BuildError` in front of the user, on a search.

One ranking problem worth recording because the fix is non-obvious: searching "2fa"
originally returned the four step-up code prompts dotted around the panel and *not* the
Two-factor auth page, because those prompts' labels literally say "2FA code" while the
page's own label says "Two-factor auth". Fixed by giving only the page-level entry a set
of aliases drawn from its template and endpoint names, scored just below a title match.
Giving every entry aliases would have boosted every control on that page equally and
lost the distinction.

### What was and wasn't verified (2026-09-05)

Verified live, driving a real Chromium against a running server with a real SMTP sink
(`aiosmtpd`) and an HTTP receiver standing in for the Discord webhook and ntfy:

- Five services ticked on one window through the actual admin form -> one Discord post,
  one ntfy push, one admin email and one per-user email per transition, each naming all
  five services.
- The full preference matrix, by observing what the SMTP sink actually received: opted
  out -> never queued; Discord-only -> queued but no email; on both lists -> the
  duplicate suppressed; no preferences row -> follows the admin default.
- The reported "email despite the box being off" scenario, reproduced exactly and then
  explained.
- Both changed pages rendered in Chromium with no console, page or request errors.

Not verified: no real Discord gateway, so the per-user **DM** half of the fan-out is
still mocked-only, as it has always been in this sandbox. No real Jellyfin either, so
the signed-in visitor's own `/account` page was exercised through
`/admin/users/<id>/account`, which renders the same template through the same save path.

## Two ways a misbehaving external service looked like a portal bug (2026-09-05)

Both reported from the user's own `instance/logs/app.log`, on the same evening, shortly
after v1.8.6 went on. Neither was visible from the UI at all - the log was the only
place either showed up, which is a decent argument for the log page existing.

### The username that was posted as an email address (fixed 2026-09-05)

```
WARNING [notifications] Email to 1 recipient(s) failed: {'zellowz_': (553, b'5.1.3 The
recipient address <zellowz_> is not a valid RFC 5321 address...')}
WARNING [notifications] Email to 1 recipient(s) failed: {'saint boboniolo': (553, ...
```

Seerr fills a user's `email` field with their **username** when it imports a Jellyfin
account that has no email of its own. `fetch_seerr_users()` copied that verbatim into
`seerr_contacts`, `adopt_seerr_contact()` promoted it into `user_preferences` on the
person's first account-page visit, and delivery then handed a bare username to Gmail.
Once per notification, per affected user, indefinitely.

Worth noting for the record that **v1.8.6 made this more visible**: before it,
`deliver()` read the preferences row directly, so only a value that had been adopted or
typed was used. Fixing that to go through `contact_for()` (correctly - it was dropping
real notifications) also meant the Seerr-cached value became reachable. The underlying
bad data predates it either way.

Guarded at four layers, because each is reachable without the others: the source
(`fetch_seerr_users()` no longer caches a non-address), `save_contact()` (which now
validates for *every* user - the existing check lived in `push_seerr_contact()`, which
only ever runs for users with a Seerr link), `contact_for()` (a stored non-address reads
as no address, so nothing has to wait for a restart) and `send_email()` (which also
covers the admin recipient list - free text nobody was validating).

**The check is deliberately permissive**, and the near-miss is the interesting part. The
first draft required a dot in the domain, matching `integrations._EMAIL_RE`. That draft
would have *deleted* a working `admin@nas` or `root@localhost` during the startup
cleanup - perfectly deliverable on the kind of home LAN this portal runs on, where a
relay on the same machine is normal. Caught because three existing tests using dot-less
addresses went red. The rule now only has to catch what it exists to catch: bare
usernames, which have no `@` at all. `integrations._EMAIL_RE` stays strict and separate
because it answers a different question ("will Seerr accept this") where a refusal costs
nothing.

Already-stored values self-heal: `_clean_non_email_contacts()` runs on every startup,
blanks what isn't an address, logs what it cleared and by design becomes a no-op
afterwards. Verified against a database seeded with the exact reported values - cleared
on the next `init_db()`, a real address beside them untouched, second restart silent.

### A 502 that read like a crash (fixed 2026-09-05)

```
ERROR [scheduler] Scheduled task 'seerr_approvals' failed
Traceback (most recent call last):
  ...
requests.exceptions.HTTPError: 502 Server Error: Bad Gateway for url: https://...:5055/...
During handling of the above exception, another exception occurred:
  ...
RuntimeError: Could not read pending requests: 502 Server Error: Bad Gateway
```

Seerr, behind a Tailscale tunnel, answered 502 for a moment. The task did exactly the
right thing - refused to overwrite a real pending count with 0, and raised so the run
was recorded as failed - but `run_task()` logged it with `.exception()`, so a transient
hiccup at the other end produced a two-deep chained traceback. The user reported it as
an error they couldn't explain, having noticed nothing wrong as a user. They were right:
nothing was wrong, and the log said otherwise.

`scheduler.TaskUnavailable` now covers this. The run is **still recorded as failed** -
the work didn't happen and `/admin/tasks` must say so - and only the logging changes, to
one warning line carrying the reason. `run_task()` additionally walks the exception
chain for a `requests` error, so the tasks that simply let one propagate (the Jellyfin
and Seerr contact syncs) get the same treatment without each needing to know this
exists. **A genuine bug in a task still gets its full traceback**, which is the half
with a test on it, because it is the half that would quietly rot.

Verified by reproducing the exact reported call, with the same URL, and reading the
resulting log line.

## Expiring announcements, and two bugs found building them (v1.8.7, 2026-09-05)

### The window times that were an hour out (caught before shipping)

The admin list renders an announcement's window through the usual `.local-time` span,
and in this sandbox it looked perfect. It was wrong.

A `datetime-local` field submits `2099-01-01T00:00` - no offset - and that form is
parsed by JavaScript as **local** time, not UTC. So `local_time.js` was shifting every
window time by the viewer's own offset. Invisible here, because this sandbox's browser
and clock are both UTC, which is the whole reason it nearly shipped: the check that
found it was re-running the page in a Chromium context set to `Europe/Paris`, where
00:00 UTC has to render as 01:00 GMT+1 and was rendering as 00:00.

Fixed by appending an explicit `Z` to `data-utc`. Worth noting that the *maintenance*
list sidesteps this by not converting at all - it prints raw UTC text - so it was never
a model to copy, and any future timestamp sourced from a `datetime-local` field has the
same trap.

### The stray `</div>` (fixed 2026-09-05)

Found while building the settings filter, not by looking for it. The filter groups
fields by the `.form-panel` they sit in, and it kept finding seven of them out of
twenty-seven. Searching "timeout" - a setting that plainly exists - matched nothing.

`admin_settings.html` had one `</div>` too many, which closed the `<form>` element
early: from "Public page layout" downwards, every field was parsed as a child of
`<body>`.

It had presumably been there a long while and was completely invisible, because an
input's **form owner is fixed at parse time**. The fields still submitted, still saved,
and the page still looked right; only something reasoning about DOM *structure* could
see it.

The test for it is instructive. The first attempt used `html.parser` to track `<form>`
depth and check no input fell outside - and it **passed with the fault reintroduced**,
because `html.parser` tokenises rather than implementing HTML5 tree construction, so a
stray `</div>` doesn't affect its idea of form depth at all. What works is counting
`<div>` against `</div>` on the *rendered* page (Jinja conditionals make a
template-level count give false positives) across every admin page - all sixteen
balance at zero, so it's a real invariant rather than a number that happened to fit.

### The feature itself

Asked for as "an expiring system such as incidents and maintenance for announcements".
Two optional fields; blank means no bound, so an announcement with neither behaves
exactly as one did before - pinned by a test, because that is the property the whole
thing has to preserve on an existing install.

The design choice worth keeping is that **expiry is evaluated at read time**, not by a
scheduled job flipping a stored flag. Nothing can fall out of sync, a window that opened
or closed during downtime needs no catching up, and extending an expired announcement
brings it straight back rather than needing the admin to switch it on again - with a
flag, "expired" would also be indistinguishable from "an admin turned this off".

Sending a notification for an announcement that isn't currently showing is refused in
all three send paths: it would point people at something they can't see, in both
directions.

### The settings filter

`/admin/settings` is ~27 settings across three forms. The filter matches labels, hints,
checkbox text *and* field names, so `show_public_gpu` copied out of a log line finds its
setting without knowing what it's called in English.

The property that needed verifying in a real browser rather than reasoned about:
**hiding is visual only**, so saving while filtered saves the whole form. Confirmed by
ticking a checkbox, filtering it off screen, saving, and checking it held - because the
alternative failure mode silently resets every setting not currently visible.

### What was and wasn't verified (2026-09-05)

Driven in real Chromium against a live server: all four announcement window states
through the actual admin form (none / scheduled / expired / open), the public page
showing exactly the right two, the backwards-window refusal keeping what was typed and
still reading as "New announcement", the Clear button, and the timezone rendering in
both UTC and Europe/Paris. For the filter: every search term above, the empty state,
Escape restoring the page, and the save-while-filtered case. No console, page or request
errors on any of it.

For the panel-wide search, driven in real Chromium: the bubble present on pages far
from Settings, "/" and Ctrl+K opening it, a search run from `/admin/logs` for a setting
that lives on `/admin/settings`, arrow-key navigation, Enter navigating cross-page, and
the arriving page scrolling to the right control, focusing it and flashing it. Also at
380px wide, where the bubble collapses to an icon and the page has no horizontal
overflow.

Not verified: the Discord embed and kiosk filtering are covered by tests only, since
there is no real Discord gateway here and the kiosk view was not driven in a browser
this time.

## Release history notes

### `v1.1.0` shipped as a full release despite unverified pieces (2026-07-23)

The second tagged release went out as a **full release, not an `-rc`**, by the user's
explicit call — despite the Windows-only monitoring pieces (CPU/disk temp, per-disk
I/O, VM detection), the Jellyfin `/Sessions` and `/ScheduledTasks` parsing, and the
Discord guild whitelist's `guild.leave()` call all still being unverified against the
real thing at release time.

That was a deliberate, informed tradeoff by the user, not an oversight. The general
lesson: don't read "it's a full release" as "everything in it was confirmed end to
end" — check the per-feature verification notes instead.

### Cosmetic wart, deliberately left

Backup folder names are UTC (`20260810-171829-...`) while the console and app log show
local time, so on a UTC+2 machine the same update reads as 19:18 in the log and 17:18
in the folder name. Consistent with everything else in this app storing UTC, and the
CLI's `list-backups`/`rollback` never require reading the timestamp by eye — but it
does look like a mismatch if you're picking a backup by hand.
