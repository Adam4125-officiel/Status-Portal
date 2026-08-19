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

Rough chronological range: 2026-07-22 through 2026-08-11.

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
