# status-portal

A personal status portal for your home server (Jellyfin, SMB, etc.): links, announcements,
incidents/maintenance, practical info — all editable from an admin panel, without ever
touching the HTML.

- Backend: **Python / Flask** (no need to know Flask, you manage everything from the browser)
- Storage: **SQLite** (a single file, `instance/portal.db`, created automatically)
- Public page auto-refreshes every 60s (configurable); light/dark theme toggle;
  the order of its content sections (services, incidents, resources, VMs...) is
  reorderable from Settings
- Optional automatic health checks per service, with auto-opened/resolved incidents
  (also from a failing Jellyfin/*Arr/Jellyseerr/Bazarr/Tdarr/Byparr status check, if
  linked), retries before marking a blip as actually down, a startup grace period so
  a slow-booting service isn't flagged before it's had a chance to start, and an
  optional per-service **API health check mode** that folds a linked integration's
  API reachability into that service's own displayed status (not just a separate
  sub-badge) — e.g. show "Down" if the web UI responds but the API doesn't
- A 502 response is treated as **down**, not degraded — unlike a 500/503/504, it
  means whatever's in front of the service (reverse proxy, gateway) couldn't reach
  it at all
- Incidents and scheduled maintenance windows can each cover **multiple services**
  at once, not just one, picked from a checkbox list
- A service can be excluded from the top-line overall-status banner, for something
  non-critical that shouldn't make the whole page look down
- Scheduled maintenance windows that flip a service to "maintenance" and back
  automatically, editable after creation
- Old resolved incidents can be **auto-hidden** from the public page after an
  admin-configurable number of days (a still-open incident is never hidden), with a
  "Load more" button to page through full history — plus a "maintenance history"
  section (ended windows, never shown publicly before) with the same paging
- A public **"Report a problem" form**, separate from the incident/maintenance
  system — visitors can flag something wrong (optionally tied to a specific
  service), landing in an admin Reports page with an unread-count badge and a
  one-click "create an incident from this report" action
- Optional Discord/ntfy push notifications on incident and maintenance events
- Optional Discord bot: presence/status updates, a self-editing status message
  posted on command, and a short `/snapshot` command (just what's down and any
  open incidents) — separate from the webhook notifications above
- Optional **two-factor authentication** (TOTP, works with Google Authenticator/
  Authy/1Password/etc.) for the admin login, off by default and never required
- Host restart/shutdown and per-VM start/stop/restart controls (Windows/Hyper-V
  for VMs), from the admin Resources page — plus a separate **System** page to
  restart the portal's own process or just its Discord bot connection, each behind
  a typed confirmation and a fresh 2FA code if enabled
- A custom **logo** (Settings → Branding), also used as the browser-tab favicon
- Per-service links (Tailscale, LAN, external domain...), per-incident update
  timelines, 30-day uptime tracking, and admin-configurable **defaults** so a new
  service's slow-threshold/retry/grace/API-health fields start pre-filled — all
  editable from `/admin`, no code involved
- Embeddable SVG status badges and an RSS feed, for use outside the page itself
- Crash/error logging to `instance/logs/app.log`, not just the console

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
  auto-resolved when it recovers. A service can also be marked to **exclude it from
  the overall status banner** — its own card still shows its real status, it just
  won't make the top-line "All services are operating normally" summary report an
  outage on its account. The public page shows a small badge on a service's card
  while it's mid-retry or still inside its startup grace period, so a visitor can
  tell the difference between "actually down" and "still figuring that out."
  Optionally set an **API health check mode** (Off / Degrade / Down) — if this
  service has an enabled Integration linked to it (see Integrations below) with
  "Show on public" turned on, that integration's own API reachability gets folded
  into this service's displayed status too, not just shown as a separate sub-badge:
  e.g. "Down" if the web UI responds but the linked app's API doesn't. A new
  service's slow-threshold/grace/retry/auto-incident/API-health-mode fields start
  pre-filled from admin-configured defaults (Settings → Service defaults) instead of
  hardcoded values — pre-fill only, changing the defaults later never affects a
  service that already exists.
- **Announcements**: banner at the top of the public portal. `**bold**`, auto-clickable links.
- **Incidents**: history of outages/maintenance, with status
  (investigating → identified → monitoring → resolved), an optional description, and
  a per-incident timeline of updates you (or the health checker) post over time.
  An incident can cover **more than one service** — check as many as apply, or none
  for a general/site-wide notice. Each open/resolve/update can push a Discord/ntfy
  notification if configured (see the config reference below). Old *resolved*
  incidents can be auto-hidden from the public page's initial view after a
  configurable number of days (Settings) — a still-open incident is never hidden
  regardless of age — with a "Load more incidents" button to page through full
  history.
- **Maintenance**: schedule a start/end time for one or more services (checked from
  a list) and they're automatically flipped to "maintenance" and back — no need to
  remember to toggle it manually on either end. Each service's own health check is
  paused for the duration, so it won't falsely open an incident while intentionally
  offline, and each one restores to its own pre-maintenance status independently. A
  scheduled window can be edited afterward (title, description, times); once it's
  already active its service list locks (to avoid orphaning the restore-state
  snapshot) but everything else stays editable. Ended maintenance windows are never
  shown on the initial public page load, but are reachable via a "Show maintenance
  history" button (also respects the auto-hide age setting above).
- **Reports**: a public **"Report a problem"** link (in the footer, and on each
  service card to reference that specific service) lets a visitor flag something
  wrong, separate entirely from the incident/maintenance system above — an optional
  contact field, light anti-spam (no account/login needed to submit). Landed reports
  show up here with an unread-count badge in the nav, can be marked reviewed/resolved
  or deleted, and a "Create incident" button turns one into a proper incident
  (pre-filled title/description) in one click.
- **Integrations**: read-only status/log checks for Jellyfin, Jellyseerr, *Arr apps,
  Bazarr, Tdarr, and Byparr. Optionally link one to a service, opt it into public
  display to show that service's API health on its public card, and/or let a failing
  check auto-open an incident on that service (off by default, to avoid noise from a
  flaky check). Tdarr and Byparr don't use an API key at all — leave that field blank
  for those two.
- **Resources**: CPU (with per-core breakdown and temperature, Windows only), memory,
  disks (auto-detected, with volume labels where available, and — Windows only —
  per-disk temperature and per-disk read/write throughput, replacing the old
  single system-wide I/O reading), network throughput, GPU (if `nvidia-ml-py` is
  installed, with temperature), and Hyper-V VM status (Windows only) — admin-only by
  default; choose exactly which of these to also show on the public page from
  Settings. CPU/disk temperature are best-effort even on Windows — some hardware
  doesn't expose them through the APIs used here, in which case they're just omitted
  rather than shown as zero. This page also has **Start/Restart/Stop buttons per
  detected VM** and **Restart/Shut down buttons for the host machine itself** — the
  host actions need a typed confirmation ("type RESTART/SHUTDOWN to confirm") given
  how destructive they are, and both are behind a fresh 2FA code if you've enabled
  two-factor authentication (see below), even if you're already logged in.
- **System**: separate from Resources above (that's the host machine's hardware,
  this is the portal's own process) — restart the whole app in place, or just the
  Discord bot's connection, without needing shell/SSH access to the machine. Same
  typed-confirmation and fresh-2FA-code protections as the host controls, since a
  full-app restart briefly takes the portal offline for everyone.
- **High-load indicator**: a badge on the public page and (optionally) the Discord
  bot that lights up when CPU, disk I/O, or network exceed admin-configurable
  thresholds (Settings) — or, if a Jellyfin integration is configured, when Jellyfin
  reports an active transcode or a running background task (e.g. trickplay image
  extraction). Separately, the public page can also show **which** Jellyfin
  background tasks are currently running (its own "Jellyfin activity" section,
  toggle in Settings) regardless of whether the high-load thresholds are tripped.
- **Info page**: free text at the bottom of the page (SMB access, VPN, contact...).
- **Settings**: site name, a custom **logo** (also used as the browser-tab favicon),
  per-cell public resource-monitor visibility, high-load thresholds, an auto-hide
  age threshold for old incidents/maintenance history, per-service defaults for the
  "New service" form, admin password, current health-check/refresh intervals,
  whether push notifications are configured, and the **order the public page's
  sections appear in** (up/down buttons — the top status banner and footer always
  stay fixed).
- **Discord Bot**: a separate, optional real bot connection (not just a webhook) — see
  its own section below.
- **Two-factor authentication**: `/admin/2fa` — see its own section below.

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
- Respond to a fixed **`/snapshot`** command — a much shorter, one-shot (never
  tracked/edited) plain-text reply: just which services are down, if any, and the
  full detail of every currently open incident (title, description, status,
  affected service(s), start time, and every update posted so far).

Both `/status` and `/snapshot` share the same authorization: optionally restrict who
can use them (a comma/newline-separated list of Discord user IDs) — leave it blank
and anyone in the server can; and optionally restrict which channels they'll respond
in (a comma/newline-separated list of channel IDs, see "server/channel management"
below) — leave it blank and they respond in any channel the bot can see.

Everything else is independently toggleable, as is exactly what's included in the
`/status` message: services (each shown with its status — including "Slow" — and any
configured links), an always-visible **active incidents** section that stays until
resolved (separate from the capped recent-incidents list, so a long-running incident
can't scroll out of view), announcements, scheduled maintenance, an optional
high-load section, and resources — where CPU, memory, disks (now including
temperature/I/O where available), network, GPU, and VM status are each individually
checkable (all off by default, to keep the message short unless you opt specific
ones in).

**Server & channel management** (`/admin/discord-bot/guilds`, linked from the main
Discord Bot page): lists every server (guild) the bot is currently in and its text
channels, alongside the channel whitelist mentioned above — handy for grabbing the
exact channel IDs to allow without leaving Discord's own UI open in another tab.

**Server whitelist (security control, separate from the user allowlist above)**:
`/admin/discord-bot` also has an optional comma/newline-separated list of Discord
server (guild) IDs. If set, the bot automatically **leaves** any server it's in
whose ID isn't on the list — checked the moment it's invited to a new server, and
again on every reconnect, so removing a server from the list later still takes
effect. Leave it blank (the default) and the bot stays in any server it's invited
to. This is stronger than the channel/user allowlists: an unwanted server could
otherwise still see the bot's presence/status updates even if nobody there is
authorized to run a slash command.

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

**Verification status**: the `/snapshot` command has been confirmed working against a
real Discord server (the user tested it live and gave feedback that shaped its current
formatting). Slash-command registration and the command handler are therefore confirmed
working in practice, not just unit-tested. Still unconfirmed against a real server: the
guild whitelist's actual `guild.leave()` call, the server/channel management page's
gateway-cache snapshot, and the restart-survives-message-editing behavior for the
tracked `/status` message — all unit tested directly (mocked Discord objects), just not
yet exercised for real. If something doesn't work as expected, check the server console
first — every failure (bad token, sync error, a channel it can't access) is logged
there, and to `instance/logs/app.log`.

### Two-factor authentication (optional, confirmed working)

`/admin/2fa` adds TOTP-based two-factor authentication to the admin login — works
with Google Authenticator, Authy, 1Password, or any standard authenticator app.
**Off by default and never required** — strongly recommended in the UI, especially
now that the admin panel can restart/shut down the host, but some people would
rather not use it, and that's fine.

To enable it:

1. Go to `/admin/2fa` → **Enable 2FA**.
2. Scan the QR code with your authenticator app (or enter the manual key shown
   below it if you can't scan).
3. Enter the 6-digit code it shows you, to confirm you scanned it correctly.

Once enabled:

- **Login becomes two steps**: password, then a code. A wrong password never even
  reaches the code prompt; a wrong code doesn't get you in either. The same login
  lockout (5 failed attempts, 5 minute cooldown) applies to wrong codes exactly
  like wrong passwords.
- **Restarting or shutting down the host asks for a fresh code**, even if you're
  already logged in — a stolen or replayed session cookie alone isn't enough to
  trigger the single most destructive thing this app can do.
- **To disable it**, go back to `/admin/2fa` and enter a current code — this also
  requires proving you still have your device, not just being logged in.

**Lost your phone / it's broken and you can't produce a code at all?** 2FA has a
recovery path that's deliberately *not* a web page: create an empty file named
`RESET_2FA` inside this app's `instance/` folder (same folder as `portal.db`). The
next time anyone loads the login page, 2FA is disabled and the file deletes itself
— no restart needed, and it's a one-shot action, not a standing switch you could
forget to turn back off. This is on purpose: a "reset 2FA" button reachable purely
over the web would just be another secret to protect, whereas creating a file
requires actually being able to reach the host's filesystem (SSH, a file manager
over the network, physical/remote-desktop access) — a meaningfully different bar
than knowing the admin password.

Requires the `pyotp` and `qrcode` packages (both in `requirements.txt` — nothing
extra to install if you already ran `pip install -r requirements.txt`).

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

Covers the DB layer (including multi-service incidents/maintenance), the
auto-incident lifecycle (service and integration-driven, including the startup
grace period, retries, and the per-service API health check merge logic),
maintenance-window scheduling and editing (including the checkbox-list service
picker), notification dispatch, badge/feed rendering, two-factor authentication
(enrollment, the two-step login, step-up on host/system control, the host-level
reset file), CSRF protection, the public report-a-problem form (anti-spam
honeypot/timing/rate-limit, admin review flow), the incident/maintenance-history
auto-hide and "load more" pagination, the Discord bot's message-building logic
(`/status` and `/snapshot`, including the guild/channel whitelists' behavior),
login/lockout, and its `stop()`/`restart()` connection lifecycle, service grouping,
the slow-status/high-load logic, host/VM/app/Discord-bot restart controls (against
a mocked `subprocess.run`/`os.execv`/fake event loop - the real commands and a real
Discord gateway connection are never invoked by the test suite), and the
Jellyfin/*Arr/Jellyseerr/Bazarr/Tdarr/Byparr status parsing (against mocked
responses - there's no way to test against real instances of those, or the
Windows-only temperature/per-disk-I/O code, from here). Not part of
`requirements.txt` since nothing here needs `pytest` at runtime.

## 6. Security notes

A few things are already in place for running this on the open internet, not just a
LAN/Tailscale: security response headers (CSP, `X-Frame-Options`, etc.) on every
response, hardened session cookie flags, CSRF protection on every admin action, a
login lockout after 5 failed admin-login attempts (5 min cooldown, applies to 2FA
codes too if enabled), optional two-factor authentication (see above), generic error
pages instead of default framework ones, and optional `ProxyFix` support
(`PORTAL_BEHIND_PROXY`) for correct behavior behind a reverse proxy. None of this
replaces putting a real reverse proxy/WAF/TLS in front if you expose this beyond a
VPN - it just means the app itself isn't the weak link.

**If you're considering exposing this beyond a private network** (Tailscale/LAN),
two things worth doing first:

- **Put a TLS-terminating reverse proxy in front of it.** This app has no built-in
  HTTPS - exposed raw, your admin password and session cookie would travel in
  plaintext on login. [Caddy](https://caddyserver.com/) gets you automatic
  Let's Encrypt certs with a few lines of config; then set `PORTAL_BEHIND_PROXY=true`
  and `PORTAL_FORCE_HTTPS_COOKIES=true`.
- **Keep the admin panel itself behind something like Tailscale or Cloudflare
  Access**, even if the public status page is reachable by anyone. The admin panel
  can now restart or shut down the host machine - that's exactly the kind of action
  worth an extra gate beyond a single password, on top of (not instead of) enabling
  two-factor authentication.

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
  monitoring.py            # CPU/RAM/disk/GPU/VM snapshot + host/VM power controls
  integrations.py          # read-only Jellyfin/*Arr/Jellyseerr/Bazarr/Tdarr/Byparr status checks
  notifications.py         # optional Discord/ntfy push notifications
  discord_bot.py           # optional Discord bot (presence, self-editing status message, /snapshot)
  twofactor.py             # optional TOTP two-factor authentication
  logging_setup.py         # crash/error logging to instance/logs/app.log
  requirements.txt, requirements-dev.txt
  Dockerfile, docker-compose.yml, .dockerignore, .env.example
  instance/portal.db      # created automatically on first launch
  instance/logs/app.log   # created automatically once the app is actually run
  templates/               # HTML pages (Jinja2)
  templates/sections/      # the public page's reorderable content blocks, one file each
  static/css/style.css     # all of the styling (dark + light theme)
  static/js/               # public page auto-refresh, local-time conversion, admin link-row editor, theme toggle, CSRF token injection, "load more" pagination
  static/uploads/          # the uploaded custom logo, if any - created automatically, not tracked in git
  tests/                   # pytest suite (see "Running the tests" below)
```
