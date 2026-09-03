# Candidate features

**Eight of the items below were built in v1.8.0 (2026-08-21)** and are marked
**BUILT** where they appear. They're kept here rather than deleted so the reasoning
that went into them stays findable; `CLAUDE.md` documents how each one actually works,
and `docs/HISTORY.md` records what was and wasn't verified.

Still open: the external connectivity check, the stuck-download alert, admin-on-a-
separate-port, the Jellyfin visibility model, the Linux-native monitoring backend,
the Discord bot's search/request commands, Windows packaging and auto-start, and
one open bug investigation (the Discord bot stopping and not restarting).

Ideas discussed with Adam, written up so they can slot straight into
`ROADMAP.md`. Each one lists a rough priority and implementation effort
(**S**mall / **M**edium / **L**arge) alongside the reasoning, not just the
label — the point is that someone new to the repo can see *why*, not just
where it landed.

## Solid features

Contained but touch the schema or add a genuinely new concept.

### Restore from backup — **BUILT (v1.8.0)**
The counterpart to the "download a backup" button (built — see CLAUDE.md).
Let an admin upload a previously-downloaded backup zip and have the portal
restore `instance/portal.db` from it. Meaningfully riskier than the export
button: this is a "replace the entire live database from an upload"
primitive, so it needs real safety machinery, not just a file swap —
validate the uploaded file is actually a well-formed SQLite database before
touching anything, take a fresh safety snapshot of the *current* database
first (reusing `db.backup_to_file()`) so a bad upload doesn't leave the
admin with nothing to fall back on, atomically replace the file, then
restart the process (`os.execv`, same pattern as `app._restart_process()`)
so no stale connection keeps writing to the old file handle. Should go
through `app._require_totp()` step-up re-authentication like the other
destructive admin actions (host restart/shutdown, app restart, self-update)
— a stolen session cookie alone must not be enough to replace the whole
database.

**Priority:** Medium · **Effort:** S-M — the individual pieces (upload
handling, SQLite validation, atomic file replace, process restart) all
already exist elsewhere in this codebase to reuse, but assembling them
safely is the real work. Treat it with the same care as the self-updater,
not as a quick add — it's the riskiest idea in this document, not the
easiest.

**Where it should live**: see "Updater quality-of-life" below — the argument
there is that someone reaching for a restore is usually recovering from
something, and will look on the About page next to rollback rather than in
Settings. Same feature, one implementation.

### Updater quality-of-life: release notes in the app, and restore from a backup zip — **BUILT (v1.8.0)**
Two related improvements to `/admin/about`, which today tells you *that* an
update exists but nothing about what's in it, and can roll code back but not
data.

**Show the changelog.** The GitHub releases API already returns each
release's `body` (the changelog written at release time) in the same
response `updater.py` parses for the version and download URL — so surfacing
"what's in the update you're being offered" costs one extra field, not one
extra request. Worth showing **both**: the notes for the version you're
running and the notes for the version on offer, so "should I take this?" is
answerable without leaving the page. If several releases have accumulated,
showing the notes for each version *between* the two is the more useful
version of this, and needs the releases list rather than just the latest —
which `updater.py` already fetches. The body is Markdown and arrives from
the network, so it must be rendered through something escaping-safe rather
than dropped into the page as HTML — the existing `richtext` filter is the
obvious starting point, and deliberately supports far less than Markdown.

**Restore the database from a backup zip.** The counterpart to the existing
"download a backup" button, offered where the other recovery machinery
already lives instead of buried in Settings. **This is the same feature as
"Restore from backup" above** — see that entry for the safety machinery it
needs (validate the upload really is a SQLite database before touching
anything, snapshot the *current* database first, atomic replace, restart the
process, step-up 2FA). Treat this entry as "and put it on the About page
next to rollback, where someone recovering from a bad update will actually
look for it", not as a second, simpler implementation.

Worth keeping the two kinds of backup distinct in the UI while doing it,
because conflating them would be a genuinely bad mistake: `updater.py`'s
backups are of **application code**, for rolling back a bad update, and
contain no data; the Settings backup button produces a **database** zip and
contains nothing else. Neither can restore the other.

**Priority:** Medium-High for the changelog (small, and it improves every
future update decision) · Medium for the restore half · **Effort:** S for
the changelog; S-M for the restore, all of which is in the risk of
assembling it rather than the amount of code.

### External internet connectivity check
Pings a fixed external target (1.1.1.1 by default, ideally configurable) on
the same schedule as service health checks. If it fails, the public page can
show a distinct "internet connectivity issue" state instead of every single
service flipping to "down" at once with no obvious common cause — useful for
anyone reading the status page during an ISP outage, and for whoever's on
call not chasing five separate incidents that are actually one.

**Priority:** High · **Effort:** M — a new check type in the health-check
loop, plus a new status concept that isn't tied to any one service and needs
its own banner/UI treatment on the public page.

### Version-checker for the *Arr apps themselves — **BUILT (v1.8.0)**
Radarr, Sonarr and Prowlarr each expose their own current version and can be
checked against their latest GitHub release — the same way this app already
checks its own version for self-updates. This would surface "Radarr has an
update available" in the admin panel, read-only, no auto-update of those
apps involved, just visibility, so nobody's manually checking three separate
web UIs to know if anything's behind.

**Priority:** Medium · **Effort:** M — the version-comparison logic in
`updater.py` is already there to reuse, but each app has its own
releases API/format to query and parse.

### Per-user notifications: Discord DM and email — **BUILT (v1.8.0)**
The portal already notifies *the admin* (Discord webhook, ntfy). This is the
other direction: telling **the person who asked** when something they care
about happens — their problem report got a reply or was turned into an
incident, a request they made moved on, or maintenance is starting on a
service they use.

Two delivery channels, and the interesting part is **where the addresses come
from**. Seerr already holds an email address and a Discord user ID for each of
its users, entered once by them. Reading those rather than asking again is the
difference between "set up notifications" being a chore and being invisible.
So:

1. If the signed-in Jellyfin user matches a Seerr user with contact details
   already filled in, use those.
2. If not, ask on the account page (which already has a contact field to grow
   into) — and offer to **push it back to Seerr**, so the user enters it once
   for both systems rather than maintaining two copies that drift.

Worth deciding before building:

- **Matching a Jellyfin user to a Seerr user.** Seerr can import Jellyfin
  accounts, in which case there's a real link to follow; if it hasn't, matching
  on email or username is guesswork and should probably just fall back to
  asking. Getting this wrong means sending someone else's notifications to the
  wrong person, so it should fail closed.
- **Writing back to Seerr is the first time this portal would modify another
  service.** Everything today is read-only except Jellyfin authentication.
  That's a real line to cross deliberately, with the user's explicit consent in
  the UI, not a silent sync.
- **Per-event opt-in, per user.** Nobody wants a DM for every maintenance
  window on every service. The natural granularity is "things about my own
  reports" (almost always wanted) versus "anything about services I use"
  (usually not), so default the first on and the second off.
- **Sending is outbound I/O and belongs in a scheduled task or the existing
  background thread**, never inline in the request that triggered it — the same
  rule every other outbound call in this app follows.

Email needs the SMTP configuration block described in "Email notifications"
below; the Discord DM path needs `discord_bot.py` to DM a user, which is a
different code path from its current guild/channel posting (the same one the
Seerr approval alert further down needs, so build it once).

**Priority:** Medium-High · **Effort:** L — two delivery channels, a matching
problem with a real failure mode, a write-back to another service, and a
per-user preferences surface. Best split: Discord DM first (the bot is already
there), email second (needs the SMTP config below), Seerr contact reuse last,
since it's the only part that can be wrong in a way that matters.

### Email notifications — **BUILT (v1.8.0)**
A third notification channel alongside the existing Discord webhook and
ntfy, for incident/maintenance events. Needs an SMTP configuration block in
`.env` (host, port, credentials, from-address) and a plain-text/HTML
template — meaningfully more setup surface than the URL-only Discord/ntfy
options. Worth it for anyone using neither of those, but it's a new
dependency and a new way for things to be misconfigured.

**Priority:** Medium · **Effort:** M — new config surface, a new dependency,
and a template; the "when to send" logic itself doesn't change.

## Bigger undertakings

New integrations or genuinely stateful logic — worth doing, but each is a
project of its own rather than an afternoon.

### Radarr + Prowlarr + qBittorrent integration — **BUILT (v1.8.0)**
Three new read-only integrations feeding a new section: what's coming up
(Radarr's release calendar), what's been requested and its current state,
and what's actively downloading right now with progress. Note that
qBittorrent authenticates with a username/password login rather than an API
key, so its integration looks more like Byparr/Tdarr's setup than
Radarr/Sonarr's. Most valuable once Jellyfin-backed user permissions exist
(below) — at that point "requested items" and "active downloads" could be
scoped per Jellyfin account instead of shown flat to everyone — but there's
no hard dependency between them; this can be built and shown flat first.

**Priority:** Medium-High · **Effort:** L — three integrations with two
different auth shapes, new parsing per app, and new UI sections on top of
the existing `integrations.py` pattern.

### Unified search across Jellyfin and Seerr (signed-in users only) — **BUILT (v1.8.0)**
One search box on the public page that queries **Jellyfin** and **Seerr**
at the same time and merges the results, so a visitor asks "do we have X?"
once instead of checking two places. Each result then offers the action that
actually applies to it: already in the library → a direct link straight to
that item in Jellyfin; not in the library → request it through Seerr without
leaving the portal.

**Sequenced after the calendar/downloads integration above**, not before —
that work brings the Seerr and *Arr API surface into this codebase, and
building search first would mean writing half of it twice.

**Signed-in users only** (the Jellyfin auth built in v1.7.0 is exactly what
makes this possible). Three reasons that restriction isn't arbitrary: the
result set reveals your whole library to anyone who can load the page;
requesting is a write action against Seerr and needs to be attributable to a
person; and a search box wired to two external APIs is a free
denial-of-service amplifier if it's open to the internet. Rate-limit it
per session on top.

Two things to decide before building:

- **Whose Seerr account requests it.** Simplest is one shared Seerr API key,
  with the portal recording which Jellyfin user asked — but then Seerr's own
  approval queue can't tell them apart. Seerr can also import Jellyfin users,
  in which case requesting *as* the matching Seerr user is possible and much
  better. Worth checking which is true of the actual setup before choosing.
- **Search has to stay off the request path's slow-I/O rule.** Unlike every
  other outbound call in this app, a search genuinely cannot be answered from
  a background-refreshed cache — the query is unknown until someone types it.
  This would be the first *legitimate* live outbound call from a request
  handler, so it needs its own short timeout and a clear "search is
  unavailable right now" degradation, rather than quietly breaking the rule.

**Priority:** Medium · **Effort:** L — two APIs, a merge/dedupe step (the
same title from both sources must not appear twice), a write action, a new
UI, and the auth/rate-limiting story above.

### Prowlarr per-indexer health — **BUILT (v1.8.0)**
Prowlarr's own API already reports each configured indexer's individual
state (healthy / down / rate-limited), not just whether Prowlarr itself is
reachable. Surfacing that per-indexer list matters because "Prowlarr is up"
hides the failure mode that actually happens in practice — one or two
indexers going stale or getting rate-limited while Prowlarr itself runs
fine.

**Priority:** Medium · **Effort:** M-L — depends on, or extends, the
Prowlarr integration above; needs its own endpoint parsing and a small
per-indexer list in the UI.

### Seerr pending-approval count + Discord DM to admins — **BUILT (v1.8.0)**
Polls Seerr for requests awaiting approval and surfaces a count, plus has
the Discord bot DM admins directly (not post to a channel) when a new one
comes in. Two things worth deciding before building: whether the count is
admin-only or shown publicly (leaning admin-only — it's operational
information, not a status signal), and how the DM target list is configured
(likely reusing the existing comma-separated Discord user ID pattern from
the `/status`/`/snapshot` authorization).

**Priority:** Medium · **Effort:** M — Seerr API polling is straightforward,
but DMing specific users is a different code path from the bot's current
guild/channel posting in `discord_bot.py`.

### Stuck-download alert
Flags a download (a qBittorrent torrent, or a *Arr download-client task)
that hasn't made progress in longer than an admin-configured window —
usually the sign of a dead indexer or a stalled torrent nobody's noticed.
Sent as a Discord DM to admins, the same delivery path as the Seerr approval
alert above.

**Priority:** Medium · **Effort:** M-L — unlike the other integrations,
which just check current state, this needs to track progress *across*
checks over time to detect "not moving" — a more stateful shape than the
existing health-check model.

### Discord bot: search, request status, and requesting content
Today the bot answers one question — is anything down (`/status`, `/snapshot`).
These extend it to the questions people actually ask in a Discord server: *do we
have X?*, *what happened to the thing I asked for?*, and *can you add X?* — the
same three the unified search page answers, reached from where the conversation
already is.

Three commands, in ascending order of risk:

- **`/requests`** — what the caller has requested and where each one is
  (pending / approved / downloading / available), read straight from the Seerr
  data `integrations.fetch_seerr_requests()` already returns.
- **`/search <title>`** — the unified Jellyfin + Seerr search, replying with the
  same merged/deduped results the web page shows, and the same per-result answer:
  in the library, already requested, or requestable.
- **`/request <title>`** — a write against Seerr, attributed to a real person.

**Identity is the whole design problem, and the answer already exists in the
database.** Every one of these is per-person, but the bot has a Discord user id
and nothing else. The link to follow is the one the portal already keeps: the
`seerr_contact_sync` task caches Seerr's per-user contact details (including
`discordIds`, read from Seerr's per-user notification-settings sub-resource) in
`seerr_contacts`, and **only for accounts Seerr itself has linked to a Jellyfin
user**. So a Discord id already resolves to a Jellyfin user id through data
that's refreshed hourly, with no new integration and no sign-in step — which is
exactly the "bypass the login by comparing the Discord id against Seerr" shape
this was asked for, except it isn't a bypass: it's the same fail-closed link
per-user notifications already rely on.

Rules that follow from that, none of them optional:

- **Never match on username or email**, in either direction. An unmatched Discord
  id gets an ephemeral "I don't know who you are — add your Discord id in Seerr,
  or on your account page in the portal" and nothing else. Same fail-closed rule
  as `user_notify`; getting it wrong here means showing one person another
  person's request history.
- **The reply must be ephemeral** for anything per-person. `/requests` in a shared
  channel otherwise publishes what everybody asked for to everybody.
- **Reuse `media_search.py`, don't reimplement it.** The merge/dedupe (title *and*
  year), the "search is unavailable" state distinct from "nothing found", and
  `SEARCH_TIMEOUT_SECONDS` are all the point of that module. The request path is
  the awkward part: the actual submit logic currently lives inside `app.py`'s
  `search_request()` route, and `discord_bot.py` cannot import `app.py` (circular —
  `app.py` imports the bot). Extracting it into `media_search.py` first, the way
  `integrations.evaluate_high_load()` was extracted so both callers could share it,
  is the real work of this item.
- **The admin-only request fields must not exist here at all.** Root folder,
  quality profile and tags are shown on the web configure page only to a browser
  that is also signed in as the portal admin, because root folders are real server
  filesystem paths. A Discord command has no such concept — it requests with
  Seerr's defaults, full stop.
- **A TV request needs seasons.** Either a Discord select menu, or default to all
  seasons (what the web path did before it had a picker) and say so in the reply.
- **Call `StatusBot._check_command_authorized()`** from every new command — the
  enabled toggle, channel whitelist and user allow-list gate, in that order. It
  exists precisely so a third command doesn't re-inline the checks.
- **Every outbound call must leave the event loop free.** These commands make
  blocking `requests` calls, and doing that directly inside a coroutine stalls the
  gateway heartbeat for the duration — see the bot-disconnect investigation below,
  where that is a leading suspect. Wrap them in `asyncio.to_thread(...)`.
- **Rate-limit per Discord user.** The web search is limited per session; a
  slash command has no session, and the underlying calls are the same two external
  APIs.

**Priority:** Medium-High — `/requests` and `/search` are small on top of what
exists and are the two people would use daily · **Effort:** M for the two read
commands; M-L including `/request`, almost all of it in extracting the submit path
out of `app.py` cleanly rather than in the Discord surface itself.

---

## Architectural ideas (carried over from the repo's own ROADMAP.md)

These were already scoped in more detail before this document existed —
kept here, with priority/effort added, so nothing gets lost when the old
`ROADMAP.md` content is replaced.

### Serve the admin panel on a separate port/subdomain from the public page
Right now `/admin/*` and the public page are the same Flask app on the same
port — `/admin` just happens to require login. Running `/admin/*` on a
second port (or a subdomain) would let the two be exposed completely
independently: the public page open to the internet as today, while
`/admin` is reachable only via Tailscale or a different reverse-proxy rule.
The lower-churn approach — a second WSGI listener on the same Flask `app`
object, gated by a `before_request` check on which port a request came in
on — was already identified as the right shape, over splitting into two
separate Flask apps. Needs a new `PORTAL_ADMIN_PORT` config value, unset by
default so today's single-port behavior is unchanged.

**Deferred from the v1.7.0 session (2026-08-21)** and explicitly kept for
later, having been considered and scheduled out rather than forgotten: it was
scoped alongside the Jellyfin auth work and deliberately not bundled with it,
since a port-level access gate and a brand-new authentication path are two
access-control changes and shipping both together means not knowing which one
broke anything. It remains the **next candidate** — the only architectural
item here whose implementation shape is already fully settled.

One wrinkle worth knowing before starting: **cookies are not scoped by port**,
so a session cookie set on the public port is sent to the admin port as well.
The split therefore buys *network-level* separation (expose, firewall or
tunnel the two independently), which is the actual goal — not session
isolation. Don't let anyone assume otherwise. Also note `app.run()` binds a
single port, so the dev entry point and `serve_waitress.py` would diverge in
behaviour unless that's handled deliberately.

**Priority:** High (next up) · **Effort:** S-M — the implementation shape is
already decided; it's mostly wiring a second listener and one
`before_request` gate.

### Jellyfin-backed user permissions — auth layer DONE (v1.7.0), visibility model still open
The **authentication half is built** (2026-08-21): visitors sign in with
their Jellyfin username and password, backed by a scheduled task that caches
Jellyfin's user list locally so an outage never signs anybody out. See
CLAUDE.md → "Jellyfin-backed user accounts" for the design, and the
`/admin/users` page for the settings. The session already carries the pieces
a permissions model needs — the Jellyfin user id, the display name, and
whether Jellyfin considers them an administrator (stored deliberately, read
by nothing yet).

**What's left is the permissions/visibility model itself**, which was
correctly scoped out of that pass rather than rushed into it: a way to mark a
piece of content (an Info page block, a service card, an announcement, a
service link) as visible only to signed-in users, or only to particular
Jellyfin accounts. That's the part that "touches how content is modeled
everywhere, not just one page", and it needs its own design conversation —
in particular whether visibility is per-item-per-user, per-item-per-group,
or just a three-way public/signed-in/admin flag, which is very likely enough
and vastly cheaper than the other two.

The first concrete use case remains the original one: Tailscale join
instructions shown only to people who actually have an account.

**Priority:** Medium · **Effort:** M for a three-way visibility flag on the
existing content types; L if per-user rules are genuinely wanted. It is also
what the Radarr/Prowlarr/qBittorrent integration above would build on to
scope "requested items" and "active downloads" per account.

### Linux-native `monitoring.py` backend/fork
Almost everything in this app is already OS-agnostic — services, incidents,
maintenance, the Discord bot, integrations, high-load detection, retries,
grace periods, all built on Flask/SQLite/psutil with nothing
Windows-specific. The exceptions all live in `monitoring.py`: Hyper-V VM
detection, Windows volume labels, CPU/disk temperature (via PowerShell/CIM),
and per-disk I/O's drive-letter correlation. A Linux-native version of that
file would in some ways be *simpler* than the Windows one:
`psutil.disk_io_counters(perdisk=True)` gives clean per-disk names on Linux
with no correlation step needed, and `psutil.sensors_temperatures()` reads
CPU temperature natively with no subprocess involved. Per-disk temperature
would still likely need `smartctl`, and VM detection would need an entirely
different concept (libvirt/KVM, or Docker container status) rather than a
direct port, since Hyper-V doesn't exist on Linux.

**Priority:** Low · **Effort:** M-L — no pressing need while running on
Windows/Hyper-V; mainly relevant if this app is ever run on a Linux host
instead.

---

## Packaging and deployment (Windows)

Everything here is about the gap between "the code is correct" and "a person has
it running on their machine and it comes back after a reboot". Nothing in this
section changes what the app does; all of it changes who can run it.

### A Windows installer (`.exe`)
One download that puts a working portal on a machine: Python and the
dependencies, the app itself, a first-run configuration step, and a shortcut —
instead of the current install Python, clone/extract, make a venv, `pip install
-r requirements.txt`, write a `.env`.

**The one decision that determines everything else: freeze, or install.** These
are not two flavours of the same thing.

- **A frozen single `.exe` (PyInstaller/Nuitka) breaks the self-updater**, which
  is not a small loss — it's the feature that keeps every install current.
  `updater.py` replaces `.py` files on disk and then re-execs
  `os.execv(sys.executable, ...)`; in a frozen build the `.py` files aren't what
  runs, and `sys.executable` is the bundled exe. Taking this path means replacing
  in-app updating with "download and run the new installer", and rewriting
  `update.py`/`updater.py` around that. It also fights the optional-dependency
  design: `discord.py` and `nvidia-ml-py` are lazy-imported precisely so they can
  be absent, and a freezer either bundles them for everyone or misses them
  entirely.
- **An installer that lays down a real Python (Inno Setup or NSIS wrapping the
  embeddable distribution, creating a venv and running `pip install`) keeps every
  existing mechanism working unchanged** — self-update, rollback, the CLI
  recovery tool, optional extras. This is the recommended shape, and it is also
  the *less* clever one on purpose.

Whichever is chosen, four things need deciding at install time, not discovered
later:

- **Where writable state lives.** `instance/` holds the database, `secret_key`
  (mode 0600), logs, and backups; `static/uploads/` holds the logo. Installing
  into `C:\Program Files` puts all of that somewhere a non-elevated process
  cannot write, and the failure is ugly and late (`config.py` degrades to a
  per-process secret key *silently*, which reads as "it logs me out at random").
  Either install per-user, or install to `%ProgramData%`, or point `instance/` at
  a writable path explicitly.
- **The optional Discord extra is a checkbox.** `requirements-discord.txt`
  already exists as a separate pinned file for exactly this — the installer runs
  a second `pip install` against it, or doesn't.
- **First-run configuration.** A short form writing `.env` (port, admin password,
  and whichever `PORTAL_*` values the user needs) beats shipping `.env.example`
  and hoping. Note `PORTAL_SECRET_KEY` no longer needs to be asked for — it is
  generated and persisted to `instance/secret_key` on first start.
- **A firewall rule for the chosen port**, and the port itself, since the default
  5000 is a common collision.

**Priority:** Medium-High if this is ever meant to be used by anyone other than
its author; Low if not · **Effort:** M for the installer-with-real-Python route ·
L for the frozen route, most of it in rebuilding the update story rather than in
the packaging.

### Start automatically on boot
Three ways, and they are not equivalent — the differences matter more than the
setup effort:

1. **A Windows service (recommended).** The only option that runs with no user
   logged in, starts before anyone signs in, and — the part that matters most
   here — **restarts the process automatically when it dies**. `WinSW` or `NSSM`
   wrapping `python serve_waitress.py` needs no code change at all; a native
   `pywin32` service would mean a new entry point and a new dependency for no
   real gain.
2. **A Scheduled Task at startup** (`schtasks /create /sc onstart`). No extra
   dependency, works today, survives reboot — but no restart-on-failure and no
   real service lifecycle. A reasonable fallback when a service wrapper can't be
   installed.
3. **A Startup-folder shortcut.** Only runs once somebody logs in interactively,
   and dies with that session. Mentioned to be ruled out.

Two things worth knowing before doing this, both of which are easy to discover
the hard way:

- **`_restart_process()` was built in a way that survives supervision.** It uses
  `os.execv`, which replaces the process image *in place, keeping the same PID* —
  so a service wrapper watching that PID sees the restart as a continuation, not
  as a crash-and-restart fight. A fork+exit design would have broken under every
  option above. Don't change it.
- **A service also unlocks something the updater currently documents as
  impossible.** `CLAUDE.md` states plainly that a failed start *after* an update's
  restart cannot be rolled back automatically, because that would need "a
  supervisor outside the process (systemd + a health check, or a wrapper), which
  this project deliberately doesn't ship because it would change how everyone
  launches the portal". An installer that sets up a service is exactly that
  change, made deliberately and once — at which point `write_pending_marker()`'s
  record could actually drive an automatic recovery instead of only naming the
  backup for a human. Worth treating as a follow-on item, not bundled in.
- **Check the monitoring calls under the service account.** The Hyper-V, CPU/disk
  temperature and per-disk I/O queries shell out to PowerShell/CIM. They currently
  run in the user's own interactive session; under `LocalSystem` or a dedicated
  service account they may fail on permissions (Hyper-V administration in
  particular). They degrade to `None` rather than crashing, so the symptom would
  be "the VM list is empty since I made it a service" — verify explicitly rather
  than assuming.

**Priority:** High (this is the difference between the portal being up after a
power cut and not) · **Effort:** S with a wrapper (`WinSW`/`NSSM` plus an
installer step and a documentation page) · S for the scheduled-task fallback.

---

## Known issues to investigate

Reported symptoms whose cause is not yet established. Written down so the next
session starts from what was already ruled out rather than from scratch.

### The Discord bot stops and never comes back until the whole app is restarted
**Symptom (reported 2026-09-03):** the bot goes offline on its own after running
for a while. The admin panel shows it as not connected. **Restarting the bot from
`/admin/system` does not fix it** — only restarting the entire process does,
either from the app-restart button or by hand.

That second half is the useful clue, because it splits the problem in two, and
they are almost certainly independent:

**Why the restart button doesn't help** has a concrete candidate visible in the
code. `stop()` schedules `client.close()` onto the bot's own event loop and waits
on `.result(timeout=10)`; if that loop is wedged the wait raises, is caught and
logged, `thread.join(timeout)` then returns with the thread still alive — and
`_runtime["client"]` is never cleared, because only `_run()`'s `finally` block
does that and `_run()` never finishes. `restart()` immediately calls `start()`,
which begins with `if _runtime["client"] is not None: return`. The result is a
button that appears to work, reconnects nothing, and stays that way until the
process image is replaced. Instrument this before fixing it: log whether the
close future timed out and whether the thread actually joined, and surface
`thread.is_alive()` / whether `_runtime` is still populated on `/admin/system`,
so the next occurrence answers the question instead of prompting another guess.

**Why it disconnects in the first place** is the open question. discord.py
reconnects on its own, so a permanent drop usually means the event loop stopped
being able to answer the gateway. The first thing to check is
`instance/logs/app.log` for discord.py's own warnings — "heartbeat blocked" or
"Shard ID None has stopped responding to the gateway" name the cause directly,
and logging is configured globally so they will already be there if it happened.
The leading suspect is blocking work inside a coroutine: the refresh loop and the
command callbacks do synchronous database and cache reads on the event loop
thread. Under a slow disk or a locked database that is exactly how a heartbeat
gets missed.

**Anything added here must not become a workaround that hides the cause.** A
watchdog (notice `_state["connected"]` has been false for N minutes, call
`restart()`) is a reasonable safety net *after* the restart path is trusted to
actually work — before that, it's a loop that calls a no-op.

**Not reproducible in this sandbox**: there is no real Discord gateway here, so
anything in this area is reasoned from the code and from logs the user can
supply, not from a live repro.

**Priority:** High — a monitoring bot that silently stops is worse than one that
was never set up · **Effort:** S for the restart path (a bounded, readable fix
plus the diagnostics to confirm it) · unknown for the disconnect itself until
there are logs.

---

## Overall take

Most of this document is now built (v1.8.0 took eight of these items). What
remains is genuinely different in character rather than more of the same. **Restore from backup** is the
one item worth flagging out of proportion to its Medium priority: it's the
riskiest thing in this document precisely because its counterpart (export)
was so easy — don't let that make restore feel like a quick add too. Of the
three architectural carry-overs, **Jellyfin auth's authentication half is now
built** (v1.7.0) and only its visibility model is still an open design
question; **admin-on-a-separate-port** has an implementation shape already
fully decided; and the **Linux fork** remains a genuinely open question worth
treating as such rather than scheduling like a normal feature.

Two things v1.7.0 added that later work should build on rather than
reinvent: the **scheduled-task framework** (`scheduler.py`) means anything
recurring — the *Arr version checker and the stuck-download alert above, a
cleanup job, a cache warmer — is a `register()` call rather than another
background thread; and the **visitor session** means anything that wants to
know who is looking at the page already has an answer.
