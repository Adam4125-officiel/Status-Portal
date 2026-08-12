# Candidate features (not built yet)

Ideas discussed with Adam, written up so they can slot straight into
`ROADMAP.md`. Each one lists a rough priority and implementation effort
(**S**mall / **M**edium / **L**arge) alongside the reasoning, not just the
label — the point is that someone new to the repo can see *why*, not just
where it landed.

## Quick wins

Small, contained changes that mostly reuse code that already exists.

### Test-notification button
A button in Settings next to the Discord/ntfy fields that fires a one-off
test message through whichever channel(s) are configured, using the exact
same dispatch path as a real incident/maintenance notification (`notifications.py`) rather than a separate code path. The point isn't just
"is the webhook URL reachable" — it's confirming the whole chain (correct
URL, correct ntfy topic, bot permissions if the bot also posts) without
waiting for, or faking, a real incident to find out it's been broken for
three weeks.

**Priority:** High · **Effort:** S — no new logic, just a route that calls
the existing notification function with a canned payload.

### Manual DB backup/export button
An admin-triggered "download a backup" button (Settings, or a small page of
its own) that zips up `instance/portal.db` and serves it as a download,
independent of the automatic backups the self-updater already takes before
every update (`instance/update_backups/<timestamp>/`). Useful as a
"before I go poke at something manually" safety net, or just to keep an
off-box copy around.

**Priority:** High · **Effort:** S — the file-copy/zip logic already exists
in `updater.py` for update rollbacks; this mostly reuses it behind a new
route.

### Low disk space alert
A second threshold, separate from the existing high-load indicator
(CPU / disk-I/O / network), that watches free disk space per volume and
opens an incident (or just fires a notification) once it drops below an
admin-configured percentage or absolute value. This is a different failure
mode from high I/O throughput — a disk can be completely idle and still be
nearly full.

**Priority:** High · **Effort:** S — extends the same threshold/notification
pipeline `monitoring.py` and `notifications.py` already share for high-load,
with one more metric.

### Dark mode that follows the OS preference
Today the light/dark toggle is a manual per-visitor switch. Adding a
`prefers-color-scheme: dark` media query as the *default* — before any
manual choice has been made — means a visitor whose system is set to dark
mode gets it automatically, while an explicit click on the toggle still
overrides it. Same pattern most sites use today.

**Priority:** Medium · **Effort:** S — CSS-only, layered under the existing
toggle logic in `static/js/`.

### Service ↔ Hyper-V VM mapping in the admin panel
The Resources page already lists detected Hyper-V VMs with start/stop/restart
controls; this adds a link from a *service* to the VM it runs on, shown right
on that service's admin card (e.g. "Seerr — runs on VM-Media02"). Mostly
about faster troubleshooting: see a service is down, immediately see which
VM to go check, without cross-referencing two separate admin pages.

**Priority:** Medium · **Effort:** S — a new optional field on the service
record pointing at an already-detected VM id, plus a small UI addition.

## Solid features

Contained but touch the schema or add a genuinely new concept.

### Service dependencies
Lets a service be marked as depending on one or more other services (e.g.
Seerr depends on Radarr and Sonarr). If a dependency is down, the dependent
service shows as **degraded** instead of either lying (showing "operational"
when it can't actually do anything useful) or falsely showing "down" (when
the service itself is fine, just starved by something upstream). This also
sets up the "media stack" idea further down — a coherent way to represent a
service that's technically running but functionally broken because of
something else.

**Priority:** High · **Effort:** M — a new relation in the schema (`db.py`),
status-computation changes on both the admin and public side, and a picker
UI (a checkbox list, similar to how incidents/maintenance already pick
multiple services).

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

### Jellyfin-backed user permissions
Uses Jellyfin's own user accounts as an identity source, so a logged-in
Jellyfin user could see personalized content on the public page (e.g.
Tailscale join instructions) instead of everyone seeing the same static Info
page. A genuinely bigger change: it adds a second authentication path
alongside the current single-admin-password login, plus a
permissions/visibility model per piece of content — worth a dedicated design
conversation before any code, since it touches how content is modeled
everywhere, not just one page.

**Priority:** Medium · **Effort:** L — new auth path, new permissions model,
and it's what the Radarr/Prowlarr/qBittorrent integration above would
ideally build on rather than duplicate later.

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

The five quick wins are close to free value — each one reuses code that
already exists (`notifications.py`, `updater.py`'s backup logic, the
high-load threshold pipeline) and touches a small, contained surface.
**Service dependencies** is worth prioritizing above what its Medium-effort
label suggests, because it's the one item here that changes how *truthful*
the status page is as a whole, not just adds a new capability. Of the three
architectural carry-overs, **admin-on-a-separate-port** is the only one with
an implementation shape already fully decided — the other two (Jellyfin
auth, Linux fork) are genuinely open design questions, not just bigger
builds, and worth treating that way rather than scheduling them like a
normal feature.
