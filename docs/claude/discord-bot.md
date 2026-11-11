## Discord bot (`discord_bot.py`) — read this before touching it again

- Uses a **slash command** (`discord.app_commands.CommandTree`), not a text/prefix
  command, because a prefix command needs Discord's privileged "Message Content"
  intent and a slash command needs no privileged intent at all
  (`docs/HISTORY.md` → "The prefix-command → slash-command rewrite"). Setup needs
  nothing toggled in the Developer Portal beyond inviting the bot.
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
  would otherwise look identical to "channel deleted" and wrongly drop the tracked row.
- **Three access controls, three different kinds of thing.** All use the same
  comma/newline-separated-IDs shape via `_parse_id_list()`, and all treat **empty as
  unrestricted** (a deliberate default-open choice so the command keeps working for
  anyone who hasn't configured it — the admin page calls this out explicitly):
  - `discordbot_allowed_user_ids` — *refuses the command* for an unlisted user,
    checked inside the command callback itself rather than via Discord's per-guild
    command permissions UI, so it's portable across whatever server the bot is invited
    to. An unauthorized attempt is logged with the offending user/id and gets an
    ephemeral reply, without ever building the (heavier) status embed.
  - `discordbot_channel_whitelist` — *refuses to reply* in an unlisted channel. Never
    makes the bot leave anything; a channel isn't something a bot is a "member" of
    independently of its server.
  - `discordbot_guild_whitelist` — makes the bot **leave** any server not on the list,
    via `StatusBot._enforce_guild_whitelist()`, called from both `on_guild_join` and
    `on_ready` (so editing the list to remove a server it's already in takes effect on
    the next reconnect, not just for future invites). This is stronger than the other
    two on purpose: presence/status updates are visible to a server just by the bot
    being in it, regardless of whether anyone there could run the command.
- **Both slash commands share one authorization gate.** `/snapshot` (a short, one-shot
  plain-text reply — down services plus full detail of every open incident, built by
  `build_snapshot_data()`/`build_snapshot_text()`, never tracked/edited like the main
  `/status` message) and the configurable main command both call
  `StatusBot._check_command_authorized()` (enabled-toggle → channel whitelist → user
  allow-list, in that order). **If you add a third command, call this helper** rather
  than re-inlining the checks. `_register_command()` guards against an admin naming
  their configurable command literally `snapshot` (falls back to `status`), since that
  would collide with the fixed command name at registration time and crash startup.
- **`build_snapshot_text()`'s formatting is a requested fix, not incidental**: full
  per-incident detail (not just a count), a bold title line per incident, the rest as
  a `>` blockquote, and a blank line between separate incidents. Keep both the
  multi-incident separation and the blockquote grouping if you touch it
  (`docs/HISTORY.md` → "`build_snapshot_text()` was too vague").
- **`discord_bot._state["guilds"]`** is a read-only snapshot of every server/channel
  the bot is currently in, populated straight from the gateway cache (`self.guilds`/
  `guild.text_channels` — no extra API calls) by `StatusBot._snapshot_guilds()`, called
  from `on_ready`, `on_guild_join`, `on_guild_remove`, and every `_refresh()` tick.
  Backs the `/admin/discord-bot/guilds` page.
- **`stop()`/`restart()` exist so a running connection can actually be commanded to
  close** — `_run()` manages its own `asyncio` event loop explicitly and stashes
  `client`/`loop`/`thread` in a module-level `_runtime` dict *before* the client starts
  connecting, which is what lets `stop()` call
  `asyncio.run_coroutine_threadsafe(client.close(), loop)` from an admin route's
  thread and then `thread.join(timeout)`. `restart()` is just `stop(); start()`,
  relying on `start()`'s already-running guard so it can't race with the old
  connection still shutting down.
- **`_edit_tracked_status_message()` must keep its two exception branches separate.**
  Only `discord.NotFound` (genuinely gone) or `discord.Forbidden` (access revoked)
  may forget the tracked row; every other exception retries up to
  `REFRESH_RETRY_ATTEMPTS` times (`REFRESH_RETRY_DELAY_SECONDS` apart) and, if still
  failing, is logged and left alone — the row stays tracked and the next
  `refresh_loop` tick tries again. **Don't collapse the branches back into one bare
  `except`**: that distinction *is* the fix for a real bug where any transient network
  blip permanently forgot a perfectly healthy message
  (`docs/HISTORY.md` → "a bare `except` that forgot live messages").
- **`stop()` must always leave `_runtime` in a state `start()` can act on.** `start()`
  begins with `if _runtime["client"] is not None: return`, and only `_run()`'s
  `finally` clears that — so a `close()` that times out on a wedged event loop used
  to leave the bot un-restartable until the whole process was replaced, which is
  exactly the "restarting the bot does nothing" report
  (`docs/HISTORY.md` → "The restart button that did nothing"). `stop()` returns
  whether the connection genuinely ended; a wedged one is abandoned (with
  `loop.stop()` scheduled onto it so it tears itself down when it unblocks) rather
  than left pinned. **`_forget_runtime()`'s identity check is what makes that safe**
  — an abandoned run finishing late must not clear the runtime of the connection
  that replaced it. Don't reintroduce an unconditional `_runtime[...] = None`.
- **Never do synchronous work inside one of the bot's coroutines — hand it to
  `asyncio.to_thread()` via `_off_loop()`.** That loop is what answers Discord's
  heartbeat, and the reads here are not cheap: `build_status_data()` makes a dozen
  SQLite queries and, with any resource toggle on, calls
  `monitoring.get_resource_snapshot()`, which can block on a psutil CPU sample and
  on `disk_usage()` for a sleeping drive. Enough missed heartbeats is a dropped
  session. Every command and the refresh tick gather their reads into one plain
  function (`_refresh_payload()`, `_command_gate()`, …) and run it off the loop; a
  new command follows that shape.
- **`watchdog()` is a last resort and must stay one.** discord.py reconnects on its
  own; the task only acts after `WATCHDOG_GRACE_SECONDS` offline, or when the
  connection thread is gone entirely. It cannot loop, because `start()` stamps
  `_state["disconnected_since"]`, so a restart that doesn't connect begins a fresh
  grace period rather than firing every tick. It is only useful *because* `stop()`
  can now recover — a watchdog over the old code would have been calling a no-op.
- **`start()`/`stop()`/`restart()` are serialised by `_lifecycle_lock` (re-entrant).**
  The watchdog can restart the bot from its own thread at the same moment an admin
  clicks the button, and `start()`'s already-running guard alone doesn't stop two
  callers both passing it before either assigned `_runtime["client"]` — which would
  be two live connections on one token.
- **`get_status()` reports the *thread*, not just the connection.** "Not connected"
  covers discord.py still retrying and nothing left running to retry; those need
  different responses, and `/admin/system` renders them differently.
- **`on_ready`, `on_resumed` and `on_disconnect` are all three load-bearing for
  `_state["connected"]`.** A resumed session fires only `on_resumed()`, never
  `on_ready()` again — without an `on_resumed()` handler the admin panel gets
  permanently stuck showing "not connected" for a bot that is working fine
  (`docs/HISTORY.md` → "the permanently stuck 'not connected' panel"). `on_resumed()`
  deliberately does *not* redo guild-whitelist enforcement or restart `refresh_loop`
  the way `on_ready()` does, since a resume means the session context didn't change.
  If you add another lifecycle-dependent piece of `_state`, remember it's these three
  events that matter, not just the first two. `on_disconnect` stamps
  `disconnected_since` only on the *first* drop of a run — it fires on every retry
  while discord.py reconnects, and resetting the clock each time would mean a bot
  stuck in a reconnect loop never looked overdue to `watchdog()`.
- **What is and isn't verified against a real Discord server**: `/snapshot` (and
  therefore slash-command registration and the command handler) is confirmed working
  live, as are the v1.8.3 restart fix and the watchdog (user-confirmed end to end,
  2026-09-03) — but **not** the heartbeat-starvation theory for *why* the bot was
  dropping, which stays an open suspicion with the log lines that would confirm it
  recorded in `ROADMAP.md` → "Known issues to investigate". Still unconfirmed for real: the guild whitelist's actual `guild.leave()` call,
  the server/channel management page's gateway-cache snapshot, and the
  restart-survives-message-editing behavior for the tracked `/status` message — all
  unit-tested with mocked Discord objects only. Don't assume "the bot" is confirmed
  working as a whole because one code path is; ask what specifically was tested.
- `discord.py` is installed in this dev sandbox's Python environment for testing even
  though it's not in `requirements.txt`. If it's missing and you need to verify code:
  `pip install discord.py` (23 tests fail with `ModuleNotFoundError` without it).

## Discord DMs and Seerr approval alerts (`seerr_alerts.py`, `/admin/notifications/seerr`)

- **`seerr_alerts.py` exists because of the import graph, not by preference.** It needs
  `integrations` (to ask Seerr) and `discord_bot` (to deliver), and `discord_bot`
  already imports `integrations` — so it can't live in either without a cycle. A thin
  layer above both is the shape that works, and it's where the next "watch something,
  tell someone" job belongs too.
- **`discord_bot.send_dm()` is a different code path from everything else the bot
  sends.** Replies and message edits happen *inside* the bot's event loop; a DM is
  initiated from a scheduled task's thread, so it uses the same
  `asyncio.run_coroutine_threadsafe` bridge `stop()` does. `get_user()` then
  `fetch_user()`, cache-then-fetch, for the same reason `_edit_tracked_status_message()`
  does it: a cold gateway cache right after a restart otherwise looks identical to "no
  such user".
- **`dm_user_ids` is default-*closed*, unlike the bot's other three ID lists.** Those
  decide who may *ask* the bot for something already visible in the channel they asked
  from; this decides who it messages unprompted. Empty means nobody.
- **A bot can only DM someone who shares a server with it and allows DMs from server
  members.** This is Discord's rule and cannot be worked around. A valid user ID can be
  undeliverable; `send_dm()` reports that case explicitly (`Forbidden`) instead of as a
  generic failure, because the fix is a human action nobody would guess otherwise. Say
  this plainly in any UI that collects DM recipients.
- **`fetch_seerr_pending()`'s title resolution must go through the same
  `_resolved_seerr_media()` helper `fetch_seerr_requests()` uses.** It didn't, once —
  `fetch_seerr_pending()` built its title with `_seerr_title()` alone, no fallback for
  a bare `"TMDB #12345"` placeholder, which is exactly why the Discord approval DM (built
  from this list) used to read "TMDB #11279 (tv) for Pakuo" instead of a real title.
  Both request-list functions in `integrations.py` must keep sharing this helper rather
  than drifting back into two independent implementations.
- **The alert is edge-triggered and its state is persisted** (`seerr_notified_request_ids`
  in `settings`, capped at `MAX_REMEMBERED_IDS`). Same rule as the low-disk alert: a
  restart while requests are still pending must not re-announce all of them.
- **A request is only marked announced once a DM actually reached somebody.** A
  disconnected bot therefore means a *delayed* alert, not a swallowed one.
- **While DMs are off, what's pending is still remembered** — so switching them on
  announces what arrives next rather than emptying the whole existing queue into
  someone's inbox at once.
- **A failed poll must leave the stored count alone.** Overwriting a real count with 0
  would quietly tell the admin their approval queue was empty.
- **The count is admin-only, deliberately.** It's operational information about a
  queue, not a signal about whether anything is working, so it never reaches the public
  page.
- **The Seerr event lifecycle (approved/declined/issues) is polling, not a webhook
  receiver — a deliberate choice, not a placeholder for one.** Extending the existing
  `seerr_approvals` task (asking the same poll more questions) was preferred over a
  public `/hooks/seerr` POST route: no new internet-reachable endpoint, no setup steps
  inside Seerr, and it can't silently go quiet just because Seerr can't reach the
  portal. The cost is up to one task interval of delay and that only state *changes*
  are seen, not e.g. an issue comment that gets edited between polls — accepted
  trade-offs, not bugs.
- **`track_request_progress()` also tracks approved/declined, not just "available".**
  The persisted per-request state (`seerr_request_media_states`) changed shape from a
  bare `media_status` int to `{"media_status": ..., "request_status": ...}` — read
  defensively (`_previous_state()`), since a stored int from before this change must
  not crash the next poll. A `pending -> approved`/`pending -> declined` transition
  (via `fetch_seerr_requests()`'s already-fetched `request_status_key`) queues a
  `"seerr_event"` notification to the requester, same `user_id_for_seerr_user()`
  resolution the arrival case already used.
- **Issues are tracked the same edge-triggered/persisted-state way**
  (`track_issue_updates()`, `integrations.fetch_seerr_issues()`, settings key
  `seerr_issue_states`). A new or changed issue DMs the admin(s) via the existing
  `discord_bot.broadcast_dm()` + `dm_enabled()` path (admin-directed, so per-user
  preferences don't apply); an *update* (not a brand-new issue — the reporter already
  knows they just filed it) also notifies whoever opened it via `"seerr_event"`.
  **Unverified beyond the published spec**: Seerr's OpenAPI spec omits a `status` field
  from the `Issue` schema entirely (not just under-documented — genuinely absent), so
  `open`/`resolved` is inferred (1/2) by analogy with every other Seerr status enum in
  `integrations.py`, not read from the spec like the rest of this file. An issue's
  embedded media also carries no `mediaType`, unlike a request's — title resolution
  tries `movie` then `tv` (cached either way, so a wrong first guess costs one extra
  call, not one per poll). Confirm against a real instance before trusting either
  assumption fully.
