# Roadmap / ideas not built yet

A couple of bigger ideas came up while scoping this project that are deliberately not
implemented — they need their own design pass rather than being bolted on quickly.

## *Arr stack + Jellyseerr + Jellyfin log integration (admin-only)

Pull errors/logs from the *Arr stack (Sonarr/Radarr/Prowlarr/...), Jellyseerr, and
Jellyfin's own API into a centralized view inside `/admin`, so failures across the
wider media stack surface here instead of requiring five different UIs. Open questions
to resolve before starting: which APIs/versions to target, how to store per-integration
API keys (likely a new `integrations` table + admin form, managed the same way
everything else here is - not env vars, since these are per-service credentials the
admin sets up one at a time), how often to poll each one, and whether a failure there
should be able to open an incident too (reusing the auto-incident mechanism services
already have via `_handle_incident_lifecycle` in `app.py`).

## Jellyfin-backed user permissions

Use Jellyfin's own user database as an identity source, so individual Jellyfin
accounts see personalized extra instructions on the public page (e.g. "here's how to
join the Tailscale network") gated by who's logged in, instead of everyone seeing the
same static info page. This is a bigger architectural change: it introduces a second
authentication path alongside the existing single-admin-password login, plus a
visibility/permissions model per piece of content - worth a dedicated design
conversation before writing any code, rather than assumptions baked in here.

---

Neither idea blocks anything already built. The current single-admin auth model and
service schema don't preclude adding either one later.
