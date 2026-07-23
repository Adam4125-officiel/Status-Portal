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
import re
import threading

import config
import db
import monitoring

_state = {"connected": False, "user": None, "last_error": None}

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

RESOURCE_KEYS = ("cpu", "memory", "disks", "disk_io", "network", "gpu", "vms")
_RESOURCE_DEFAULTS = {key: "0" for key in RESOURCE_KEYS}  # all off by default, admin opts in per-item

INCLUDE_KEYS = ("services", "incidents", "announcements", "maintenance")
_INCLUDE_DEFAULTS = {"services": "1", "incidents": "1", "announcements": "1", "maintenance": "1"}


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


def allowed_user_ids():
    """Discord user IDs (as strings) permitted to invoke the slash command - parsed
    from a comma/newline-separated DB setting. An empty result means unrestricted
    (anyone in the server can use it) - this is the default, so the command keeps
    working for anyone who hasn't set this up, but it's called out clearly in the
    admin UI since leaving it unrestricted is exactly what allows the spam/abuse this
    setting exists to prevent."""
    raw = db.get_setting("discordbot_allowed_user_ids", "")
    return {part.strip() for part in raw.replace("\n", ",").split(",") if part.strip()}


def normalize_user_ids(raw):
    """Cleans up admin-entered input (mixed commas/newlines/whitespace, digits only
    per ID) into a canonical comma-separated string for storage/redisplay."""
    ids = [part.strip() for part in (raw or "").replace("\n", ",").split(",") if part.strip()]
    return ", ".join(ids)


def _overall_status(services):
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
        lines.append(f"CPU: {snap['cpu_percent']}%")
    if toggles.get("memory"):
        lines.append(f"RAM: {snap['mem_used_gb']}/{snap['mem_total_gb']} GB ({snap['mem_percent']}%)")
    if toggles.get("disks"):
        for d in snap["disks"]:
            lines.append(f"💽 {d['display_name']}: {d['percent']}% used, {d['free_gb']} GB free")
    if toggles.get("disk_io") and snap["disk_io"]:
        lines.append(f"Disk I/O: ↓{snap['disk_io']['read_mb_s']} MB/s ↑{snap['disk_io']['write_mb_s']} MB/s")
    if toggles.get("network") and snap["network"]:
        lines.append(f"Network: ↓{snap['network']['down_mb_s']} MB/s ↑{snap['network']['up_mb_s']} MB/s")
    if toggles.get("gpu"):
        for g in snap["gpus"]:
            lines.append(f"🎮 {g['name']}: {g['util_percent']}%, {g['mem_used_gb']}/{g['mem_total_gb']} GB")
    if toggles.get("vms"):
        for vm in monitoring.get_vm_snapshot():
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
        lines = [f"{STATUS_ICON.get(s['status'], '⚪')} **{s['name']}** — "
                 f"{STATUS_LABEL.get(s['status'], s['status'])}" for s in services]
        sections.append(("Services", lines))

    if include.get("maintenance"):
        windows = db.list_public_maintenance_windows()
        if windows:
            lines = []
            for w in windows:
                state = "in progress" if w["applied"] else "scheduled"
                lines.append(f"🛠 **{w['service_name']}**: {w['title']} ({state}, "
                              f"{w['starts_at'][:16]} → {w['ends_at'][:16]} UTC)")
            sections.append(("Scheduled maintenance", lines))

    if include.get("incidents"):
        incidents = db.list_incidents(limit=5)
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

    return {"site_name": site_name, "overall": overall, "sections": sections}


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

        def _register_command(self):
            command_name = sanitize_command_name(db.get_setting("discordbot_command_name", "status"))

            @self.tree.command(name=command_name, description="Show the current status page summary")
            async def status_command(interaction):
                if db.get_setting("discordbot_channel_command_enabled", "0") != "1":
                    await interaction.response.send_message(
                        "This command is currently disabled in /admin/discord-bot.", ephemeral=True)
                    return
                allowed = allowed_user_ids()
                if allowed and str(interaction.user.id) not in allowed:
                    print(f"[discord-bot] rejected /{command_name} from unauthorized user "
                          f"{interaction.user} ({interaction.user.id})")
                    await interaction.response.send_message(
                        "You're not authorized to use this command.", ephemeral=True)
                    return
                embed = build_embed(discord, build_status_data(include_settings()))
                await interaction.response.send_message(embed=embed)
                sent = await interaction.original_response()
                db.set_discord_status_message(interaction.channel_id, sent.id)

        async def setup_hook(self):
            # Command registration is only picked up on (re)connect - changing the
            # command name or adding it later requires an app restart, same as any
            # other startup-time config in this app.
            guild_id = config.DISCORD_BOT_GUILD_ID
            if guild_id:
                guild = discord.Object(id=int(guild_id))
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                print(f"[discord-bot] slash command synced to guild {guild_id}")
            else:
                await self.tree.sync()
                print("[discord-bot] slash command synced globally "
                      "(can take up to an hour to first appear - set PORTAL_DISCORD_BOT_GUILD_ID "
                      "for instant registration on a single server)")

        async def on_ready(self):
            _state["connected"] = True
            _state["user"] = str(self.user)
            _state["last_error"] = None
            print(f"[discord-bot] connected as {self.user}")
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
            except Exception as e:
                print(f"[discord-bot] refresh loop error: {e}")

    return StatusBot


def start():
    """Starts the bot in a background daemon thread, if configured. Safe no-op
    otherwise - called unconditionally from app.py/serve_waitress.py, same as
    start_background_checker()."""
    if not config.DISCORD_BOT_TOKEN:
        return
    discord, app_commands, tasks = _try_import_discord()
    if discord is None:
        print("[discord-bot] PORTAL_DISCORD_BOT_TOKEN is set but discord.py isn't installed - "
              "run `pip install discord.py` to enable this optional feature.")
        return

    def _run():
        try:
            intents = discord.Intents.default()  # slash commands need no privileged intents
            client_cls = _make_client_class(discord, app_commands, tasks)
            client = client_cls(intents=intents)
            client.run(config.DISCORD_BOT_TOKEN)
        except Exception as e:
            _state["connected"] = False
            _state["last_error"] = str(e)
            print(f"[discord-bot] failed to start: {e}")

    threading.Thread(target=_run, daemon=True, name="discord-bot").start()
