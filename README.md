# Status Portal

A personal status portal for your home server (Jellyfin, *Arr stack, SMB, etc.) —
links, announcements, incidents/maintenance and practical info, all editable from an
admin panel, no Flask knowledge or HTML editing required.

**[Project website](https://adam4125-officiel.github.io/Status-Portal/)** — screenshots,
the full feature list and integrations at a glance.

## Features

- Backend: Python/Flask · Storage: SQLite (a single file, created automatically)
- Automatic health checks per service, with auto-opened/resolved incidents, retries,
  a startup grace period, and a "slow" status tier
- Incidents and maintenance windows can each cover multiple services at once
- Scheduled maintenance windows that flip services to "maintenance" and back automatically
- **Scheduled tasks** admin page for the portal's own recurring background jobs
- Optional **Jellyfin-backed visitor sign-in**, fully separate from the admin login,
  with a personal account page (report history, admin replies, theme preference)
- **Unified search** across Jellyfin and Jellyseerr for signed-in visitors, with
  one-click requesting of anything not already in the library
- Public **"Report a problem"** form, feeding an admin Reports page
- Optional Discord/ntfy/**email** notifications, plus an optional Discord bot
  (self-editing status message, `/snapshot` command, and a watchdog that reconnects
  it by itself if the connection drops)
- **Kiosk mode** — a full-screen display at `/kiosk` for a wall-mounted TV or a spare
  tablet, rotating through services, incidents, VMs and resources on a timer
- **Log viewer** in the admin panel — recent entries with a level filter, and the
  full log downloadable as a `.log` file
- Optional **two-factor authentication** (TOTP) for the admin login
- Host restart/shutdown and per-VM controls (Windows/Hyper-V), plus app/bot restart,
  from the admin panel
- Custom logo/favicon, per-service links, 30-day uptime tracking, embeddable SVG
  badges, and an RSS feed
- **Self-updating** — one-click update from the admin panel or a standalone
  `update.py` script, with integrity checks, automatic backups and rollback

## Screenshots

| Public status page | Admin panel |
| --- | --- |
| ![Public status page](docs/images/screenshots/public-desktop-light.png) | ![Admin services page](docs/images/screenshots/admin-dashboard-desktop.png) |

More screenshots (incidents, scheduled tasks, notifications, dark mode, mobile) are on
the [project website](https://adam4125-officiel.github.io/Status-Portal/) and the
[wiki](https://github.com/Adam4125-officiel/Status-Portal/wiki).

## Quick start

```bash
cd status-portal
pip install -r requirements.txt
pip install -r requirements-discord.txt   # optional, only for the Discord bot
cp .env.example .env   # optional
python app.py
```

Open `http://localhost:5000` for the public page, `http://localhost:5000/admin` to set
the admin password on first launch. For continuous/production use, run
`python serve_waitress.py` instead of `app.py`. Docker is also supported.

## Jellyfin compatibility

**Jellyfin 10.8 through 12.0 (inclusive).** The same build works across all of them —
there is nothing to configure and no version to tell the portal about.

This matters because **Jellyfin 12.0 disables "legacy authorization" by default**, and
ships a migration that disables it on existing installs too, so simply upgrading your
server is enough to trigger it. Portal versions **before v1.8.8 authenticate with the
`X-Emby-Token` header, which 12.0 ignores** — on those, every Jellyfin feature (visitor
sign-in, the user sync, unified search, the health check, the transcode/high-load
signal and the version check) fails with a 401 the moment you upgrade Jellyfin.
**Update the portal to v1.8.8 or later before upgrading Jellyfin to 12.0.**

| Jellyfin | Status |
| --- | --- |
| 12.0 | Supported. All endpoints used by the portal checked against the official 12.0 OpenAPI document — all present, none deprecated |
| 10.11.x | Supported |
| 10.10.x | Supported |
| 10.8.x – 10.9.x | Supported |
| Older than 10.8 | Untested. The authorization method the portal uses is accepted as far back as 10.0.0, but the endpoints have not been checked against those releases |

<details>
<summary>How this was verified</summary>

The portal sends its token in the `Authorization` header with the `MediaBrowser`
scheme, which is the only mechanism accepted by every version in that range. Jellyfin's
own `AuthorizationContext.cs` was read at v10.0.0, v10.2.2, v10.3.7, v10.4.3, v10.5.5,
v10.6.4, v10.7.7, v10.8.13, v10.9.11, v10.10.7, v10.11.11 and v12.0 — there are three
distinct eras of behaviour, and every call the portal makes was then driven against a
stand-in server reproducing each one:

- **10.0 – 10.10** read `X-Emby-Authorization` first and fall back to `Authorization`
  when it is absent. The portal no longer sends the former, so the fallback applies.
- **10.11** reads `Authorization` first, with the legacy headers still enabled.
- **12.0** is the same code with legacy authorization off by default.

Endpoint availability was confirmed from Jellyfin's controller source at 10.8.13 and
10.10.7, and from the published OpenAPI document for 12.0.

No real Jellyfin server exists in the project's development environment, so this
verifies the authorization mechanism and the endpoint surface, not the behaviour of a
live install.
</details>

**If you do upgrade to Jellyfin 12.0**, follow Jellyfin's own release notes: take a full
backup first (rolling back is not possible without one), remove non-built-in plugins
before migrating, and run a full library scan afterwards.

## Documentation

Full documentation — installation (native Python + Docker), the configuration
reference, the daily-use admin panel guide, updating & rollback, security notes,
project structure, and testing — lives in the
**[GitHub Wiki](https://github.com/Adam4125-officiel/Status-Portal/wiki)**.

## License

[AGPL-3.0](LICENSE)
