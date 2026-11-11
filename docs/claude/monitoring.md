## Monitoring architecture (`monitoring.py`)

- The Windows-only, PowerShell/CIM-backed queries (Hyper-V VM list, CPU temperature,
  per-disk temperature + drive-letter-to-physical-disk mapping) are **polled by a
  background thread** (`monitoring.start_background_refresh()`, started from both
  `app.py` and `serve_waitress.py` at startup) into a module-level cache
  (`_WINDOWS_CACHE`), not queried live inside a request handler — the same
  "never call slow I/O in a request handler" rule as the integration cache, applied to
  local subprocess calls. `get_vm_snapshot()` and the `_query_*()` functions stay
  live/directly-callable (that's what's unit-tested by mocking `subprocess.run`);
  `get_cached_vm_snapshot()` and `get_resource_snapshot()`'s `cpu_temp_c`/per-disk
  `temp_c`/`io` fields are the cache-reading wrappers request handlers should use.
- **The Windows queries run on their own cadence and never on the loop's thread**
  (`_WINDOWS_JOBS`): VMs plus CPU temperature every 60s, the disk mapping plus drive
  temperatures every 12 minutes. When one comes due, `_dispatch_due_windows_jobs()`
  starts it on a one-shot thread and the loop moves on. Before 1.9.1 all three
  PowerShell queries ran inline every 10s (~26k cold starts a day), and a slow one
  (up to 10+10+15s on timeouts) delayed the CPU sample past its `max_age`, sending
  every page back to the blocking psutil read. A job never overlaps itself.
  `clear_caches()` makes every job due again, or the disk mapping, and with it every
  disk's temperature and I/O, would stay blank for up to 12 minutes after the admin's
  clear-caches button. **Only confirmed with mocked `subprocess`; this sandbox has no
  Windows.** Verify on the real host before trusting the cadence.
- **Network and per-disk I/O rates are computed by the loop, once per tick**
  (`_refresh_rates()` into `_RATES_CACHE`), and callers read them. A rate is a delta
  between two readings, and when every caller (request threads, the health loop, the
  Discord bot) took its own, each moved the shared baseline for the others: two
  callers milliseconds apart turned a steady transfer into a spike and a false
  high-load badge. `_rates_from_the_loop()` decides: loop running means read the cache,
  and "not published yet" is no reading rather than a caller's own. With no loop
  (tests, an entry point that never started it) or a dead one, callers fall back to
  the old per-call delta.
- **Never `nvmlInit()`/`nvmlShutdown()` per call.** `_get_gpu_snapshot()` initialises
  NVML once, lazily, under `_nvml_lock`, and keeps it initialised for the life of the
  process (`_nvml_state`). On real drivers every init/shutdown cycle leaks ~7 KB of the
  kernel's non-paged `NVRM` pool until the process exits - ~2.6 MB/h at the 10s refresh,
  invisible to Task Manager's per-process view. An NVML error drops the flag (best-effort
  shutdown) so the next call re-inits. `clear_caches()` must **not** touch it (it is a
  driver session, not a reading; re-initialising there would leak again) - `conftest`
  resets the flag for tests instead. Any new NVML caller goes through this function.
  Story and numbers: `docs/HISTORY.md` -> "1.12.2: the NVRM kernel-pool leak". Verified
  on the real host (2026-11-06): `NVRM` flat after the fix.
- Per-disk temperature and I/O are **Windows-only** — correlating a mountpoint to a
  physical disk (needed for `psutil.disk_io_counters(perdisk=True)`'s
  `PhysicalDriveN` keys) uses `Get-Partition`'s drive-letter-to-disk-number mapping,
  which has no equivalent implemented here for Linux. The old aggregate
  (all-disks-combined) I/O reading was retired in favor of this — there's no
  system-wide I/O card anymore, only per-disk.
- **CPU temp (ACPI thermal zone WMI) and disk temp (`Get-StorageReliabilityCounter`)
  are both well-known-unreliable on real Windows hardware** — many systems return
  null/nothing through either. Both degrade to `None` gracefully. Confirmed on the
  user's actual desktop that CPU temp returns nothing at all, and that one drive
  reported a literal `0` (now treated as "no reading", since a real drive is never
  0°C). Neither swaps in a better data source — see `ROADMAP.md` → "More reliable
  CPU/disk temperature via HWiNFO", and `docs/HISTORY.md` for what was actually
  observed. Don't assume this area is fully fixed.

## High-load indicator (`monitoring.evaluate_high_load()` / `integrations.evaluate_high_load()`)

- Split across two functions on purpose: `monitoring.evaluate_high_load(snapshot,
  thresholds)` is pure (no DB access — keeps `monitoring.py` DB-free, a constraint
  that predates this feature) and only compares system metrics (CPU/disk-I/O/network)
  against admin-configured thresholds. `integrations.evaluate_high_load(snapshot)`
  wraps it and layers in Jellyfin-derived signals (active transcode count via
  `/Sessions`, running scheduled tasks like trickplay generation via
  `/ScheduledTasks`, cached the same way `_integration_status_cache` works).
  `integrations.py` is the single place both `app.py` (public page) and
  `discord_bot.py` call this from — `discord_bot.py` can't import `app.py` (circular:
  `app.py` already imports `discord_bot.py`), so the shared logic had to live
  somewhere neither of them owns. If you add another cross-cutting signal both the web
  page and the bot need, this is probably where it belongs, not duplicated in both.
- **The public page can also show Jellyfin's running scheduled tasks directly**
  (`show_public_jellyfin_tasks` setting) — separate from the high-load indicator, this
  just renders `integrations.get_cached_jellyfin_activity()["running_tasks"]` as its
  own "Jellyfin activity" section whenever the list is non-empty, regardless of
  whether high-load's thresholds are also tripped. Reads the same
  background-refreshed cache — no extra polling added.

- **End of a cycle is `_finish_health_cycle()`**, the last step of `run_health_checks()`:
  it stores `last_health_cycle_at` in `settings` (shown on `/admin/tasks`) and, when
  `PORTAL_HEARTBEAT_URL` is set, sends a GET with a 5 s timeout. It must never raise,
  and it logs only the exception type, never the URL: heartbeat URLs carry a secret
  token. The ping goes at the *end* of the try block on purpose - a cycle that raised
  sends no heartbeat, which is what lets the external monitor notice.
