# status-portal

A personal status portal for your home server (Jellyfin, SMB, etc.): links, announcements,
incidents/maintenance, practical info — all editable from an admin panel, without ever
touching the HTML.

- Backend: **Python / Flask** (no need to know Flask, you manage everything from the browser)
- Storage: **SQLite** (a single file, `instance/portal.db`, created automatically)
- Public page auto-refreshes every 60s (configurable)
- Optional automatic health checks per service, with auto-opened/resolved incidents
- Per-service links (Tailscale, LAN, external domain...), per-incident update timelines,
  30-day uptime history — all editable from `/admin`, no code involved

---

## 1. Run it with Docker (recommended)

```bash
cp .env.example .env
# edit .env: at minimum, set PORTAL_SECRET_KEY to a long random string
docker compose up -d
```

Open `http://localhost:5000` (or whatever `HOST_PORT` you set in `.env`). The SQLite
database lives in a named Docker volume (`portal_data`), so it survives rebuilds/updates.

All configuration lives in `.env` — see `.env.example` for every available option
(ports, health-check frequency, public refresh frequency, disk path for the resource
monitor). No code to edit, ever, for day-to-day tuning.

### Publishing it

This app only needs to publish its own port — how you expose that from there is entirely
up to you: a Tailscale/VPN network, a reverse proxy (Caddy, nginx, Traefik...) in front of
a real domain, a tunnel (Cloudflare Tunnel, etc.), or nothing at all if it's LAN-only.
None of that is this project's concern — point whatever you use at
`http://<this-machine>:<HOST_PORT>` and you're done.

## 2. Run it without Docker

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

## 3. Daily use

Everything happens in `/admin`:

- **Services**: add/edit your services (Jellyfin, SMB...), with a main link, icon, status,
  and any number of extra links (Tailscale, LAN, external domain...). Turn on "auto-check"
  and give it a check URL if the service has an endpoint that responds with HTTP 200 —
  otherwise leave it on manual status and change it yourself. A service that goes down
  automatically gets an incident opened for it, and auto-resolved when it recovers.
- **Announcements**: banner at the top of the public portal. `**bold**`, auto-clickable links.
- **Incidents**: history of outages/maintenance, with status
  (investigating → identified → monitoring → resolved) and a per-incident timeline of
  updates you (or the health checker) post over time.
- **Info page**: free text at the bottom of the page (SMB access, VPN, contact...).
- **Settings**: change the admin password; see the current health-check/refresh intervals.

Nothing to rebuild, nothing to redeploy: every change is in the database and visible
immediately (or within 60s max, the time it takes for the public page to auto-refresh).

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
| `PORTAL_MONITOR_DISK_PATH` | `/` | Disk path reported in the admin resource monitor |

These two intervals are independent — don't confuse them: the health checker polls each
service's `check_url` on its own schedule, unrelated to how often a visitor's browser
reloads the public page.

## 5. Project structure

```
status-portal/
  app.py                  # Flask routes (public + admin)
  serve_waitress.py       # run this in production (instead of app.py)
  config.py               # all configuration, read from env vars / .env
  db.py                    # the entire SQLite layer
  requirements.txt
  Dockerfile, docker-compose.yml, .dockerignore, .env.example
  instance/portal.db      # created automatically on first launch
  templates/               # HTML pages (Jinja2)
  static/css/style.css     # all of the styling
  static/js/               # public page auto-refresh + admin link-row editor
```
