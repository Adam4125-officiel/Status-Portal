## Keeping rules enforceable (`tests/test_conventions.py`)

- **When you add a rule to this file, ask whether it can be a test instead.** Prose in
  a 1200-line file is a request; a failing test is a fact. Anything checkable without
  running the app (a naming rule, a "these three files must agree" rule, an invariant
  expressible over the AST) belongs in `tests/test_conventions.py` *as well as* here —
  the prose explains why, the test makes forgetting it impossible.
- **No false positives, ever.** A check that fires on legitimate code is worse than no
  check at all, because the correct response to it becomes "ignore the convention
  tests", and that generalizes to the ones that matter. Encode real exceptions
  explicitly (see the uploaded-logo case in `test_templates_use_asset_url_for_css_and_js`)
  rather than watering the rule down until it never fires.
- **Failure messages are the interface.** Write them for someone who has *not* read the
  matching section here: say what is wrong, and what to do about it. That's the whole
  point — it delivers the rule at the moment it's being broken, instead of hoping it
  was read 900 lines earlier.
- **Prove a new check can fail.** Temporarily introduce the violation, watch the test
  go red, then revert. A convention test that silently can't fire is worse than none,
  because it reads as coverage.
- Static checks only. Anything needing a running app belongs in `tests/test_app.py`.
- **A `Stop` hook runs these checks automatically** (`.claude/settings.json` ->
  `.claude/check_conventions.sh`), so a violation surfaces at the end of the turn
  without anyone having to remember to run the suite — which is the same failure mode
  as the forgotten rule itself. It is silent when the checks pass. On a failure it
  asks once for a fix, and if it has *already* woken the model once
  (`stop_hook_active`), it downgrades to a message for the human instead of asking
  again — a violation that can't be fixed must never become a loop. To disable it
  temporarily, `/hooks`; to see what it does, read the script, it's 40 lines.

## Testing/verification habits (established over many sessions — keep following them)

- Run the full `pytest` suite *and* a live `python app.py` + `curl` smoke test of
  whatever routes actually changed before calling something done. Several real bugs
  (the integration-blocking page load, the maintenance-window timing bug, the 2FA
  enrollment `KeyError`) were only ever caught by actually running the server, never by
  unit tests alone.
- **For anything that depends on client-side JS (AJAX "load more" buttons, the
  favicon/logo actually rendering, console errors), `curl` alone isn't enough — drive a
  real browser.** A previous version of this file claimed Playwright/Chromium was
  pre-installed and told you not to run `playwright install`; that was wrong as of
  2026-08-19, when a fresh sandbox had neither. **Just install it** (the user has
  standing authorization to install tooling in this sandbox without asking):
  `pip install playwright && python -m playwright install --with-deps chromium`, ~1
  minute. **On the Ubuntu 20.04 codespace that fails** (found 2026-09-24): current
  Playwright no longer supports 20.04, and `--with-deps` trips over an unsigned yarn
  apt repository. `pip install playwright==1.48.0 && python -m playwright install
  chromium` (no `--with-deps`; the system libraries are already there) works. Then drive it with `sync_playwright()`, hooking `console`, `pageerror` and
  `requestfailed` so an error can't pass unnoticed. This caught a real bug:
  `report_problem()` never passed `site_name` to `report.html`, silently leaving the
  topbar brand text and page title blank — every route-level pytest test for that route
  only checked for form-field presence, and `curl` output looked fine since the HTML was
  syntactically valid, just missing content.
- **The suite takes ~150s serially and ~75s with `pytest-xdist`**
  (`pip install pytest-xdist`, then `python -m pytest -q -n 8 -p no:cacheprovider`).
  Every test gets its own `tmp_path` database, so it is safe in parallel; worth it
  when running the whole suite after every commit. A test that monkeypatches module
  state without `isolated_db` doesn't get `_reset_module_state()`, so reset what it
  touches itself.
- **When curl-smoke-testing a multi-request flow that depends on session state** (login
  steps, flash messages, anything the server writes back via `Set-Cookie`), every
  request needs *both* `-b cookiejar` (send) *and* `-c cookiejar` (save the response's
  possibly-updated cookie). `-b` alone silently reads a stale cookie and looks exactly
  like a real bug (a missing flash message, a "lost" session value).
- **Never combine `curl -X POST` with `-L` against an admin route.** `-X` forces the
  method on the redirect too, so curl re-POSTs to the redirect target - which for every
  route here is a page whose form has a *different* CSRF token, giving a 400 that looks
  exactly like the action having failed when it actually succeeded (302) a moment
  earlier. Use `--data` without `-X` (curl then correctly switches to GET on 302), or
  drop `-L` and check the 302 directly.
- **Don't `pkill -f "python app.py"` from a Bash tool call.** `-f` matches full command
  lines, and the shell running the command contains that string, so it kills its own
  session. `kill $(lsof -t -i:5000)` targets the actual listener instead.
- This sandbox is Linux. Hyper-V VM detection, Windows volume labels, CPU/disk
  temperature and per-disk I/O (all Windows-only, PowerShell/CIM-backed), real
  Jellyfin/*Arr/Jellyseerr instances, and a real Discord gateway connection can't be
  fully exercised here. Say so explicitly rather than implying full verification — and
  if the user reports a bug in one of these areas, ask for the actual error text first
  (most of these paths now log real errors instead of swallowing them) rather than
  guessing blind.
- **Check what the test run and the smoke test left in `instance/` before finishing**,
  not just that the tests passed. Two real problems have been found that way and by no
  other means: restore tests writing real safety snapshots into the developer's own
  `instance/db_backups/` (they now monkeypatch `DB_SAFETY_BACKUP_DIR`), and every
  database restore orphaning `-wal`/`-shm` sidecars. `ls instance/` costs nothing.
- Clean up after smoke testing: remove any `instance/portal.db` created during a test
  run, and any cookie jars, before finishing — don't leave a test admin password or
  fake data sitting in what could become the user's real database.
- **Check whether `instance/portal.db` is somebody's real data before a smoke test, and
  if it might be, never point the server at it.** On the owner's machine it can be (a
  dated, 200 KB file next to `secret_key` is not a leftover). Run the real app from a
  small launcher script outside the repo instead: set `db.DB_PATH` to a scratch file
  *before* anything opens it (what `tests/conftest.py` does), call `db.init_db()`, set an
  admin password and any settings you need (2FA: `admin_totp_secret` +
  `admin_totp_enabled`; device API: `device_api_enabled` + `device_api_key`), monkeypatch
  `monitoring.get_cached_vm_snapshot` for fake VMs (this is Linux), set
  `PORTAL_RESOURCE_REFRESH_SECONDS` low to watch an auto-refresh, then
  `app.app.run(port=..., use_reloader=False)`. Two servers on two ports with two scratch
  databases is how the 2FA-on and 2FA-off flows were both driven in one session.
- **Driving a browser here: a scratch virtualenv is enough.** `python3 -m venv <scratch>`,
  `pip install playwright pyotp`, and launch with
  `executable_path=~/.cache/ms-playwright/chromium-*/chrome-linux64/chrome` when the
  browser is already cached and the pip version's own build isn't (the pinned
  `playwright install` is then unnecessary, and the project's `.venv` stays untouched).
  Prove a browser test can fail by running it against the *previous* script/template
  before trusting it - the refresh and group-picker tests were checked that way.
- **Never live-invoke anything that shells out to actually restart/shut down a machine
  (`monitoring.control_host()`), even in this sandbox, even just to "see what
  happens."** Verify exclusively via a mocked `subprocess.run` in pytest.
- **A `/code-review`-style security pass over a session's accumulated diff is worth
  running before a release that adds any new admin-facing control surface** — it caught
  a real XSS bug that manual review and the existing test suite both missed
  (`docs/HISTORY.md` → "VM-name XSS"). Doesn't replace live smoke testing; catches a
  different class of issue.

## Commit cadence — commit per fix, not per session

**Explicitly asked for on 2026-08-20**, after a session landed a whole batch (session
persistence + performance + a new admin control + docs) as one enormous commit.
Don't do that.

**Commit as soon as a single fix or feature is complete and its tests pass** — one
self-contained change per commit, with its own tests and its own doc/CLAUDE.md
updates included in that same commit. A session that fixes three bugs and adds a
button should produce four commits, not one.

"Complete" means the suite passes and the thing works, not that the whole batch is
done. Don't wait for the end of the session, don't wait for the user to confirm the
whole batch, and don't hold everything back for one tidy final commit — the tidy
final commit is the problem, not the goal.

Why this matters more here than the size of the diff suggests:

- **`git bisect` and `git revert` only work at commit granularity.** One 19-file
  commit spanning sessions, performance and a new feature can't be reverted to undo
  just the risky third of it — which, on a project where the user runs the release
  on a real home server and finds regressions by using it, is exactly the operation
  most likely to be needed.
- **The release notes write themselves** from `git log <previous-tag>..HEAD --oneline`
  (see the changelog step below). One giant commit collapses that into a single
  useless line.
- **Review is possible at all.** The PR convention below exists so a risky batch gets
  looked at; a single commit containing everything defeats it.

This does not change the *release* cadence — mid-session pre-releases stay coarse
(one `-rc.N` per completed batch, per the checkpoint rule below). Committing often
and releasing at checkpoints are separate things: commit at every completed fix, cut
a release when a batch is done.

## Ending a session — and only then

**Wrap-up work happens when the user says the session is ending, and not before.**
"This session ends here", "that's it for today", "good session" — an explicit
end, not a "looks good" about one change. Until that point, stay on the work.

When it fires, the wrap-up is all of this, in this order:

1. **Update the markdown.** Rules learned into `CLAUDE.md`, stories and verification
   records into `docs/HISTORY.md`, and `ROADMAP.md` trimmed so anything shipped stops
   reading as outstanding work (one index line, not a write-up — see the file's own
   header for the shape).
2. **Release, if and only if the user said the work is stable.** They will say so.
   Untested work stays at its `-rc.N` prerelease and the branch stays open; that is a
   perfectly good place for a session to end.
3. **Clean up.** Delete every merged and stale branch, remote *and* local — the branch
   list is read as "what is still in flight", so anything left there is a lie about
   the state of the project. Also check `instance/` for what the session's testing
   left behind (stray `portal.db`, `-wal`/`-shm` sidecars, cookie jars).

Two failure modes worth naming, because both have happened: doing the wrap-up
mid-session on a "that works!" that only meant one fix was fine, and finishing a
stable release but leaving the merged branch sitting there.

**A related note on how sessions start.** The opening message often mixes "do this
now" with "write this down for later" — a batch of roadmap ideas and a bug report can
arrive in one paragraph. If which is which isn't explicit, ask before starting rather
than picking; guessing wrong burns the first chunk of the session on the wrong half.
And when the ask is "why does X break", the single most useful thing to ask for is
`instance/logs/app.log` around the failure, not more description — nearly every path
in this app logs a real error now.

## Dependency lock files

- `requirements.in`, `requirements-dev.in` and `requirements-discord.in` hold the
  direct dependencies. The `.txt` files are generated from them and carry hashes; do
  not edit a `.txt` by hand. `requirements.txt` keeps its name (the updater, the
  Dockerfile and the README use it).
- Once one requirement has a hash, pip demands hashes for everything. That is why dev
  and Discord have their own locked files rather than `-r requirements.txt` plus a loose
  `pytest`. The dev file includes the runtime dependencies (`-r requirements.in`,
  constrained by `-c requirements.txt`); the Discord file is an add-on to the runtime.
- Regenerate as a **universal** lock, so one file serves every platform and Python the
  README supports (3.10+). A plain `pip-compile` run on Linux drops everything behind a
  marker: `colorama` on Windows, `typing-extensions`/`async-timeout` on older Pythons,
  `audioop-lts` on 3.13+. With hashes in the file pip then refuses the install on those
  machines, and a self-update fails. Use uv:

  ```
  pip install uv
  for n in requirements requirements-dev requirements-discord; do
    uv pip compile --universal --generate-hashes --python-version 3.10 --no-header $n.in -o $n.txt
  done
  ```

  Add `--upgrade` to move versions. Regenerate before every release and commit the
  `.txt` files together with the `.in` change that caused them. Check that
  `requirements.txt` still has `colorama==... ; sys_platform == 'win32'`.
- Verify in a throwaway venv: `pip install -r requirements-dev.txt -r
  requirements-discord.txt`, then `python -c "import updater;
  updater._install_dependencies(print)"` (the updater runs a plain `pip install -r
  requirements.txt`, which enforces the hashes by itself).

## Release process

**Never commit straight to `main`, for anything — always a branch + PR.** Explicitly
stated by the user 2026-08-30, superseding an earlier, narrower rule that only
required a branch for untested/status-logic-touching batches (small direct fixes
used to go straight to `main`). That distinction is gone: every change, regardless
of size or risk, goes on a branch with a PR, merged the normal way (regular merge
commit — see "Promoting a feature branch to stable" below, never squash/rebase).

**One branch at a time, held open until the work is stable — not a branch and a PR
per fix.** Corrected by the user 2026-09-05, after a session opened three PRs in a
row and merged each as it passed its own tests. The session's work accumulates on a
single branch (still **one commit per completed fix** — that convention is
unchanged, see *Commit cadence*), the branch stays open while the user tests it end
to end on their own portal, and only then is it merged and released stable. A
mid-session pre-release (`-rc.N`) off that branch is the right way to hand something
over for testing; merging to `main` is not.

The exception is a genuine emergency fix the user has asked to go out immediately —
the v1.8.6 notification batch was one, and the user named it as exceptional rather
than the pattern. Don't generalise from it: if the user hasn't said "this is
urgent, ship it", the branch stays open.

**One exception: `CLAUDE.md` and other markdown docs Claude itself maintains for its
own context** (`docs/HISTORY.md`, `ROADMAP.md`). If a branch is already open for
related work, the doc edit rides along on it. **If no branch is open, a doc-only
edit goes straight to `main`** — a branch and a PR for four lines of prose nobody
reviews is ceremony, not safety. (Corrected 2026-09-03: this file previously said
the opposite, that a doc edit with no branch open needed one of its own. It
doesn't.) This does *not* extend to release finalization: the `VERSION` bump to a stable number is not one of these
files, and still lands as a direct commit on `main` as the last step of "Promoting a
feature branch to stable" below, exactly as it always has - confirmed explicitly by
the user rather than assumed, precisely because the phrasing above could have been
read either way.

**Trigger**: the user says the session/work is done *and* that things are working —
e.g. "this session ends here", "that's it for today, everything works", "we're done,
thanks". Not every "looks good" or "great, that works" mid-session — those are just
confirmation of one change. When genuinely unsure whether a message means "wrap up the
whole session" vs. "this one thing is fine, keep going", ask rather than guessing — a
release is a public, visible action.

Once the trigger fires: this is standing, pre-authorized (per the same blanket
authorization covering commit/push/shell commands for this project, granted
2026-07-22) — don't ask for confirmation again each time, just run the steps below.

**Mid-session checkpoint, not just end-of-session**: don't wait only for the full
"session's done, everything works" trigger. Whenever a *complete chunk of requested
work* finishes — a whole batch of asks handed over together, even if more requests come
later in the same session — proactively tell the user specifically what to test and
how, and cut a **pre-release** (`-rc.N`, marked prerelease on GitHub) covering that
chunk. This is deliberately coarser than "after every individual step": a batch of many
related asks worked in one pass is one chunk, not one release per item (confirmed with
the user on a 16-item batch, where the agreed cadence was one pre-release at the end of
the whole batch). The stable-release trigger above still governs promoting a
pre-release or cutting a fresh full release.

**Promoting a feature branch to stable means merging its PR with a regular merge
commit (`gh pr merge N --merge`; the PR is opened as a draft, so `gh pr ready N` comes
first), never squash or rebase.** Every past PR on this
repo merged this way (`git log main --merges` shows it), and it's not a style preference —
squashing would collapse a branch's carefully separated per-fix commits into one,
which defeats the entire point of the commit-cadence convention above (`git bisect`/
`git revert` need the individual commits to still exist on `main`). Do this *before*
bumping `VERSION` to the stable number, since the version bump and tag belong on `main`
after the merge, not on the feature branch.

0. **Bump `VERSION` first — a required step, not a formality.** The `VERSION` file at
   the repo root must contain the exact version being released, *without* the leading
   `v` (`1.5.0`, `1.5.0-rc.1`), committed before the tag is created, so the tagged
   commit — and therefore the `git archive` zip built from it — carries the right
   number. Nothing derives this automatically. Getting it wrong is not cosmetic now
   that self-update exists: `updater.py` compares the running `VERSION` against release
   tags, so a zip shipping a stale value makes an installed portal either believe it's
   already up to date (and refuse a real update) or offer an update it already has.
   Sanity check after tagging: `git show vX.Y.Z:VERSION` must equal `X.Y.Z`.
1. **Versioning**: `vMAJOR.MINOR.PATCH`, with an optional `-rc.N` suffix for anything
   not yet user-verified end-to-end (mark the GitHub release as a pre-release too).
   First tagged release was `v1.0.0` (2026-07-22). Bump MINOR for new features, PATCH
   for bug-fix-only changes, MAJOR only for an actual breaking change (should be rare —
   the whole `_ensure_column` schema policy exists to avoid needing these).
2. **Changelog**: build from `git log <previous-tag>..HEAD --oneline`, grouped
   informally into Added / Fixed / Changed, written as the release body. Keep it
   readable — a person, not a machine, reads this.
3. **Asset**: `git archive --format=zip -o status-portal-vX.Y.Z.zip HEAD` (or the new
   tag once created). `git archive` only includes tracked files, so it's already clean —
   no `.git`, no `instance/portal.db`, no `.env`, no `__pycache__` — with no manual
   exclusion list to maintain. **The zip must always be attached to the release**:
   `updater.py` prefers a `.zip` asset and only falls back to GitHub's auto-generated
   zipball, which publishes neither a size nor a digest, so a release without the asset
   silently downgrades every updater's integrity check to TLS-plus-tag-pinning alone.
4. **Publish**:
   ```
   git tag vX.Y.Z
   git push origin vX.Y.Z
   gh release create vX.Y.Z status-portal-vX.Y.Z.zip --title "vX.Y.Z" --notes "<changelog>" [--prerelease]
   ```
   **If this step fails outright — no `gh` CLI, no direct GitHub API access, or
   `git push origin vX.Y.Z` itself rejected:** this is normal for a session running in
   Anthropic's cloud infrastructure rather than on the user's own machine. Those
   sessions' git credentials are scoped to push *branches* (confirmed: a brand-new
   branch name succeeds) but not to create *tags* (confirmed: `git push origin vX.Y.Z`
   gets a `403` — tags sit outside the allowed ref pattern), and the GitHub MCP toolset
   available there may have no release-creation tool at all (only read tools like
   `list_releases`/`get_latest_release`/`get_tag` — check via `ToolSearch` before
   concluding this). Don't just report failure and stop — fall back to: build the zip
   anyway (`git archive` works locally, no push needed), push a **branch**, and hand
   the user everything they need to finish the last step themselves: exact tag name,
   target commit/branch, title, changelog body, prerelease flag, and the zip itself
   (via `SendUserFile`). A locally-run session usually has full `git`/`gh` access and
   can complete every step directly — this fallback is for when it can't, not a
   replacement for trying the real steps first.

## Continuous integration (`.github/workflows/ci.yml`)

- Push and pull requests run `python -m pytest -q` on Python 3.12. The job installs
  `requirements-discord.txt` as well as `requirements-dev.txt`: several test modules
  import discord.py and fail without it. A clean venv with only `requirements-dev.txt`
  gives 28 failures in `tests/test_discord_bot.py` and similar, so do not drop it.
- A separate `docker` job builds the image, starts it and expects `/` to answer 200
  within about a minute. It is blocking, unlike ruff and pip-audit. If it fails, the
  container logs are printed in the job.
- Ruff and pip-audit are advisory (`continue-on-error`) until their findings are
  cleared. Ruff's default rule set changes between versions, so its count moves too.
- Dependabot checks pip and GitHub Actions weekly. Security reports go through
  GitHub's private reporting, described in `SECURITY.md`.
