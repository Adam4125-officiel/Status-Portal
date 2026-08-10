"""
discord_bot.py — optional Discord bot integration (separate from the simple Discord
webhook in notifications.py, which only ever sends one-way messages). This one is a
real bot connection: it can update its own presence/status, and/or respond to a
`/status`-style slash command in any channel by posting a status summary and then
continuously editing that same message on a timer instead of spamming new ones.

Uses application (slash) commands, not a text prefix command - Discord's own
guidance is that reading plain message text for commands requires the privileged
"Message Content" intent, which is unnecessary friction for something a slash command
does natively without it, and prefix commands aren't a valid justification for that
intent if a bot is ever large enough to need approval for it. Slash commands need no
privileged intent at all.

Fully optional and lazily loaded - this module has NO hard dependency on the
`discord.py` package. If PORTAL_DISCORD_BOT_TOKEN is unset, or discord.py isn't
installed, start() is a clean no-op and logs why. Nothing else in this app is
affected either way - app.py imports this module unconditionally, so it must never
raise merely from being imported.
"""
import asyncio
import logging
import re
import threading

import config
import db
import integrations
import monitoring

_logger = logging.getLogger(__name__)

_state = {"connected": False, "user": None, "last_error": None, "guilds": []}

# The running bot's client/event-loop/thread, if any - set by start(), cleared by
# stop(). Unlike _state above (a read-only snapshot for the admin page), this is
# what actually lets stop()/restart() below command the running connection to shut
# down from a different thread (the request-handling thread an admin's "restart"
# click runs on), something the original fire-and-forget start() had no way to do.
_runtime = {"client": None, "loop": None, "thread": None}

STATUS_ICON = {"operational": "🟢", "slow": "🐢", "degraded": "🟡", "maintenance": "🟣", "down": "🔴"}
STATUS_LABEL = {"operational": "Operational", "slow": "Slow", "degraded": "Degraded",
                "maintenance": "Maintenance", "down": "Down"}

# Phrasing for the bot's own presence/activity text and embed summary specifically -
# "Operational" reads like a status-page label, not something you'd say out loud.
PRESENCE_TEXT = {
    "operational": "✅ All services up!",
    "slow": "🐢 Some services are responding slowly",
    "degraded": "⚠️ Some services are degraded",
    "down": "🔴 Some services are down",
    "maintenance": "🛠 Maintenance in progress",
}
_EMBED_COLOR_NAME = {"operational": "green", "slow": "orange", "degraded": "gold",
                      "down": "red", "maintenance": "purple"}

RESOURCE_KEYS = ("cpu", "memory", "disks", "network", "gpu", "vms")
_RESOURCE_DEFAULTS = {key: "0" for key in RESOURCE_KEYS}  # all off by default, admin opts in per-item

INCLUDE_KEYS = ("services", "incidents", "announcements", "maintenance", "highload")
_INCLUDE_DEFAULTS = {"services": "1", "incidents": "1", "announcements": "1", "maintenance": "1",
                     "highload": "0"}  # opt-in, like resources - costs a live resource snapshot


def get_status():
    """Read-only snapshot for the admin page - never raises, reflects whatever the
    bot thread has last reported via the module-level _state dict."""
    return dict(_state)


def include_settings():
    include = {key: db.get_setting(f"discordbot_include_{key}", _INCLUDE_DEFAULTS[key]) == "1"
               for key in INCLUDE_KEYS}
    include["resources"] = {key: db.get_setting(f"discordbot_resource_{key}", _RESOURCE_DEFAULTS[key]) == "1"
                             for key in RESOURCE_KEYS}
    return include


def _parse_id_list(raw):
    """Shared parsing for every comma/newline-separated Discord-ID list setting in
    this module (allowed user IDs, allowed guild/server IDs)."""
    return [part.strip() for part in (raw or "").replace("\n", ",").split(",") if part.strip()]


def allowed_user_ids():
    """Discord user IDs (as strings) permitted to invoke the slash command - parsed
    from a comma/newline-separated DB setting. An empty result means unrestricted
    (anyone in the server can use it) - this is the default, so the command keeps
    working for anyone who hasn't set this up, but it's called out clearly in the
    admin UI since leaving it unrestricted is exactly what allows the spam/abuse this
    setting exists to prevent."""
    return set(_parse_id_list(db.get_setting("discordbot_allowed_user_ids", "")))


def normalize_user_ids(raw):
    """Cleans up admin-entered input (mixed commas/newlines/whitespace, digits only
    per ID) into a canonical comma-separated string for storage/redisplay."""
    return ", ".join(_parse_id_list(raw))


def allowed_guild_ids():
    """Discord server (guild) IDs (as strings) this bot is allowed to remain in -
    parsed the same way as allowed_user_ids(). An empty result means unrestricted
    (the bot stays in any server it's invited to) - same default-open rationale as
    the user allowlist, called out in the admin UI."""
    return set(_parse_id_list(db.get_setting("discordbot_guild_whitelist", "")))


def normalize_guild_ids(raw):
    return ", ".join(_parse_id_list(raw))


def allowed_channel_ids():
    """Discord channel IDs (as strings) the slash commands are allowed to respond in
    - parsed the same way as allowed_user_ids()/allowed_guild_ids(). An empty result
    means unrestricted (any channel the bot can see), same default-open convention.
    Unlike the guild whitelist, this only refuses the command reply - it has no
    "leave" behavior, since a channel isn't something the bot can be a member of
    independently of its server."""
    return set(_parse_id_list(db.get_setting("discordbot_channel_whitelist", "")))


def normalize_channel_ids(raw):
    return ", ".join(_parse_id_list(raw))


def _overall_status(services):
    """Mirrors app.compute_overall_status() - a service with ignore_in_overall_status
    set is excluded from this aggregate the same way, so the two never disagree."""
    services = [s for s in services if not s.get("ignore_in_overall_status")]
    statuses = [s["status"] for s in services]
    if "down" in statuses:
        return "down"
    if "degraded" in statuses:
        return "degraded"
    if "maintenance" in statuses:
        return "maintenance"
    if "slow" in statuses:
        return "slow"
    return "operational"


def sanitize_command_name(raw):
    """Discord slash command names must be 1-32 chars, lowercase, and effectively
    only letters/digits/-/_ . Also strips a leading '!' or '/' in case an old
    prefix-command-style value is still stored from before this became a slash
    command. Falls back to 'status' if nothing usable is left."""
    name = (raw or "").strip().lstrip("!/").lower()
    name = re.sub(r"[^a-z0-9_-]", "", name)
    return name[:32] or "status"


def _resource_lines(toggles):
    if not any(toggles.values()):
        return []
    try:
        snap = monitoring.get_resource_snapshot()
    except Exception:
        return []
    lines = []
    if toggles.get("cpu"):
        cpu_line = f"CPU: {snap['cpu_percent']}%"
        if snap.get("cpu_temp_c") is not None:
            cpu_line += f", {snap['cpu_temp_c']}°C"
        lines.append(cpu_line)
    if toggles.get("memory"):
        lines.append(f"RAM: {snap['mem_used_gb']}/{snap['mem_total_gb']} GB ({snap['mem_percent']}%)")
    if toggles.get("disks"):
        for d in snap["disks"]:
            line = f"💽 {d['display_name']}: {d['percent']}% used, {d['free_gb']} GB free"
            if d.get("temp_c") is not None:
                line += f", {d['temp_c']}°C"
            if d.get("io"):
                line += f" (Read: {d['io']['read_mb_s']} Write: {d['io']['write_mb_s']} MB/s)"
            lines.append(line)
    if toggles.get("network") and snap["network"]:
        lines.append(f"Network: Down {snap['network']['down_mb_s']} MB/s, Up {snap['network']['up_mb_s']} MB/s")
    if toggles.get("gpu"):
        for g in snap["gpus"]:
            line = f"🎮 {g['name']}: {g['util_percent']}%, {g['mem_used_gb']}/{g['mem_total_gb']} GB"
            if g.get("temp_c") is not None:
                line += f", {g['temp_c']}°C"
            lines.append(line)
    if toggles.get("vms"):
        for vm in monitoring.get_cached_vm_snapshot():
            lines.append(f"🖥 {vm['name']}: {vm['state']}")
    return lines


def build_status_data(include):
    """Pure and side-effect free (no discord.py dependency, no network calls beyond
    the local psutil/PowerShell reads already used elsewhere) so it's cheaply
    testable on its own - given the same DB/system state, always builds the same
    data, regardless of how it's ultimately rendered."""
    site_name = db.get_setting("site_name", "Server")
    services = db.list_services()
    overall = _overall_status(services)
    sections = []

    if include.get("services") and services:
        lines = []
        for s in services:
            line = (f"{STATUS_ICON.get(s['status'], '⚪')} **{s['name']}** — "
                    f"{STATUS_LABEL.get(s['status'], s['status'])}")
            all_links = ([("Open", s["url"])] if s.get("url") else []) + \
                [(l["label"], l["url"]) for l in db.list_service_links(s["id"])]
            if all_links:
                line += " (" + ", ".join(f"[{label}]({url})" for label, url in all_links) + ")"
            lines.append(line)
        sections.append(("Services", lines))

    if include.get("maintenance"):
        windows = db.list_public_maintenance_windows()
        if windows:
            lines = []
            for w in windows:
                state = "in progress" if w["applied"] else "scheduled"
                lines.append(f"🛠 **{w['service_names']}**: {w['title']} ({state}, "
                              f"{w['starts_at'][:16]} → {w['ends_at'][:16]} UTC)")
            sections.append(("Scheduled maintenance", lines))

    if include.get("incidents"):
        # Any still-open incident, uncapped and separate from the "recent" list below
        # - a long-running incident shouldn't silently scroll out of the summary just
        # because newer (already-resolved) incidents pushed it past a 5-item cap.
        open_incidents = [i for i in db.list_incidents(limit=20) if i["status"] != "resolved"]
        if open_incidents:
            lines = [f"🚨 [{i['status']}] {i['title']}" for i in open_incidents]
            sections.append(("Active incident(s)", lines))
        # Resolved only, and filtered before capping to 5 - an incident moves here
        # once it's resolved, not while it's still open (it's already shown above,
        # in "Active incident(s)"); filtering after a limit=5 fetch could let
        # currently-open incidents crowd out older resolved ones from this list.
        incidents = [i for i in db.list_incidents(limit=20) if i["status"] == "resolved"][:5]
        if incidents:
            lines = [f"• [{i['status']}] {i['title']}" for i in incidents]
            sections.append(("Recent incidents", lines))

    if include.get("announcements"):
        announcements = db.list_announcements(limit=3)
        if announcements:
            lines = [f"📣 **{a['title']}**: {a['message']}" for a in announcements]
            sections.append(("Announcements", lines))

    resource_lines = _resource_lines(include.get("resources") or {})
    if resource_lines:
        sections.append(("Resources", resource_lines))

    if include.get("highload"):
        try:
            high_load = integrations.evaluate_high_load(monitoring.get_resource_snapshot())
        except Exception:
            high_load = {"active": False, "reasons": []}
        if high_load["active"]:
            sections.append(("High load", [f"⚠ {r}" for r in high_load["reasons"]]))

    return {"site_name": site_name, "overall": overall, "sections": sections}


def build_snapshot_data():
    """Pure/side-effect-free like build_status_data(), but deliberately minimal - just
    enough for a short, instantaneous reply: which services are currently down, and
    the full detail (title, description, status, service(s), started-at, and every
    update posted so far) of each currently open incident, plus whether any
    maintenance window is in progress. Unlike /status, this is a one-shot reply,
    never tracked/edited afterward."""
    down = [s["name"] for s in db.list_services() if s["status"] == "down"]
    open_incidents = [i for i in db.list_incidents(limit=20) if i["status"] != "resolved"]
    incidents = [
        {
            "title": i["title"],
            "description": i["description"],
            "status": i["status"],
            "service_names": i.get("service_names") or "",
            "started_at": i["started_at"],
            "updates": db.list_incident_updates(i["id"]),
        }
        for i in open_incidents
    ]
    # list_public_maintenance_windows() already excludes ended ones, but a window
    # that's merely scheduled (not yet applied) isn't happening right now.
    active_maintenance = [w for w in db.list_public_maintenance_windows() if w["applied"]]
    return {"down": down, "incidents": incidents, "maintenance_count": len(active_maintenance)}


def build_snapshot_text(data):
    """Uses Discord's own markdown (bold title line, blockquote for everything
    underneath it) to give each incident the same visual hierarchy as the public
    page's incident cards - consecutive '>' lines render as one continuous quoted
    block with a left bar in every Discord client, so an incident's detail reads
    as clearly nested under its own title instead of blending into a flat wall of
    text. A blank line between incidents keeps multiple open ones from running
    together."""
    if not data["down"] and not data["incidents"] and not data["maintenance_count"]:
        return "✅ All services up. No open incidents or maintenance."
    parts = []
    if data["down"]:
        parts.append("🔴 **Down:** " + ", ".join(data["down"]))
        parts.append("")
    for i in data["incidents"]:
        title_line = f"🚨 **{i['title']}** — [{i['status']}]"
        if i["service_names"]:
            title_line += f" · {i['service_names']}"
        parts.append(title_line)
        parts.append(f"> Started {i['started_at'][:16].replace('T', ' ')} UTC")
        if i["description"]:
            parts.append(f"> {i['description']}")
        for u in i["updates"]:
            parts.append(f"> [{u['status']}] {u['message']} — {u['created_at'][:16].replace('T', ' ')} UTC")
        parts.append("")
    if data["maintenance_count"]:
        parts.append(f"🛠 {data['maintenance_count']} maintenance window(s) in progress")
    return _truncate("\n".join(parts).strip(), 1900)


def _truncate(text, limit):
    if len(text) <= limit:
        return text
    return text[:limit - 1].rstrip() + "…"


def build_embed(discord_module, data):
    """The only function in this module that touches discord.py types directly -
    takes the already-imported module as a parameter (see _make_client_class)
    rather than importing it itself, so the rest of this file stays free of a hard
    dependency on the optional package."""
    color = getattr(discord_module.Color, _EMBED_COLOR_NAME.get(data["overall"], "green"))()
    embed = discord_module.Embed(
        title=f"{data['site_name']} Status",
        description=PRESENCE_TEXT.get(data["overall"], data["overall"]),
        color=color,
    )
    for heading, lines in data["sections"]:
        embed.add_field(name=heading, value=_truncate("\n".join(lines), 1024), inline=False)
    if not data["sections"]:
        embed.add_field(name="Nothing to show", value="Check what's included in /admin/discord-bot.", inline=False)
    embed.set_footer(text="Last updated")
    embed.timestamp = discord_module.utils.utcnow()
    return embed


def _try_import_discord():
    try:
        import discord
        from discord import app_commands
        from discord.ext import tasks
        return discord, app_commands, tasks
    except ImportError:
        return None, None, None


def _make_client_class(discord, app_commands, tasks):
    """Built lazily, only once discord.py is confirmed importable."""

    class StatusBot(discord.Client):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.tree = app_commands.CommandTree(self)
            self.refresh_loop = tasks.loop(seconds=config.DISCORD_BOT_REFRESH_SECONDS)(self._refresh)
            self._register_command()

        async def _check_command_authorized(self, interaction, command_name):
            """Shared authorization gate for every slash command this bot registers
            (currently /<command_name> and /snapshot) - one place for the
            enabled-toggle/user-allowlist/channel-whitelist checks rather than
            duplicated per command. Sends the appropriate ephemeral reply itself and
            returns False if not authorized; returns True (sending nothing) if the
            command should proceed."""
            if db.get_setting("discordbot_channel_command_enabled", "0") != "1":
                await interaction.response.send_message(
                    "This command is currently disabled in /admin/discord-bot.", ephemeral=True)
                return False
            allowed_channels = allowed_channel_ids()
            if allowed_channels and str(interaction.channel_id) not in allowed_channels:
                _logger.warning("rejected /%s in unauthorized channel %s", command_name,
                                interaction.channel_id)
                await interaction.response.send_message(
                    "This command isn't allowed in this channel.", ephemeral=True)
                return False
            allowed = allowed_user_ids()
            if allowed and str(interaction.user.id) not in allowed:
                _logger.warning("rejected /%s from unauthorized user %s (%s)",
                                command_name, interaction.user, interaction.user.id)
                await interaction.response.send_message(
                    "You're not authorized to use this command.", ephemeral=True)
                return False
            return True

        def _register_command(self):
            command_name = sanitize_command_name(db.get_setting("discordbot_command_name", "status"))
            if command_name == "snapshot":
                # "snapshot" is reserved for the fixed second command below - an admin
                # who typed it as their custom main-command name would otherwise crash
                # registration with a duplicate-command error.
                command_name = "status"

            @self.tree.command(name=command_name, description="Show the current status page summary")
            async def status_command(interaction):
                if not await self._check_command_authorized(interaction, command_name):
                    return
                embed = build_embed(discord, build_status_data(include_settings()))
                await interaction.response.send_message(embed=embed)
                sent = await interaction.original_response()
                db.set_discord_status_message(interaction.channel_id, sent.id)

            @self.tree.command(name="snapshot",
                                description="Quick snapshot: down services and open incidents/maintenance")
            async def snapshot_command(interaction):
                if not await self._check_command_authorized(interaction, "snapshot"):
                    return
                await interaction.response.send_message(build_snapshot_text(build_snapshot_data()))

        async def setup_hook(self):
            # Command registration is only picked up on (re)connect - changing the
            # command name or adding it later requires an app restart, same as any
            # other startup-time config in this app.
            guild_id = config.DISCORD_BOT_GUILD_ID
            if guild_id:
                guild = discord.Object(id=int(guild_id))
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                _logger.info("slash command synced to guild %s", guild_id)
            else:
                await self.tree.sync()
                _logger.info("slash command synced globally (can take up to an hour to first "
                             "appear - set PORTAL_DISCORD_BOT_GUILD_ID for instant registration "
                             "on a single server)")

        async def _enforce_guild_whitelist(self, guild):
            """Leaves `guild` immediately if a server whitelist is configured and
            this guild isn't on it - the whitelist is a security control, so it's
            enforced by actually leaving, not just by refusing the slash command
            (an unwanted server could otherwise still see the bot's presence/status
            updates). An empty whitelist means unrestricted, same default-open
            convention as the user allowlist. Returns True if it left."""
            whitelist = allowed_guild_ids()
            if whitelist and str(guild.id) not in whitelist:
                _logger.info("leaving server '%s' (%s) - not in the configured server whitelist",
                             guild.name, guild.id)
                try:
                    await guild.leave()
                except Exception:
                    _logger.exception("failed to leave server %s", guild.id)
                return True
            return False

        def _snapshot_guilds(self):
            """Read-only snapshot of every server/channel the bot is currently in,
            straight from the gateway cache (self.guilds/guild.text_channels) - no
            extra API calls, so this is cheap enough to call on every lifecycle event
            and each _refresh() tick. Populates _state["guilds"] for the admin
            "manage servers" page to read, same read-the-cache pattern get_status()
            already uses for connection state."""
            _state["guilds"] = [
                {"id": str(guild.id), "name": guild.name,
                 "channels": [{"id": str(c.id), "name": c.name} for c in guild.text_channels]}
                for guild in self.guilds
            ]

        async def on_guild_join(self, guild):
            # Catches the whitelist at the moment the bot is invited to a new
            # server - the earliest point it can act, before anyone there gets a
            # chance to use the slash command.
            await self._enforce_guild_whitelist(guild)
            self._snapshot_guilds()

        async def on_guild_remove(self, guild):
            # Fires whether the bot left on its own (the whitelist above) or was
            # kicked/the server deleted the bot's access - either way, the admin page
            # shouldn't keep showing a server the bot is no longer actually in.
            self._snapshot_guilds()

        async def on_ready(self):
            _state["connected"] = True
            _state["user"] = str(self.user)
            _state["last_error"] = None
            _logger.info("connected as %s", self.user)
            # Also re-checked on every (re)connect, not just on_guild_join - covers
            # a server the bot was already in before the whitelist was configured
            # or edited, so tightening it later actually takes effect.
            for guild in list(self.guilds):
                await self._enforce_guild_whitelist(guild)
            self._snapshot_guilds()
            if not self.refresh_loop.is_running():
                self.refresh_loop.start()

        async def on_disconnect(self):
            _state["connected"] = False

        async def _fetch_channel(self, channel_id):
            # get_channel() is a cache lookup only - right after a restart the cache
            # may not be warm yet even though the channel is perfectly reachable, so
            # fetch_channel() (a real API call) is tried before giving up on it. Only
            # an actual failure here (channel deleted, access revoked) should drop
            # the tracked message - not a cold cache.
            return self.get_channel(channel_id) or await self.fetch_channel(channel_id)

        async def _refresh(self):
            try:
                self._snapshot_guilds()
                data = build_status_data(include_settings())
                if db.get_setting("discordbot_update_presence", "0") == "1":
                    await self.change_presence(activity=discord.Activity(
                        type=discord.ActivityType.watching,
                        name=PRESENCE_TEXT.get(data["overall"], data["overall"])))
                if db.get_setting("discordbot_channel_command_enabled", "0") == "1":
                    embed = build_embed(discord, data)
                    for row in db.list_discord_status_messages():
                        try:
                            channel = await self._fetch_channel(int(row["channel_id"]))
                            msg = await channel.fetch_message(int(row["message_id"]))
                            await msg.edit(embed=embed)
                        except Exception:
                            db.delete_discord_status_message(row["channel_id"])
            except Exception:
                _logger.exception("refresh loop error")

    return StatusBot


def start():
    """Starts the bot in a background daemon thread, if configured. Safe no-op
    otherwise - called unconditionally from app.py/serve_waitress.py, same as
    start_background_checker(). Also a no-op if the bot is already running (e.g. a
    stray extra call) rather than starting a second concurrent connection -
    restart() below relies on this by calling stop() first, which clears _runtime
    back to its "not running" state.

    Manages its own asyncio event loop explicitly (loop.run_until_complete(...))
    rather than the simpler client.run(...) convenience wrapper, specifically so
    the loop is captured in _runtime *before* the client starts connecting - that
    reference is what lets stop() below schedule client.close() onto this exact
    loop from a different thread."""
    if _runtime["client"] is not None:
        return
    if not config.DISCORD_BOT_TOKEN:
        return
    discord, app_commands, tasks = _try_import_discord()
    if discord is None:
        _logger.warning("PORTAL_DISCORD_BOT_TOKEN is set but discord.py isn't installed - "
                        "run `pip install discord.py` to enable this optional feature.")
        return

    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        intents = discord.Intents.default()  # slash commands need no privileged intents
        client_cls = _make_client_class(discord, app_commands, tasks)
        client = client_cls(intents=intents)
        _runtime["client"] = client
        _runtime["loop"] = loop

        async def runner():
            async with client:
                await client.start(config.DISCORD_BOT_TOKEN)

        try:
            loop.run_until_complete(runner())
        except Exception as e:
            _state["connected"] = False
            _state["last_error"] = str(e)
            _logger.exception("failed to start")
        finally:
            loop.close()
            _runtime["client"] = None
            _runtime["loop"] = None
            _runtime["thread"] = None

    thread = threading.Thread(target=_run, daemon=True, name="discord-bot")
    _runtime["thread"] = thread
    thread.start()


def stop(timeout=10):
    """Cleanly stops the running bot connection, if any - safe no-op if the bot was
    never started. Schedules client.close() onto the bot's own event loop from
    whatever thread calls this (an admin route's request-handling thread, not the
    bot's own thread) via asyncio.run_coroutine_threadsafe, then waits for the
    bot's background thread to actually finish - so by the time this returns, the
    connection is genuinely closed, not just "asked nicely", and a following
    start() (see restart() below) can't race with the old one still shutting
    down."""
    client = _runtime["client"]
    loop = _runtime["loop"]
    thread = _runtime["thread"]
    if client is None or loop is None:
        return
    try:
        asyncio.run_coroutine_threadsafe(client.close(), loop).result(timeout=timeout)
    except Exception:
        _logger.exception("error stopping discord bot")
    if thread is not None:
        thread.join(timeout)
    _state["connected"] = False


def restart():
    """Stops the current connection (if any) and starts a fresh one - e.g. after
    changing the bot token or another startup-time setting without restarting the
    whole app process."""
    stop()
    start()
