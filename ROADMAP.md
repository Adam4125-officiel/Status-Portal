# Roadmap

What is still open, and why — with a rough priority and effort (**S**mall /
**M**edium / **L**arge) on each, plus the reasoning behind them, so someone new to
the repo can see *why* an idea landed where it did rather than just where.

**This file is only about what is left.** Ideas that have been built are listed in
one line each under "Already shipped" at the bottom; their design write-ups were
removed once the code existed, because a roadmap that keeps them reads as a much
longer list of open work than there is. What replaced each write-up is better
anyway: `CLAUDE.md` documents how the built thing actually works and what must not
be broken, and `docs/HISTORY.md` records how its bugs presented and what has been
verified against real hardware.

## Features

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

### Stuck-download alert
Flags a download (a qBittorrent torrent, or a *Arr download-client task)
that hasn't made progress in longer than an admin-configured window —
usually the sign of a dead indexer or a stalled torrent nobody's noticed.
Sent as a Discord DM to admins, reusing the delivery path the Seerr approval
alert already uses.

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

## Architecture

Each of these changes the shape of the app rather than adding to it.

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

### Per-content visibility for signed-in Jellyfin users
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
what the media section (requests, active downloads) would build on to scope
what it shows per account instead of showing everyone everything.

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

## Packaging and deployment (Windows)

Everything here is about the gap between "the code is correct" and "a person has it
running on their machine and it comes back after a reboot". Nothing in this section
changes what the app does; all of it changes who can run it.

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

Three things worth knowing before doing this, all of them easy to discover the
hard way instead:

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

## Known issues to investigate

Symptoms whose cause is not established. Written down so the next session starts
from what was already ruled out rather than from scratch.

### The Discord bot disconnecting on its own — half explained

**Half of this was fixed and verified in v1.8.3** (2026-09-03): restarting the bot
from `/admin/system` used to do nothing, because a wedged event loop left the old
connection recorded as still running and `start()` refuses to run while one is. That
part is closed — see `docs/HISTORY.md` → "The restart button that did nothing".

**The other half is still a suspicion, deliberately written down rather than
declared solved.** Why the bot dropped in the first place was never proven. The
leading theory is heartbeat starvation: everything the refresh tick read
(`build_status_data()`'s dozen SQLite queries, plus
`monitoring.get_resource_snapshot()`'s blocking psutil CPU sample and its
`disk_usage()` walk of every mountpoint) ran on the same event loop that answers
Discord's heartbeat. Enough missed heartbeats and Discord drops the session. All of
that now runs off the loop, which is correct regardless — but "the symptom stopped"
is not the same as "that was the cause", and a v1.8.3 that runs for weeks without
dropping is evidence, not proof.

**What would settle it**, if it ever happens again: `instance/logs/app.log` will
contain discord.py's own `heartbeat blocked` or `Shard ID None has stopped
responding to the gateway` if the loop was starved. Neither line present means the
cause is something else entirely, and the log either side of that timestamp is the
thing to read. The watchdog task (`/admin/tasks` → "Discord bot watchdog") also
records each time it had to step in, which is a second, coarser signal that
something is still dropping.

**Priority:** Low while it stays fixed in practice; High again the moment it
recurs · **Effort:** unknown until there are logs — which is exactly why this
entry exists rather than a guess at a fix.

## Already shipped

Kept as an index, not a write-up. Each one's design decisions and its "don't break
this" rules are in `CLAUDE.md`; how its bugs actually presented is in
`docs/HISTORY.md`.

**v1.7.0** (2026-08-21)
- Jellyfin-backed visitor sign-in, and the account page that came with it. The
  *permissions* half is still open — see "Per-content visibility" above.
- The scheduled-task framework (`scheduler.py`), which is why nothing since has
  needed another background thread.

**v1.8.0** (2026-08-21)
- Restore the database from a backup zip, on the About page next to rollback.
- Release notes shown in-app, for the running version and everything newer.
- Version checks for the *Arr apps, Jellyfin and Seerr against their own releases.
- Per-user notifications by Discord DM and email, with contact details reused from
  Seerr rather than asked for twice.
- Email as a third notification channel.
- Radarr/Sonarr calendar, Seerr requests, qBittorrent downloads and Prowlarr's
  per-indexer health, as one media section.
- Unified search across Jellyfin and Seerr for signed-in visitors, with requesting.
- Seerr pending-approval count, and a Discord DM to admins when one arrives.

**v1.8.3** (2026-09-03)
- The Discord bot restart path, and a watchdog that brings the bot back on its own.
  The disconnect's underlying cause is still open — see "Known issues" above.

**v1.8.4** (2026-09-03)
- Reading the portal's own logs from `/admin/logs`, live, with a level filter and a
  download — plus daily log rotation with a retention window, so the page shows
  recent history rather than months of it.
- Keeping your scroll position when any admin form is saved, which was a panel-wide
  annoyance that had simply never been named.

**v1.8.5** (2026-09-04)
- Kiosk mode: a full-screen rotating display at `/kiosk` for a wall-mounted screen or
  a spare tablet, off by default, with each view gated by the same `show_public_*`
  settings the public pages use.
- A view too tall for the screen scrolls itself to the bottom and back within its own
  rotation slot, on a measured overflow rather than a screen-width breakpoint.

**v1.8.6** (2026-09-05)
- A maintenance window covering several services now sends one notification naming all
  of them, down every channel, instead of one per service. Three further notification
  bugs found by auditing the rest of that path went with it.
- The two email systems (the admin alert list, which ignores personal settings by
  design, and the per-user path the account checkboxes gate) are now explained on both
  pages that touch them, and no longer send the same maintenance event twice to an
  address on both.

**v1.8.7** (2026-09-05)
- Announcements take an optional display window - schedule one for later, or have it
  expire on its own. Blank means no bound, so existing announcements are unaffected.
- A filter box on the Settings page, which is long enough that finding one setting
  meant reading every label.
- Fixes: a Jellyfin username being used as an email address (Seerr reports one in its
  `email` field when an imported account has none), an unreachable service logging a
  traceback that read like a portal crash, and a stray `</div>` that closed the
  settings form early.

## Overall take

The list is short now, and what is on it is genuinely different in character rather
than more of the same.

**Windows packaging is the item most out of proportion to its priority label.**
Everything else here makes the portal do more; an installer and a service change who
can run it at all, and the auto-start half is the difference between the portal being
up after a power cut and not.

**Admin-on-a-separate-port** is the only architectural item whose implementation
shape is already fully settled, which makes it the cheapest of the three to pick up.
**The Linux fork** remains a genuinely open question, worth treating as one rather
than scheduling like a normal feature. **Per-content visibility** needs a design
conversation about granularity before any code — very likely a three-way
public/signed-in/admin flag, which is vastly cheaper than per-user rules and probably
enough.

Two things to build on rather than reinvent: the **scheduled-task framework** means
anything recurring is a `register()` call, not another background thread; and the
**visitor session** means anything that wants to know who is looking at the page
already has an answer.
