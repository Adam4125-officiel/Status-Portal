# Roadmap / ideas not built yet

Everything proposed in earlier passes has been built: per-service/per-integration
auto-incidents, maintenance-window scheduling, Discord/ntfy push notifications, a
light theme toggle, SVG status badges, an RSS feed, per-service "slow" detection and
startup grace periods, CPU/GPU/per-disk temperature and per-disk I/O (Windows), a
high-load indicator (system metrics + Jellyfin transcode/task activity), and a
Discord server whitelist. What's left:

## Verification against real, live instances

The Jellyfin/*Arr/Jellyseerr integration parsing (`integrations.py`, including the
newer `/Sessions` and `/ScheduledTasks` fetchers behind the high-load indicator) is
only tested against mocked API payloads matching the documented response shapes -
this sandbox has no real instance of any of them to test against. Likewise the
Hyper-V VM status, Windows volume-label code, and the newer CPU/disk temperature +
per-disk I/O code (all PowerShell/CIM-backed) in `monitoring.py` have only ever run
their Linux/no-op branches for real - CPU temp (ACPI thermal zone) and disk temp
(`Get-StorageReliabilityCounter`) are both known-unreliable even on real Windows
hardware, so "shows nothing" there may be the platform, not a bug. If something
looks wrong against a real instance of any of these, start there rather than
assuming the Linux-tested paths are the problem.

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
service schema don't preclude adding this later.
