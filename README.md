# status-portal

A personal status portal for your home server (Jellyfin, SMB, etc.): links, announcements,
incidents/maintenance, practical info — all editable from an admin panel, without ever
touching the HTML.

- Backend: **Python / Flask** (no need to know Flask, you manage everything from the browser)
- Storage: **SQLite** (a single file, `instance/portal.db`, created automatically)
- Public page auto-refreshes every 60s (configurable); light/dark theme toggle
- Optional automatic health checks per service, with auto-opened/resolved incidents
  (also from a failing Jellyfin/*Arr/Jellyseerr status check, if linked)
- Scheduled maintenance windows that flip a service to "maintenance" and back automatically
- Optional Discord/ntfy push notifications on incident and maintenance events
- Optional Discord bot: presence/status updates and/or a self-editing status message
  posted on command in any channel (separate from the webhook notifications above)
- Per-service links (Tailscale, LAN, external domain...), per-incident update timelines,
  30-day uptime history — all editable from `/admin`, no code involved
- Embeddable SVG status badges and an RSS feed, for use outside the page itself

---

## 1. Run it with native Python (recommended)

You need Python 3.10+ on the machine that will host this.

```bash
cd status-portal
pip install -r requirements.txt
cp .env.example .env   # optional, or just export the env vars yourself
python app.py
```

Open `http://localhost:5000` → public page.
Open `http://localhost:5000/admin` → asks you to create the admin password on first
launch (no default password, you choose it). Write it down — there's no "forgot password" flow.

Once you've confirmed it works, `Ctrl+C` to stop — `app.py` is the **dev** server,
not meant to run 24/7. For continuous/production use, run `python serve_waitress.py`
instead — it's a real WSGI server (waitress), not Flask's dev server.

Use whatever your OS provides for running a background process on boot (a systemd unit,
Task Scheduler, a process manager like `pm2`/`supervisord`, etc.) to run
`python serve_waitress.py` on startup, from the `status-portal` folder.

### ⚠️ Important: the session key

Without `PORTAL_SECRET_KEY` set (in `.env` or as a real env var), every server restart
logs you out of the admin panel. Generate a random string once and set it there, then
forget about it.

### Publishing it

This app only needs to publish its own port — how you expose that from there is entirely
up to you: a Tailscale/VPN network, a reverse proxy (Caddy, nginx, Traefik...) in front of
a real domain, a tunnel (Cloudflare Tunnel, etc.), or nothing at all if it's LAN-only.
None of that is this project's concern — point whatever you use at
`http://<this-machine>:<PORTAL_PORT>` and you're done.

### Updating to a new release

1. Stop the running process (`Ctrl+C`, or stop the service/task if it runs in the
   background).
2. Download the new release zip and extract it directly over your existing
   `status-portal` folder, overwriting the `.py`/template/static files.
3. Leave `instance/` alone — don't delete it, don't extract over it manually.
   There's nothing to do here: the release zip is built with `git archive`, which
   only ever includes files tracked in git, and `instance/portal.db` never has
   been one. Extracting the zip can't touch it — your services, incidents,
   announcements, and admin password all survive untouched.
4. `pip install -r requirements.txt` again, in case a dependency changed.
5. Start it back up (`python serve_waitress.py`).

No database migration step, no export/import — the "unzip and replace" you were
already doing is the whole process.

## 2. Run it with Docker (optional alternative)

If you'd rather run it in a container instead of directly on the host:

```bash
cp .env.example .env
# edit .env: at minimum, set PORTAL_SECRET_KEY to a long random string
docker compose up -d
```

Open `http://localhost:5000` (or whatever `HOST_PORT` you set in `.env`). The SQLite
database lives in a named Docker volume (`portal_data`), so it survives rebuilds/updates.

All configuration lives in `.env` — see `.env.example` for every available option
(ports, health-check frequency, public refresh frequency). No code to edit, ever, for
day-to-day tuning.

## 3. Daily use

Everything happens in `/admin`. Click **+ New** on either the Services or Integrations
list for a short wizard that asks whether you want a service, a status check, or both
created together (the common case for Jellyfin/*Arr/Jellyseerr).

- **Services**: add/edit your services (Jellyfin, SMB...), with an optional main link
  (leave it blank to hide the public "Open" button), icon, status, group/category, and
  any number of extra links (Tailscale, LAN, external domain...). Turn on "auto-check"
  and give it a check URL to have it pinged automatically — any response counts as
  reachable (a 401/403 login prompt from a service that gates its UI behind Basic
  Auth still means it's up), only a 5xx or a failed connection counts against it.
  Optionally set a **slow threshold (ms)**: a healthy-but-slow response shows "Slow"
  instead of "Operational" (purely informational — it never opens an incident on its
  own). Optionally set a **startup grace period (seconds)**: status/response time are
  still recorded normally, but no automatic incident opens for that service until the
  grace period (since the portal itself started) has elapsed, so a slow-booting
  service doesn't get flagged down before it's even had a chance to start. Optionally
  set **retry attempts / seconds between retries**: if a check fails, it's retried
  that many times (spaced that many seconds apart) before being treated as actually
  down — handles a service that blips and recovers on its own within seconds/minutes
  without opening a spurious incident (0 retries = mark it down on the first failed
  check, the original behavior). A service that goes down (outside its grace period,
  after any configured retries) automatically gets an incident opened for it, and
  auto-resolved when it recovers.
- **Announcements**: banner at the top of the public portal. `**bold**`, auto-clickable links.
- **Incidents**: history of outages/maintenance, with status
  (investigating → identified → monitoring → resolved) and a per-incident timeline of
  updates you (or the health checker) post over time. Each open/resolve/update can push
  a Discord/ntfy notification if configured (see the config reference below).
- **Maintenance**: schedule a start/end time for a service and it's automatically flipped
  to "maintenance" and back — no need to remember to toggle it manually on either end.
  The service's own health check is paused for the duration, so it won't falsely open
  an incident while intentionally offline.
- **Integrations**: read-only status/log checks for Jellyfin, Jellyseerr, and *Arr apps.
  Optionally link one to a service, opt it into public display to show that service's API
  health on its public card, and/or let a failing check auto-open an incident on that
  service (off by default, to avoid noise from a flaky check).
- **Resources**: CPU (with per-core breakdown and temperature, Windows only), memory,
  disks (auto-detected, with volume labels where available, and — Windows only —
  per-disk temperature and per-disk read/write throughput, replacing the old
  single system-wide I/O reading), network throughput, GPU (if `nvidia-ml-py` is
  installed, with temperature), and Hyper-V VM status (Windows only) — admin-only by
  default; choose exactly which of these to also show on the public page from
  Settings. CPU/disk temperature are best-effort even on Windows — some hardware
  doesn't expose them through the APIs used here, in which case they're just omitted
  rather than shown as zero.
- **High-load indicator**: a badge on the public page and (optionally) the Discord
  bot that lights up when CPU, disk I/O, or network exceed admin-configurable
  thresholds (Settings) — or, if a Jellyfin integration is configured, when Jellyfin
  reports an active transcode or a running background task (e.g. trickplay image
  extraction). Separately, the public page can also show **which** Jellyfin
  background tasks are currently running (its own "Jellyfin activity" section,
  toggle in Settings) regardless of whether the high-load thresholds are tripped.
- **Info page**: free text at the bottom of the page (SMB access, VPN, contact...).
- **Settings**: site name, per-cell public resource-monitor visibility, high-load
  thresholds, admin password, current health-check/refresh intervals, and whether
  push notifications are configured.
- **Discord Bot**: a separate, optional real bot connection (not just a webhook) — see
  its own section below.

Nothing to rebuild, nothing to redeploy: every change is in the database and visible
immediately (or within 60s max, the time it takes for the public page to auto-refresh).

Every timestamp on the public page (incidents, announcements, maintenance windows)
is shown in **the visitor's own local time**, not UTC — the server renders UTC and a
small script converts it in the browser, since the server has no way to know a
visitor's timezone. If JavaScript is unavailable, it falls back to showing the UTC
time with a "UTC" label instead of silently showing the wrong time.

### Discord Bot (optional, separate from the webhook notifications above)

`/admin/discord-bot` configures a real Discord bot connection that can:

- Update its own presence/status (e.g. "✅ All services up!") on a timer, and/or
- Respond to a `/status`-style **slash command** (name configurable) in any channel it
  can see, by posting a status summary embed — then keep **editing that same message**
  on a timer instead of posting a new one each time, to avoid spamming the channel.
  This survives an app restart: the tracked message id is stored in the database, not
  just in memory, so it keeps editing the same message rather than starting a new one.
  Optionally restrict who can even use the command (a comma/newline-separated list of
  Discord user IDs in the admin page) — leave it blank and anyone in the server can
  use it; set it to stop randos from spamming the command.

Both behaviors are independently toggleable, as is exactly what's included in the
message: services (each shown with its status — including "Slow" — and any
configured links), an always-visible **active incidents** section that stays until
resolved (separate from the capped recent-incidents list, so a long-running incident
can't scroll out of view), announcements, scheduled maintenance, an optional
high-load section, and resources — where CPU, memory, disks (now including
temperature/I/O where available), network, GPU, and VM status are each individually
checkable (all off by default, to keep the message short unless you opt specific
ones in).

**Server whitelist (security control, separate from the user allowlist above)**:
`/admin/discord-bot` also has an optional comma/newline-separated list of Discord
server (guild) IDs. If set, the bot automatically **leaves** any server it's in
whose ID isn't on the list — checked the moment it's invited to a new server, and
again on every reconnect, so removing a server from the list later still takes
effect. Leave it blank (the default) and the bot stays in any server it's invited
to. This is stronger than the user allowlist: an unwanted server could otherwise
still see the bot's presence/status updates even if nobody there is authorized to
run the slash command.

Uses a slash command, not a legacy text/prefix command — Discord's own guidance is
that reading plain message text requires the privileged **Message Content** intent,
which a slash command doesn't need at all. Nothing to enable in the Developer Portal
beyond inviting the bot.

To enable it:

1. Create an application + bot at the [Discord Developer Portal](https://discord.com/developers/applications).
2. Invite it to your server with permission to view channels and send messages.
3. `pip install discord.py` — this is an **optional** dependency, not in
   `requirements.txt` (same idea as `nvidia-ml-py` for GPU monitoring: nothing else in
   this app needs it, so it isn't forced on everyone).
4. Set `PORTAL_DISCORD_BOT_TOKEN` in `.env`.
5. Optional but recommended if this bot only lives in one server: set
   `PORTAL_DISCORD_BOT_GUILD_ID` to that server's ID (enable Developer Mode in Discord's
   settings, then right-click the server icon → Copy Server ID) so the slash command
   registers instantly. Without it, the command still works, just via a global sync
   that can take up to an hour to first appear.
6. Restart the app.

Leave `PORTAL_DISCORD_BOT_TOKEN` blank (the default) to disable this feature entirely —
nothing related to it runs, and `discord.py` doesn't need to be installed at all.

**Verification status**: the previous (now-replaced) plain-text `!status` version of
this feature was confirmed working end-to-end by actually running it. This slash-command
rewrite has not yet been re-confirmed against a real Discord server — the
message-building/embed logic, the command handler's authorization, and the guild
whitelist's leave behavior are all unit tested directly, and the connection handling was
smoke-tested against Discord's real login endpoint (confirms it starts cleanly in a
background thread and fails gracefully on a bad token, surfacing the error correctly on
the admin page), but slash-command registration, the command handler itself, the
restart-survives-message-editing behavior, and the guild whitelist's actual
`guild.leave()` call haven't been exercised against a real bot/server yet. If something
doesn't work as expected, check the server console first — every failure (bad token,
sync error, a channel it can't access) is logged there.

### Outside the page itself

- **Status badges**: `GET /badge.svg` (overall) and `GET /badge/<service-id>.svg`
  (per-service) — small embeddable SVG pills, handy in a GitHub README or another
  dashboard. Grab the exact URL from the "Badge" link next to each service in
  `/admin/services`. Public, no auth, no external service call.
- **RSS feed**: `GET /feed.xml` — incidents and announcements, for a feed reader instead
  of checking the page or setting up Discord/ntfy.
- **JSON status**: `GET /api/status` — the same data shown on the public page, as JSON.

## 4. Configuration reference

All of these are environment variables — set them in `.env` (Docker or bare Python, both
read it via `python-dotenv`) or as real env vars. See `.env.example`.

| Variable | Default | What it does |
|---|---|---|
| `PORTAL_SECRET_KEY` | *(random each restart)* | Flask session key — set this or you get logged out on every restart |
| `PORTAL_PORT` | `5000` | Port the app listens on (bare Python only; Docker uses `HOST_PORT` for the host side) |
| `HOST_PORT` | `5000` | Host port mapped to the container (Docker only) |
| `PORTAL_CHECK_INTERVAL_SECONDS` | `120` | Backend health-check frequency |
| `PORTAL_PUBLIC_REFRESH_SECONDS` | `60` | Public page auto-refresh frequency |
| `PORTAL_RESOURCE_REFRESH_SECONDS` | `10` | Auto-refresh frequency of the resources page (admin, and public if enabled) |
| `PORTAL_BEHIND_PROXY` | `false` | Set `true` only if a reverse proxy sits in front (trusts its `X-Forwarded-*` headers) |
| `PORTAL_FORCE_HTTPS_COOKIES` | `false` | Set `true` once served over HTTPS, to mark the session cookie `Secure` |
| `PORTAL_DISCORD_WEBHOOK_URL` | *(blank = disabled)* | Discord webhook URL — get pinged on incident/maintenance events |
| `PORTAL_NTFY_URL` | *(blank = disabled)* | Full [ntfy](https://ntfy.sh) topic URL — same events, no Discord account needed |
| `PORTAL_DISCORD_BOT_TOKEN` | *(blank = disabled)* | Enables the optional Discord bot (see its own section above) — requires `pip install discord.py` |
| `PORTAL_DISCORD_BOT_REFRESH_SECONDS` | `300` | How often the bot updates its presence / edits its tracked status messages |
| `PORTAL_DISCORD_BOT_GUILD_ID` | *(blank = global sync)* | Set to your server's ID for instant slash-command registration on a single-server bot |

## 5. Running the tests

```bash
pip install -r requirements-dev.txt
pytest
```

Covers the DB layer, the auto-incident lifecycle (service and integration-driven,
including the startup grace period), maintenance-window scheduling, notification
dispatch, badge/feed rendering, the Discord bot's message-building logic (including
the guild whitelist's leave behavior) and login/lockout, service grouping, the
slow-status/high-load logic, and the Jellyfin/*Arr/Jellyseerr status parsing
(against mocked responses - there's no way to test against real instances of those,
or the Windows-only temperature/per-disk-I/O code, from here). Not part of
`requirements.txt` since nothing here needs `pytest` at runtime.

## 6. Security notes

A few things are already in place for running this on the open internet, not just a
LAN/Tailscale: security response headers (CSP, `X-Frame-Options`, etc.) on every
response, hardened session cookie flags, a login lockout after 5 failed admin-login
attempts (5 min cooldown), generic error pages instead of default framework ones, and
optional `ProxyFix` support (`PORTAL_BEHIND_PROXY`) for correct behavior behind a
reverse proxy. None of this replaces putting a real reverse proxy/WAF/TLS in front if
you expose this beyond a VPN - it just means the app itself isn't the weak link.

These two intervals are independent — don't confuse them: the health checker polls each
service's `check_url` on its own schedule, unrelated to how often a visitor's browser
reloads the public page.

## 7. Project structure

```
status-portal/
  CLAUDE.md                # notes for future AI coding sessions on this repo
  app.py                  # Flask routes (public + admin)
  serve_waitress.py       # run this in production (instead of app.py)
  config.py               # all configuration, read from env vars / .env
  db.py                    # the entire SQLite layer
  monitoring.py            # CPU/RAM/disk/GPU/VM snapshot for the resources page
  integrations.py          # read-only Jellyfin/*Arr/Jellyseerr status checks
  notifications.py         # optional Discord/ntfy push notifications
  discord_bot.py           # optional Discord bot (presence + self-editing status message)
  requirements.txt, requirements-dev.txt
  Dockerfile, docker-compose.yml, .dockerignore, .env.example
  instance/portal.db      # created automatically on first launch
  templates/               # HTML pages (Jinja2)
  static/css/style.css     # all of the styling (dark + light theme)
  static/js/               # public page auto-refresh, local-time conversion, admin link-row editor, theme toggle
  tests/                   # pytest suite (see "Running the tests" below)
```
