# Candidate features (not built yet)

Ideas discussed with Adam, written up so they can slot straight into
`ROADMAP.md`. Each one lists a rough priority and implementation effort
(**S**mall / **M**edium / **L**arge) alongside the reasoning, not just the
label — the point is that someone new to the repo can see *why*, not just
where it landed.

## Solid features

Contained but touch the schema or add a genuinely new concept.

### Restore from backup
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

### Updater quality-of-life: release notes in the app, and restore from a backup zip
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

### Version-checker for the *Arr apps themselves
Radarr, Sonarr and Prowlarr each expose their own current version and can be
checked against their latest GitHub release — the same way this app already
checks its own version for self-updates. This would surface "Radarr has an
update available" in the admin panel, read-only, no auto-update of those
apps involved, just visibility, so nobody's manually checking three separate
web UIs to know if anything's behind.

**Priority:** Medium · **Effort:** M — the version-comparison logic in
`updater.py` is already there to reuse, but each app has its own
releases API/format to query and parse.

### Email notifications
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

### Radarr + Prowlarr + qBittorrent integration
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

### Unified search across Jellyfin and Seerr (signed-in users only)
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

### Prowlarr per-indexer health
Prowlarr's own API already reports each configured indexer's individual
state (healthy / down / rate-limited), not just whether Prowlarr itself is
reachable. Surfacing that per-indexer list matters because "Prowlarr is up"
hides the failure mode that actually happens in practice — one or two
indexers going stale or getting rate-limited while Prowlarr itself runs
fine.

**Priority:** Medium · **Effort:** M-L — depends on, or extends, the
Prowlarr integration above; needs its own endpoint parsing and a small
per-indexer list in the UI.

### Seerr pending-approval count + Discord DM to admins
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

**Priority:** Medium-High · **Effort:** S-M — the implementation shape is
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

## Overall take

The original five quick wins plus service dependencies are all built now
(see CLAUDE.md for the reasoning behind each) — what's left below is
genuinely more work, not more of the same. **Restore from backup** is the
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
