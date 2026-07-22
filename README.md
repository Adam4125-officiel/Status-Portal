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
  and give it a check URL if the service has an endpoint that responds with HTTP 200 —
  otherwise leave it on manual status and change it yourself. A service that goes down
  automatically gets an incident opened for it, and auto-resolved when it recovers.
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
- **Resources**: CPU (with per-core breakdown), memory, disks (auto-detected, with
  volume labels where available), disk and network throughput, GPU (if
  `nvidia-ml-py` is installed), and Hyper-V VM status (Windows only) — admin-only by
  default; choose exactly which of these to also show on the public page from Settings.
- **Info page**: free text at the bottom of the page (SMB access, VPN, contact...).
- **Settings**: site name, per-cell public resource-monitor visibility, admin password,
  current health-check/refresh intervals, and whether push notifications are configured.

Nothing to rebuild, nothing to redeploy: every change is in the database and visible
immediately (or within 60s max, the time it takes for the public page to auto-refresh).

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

## 5. Running the tests

```bash
pip install -r requirements-dev.txt
pytest
```

Covers the DB layer, the auto-incident lifecycle (service and integration-driven),
maintenance-window scheduling, notification dispatch, badge/feed rendering,
login/lockout, service grouping, and the Jellyfin/*Arr/Jellyseerr status parsing
(against mocked responses - there's no way to test against real instances of those
from here). Not part of `requirements.txt` since nothing here needs `pytest` at runtime.

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
  app.py                  # Flask routes (public + admin)
  serve_waitress.py       # run this in production (instead of app.py)
  config.py               # all configuration, read from env vars / .env
  db.py                    # the entire SQLite layer
  monitoring.py            # CPU/RAM/disk/GPU/VM snapshot for the resources page
  integrations.py          # read-only Jellyfin/*Arr/Jellyseerr status checks
  notifications.py         # optional Discord/ntfy push notifications
  requirements.txt, requirements-dev.txt
  Dockerfile, docker-compose.yml, .dockerignore, .env.example
  instance/portal.db      # created automatically on first launch
  templates/               # HTML pages (Jinja2)
  static/css/style.css     # all of the styling (dark + light theme)
  static/js/               # public page auto-refresh, admin link-row editor, theme toggle
  tests/                   # pytest suite (see "Running the tests" below)
```
