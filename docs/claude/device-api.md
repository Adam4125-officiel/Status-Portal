## Display device API (`device_api.py`, `GET /api/device/summary`) — added 2026-10-06, v1.10.0; extended in v1.11.0, v1.11.1 and v1.12.0

A compact JSON summary for **Status-ESP**, the sibling firmware for a 240x240 ESP8266
display (GeekMagic SmallTV-Ultra, ~35 KB of free RAM, plain HTTP only, no TLS). The
shape is defined by `device_api.py`, and `device_api.example_summary()` — a full response
built through the same code, which the admin page renders — is the living contract:
change the shape and the example changes with it.

- **It is its own endpoint, not `/api/status`.** `/api/status` is public, large, and a
  published contract for external dashboards (a test pins its key set), so it cannot be
  shrunk or extended for a device. This one has to stay under 4 KB and is allowed to
  carry what the public pages hide, which is exactly why it is keyed.
- **Auth is a key of its own: `X-Api-Key`, `secrets.compare_digest`, a settings row**
  (`device_api_key`, plus `device_api_enabled`, default off) — the gamesportal
  `/api/notify/*` mechanism, reused, not reinvented. **Never the same secret as
  `notify_api_key`**: that one makes the portal post to Discord and send email, this one
  can only read, and a display on a shelf (key sitting in its flash, sent in clear over
  HTTP on the LAN) should not hold the stronger credential. A test pins that neither
  key opens the other's door. Both keys go through the single `_check_api_key()`; a
  third machine-to-machine key means calling it, not copying the comparison.
- **Off is 404, not 401.** `device_api_enabled()` is "switched on **and** a key exists".
  Disabled keeps the key (re-enabling reconnects the same device) but closes the
  endpoint, and an install that never turned it on answers exactly as one that never had
  it. A wrong or missing key is a JSON 401. The key is accepted **only in the header** —
  a query-string key lands in access logs and proxy logs.
- **The route's first two statements are the gate and the key check, and a convention
  test enforces it** (`test_the_device_api_is_get_only_and_gated_first`): a lookup added
  above them would answer an unauthenticated request. It is also **GET-only by test**.
  That is what makes leaving it out of the CSRF audit a decision rather than an
  oversight: CSRF defends a state change riding on a browser's ambient credentials; this
  reads one document and has no session behind it. A future write route for a device is
  a new decision, in `_csrf_required_for()` and the conventions list, not an extension
  of this one.
- **Resource data is returned regardless of the `show_public_*` switches — on purpose.**
  Those switches decide what *visitors* see; the key decides what the admin's own display
  sees. This is the opposite of the kiosk's rule ("shows nothing a visitor couldn't
  already see"), and the difference is the credential: `/kiosk` is unauthenticated,
  this is not. What must hold, and is tested with every `show_public_*` off, is that the
  numbers are unreachable without the key. The admin page says so in as many words.
- **Bounded by construction, proven against the worst case.** `device_api.py` is pure
  shaping (no Flask, no database; `routes.api.api_device_summary()` gathers rows and hands them
  in). Every list has a fixed cap (`SERVICE_ITEMS`, `INCIDENT_ITEMS`, …) with a count
  beside it for "+N more", and every string a fixed cap in UTF-8 **bytes**, not
  characters (an emoji is four). `test_the_worst_possible_response_is_under_the_hard_cap`
  fills every list with quotes, backslashes, emoji and control characters and asserts
  `< MAX_BYTES` (4096); that case measures 3.5 KB and a realistic response about 1.2-2 KB.
  **Raising a cap means re-running that test, not trusting arithmetic.** Strings are
  one line (whitespace collapsed, non-printables dropped, cut with an ASCII `...`) and
  sent as raw UTF-8, never `\uXXXX` — the device folds to ASCII itself and escapes would
  triple every accented letter.
- **It reuses the portal's decisions instead of re-deriving them**: `compute_overall_status()`
  for the headline (so `slow` ranks where it does everywhere else), `ignore_in_overall_status`
  honoured in the counts *and* the list (a device saying "all up" above a muted service
  would contradict itself), `db.list_active_announcements()` (a scheduled announcement is
  unpublished content), `applied` for "maintenance in progress" (the public page's own
  test), `_request_snapshot()` and `monitoring._severity`'s `ok/warn/crit` passed through as
  `*_sev` so the device colours its bars as the web page does and carries no second copy
  of the thresholds. `STATUS_RANK` is in the status-map convention test (the `slow` rule).
- **Open incidents come from `db.list_incidents(open_only=True)`**, never "newest N, then
  filter": an old, still-open incident sitting behind eight newer resolved ones is the
  incident a status display most needs to show. `open` is a separate `COUNT(*)`.
- **Only requested sections cost anything.** `?sections=` (comma list; absent/blank = all;
  unknown names ignored so a newer firmware keeps working; *only* unknown names is a 400,
  because a header-only answer to a typo reads as a quiet portal). The resource
  snapshot — the most expensive thing a public page does — is not taken unless asked for;
  a test fails if it is. A failing snapshot degrades to `"resources": null`, not a 500.
- **No new cache, no new loop, no slow I/O.** Everything is a database read or an
  existing background cache, same as the public pages. The query count is asserted not to
  grow with the data (grouped queries only).
- **Timestamps are `YYYY-MM-DDTHH:MM:SSZ`, normalised** from the two shapes the database
  holds (offset-bearing ISO for incidents, naive `datetime-local` for maintenance, which
  the form documents as UTC). A response carries `now` so the device can age a timestamp
  against the server's clock without trusting its own before NTP has synced.
- **The admin side is its own page, `/admin/device` ("Display device", System group),
  not a panel on Channels or Settings.** It is neither a notification (Channels is "how
  do I get told") nor one more field of the big Settings form, whose nested-`<form>`
  trap a key-regeneration button would walk straight into. It carries the enable switch,
  the key (shown, regenerable — no grace period, like the notification key), the
  "what to type into Status-ESP" steps and a **rendered example built from
  `device_api.example_summary()`**, never hand-typed. Enabling with no key generates one
  (an on-switch with nothing behind it would 404 while the page said "on"); disabling
  keeps the key so the same device can reconnect. Page `<h1>`, title and nav label all
  read "Display device"; it is registered in `admin_search.PAGES` and
  `ADMIN_PAGES_FOR_MARKUP_CHECK`. The regenerate confirm is a constant string with no
  `{{ }}` in it, so it needs no `_INLINE_HANDLER_EXPRESSIONS_ALLOWED` entry — keep it
  that way.
- **Verified against stand-ins and a real server, not a real device** — see
  `docs/HISTORY.md` → "The display-device API". Status-ESP's firmware side (how it parses
  this on 35 KB of RAM) is that repository's to verify.
- **`services=all` (added in 1.11.0) also lists the operational services**, worst first
  then the admin's order, capped at `ALL_SERVICE_ITEMS` (40), with its own ceiling
  `MAX_BYTES_ALL_SERVICES` (7 KB) — Status-ESP pages through the full list on its screen.
  Without the parameter the response is byte-for-byte what 1.10.0 sent (unhealthy only, 6
  items, 4 KB), so an older firmware is unaffected. Both ceilings are asserted against the
  adversarial worst case in `tests/test_device_api.py`.
- **`resources=all` (1.11.0) sends every disk and the GPUs**, the same
  opt-in shape as `services=all`: up to `ALL_DISK_ITEMS` (8) disks, fullest first, plus
  `gpu_count` and `gpus` (`GPU_ITEMS` = 4; `name`, `pct`, `sev`, `mem_used_gb`,
  `mem_total_gb`, `temp_c`). Without the parameter the section is byte-for-byte what 1.10.0
  sent, with no `gpus` key at all, so an older firmware is never handed more than it sized
  for. Asked for together with `services=all` (which is what Status-ESP does) this is the
  largest answer the endpoint can give; its ceiling is `MAX_BYTES_ALL` (8 KB) and the
  adversarial worst case measures about 7.1 KB, asserted in
  `test_the_largest_possible_answer_fits_its_ceiling`. **Raising a cap means re-running that
  test.** A GPU's `sev` is `monitoring._severity()` of its load, added to the GPU snapshot in
  the same release (a snapshot without it reads `null`, never an error).
- **`services=all` items also carry `ms`, and `jellyfin=1` adds a `jellyfin` object
  (1.11.0).** `ms` is `services.response_ms` (time to
  the response headers of the last check), sent only for `operational`/`slow` services that
  were actually measured and only with `services=all`: a manual service has no number rather
  than a `0` that reads as "instant", and a timeout is not a latency. `jellyfin` is
  `{"transcodes": n, "tasks": [names]}` (3 tasks, 28 bytes each) read from
  `integrations.get_cached_jellyfin_activity()`, the cache the public page already reads, so
  there is still no outbound call in the request. It sits **in the header, next to
  `overall`, not inside a section**: a display shows it above everything (a band over the
  status banner), so it has to arrive whichever sections the device switched on. It is
  opt-in (`jellyfin=1`), like the two `=all` parameters: without it the response is what
  1.10.0 sent. It carries **names only**: the cache has no task progress, so a device cannot
  draw a bar for one (extending the cache is the way, not a second `/ScheduledTasks` call
  here). The largest possible answer (all three parameters) is about 7.7 KB against the 8 KB
  ceiling.
- **Every extension of this endpoint is an opt-in parameter, and the default answer never
  grows.** `services=all`, `resources=all`, `jellyfin=1` and `detail=full` all follow it: a firmware that
  does not send the parameter gets exactly what 1.10.0 sent, so an old Status-ESP is never
  handed a body bigger than the buffer it was sized for. A new field means a new parameter
  (or a new value of an existing one), a ceiling re-measured with the adversarial worst case,
  and a test that the default response does not carry it - `tests/test_device_api.py` has that
  test for each of the three. Only a change to what `device_api.example_summary()` shows needs
  the admin page's description of it updated too (`admin_device.html`).
- **`vms` (1.11.1) is the first *section* added the opt-in way**: `SECTIONS` stays the five
  that an absent/blank `sections=` means, `OPT_IN_SECTIONS = ("vms",)` is accepted only when
  named, and `ALL_SECTIONS` is what the 400 lists as valid. The answer is `{"total", "running",
  "items": [{"name", "state", "up"}]}` (up to `VM_ITEMS` = 10, by name), read from
  `monitoring.get_cached_vm_snapshot()` - the cache the public `/vms` page reads, no PowerShell
  in the request - and, like the resources, **not gated by `show_public_vms`** (the key is the
  gate). `state` is Hyper-V's own word and `up` the portal's short uptime text, meaningless for a
  VM that is not running; no VMs is an empty list, an unreadable cache is `null`. The largest
  possible answer (every section, all three modifiers, `vms`) measures 8.9 KB against
  `MAX_BYTES_ALL_WITH_VMS` = 9 KB, asserted in
  `test_the_largest_possible_answer_with_vms_fits_its_ceiling`; anything not naming `vms` keeps
  the older 8 KB / 4 KB ceilings. **No CPU/RAM per VM**: the portal only knows name, state and
  uptime, and getting more means changing the `Get-VM` query, which cannot be checked on Linux.
- **`detail=full` (1.12.0) is the opt-in answer for a consumer that is a server, not a display.**
  The Jelly Portal Jellyfin plugin (its own repository) shows the portal's status to Jellyfin users
  and always asks exactly `?sections=services,incidents,maintenance,announcements&services=all&detail=full`
  with the `X-Api-Key` header. `device_api.parse_detail()` opts in only for the exact value `full`
  (`strip().lower()`); `detail=`, `detail=bogus`, `detail=ful` and every other value are the default
  answer, like `services=`/`resources=` treat anything but `all`. **Without it the response is byte for
  byte what 1.11.1 sent**: `tests/test_device_api_golden.py` pins the shaping against a golden file
  generated from the unmodified 1.11.1 code (never regenerate it to make a diff go away - a changed
  default is a decision about every installed display), and
  `test_any_detail_value_but_full_is_byte_for_byte_the_default_answer` pins it through the route on a
  seeded database. The route's first two statements are still the gate and the key check; a wrong key
  with `detail=full` gets the same 401 with no data, a disabled API the same 404.
- **`detail=full` changes the site name and four sections, and nothing else.** `device_api.Limits` (a
  `NamedTuple`; `COMPACT` is built from the original constants, `FULL` from the `FULL_*` ones) is the
  one argument the shaping functions take, so the compact path calls `text()` with the very values it
  always did. `FULL`: site name 64 bytes; up to 100 services (with *and* without `services=all`), names
  64 bytes; up to 10 open incidents, title and service string 120 bytes; up to 20 maintenance windows,
  title and service string 120 bytes, description 400 bytes; up to 10 announcements, title 100 bytes,
  text 500 bytes (still one plain line, `**` stripped); 16 names per name list. `resources`, `vms` and
  `jellyfin` are **not** shaped by it and a plugin must never ask for them (they describe the machine,
  and the key is what lets a display see them, not every Jellyfin user); a test asserts they are
  identical with and without the parameter. The ignore rule (`ignore_in_overall_status`) and the
  active-only announcements apply in full mode exactly as before; incidents and windows are, as
  before, not filtered by it.
- **The marker and the new keys.** A full answer carries `"detail":"full"` right after `overall` (before
  `jellyfin`, so the first four keys are what they always were). It exists because an older portal
  ignores an unknown parameter and answers compact, without it: the marker is how a caller tells which
  portal it is talking to. New keys, appended after the existing ones of an item: `incidents.items[]
  .service_names` and `maintenance.items[].service_names` (JSON arrays of full names, `[]` when a row
  covers none) and `maintenance.items[].description` (one line, `""` when empty). The existing
  `services` string stays, with the larger cap. **The arrays come from the `services` lists `db.py`
  already attaches** (`_attach_incident_services`, `_attach_window_services`: grouped queries), so
  full mode runs exactly the statements the default runs
  (`test_detail_full_runs_the_same_statements_as_the_default_and_none_of_them_writes`). Each name is cut
  with the same `text()` and `FULL_SERVICE_NAME_BYTES` as `services.items[].name`, so a name in an
  array compares equal to the same service's name there; the array is the only way to get a name that
  contains a comma or that the old string cut, which is why it exists at all. Empty names are skipped
  before the 16-name cap counts.
- **`detail=full` has its own ceilings, in tens of kilobytes, and they are asserted like the others.**
  `MAX_BYTES_FULL_SECTIONS` (112 KB) is for the plugin's exact request and `MAX_BYTES_FULL` (116 KB)
  for the largest answer the endpoint can ever give (every section, every modifier, `vms` and
  `detail=full`); the adversarial worst cases measure 112,785 and 115,576 bytes
  (`test_the_plugins_worst_possible_request_fits_its_ceiling`,
  `test_the_largest_possible_full_answer_fits_its_ceiling`), where most of it is the maintenance list
  (20 windows x title, string, description and a 16-name array, all made of characters JSON has to
  escape). A realistic answer (30 services, 3 incidents, 5 windows, 3 announcements) is about 4 KB.
  **Raising a `FULL_*` cap means re-running those two tests and moving the ceilings in whole 4 KB
  steps**, not trusting arithmetic. The compact ceilings are untouched. `device_api.example_full_summary()`
  is built through `build_summary()` from canned rows like the other examples and is shown, with the
  plugin's request, on `/admin/device`.
- **Two consumers, one endpoint, two repositories each.** Status-ESP uses the compact answer and never
  sends `detail`; the plugin is the only caller of `detail=full`. A change to the full shape is a change
  in the plugin's repository too, and what that parser does with it is that repository's to verify.
- **Status-ESP lives in its own repository** (`Adam4125-officiel/Status-ESP`; firmware 1.0.0
  paired with portal 1.11.0, and **firmware 1.1.0 - stable 2026-10-08 - with portal 1.11.1**). It asks for all three parameters and degrades by itself against an
  older portal (it just shows less: four disks, no GPU, no latency, no Jellyfin band), which is
  why **a portal release goes out first and the firmware second**, never the other way round.
  The firmware needs 1.10.0 for anything at all, 1.11.0 for everything the 1.0.x screens show,
  and 1.11.1 for the VM screen of firmware 1.1.0 (`sections=...,vms`; an older portal ignores the
  name, and a request naming only `vms` gets its 400, which the device shows). A change to the shape
  of the answer is therefore a change in two repositories, and the firmware's parser is the
  part this repository cannot test. **What has not been seen**: 1.11.0's new fields (GPUs, latency, the
  Jellyfin band) were exercised against stand-ins and a real device on an older portal, never as real
  data on a real screen (`docs/HISTORY.md` -> "1.11.0: what the display asked for next").
  1.11.1's `vms` section has the same caveat in a different place: it was only ever answered from
  a faked VM cache here (this sandbox has no Hyper-V). Firmware 1.1.0 draws it, and keeps its text
  clear of the plastic over the glass (the last ~16 px at the bottom are hidden on the owner's
  SmallTV-Ultra) - that is the firmware's rule, in its CLAUDE.md, not this repository's.
