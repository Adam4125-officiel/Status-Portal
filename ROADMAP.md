# Roadmap / ideas not built yet

Everything proposed in earlier passes has been built: per-service/per-integration
auto-incidents, maintenance-window scheduling, Discord/ntfy push notifications, a
light theme toggle, SVG status badges, an RSS feed, per-service "slow" detection and
startup grace periods, CPU/GPU/per-disk temperature and per-disk I/O (Windows), a
high-load indicator (system metrics + Jellyfin transcode/task activity), a Discord
server whitelist, a public "report a problem" form, component restart controls
(whole app + Discord bot), a custom logo/favicon, Bazarr/Tdarr/Byparr integrations,
a per-service API health check mode, and auto-hiding old incidents/maintenance
history. What's left:

## Verification against real, live instances

The Jellyfin/*Arr/Jellyseerr/Bazarr/Tdarr/Byparr integration parsing (`integrations.py`,
including the newer `/Sessions` and `/ScheduledTasks` fetchers behind the high-load
indicator) is only tested against mocked API payloads matching each app's
documented response shape - this sandbox has no real instance of any of them to
test against. Bazarr/Tdarr/Byparr specifically (added 2026-08-10) are built from
each project's own published API docs/source, not confirmed against a running
instance - if a real Bazarr/Tdarr/Byparr's actual response shape differs from what
`fetch_bazarr_status()`/`fetch_tdarr_status()`/`fetch_byparr_status()` in
`integrations.py` expect, start there first. Likewise the Hyper-V VM status,
Windows volume-label code, and the newer CPU/disk temperature + per-disk I/O code
(all PowerShell/CIM-backed) in `monitoring.py` have only ever run their Linux/no-op
branches for real - CPU temp (ACPI thermal zone) and disk temp
(`Get-StorageReliabilityCounter`) are both known-unreliable even on real Windows
hardware, so "shows nothing" there may be the platform, not a bug. The Discord
bot's new `stop()`/`restart()` (see CLAUDE.md's Discord bot section) are also
unit-tested only, against a fake in-thread event loop, not a real gateway
connection. If something looks wrong against a real instance of any of these,
start there rather than assuming the Linux-tested/mocked paths are the problem.

## More reliable CPU/disk temperature via HWiNFO

Confirmed on the user's real box (2026-07-23): the current sources are unreliable
on desktop/enthusiast hardware — CPU temp via the ACPI thermal zone
(`MSAcpi_ThermalZoneTemperature`) returns nothing at all, and per-disk temp via
`Get-StorageReliabilityCounter` returns a literal `0` (now treated as "no
reading", not displayed as a real 0°C — see `monitoring._query_windows_disk_details()`)
for at least one drive, apparently one whose SMART temperature is only exposed as
attribute 190 "Airflow Temperature" rather than attribute 194
"Temperature"/"Drive Temperature". The user already runs HWiNFO, which reads both
sensors correctly on this exact hardware.

Discussed 2026-07-23, deliberately not built yet — the user chose to leave it as
today's graceful-degradation behavior (temp just doesn't show) for now rather than
take on a new dependency or implementation. If revisited, the options considered:

- **Read HWiNFO's Shared Memory** (leaning option) — reuses a tool already running
  and already correctly identifying these exact sensors, no new software to
  install. Requires "Shared Memory Support" enabled in HWiNFO Sensors' settings and
  HWiNFO kept running. More implementation work here: HWiNFO has no WMI provider,
  so this means parsing a documented but non-trivial binary shared-memory layout
  via `ctypes`/`mmap`, and temps go blank if HWiNFO isn't running.
- **smartmontools (`smartctl`) for disks + LibreHardwareMonitor's WMI provider for
  CPU** — two more standard, focused tools instead of one shared-memory
  integration. `smartctl` reads raw SMART attribute 194 first, falling back to 190,
  which would fix the specific drive precisely. LibreHardwareMonitor's WMI provider
  is queryable the same simple way this app already queries Hyper-V. More things to
  install, but each piece is simpler than shared-memory parsing.

## Possible Linux-specific fork/mode

Idea floated 2026-07-23, prompted by live-testing the retry feature entirely in
this (Linux) sandbox with real HTTP servers and no shortcuts — a reminder that
almost everything in this app (services/incidents/announcements/maintenance, the
Discord bot, integrations, high-load via system metrics, retry, slow status, grace
periods) is already fully cross-platform, built on psutil/Flask/SQLite with nothing
OS-specific. The *only* Windows-only pieces are in `monitoring.py`: Hyper-V VM
detection, Windows volume labels, CPU/disk temperature (PowerShell/CIM), and
per-disk I/O's drive-letter-to-PhysicalDriveN correlation.

Not started, just noted — if picked up, a Linux-native `monitoring.py` backend
would likely be *simpler* than the Windows one in places, not harder:

- **Per-disk I/O** is actually easier on Linux — `psutil.disk_io_counters(perdisk=True)`
  already returns clean device names (`sda`, `nvme0n1`) with no PhysicalDriveN-style
  correlation step needed; only partition→parent-disk name mapping (stripping a
  trailing partition number/`pN` suffix) is required, which is more
  straightforward than the Windows `Get-Partition`/`Get-PhysicalDisk` dance.
- **CPU temperature** — `psutil.sensors_temperatures()` works natively on Linux
  (reads `/sys/class/hwmon` under the hood), no PowerShell/CIM subprocess needed at
  all, and no ACPI-thermal-zone unreliability class of problem.
- **Per-disk temperature** — likely still needs `smartctl` (or parsing
  `/sys/class/hwmon` labels, less reliable) for the same reason as the
  HWiNFO/smartmontools option discussed above — no free lunch here on either OS.
- **VM detection** — Hyper-V doesn't apply; would need a different concept
  entirely (libvirt/KVM, or Docker container status) rather than a direct port.

## Jellyfin-backed user permissions

Use Jellyfin's own user database as an identity source, so individual Jellyfin
accounts see personalized extra instructions on the public page (e.g. "here's how to
join the Tailscale network") gated by who's logged in, instead of everyone seeing the
same static info page. This is a bigger architectural change: it introduces a second
authentication path alongside the existing single-admin-password login, plus a
visibility/permissions model per piece of content - worth a dedicated design
conversation before writing any code, rather than assumptions baked in here.

## Serve the admin panel on a separate port/subdomain from the public page

Idea from the user, 2026-08-01: run `/admin/*` on a different port (or a
dedicated subdomain) than the public status page, so the two can be exposed
completely independently — e.g. the public page reachable from the open
internet as today, while `/admin` is only reachable via Tailscale, a
different reverse-proxy rule, or not published externally at all. Right now
they're inseparable: same Flask app, same port, `/admin` just happens to
require login.

Worth doing, not started. Roughly two shapes to pick between if this gets
picked up:
- **Two WSGI listeners, one Flask app** — run the existing `app` object on a
  second port too (waitress supports binding multiple listeners, or run two
  `serve()` calls in separate threads), with a `before_request` check that
  404s `/admin/*` on the public-facing port and 404s everything *except*
  `/admin/*` on the admin-facing port. Simplest change, no code duplication,
  same process/DB connection pool.
- **Split into two Flask apps / blueprints** sharing `db.py` — cleaner
  separation but a bigger refactor (route registration, shared `before_request`
  hooks like CSRF and security headers would need to move to whatever's
  common, static file serving duplicated or centralized).

The first option is almost certainly the right call unless a reason turns up
not to — far less code churn for the same practical outcome. Needs a
decision on how the second port's bind address/number is configured
(probably a new `PORTAL_ADMIN_PORT` env var in `config.py`, unset = today's
behavior, single port, both public and admin).

---

Nothing above blocks anything already built. The current single-admin auth model and
service schema don't preclude adding this later.
