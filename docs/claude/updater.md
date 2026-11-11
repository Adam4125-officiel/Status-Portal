## Self-update (`updater.py`, `update.py`, `/admin/about`)

- **`VERSION` (a tracked file at the repo root) is the single source of truth, and
  bumping it is a required step of cutting a release** (see Release process below).
  It has to be a file rather than a literal in `config.py` because `updater.py` reads
  the *incoming* release's version straight out of an extracted zip without importing
  the new, not-yet-installed code. It has to be a tracked file rather than anything
  git-derived because `git archive` strips `.git` — a shipped zip has no git metadata
  at all. `config.IS_GIT_CHECKOUT` (one `os.path.isdir(".git")` at import, no git
  subprocess) distinguishes a working tree from an extracted release;
  `config.VERSION_DISPLAY` appends `+dev` for the former. **`+dev` is a label only —
  never compare against it**, every version comparison uses `config.VERSION`.
- **`updater.py` is the one implementation; `update.py` and the admin route are both
  thin wrappers.** This was an explicit requirement, and it's what stops the CLI and
  the button drifting apart. `updater.py` imports only `config` and `db` — never
  `app.py` (which imports it) and never Flask, so the CLI works on a portal that
  won't start.
- **The repo URL is a module constant and must never become configurable** — not a DB
  setting, not an env var, not a CLI flag. A configurable update source is a "point
  this server at my code" primitive for anyone who can write a setting. There's a test
  asserting the constants and that certificate verification is never disabled; keep it.
- **`browser_download_url` is untrusted input, not a constant** — it arrives over the
  network from the API response, so `_validate_download_url()` checks scheme and host
  against `ALLOWED_DOWNLOAD_HOSTS` both before the request *and* on `response.url`
  after redirects have been followed.
- **Integrity verification protects the transfer, not the publisher.** The size and
  SHA-256 are checked against what the releases API declares, which comes from the
  same origin as the bytes — so this catches truncation/corruption/a mangling proxy
  and (via TLS) an in-transit substitution, but not a malicious release. Real
  publisher authenticity would need a detached signature against a key shipped with
  the app. Say this plainly whenever documenting the feature rather than letting
  "checksum verified" imply more.
- **Which files get replaced comes from the release archive's own member list** — a
  whitelist by construction, and structurally incapable of containing `instance/`,
  `.env` or `static/uploads/` since all three are gitignored and releases are built
  with `git archive`. `PROTECTED_PREFIXES`/`PROTECTED_FILES` are a second, redundant
  check that **aborts the entire update** if one ever shows up, rather than skipping
  that entry — an archive containing one means the release was built wrong, which is
  not a condition to proceed through quietly.
- **Channel = GitHub release channel (stable/unstable), deliberately not a git
  branch.** A branch head has no version identity, so "are you behind", "what am I
  installing" and "roll back to what" all become unanswerable; a branch is also
  arbitrary mid-work code rather than something that was cut and tested. Stable =
  non-prerelease releases only; unstable = prereleases (`-rc.N`) too. The latest is
  picked by **parsed version, not publish date**, so republishing an old release can't
  look like an update. Channel is a DB setting (routine admin toggle); the check
  *interval* is an env var (`PORTAL_UPDATE_CHECK_INTERVAL_SECONDS`) — the standard
  config split, applied.
- **The About page reads a cache and never checks GitHub inline.** Same rule and shape
  as `_integration_status_cache`. `refresh_update_cache_if_stale()` is called from the
  existing health-check loop rather than starting a second thread, and no-ops until
  its own (6h) TTL elapses so a 120s health-check interval doesn't become a GitHub
  call every 120s. "Check now" (`/admin/about/check`) is the sanctioned
  explicit-slow-action exception. A failed check renders as "couldn't check" and
  affects nothing else on the page.
- **`perform_update()` runs synchronously inside the admin route.** That is the same
  sanctioned exception — an explicit one-shot action the admin knowingly triggered —
  not a violation of the no-slow-I/O rule. Don't "fix" it by moving it to a background
  thread; the admin needs the success/failure in the response.
- **What rollback can and cannot do — don't overstate this.** Every failure *before*
  the restart (download, verification, a file that won't replace part way through, a
  failed `pip install`) is rolled back automatically and in-process, and the portal
  keeps running what it already had. The failure *after* the restart cannot be: once
  `os.execv` replaces the process image nothing from the old version exists to detect
  a bad start. `write_pending_marker()` therefore only buys two things — the next
  *successful* start confirms and clears it, and a failed one leaves a record naming
  the exact backup to restore. Genuine automatic post-restart rollback needs a
  supervisor outside the process (systemd + a health check, or a wrapper), which this
  project deliberately doesn't ship because it would change how everyone launches the
  portal.
- **`update.py rollback --emergency` is not a second update implementation and must
  not grow into one** — it only restores an existing backup, reading only the
  `manifest.json` `updater.py` already wrote. Every `update.py` import is lazy for the
  same reason: the recovery tool must still work on a tree the update broke
  (`docs/HISTORY.md` → "`update.py rollback --emergency`").
- **`requirements.txt` is a hash-locked file generated from `requirements.in`** (see
  "Dependency lock files" in `testing-and-workflow.md`); the updater's plain
  `pip install -r` verifies the hashes, so never hand-edit it.
- **`pip install` runs only when `requirements.txt` actually changed, and a failure
  rolls the update back** — restarting into code whose dependencies aren't installed
  just fails to start, and this is the last moment where something is still running
  that can undo it.
- **Windows file locking**: every file is written to a sibling temp file then
  `os.replace()`d (atomic; a crash mid-write leaves the old file intact). The rename
  can still fail on Windows while another process holds the destination open, hence
  `REPLACE_RETRY_ATTEMPTS` with backoff, and a whole-update rollback if it still
  fails. If even the rollback can't write, the error names the backup folder to
  restore by hand rather than claiming it was handled — there's a test for that case.
  **This retry path has never actually fired in the wild** — see the verification
  records in `docs/HISTORY.md` before assuming it's proven.
- **Files a release dropped are removed only through `instance/installed_files.json`**
  **and `RETIRED_FILES`.**
  Each successful update records the files it wrote there (written last, after
  dependencies installed). The next update backs up, then deletes, the files from
  that list the new archive no longer has (`_stale_files`), and only after every new
  file is in place. Rules that must survive any rework:
  - **No list, or an unreadable one, deletes nothing but `RETIRED_FILES`.** Without a
    list a dropped file can't be told from one the admin added; never infer the list
    from what's on disk. An install updated by a version without the list gets a
    first list from the *next* archive, which never names what was dropped earlier -
    so `RETIRED_FILES` names those exact paths and they go through the same filter.
  - **Deleting a file a release shipped → add its path to `RETIRED_FILES`** in the
    same commit. Installs that skip straight from a pre-list version to a later one
    only have that constant to go on. A test checks each entry is absent from the
    repo and not protected.
  - **An entry is re-validated like an archive member and skipped, never acted on,**
    if it is protected (`_is_protected`, case-insensitive), absolute, has `..`, or
    leaves the app root. The worst a bad entry may cause is a stale file left behind.
  - **Paths are compared case-insensitively (`_path_key`) on every platform.** On
    Windows `Foo.py` and `foo.py` are one file; a case-only rename compared exactly
    would delete the file the update just wrote.
  - **A file that can't be deleted (Windows lock) doesn't undo the update.** It stays,
    is kept in the new list, and the next update retries. Leaving it is how every
    update behaved before, not a half-updated tree.
  - **Rollback puts removed files back and restores the previous list** (the
    manifest's `removed` and `installed_before`; `update.py rollback --emergency`
    does both too). A backup made before this existed has no `installed_before`, and
    rollback then leaves the list alone.
- **Nothing in `updater.py` may read a DB setting without going through
  `_read_setting()`.** `sqlite3.connect()` *creates* an empty file for a path that
  doesn't exist, so a bare `db.get_setting()` from the CLI would leave a zero-table
  `instance/portal.db` behind on a fresh install — which `init_db()` then has to cope
  with, and which looks exactly like a corrupted database. `_read_setting()` checks
  `os.path.isfile(db.DB_PATH)` first and falls back to the default. It also swallows
  read errors, because the CLI is the tool you reach for when things are broken, up to
  and including the database.
- **`update.py`'s output must stay pure ASCII.** An em dash raised
  `UnicodeEncodeError` on a Windows console using codepage 437. This is the recovery
  tool — it must not be able to fail on a decorative character. `updater.py`'s
  `progress()` messages are subject to the same rule since the CLI prints them.
  There's a check for this in `tests/test_updater.py`.
- **Backups are pruned to `KEEP_BACKUPS` (5) after each successful update.**
  `_prune_backups()` reads the module constant *inside* the function rather than as a
  default argument value — a default arg binds at def time, which silently ignores a
  monkeypatched constant and made the pruning test pass for the wrong reason until it
  was fixed.
- **The About page's `list_backups()` is a local `os.listdir` + small JSON reads** —
  the same class of call as `asset_url()`'s `getmtime`, not the kind of slow outbound
  I/O the no-blocking-in-a-request-handler rule is about. Don't "fix" it into another
  cache.
- **Release notes come from the same response, not a second request.**
  `fetch_releases()` returns every release on the channel sorted by *parsed version*
  (never publish date - republishing an old release must not reorder the changelog);
  `fetch_latest_release()` is now just its first element. `check_for_update()` carries
  `release_notes` (everything strictly newer than what's running, newest first),
  `release_notes_omitted` and `current_notes` (the running version's own entry) into
  the cache the About page reads.
- **A release `body` is untrusted network input and must only ever be rendered through
  `richtext`.** It escapes first and then permits a deliberately tiny subset (bold,
  links, line breaks), so Markdown headings and bullets show as the literal characters
  the release author typed. That is the accepted trade - never swap in a real Markdown
  renderer here without one that is escaping-safe by construction, and never mark a
  body `|safe`. The CSS deliberately has **no `white-space: pre-wrap`**: `richtext`
  already turns every newline into a `<br>`, so preserving the newlines too would
  double every line break.
- **`MAX_RELEASE_NOTES` (20) caps how many intervening releases are rendered**, with
  the remainder reported as a count rather than silently dropped - a portal left
  un-updated for a long time must not render, or cache, a page of unbounded length.
- **A failed check must still leave the note fields present and empty.** The About
  page reads them unconditionally, so a network failure has to degrade to "couldn't
  check", not to a template error.
- **Changing the channel must clear the update cache** (`admin_about_settings` does).
  Otherwise the page renders a "latest available" that was fetched for the *previous*
  channel right next to the newly-selected one.
- **`_inject_admin_badges()` also exposes `update_available`** for the nav's About
  badge. It reads the cache only (never triggers a check), so a miss or a failed check
  simply means no badge — exactly like having no unread reports.
- **Test-suite gotcha: `config.IS_GIT_CHECKOUT` is genuinely `True` when pytest runs
  from this repo**, so every update route and the About page's button correctly
  refuse. `tests/test_app.py` has an autouse fixture (`_update_test_environment`) that
  patches it to `False` and clears the update cache, so those tests stand in for a
  normal install. Without it a test can "pass" by hitting the git-checkout refusal
  rather than the behavior it meant to assert — if you add an update-route test, make
  sure it's actually reaching the code you think it is.
- **`config.ENABLE_INAPP_UPDATE` is an env var on purpose.** The risk it addresses is
  "someone compromised the admin panel"; a DB toggle that same attacker could flip
  from that same panel would address nothing. Same reasoning as
  `twofactor.RESET_2FA` being a host-level file. It defaults to **enabled** (the button
  was explicitly asked for), and the route checks it, not just the template.

## Servarr version checks (`version_checks.py`, `/admin/integrations`)

- **`version_checks.parse_version()` is deliberately not `updater.parse_version()`, and
  this is the trap.** Servarr versions have **four** numeric components
  (`6.4.1.10545`) and the fourth - the build number - is the one that actually moves
  between releases. `updater.parse_version` keeps three and spends its fourth slot on a
  prerelease rank, so it parses `6.4.0.10540` and `6.4.0.10523` as the *identical*
  tuple and would report a months-old Radarr as up to date. There is a test asserting
  exactly that. The portal's own tags are semver with `-rc.N` and genuinely need the
  other function; these two formats must not share a parser.
- **Which app an integration is comes from the app's own `appName`**, never from the
  name the admin typed. `movies-4k` is still Radarr and something called `radarr`
  might not be; guessing from the label eventually checks the wrong project's releases
  and reports the wrong answer confidently.
- **Two lookup tables, because there are two situations.** `KNOWN_APPS` maps a Servarr
  `appName` to a repo (several apps share the `arr` kind, so the app has to be asked
  what it is). `DIRECT_APPS` maps an integration *kind* straight to a repo and version
  endpoint, for Jellyfin (`/System/Info` → `Version`) and Seerr (`/api/v1/status` →
  `version`), where the kind already settles it.
- **Seerr's repo is `seerr-team/seerr`** — the project was renamed from Jellyseerr. The
  integration kind stays `jellyseerr` because it's stored in every existing database;
  only the label changes.
- **Seerr carries `preview-*` tags with no release attached**, which is exactly the
  "tags that aren't version numbers" hazard below. `/releases/latest` ignores them by
  definition, which is why this uses that endpoint rather than the tag list.
- **`KNOWN_APPS` is a module constant and must stay one** - same argument as
  `updater.py`'s repo constant. Adding an app is a line there *plus* a check that its
  release tags really are plain version numbers: a project tagging releases
  `2024.10-hotfix` parses as `0.0.0` and would silently report "up to date" forever.
- **Results are persisted to the `settings` table, not held in a module cache.** This
  is a daily task, so an in-memory result would mean the admin page saying "not checked
  yet" for up to a day after every restart.
- **A run where *every* app failed is recorded as a failure, not a success** - it
  answered no question at all, and showing green for it hides the most likely cause.
  Per-app results are stored *before* that failure is raised, so the page still
  explains what went wrong instead of going blank. (Found by live-testing against a
  genuinely rate-limited GitHub, not by the unit tests.)
- **GitHub's unauthenticated API allows 60 requests/hour per IP, shared across
  everything this portal asks it** - the self-update check and these three. Fine at
  daily/6-hourly cadence; worth remembering before adding a fourth caller or shortening
  an interval.
