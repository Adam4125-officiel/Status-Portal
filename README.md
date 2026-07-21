# status-portal

A personal status portal for your home server (Jellyfin, SMB, etc.): links, announcements,
incidents/maintenance, practical info — all editable from an admin panel, without ever
touching the HTML.

- Backend: **Python / Flask** (no need to know Flask, you manage everything from the browser)
- Storage: **SQLite** (a single file, `instance/portal.db`, created automatically)
- Public page auto-refreshes every 60s
- Optional automatic health checks (periodic HTTP ping per service)

---

## 1. Installation

You need Python 3.10+ on the machine that will host this.

```bash
cd status-portal
pip install -r requirements.txt
```

## 2. First run (local test)

```bash
python app.py
```

Open `http://localhost:5000` → public page.
Open `http://localhost:5000/admin` → asks you to create the admin password on first
launch (no default password, you choose it). Write it down — there's no "forgot password" flow.

Once you've confirmed it works, `Ctrl+C` to stop — `app.py` is the **dev** server,
not meant to run 24/7.

## 3. Running continuously (production)

Use `serve_waitress.py` instead of `app.py` — it's a real WSGI server (waitress),
not Flask's dev server.

```bash
python serve_waitress.py
```

It listens on port 5000 by default (change it with the `PORTAL_PORT` env var).

### Publishing it

This app only needs to publish its own port — how you expose that port from there is
entirely up to you: a Tailscale/VPN network, a reverse proxy (Caddy, nginx, Traefik...)
in front of a real domain, a tunnel (Cloudflare Tunnel, etc.), or nothing at all if it's
LAN-only. None of that is this project's concern — point whatever you use at
`http://<this-machine>:5000` (or your `PORTAL_PORT`) and you're done.

### Running it at startup

Use whatever your OS provides for running a background process on boot (a systemd unit,
Task Scheduler, a process manager like `pm2`/`supervisord`, etc.) to run
`python serve_waitress.py` from the `status-portal` folder.

### ⚠️ Important: the session key

Without this, every server restart logs you out of the admin panel. Set a fixed
environment variable:

```bash
export PORTAL_SECRET_KEY="a-long-random-string-of-your-own"
```

(Generate a random string once, set it there, then forget about it.)

## 4. Daily use

Everything happens in `/admin`:

- **Services**: add/edit your services (Jellyfin, SMB...), with a link, icon, status.
  Turn on "auto-check" and give it a check URL if the service has an endpoint that
  responds with HTTP 200 — otherwise leave it on manual status and change it yourself.
- **Announcements**: banner at the top of the public portal. `**bold**`, auto-clickable links.
- **Incidents**: history of outages/maintenance, with status
  (investigating → identified → monitoring → resolved).
- **Info page**: free text at the bottom of the page (SMB access, VPN, contact...).
- **Settings**: change the admin password.

Nothing to rebuild, nothing to redeploy: every change is in the database and visible
immediately (or within 60s max, the time it takes for the public page to auto-refresh).

## 5. Project structure

```
status-portal/
  app.py                  # Flask routes (public + admin)
  serve_waitress.py       # run this in production (instead of app.py)
  db.py                   # the entire SQLite layer
  requirements.txt
  instance/portal.db      # created automatically on first launch
  templates/               # HTML pages (Jinja2)
  static/css/style.css     # all of the styling
  static/js/main.js        # public page auto-refresh
```

Two different, independent intervals control this app's cadence — don't confuse them:
- The **public page auto-refresh** (browser-side, `static/js/main.js`, `REFRESH_SECONDS`, 60s by default).
- The **backend health-check frequency** (server-side polling of each service's `check_url`,
  `CHECK_INTERVAL_SECONDS` near the top of `app.py`, 120s by default).
