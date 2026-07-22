# Notes for future Claude Code sessions on this repo

This file is for context that survives between sessions — architecture decisions,
gotchas, and standing workflows that aren't obvious from reading the code cold. Keep
it updated as the project evolves; don't let it go stale. See `ROADMAP.md` for open
feature ideas (this file is about *how the existing code works and how to work on
it*, not what's left to build).

## What this is

A personal Flask/SQLite status portal for a home server (Jellyfin, *Arr stack,
Jellyseerr, SMB...), meant to run via plain Python (`python serve_waitress.py` in
production), not Docker-first (Docker support exists as a secondary option). Single
admin, no user accounts. The user manages everything from `/admin` — they're
Python-comfortable but explicitly don't want to need Flask knowledge, so anything
routine (toggles, text, intervals that make sense to change often) belongs in the
DB-backed Settings pages, not a code edit.

## Conventions that matter (don't relitigate these)

- **`db.py` has no ORM, no migration framework.** Schema changes to a table that
  isn't brand-new need a `_ensure_column()` call at the end of `init_db()` — a real
  user database (`instance/portal.db`) has existed since mid-project, and
  `CREATE TABLE IF NOT EXISTS` is a silent no-op on a table that already exists, so a
  forgotten `_ensure_column()` call means every write touching the new column throws
  `sqlite3.OperationalError: no such column` in production. Brand-new tables are fine
  with plain `CREATE TABLE IF NOT EXISTS`.
- **Never call slow/external I/O directly inside a Flask request handler.** This bit
  the project once already: the public page used to call the Jellyfin/*Arr status
  checker synchronously on every load, and an unreachable integration added ~10s to
  every single page load (including every auto-refresh). Fix pattern, now standard:
  background thread polls and writes to a module-level cache dict
  (`app._integration_status_cache`-style); request handlers only ever *read* the
  cache. The one sanctioned exception is an explicit one-shot admin action the user
  knows will be slow (e.g. the "Check now" button) — never something that fires
  automatically or on every page load.
- **Config split**: secrets and things that behave like static deployment config
  (webhook URLs, bot tokens, check intervals) are env vars via `config.py` — nothing
  else should read `os.environ` directly, and changing them needs a restart. Routine
  admin-tunable toggles (site name, visibility checkboxes, command names) are DB
  `settings` rows, editable live from the browser, no restart needed. When adding a
  new setting, decide which bucket it belongs in based on that distinction, not
  convenience.
- **Optional heavy dependencies are never imported at module level.** `nvidia-ml-py`
  (GPU stats) and `discord.py` (bot) both follow this: lazy-imported only inside the
  function that needs them, wrapped so a missing package degrades to "feature
  disabled, log why" rather than crashing the whole app on import. Neither is in
  `requirements.txt`. If you add another optional integration, follow the same shape.
- **This app is meant to survive its own restart cleanly.** State that needs to
  persist across a process restart (maintenance-window progress, the Discord bot's
  tracked live-message id) lives in SQLite, never only in memory.

## Discord bot (`discord_bot.py`) — reworked 2026-07-22, read this before touching it again

- Uses a **slash command** (`discord.app_commands.CommandTree`), not a text/prefix
  command. An earlier version matched a literal `!status` in `on_message` — it
  worked, but needs Discord's privileged "Message Content" intent. Switched to slash
  commands on request + web research confirming this is Discord's own current
  guidance: prefix-command convenience isn't accepted as a reason to request that
  intent, and slash commands don't need any privileged intent at all. Setup no longer
  needs anything toggled in the Discord Developer Portal beyond inviting the bot.
- **`build_status_data(include)` must stay a pure function with zero `discord.py`
  import** — it returns a plain dict of sections. `build_embed(discord_module, data)`
  is the *only* function that touches `discord.Embed`/`discord.Color`, and it takes
  the already-imported module as a parameter rather than importing it itself. This is
  what keeps the module both unit-testable without the optional dependency installed
  and safely importable from `app.py` even when it isn't. Don't collapse these back
  into one discord.py-dependent function.
- Command registration happens in `StatusBot._register_command()` (called from
  `__init__`); syncing happens in `setup_hook()` — order matters, commands must exist
  in the local tree before `tree.sync()` runs. Guild-scoped sync (near-instant) if
  `PORTAL_DISCORD_BOT_GUILD_ID` is set; otherwise global sync (works everywhere, but
  can take up to ~1 hour to first appear).
- The tracked "live" status message (channel_id → message_id) lives in the
  `discord_status_messages` SQLite table, not memory, specifically so a restart keeps
  editing the same message instead of starting a new one. The refresh loop resolves
  the channel via `get_channel()` (cache) falling back to `fetch_channel()` (real API
  call) — the fallback matters because a cold gateway cache right after a restart
  would otherwise look identical to "channel deleted" and wrongly drop the tracked
  row.
- **Access control**: `discordbot_allowed_user_ids` (DB setting, comma/newline
  separated Discord user IDs) restricts who can invoke the slash command — checked
  inside the command callback itself (`allowed_user_ids()` / `normalize_user_ids()`),
  not via Discord's own per-guild command permissions UI, so it's portable across
  whatever server the bot is invited to without extra per-server setup. **Empty list
  = unrestricted (default)** — this was a deliberate choice so the command keeps
  working for anyone who hasn't configured it, at the cost of being open by default;
  the admin page calls this out explicitly rather than silently locking everyone out.
  An unauthorized attempt is logged to the console (with the offending user/id) and
  gets an ephemeral "not authorized" reply, without ever building the (heavier) status
  embed — the point is to stop both spam *and* the wasted work of building a response
  nobody authorized should see.
- **Verification status**: the old prefix-command version was confirmed working
  end-to-end by the user actually running it. The slash-command rewrite has *not*
  been re-confirmed against a real Discord server as of 2026-07-22 — message-building,
  the embed logic, and (unusually thoroughly for this module) the command callback's
  own authorization logic are all unit tested by actually constructing a `StatusBot`
  and invoking its real registered `command.callback(interaction)` with a mocked
  `Interaction` (see `_build_test_client()`/`_make_interaction()` in
  `tests/test_discord_bot.py`) — not just testing the helper functions around it. A
  real (fake-token) connection attempt was also smoke-tested against Discord's actual
  login endpoint to confirm the background thread behaves correctly and fails
  gracefully. What's still unverified: actual slash-command *registration/sync*
  against Discord's API, and the restart-survives-editing behavior, both of which
  need a real bot/server to exercise. Don't assume "the bot" is confirmed working as a
  whole just because an earlier version of it was, or because one code path is now
  well-tested — ask what specifically was tested.
- `discord.py` is installed in this dev sandbox's Python environment for testing
  purposes even though it's not in `requirements.txt` (it's optional). If it's ever
  missing here and you need to verify code again: `pip install discord.py`.

## Testing/verification habits (established over many sessions — keep following them)

- Run the full `pytest` suite *and* a live `python app.py` + `curl` smoke test of
  whatever routes actually changed before calling something done. Several real bugs
  (the integration-blocking page load, the maintenance-window timing bug) were only
  ever caught by actually running the server, never by unit tests alone.
- This sandbox is Linux. Hyper-V VM detection, Windows volume labels, real
  Jellyfin/*Arr/Jellyseerr instances, and a real Discord gateway connection can't be
  fully exercised here. Say so explicitly rather than implying full verification —
  and if the user reports a bug in one of these areas, ask for the actual error text
  first (most of these paths now log real errors instead of swallowing them) rather
  than guessing blind again.
- Clean up after smoke testing: remove any `instance/portal.db` created during a
  test run, and any cookie jars, before finishing — don't leave a test admin password
  or fake data sitting in what could become the user's real database.

## Release process

**Trigger**: the user says the session/work is done *and* that things are working —
e.g. "this session ends here", "that's it for today, everything works", "we're done,
thanks". Not every "looks good" or "great, that works" mid-session — those are just
confirmation of one change, not a signal to release. When genuinely unsure whether a
message means "wrap up the whole session" vs. "this one thing is fine, keep going",
ask rather than guessing — a release is a public, visible action.

Once the trigger fires: this is standing, pre-authorized (per the same blanket
authorization covering commit/push/shell commands for this project, granted
2026-07-22) — don't ask for confirmation again each time, just run the steps below.

1. **Versioning**: `vMAJOR.MINOR.PATCH`, with an optional `-rc.N` suffix for anything
   not yet user-verified end-to-end (mark the GitHub release as a pre-release too).
   First tagged release is `v1.0.0` — the project reached a "working great",
   feature-complete state as of 2026-07-22. Bump MINOR for new features, PATCH for
   bug-fix-only changes, MAJOR only for an actual breaking change (should be rare —
   the whole `_ensure_column` schema policy exists specifically to avoid needing
   these).
2. **Changelog**: build from `git log <previous-tag>..HEAD --oneline`, grouped
   informally into Added / Fixed / Changed, written as the release body. Keep it
   readable — a person, not a machine, reads this.
3. **Asset**: `git archive --format=zip -o status-portal-vX.Y.Z.zip HEAD` (or the new
   tag once created). `git archive` only includes tracked files, so it's already
   clean — no `.git`, no `instance/portal.db`, no `.env`, no `__pycache__` — without
   any manual exclusion list to maintain.
4. **Publish**:
   ```
   git tag vX.Y.Z
   git push origin vX.Y.Z
   gh release create vX.Y.Z status-portal-vX.Y.Z.zip --title "vX.Y.Z" --notes "<changelog>" [--prerelease]
   ```
