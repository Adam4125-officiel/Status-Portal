# Roadmap / ideas not built yet

## *Arr stack + Jellyseerr + Jellyfin status (done, read-only)

Implemented: `/admin/integrations` now shows read-only health/log status for Jellyfin,
Jellyseerr/Overseerr, and Servarr-family apps (Sonarr, Radarr, Prowlarr, Lidarr,
Readarr) via `integrations.py`. Not yet done, and worth a follow-up if wanted:

- Wiring a failing integration into the existing auto-incident mechanism
  (`_handle_incident_lifecycle` in `app.py`), the same way a down service opens one.
- Background polling (today's check is live-on-page-load, like the resource monitor -
  no history, no notification if you're not looking at the page).
- Verification against real, live Jellyfin/Sonarr/Radarr/Jellyseerr instances - the
  parsing logic is tested against mocked API payloads matching the documented response
  shapes, but this hasn't been exercised against an actual running instance of any of
  them.

## Jellyfin-backed user permissions

Use Jellyfin's own user database as an identity source, so individual Jellyfin
accounts see personalized extra instructions on the public page (e.g. "here's how to
join the Tailscale network") gated by who's logged in, instead of everyone seeing the
same static info page. This is a bigger architectural change: it introduces a second
authentication path alongside the existing single-admin-password login, plus a
visibility/permissions model per piece of content - worth a dedicated design
conversation before writing any code, rather than assumptions baked in here.

---

Nothing above blocks anything already built. The current single-admin auth model and
service schema don't preclude adding either one later.
