"""
discord_bot.py — optional Discord bot integration (separate from the simple Discord
webhook in notifications.py, which only ever sends one-way messages). This one is a
real bot connection: it can update its own presence/status, and/or respond to a
command word in any channel it can see by posting a status summary and then
continuously editing that same message on a timer instead of spamming new ones.

Fully optional and lazily loaded - this module has NO hard dependency on the
`discord.py` package. If PORTAL_DISCORD_BOT_TOKEN is unset, or discord.py isn't
installed, start() is a clean no-op and logs why. Nothing else in this app is
affected either way - app.py imports this module unconditionally, so it must never
raise merely from being imported.
"""
import threading

import config
import db
import monitoring

_state = {"connected": False, "user": None, "last_error": None}

STATUS_ICON = {"operational": "🟢", "degraded": "🟡", "maintenance": "🟣", "down": "🔴"}
STATUS_LABEL = {"operational": "Operational", "degraded": "Degraded",
                "maintenance": "Maintenance", "down": "Down"}

INCLUDE_KEYS = ("services", "incidents", "announcements", "maintenance", "resources")
_INCLUDE_DEFAULTS = {"services": "1", "incidents": "1", "announcements": "1",
                     "maintenance": "1", "resources": "0"}


def get_status():
    """Read-only snapshot for the admin page - never raises, reflects whatever the
    bot thread has last reported via the module-level _state dict."""
    return dict(_state)


def include_settings():
    return {key: db.get_setting(f"discordbot_include_{key}", _INCLUDE_DEFAULTS[key]) == "1"
            for key in INCLUDE_KEYS}


def _overall_status(services):
    statuses = [s["status"] for s in services]
    if "down" in statuses:
        return "down"
    if "degraded" in statuses:
        return "degraded"
    if "maintenance" in statuses:
        return "maintenance"
    return "operational"


def build_status_message(include):
    """Pure and side-effect free (no Discord/network calls) so it's cheaply testable
    on its own - given the same DB state, always builds the same text."""
    site_name = db.get_setting("site_name", "Server")
    services = db.list_services()
    overall = _overall_status(services)
    lines = [f"**{site_name} status** — {STATUS_ICON[overall]} {STATUS_LABEL[overall]}"]

    if include.get("services") and services:
        lines.append("")
        lines.append("**Services**")
        for s in services:
            icon = STATUS_ICON.get(s["status"], "⚪")
            lines.append(f"{icon} {s['name']} — {STATUS_LABEL.get(s['status'], s['status'])}")

    if include.get("maintenance"):
        windows = db.list_public_maintenance_windows()
        if windows:
            lines.append("")
            lines.append("**Scheduled maintenance**")
            for w in windows:
                state = "in progress" if w["applied"] else "scheduled"
                lines.append(f"🛠 {w['service_name']}: {w['title']} ({state}, "
                              f"{w['starts_at'][:16]} → {w['ends_at'][:16]} UTC)")

    if include.get("incidents"):
        incidents = db.list_incidents(limit=5)
        if incidents:
            lines.append("")
            lines.append("**Recent incidents**")
            for i in incidents:
                lines.append(f"• [{i['status']}] {i['title']}")

    if include.get("announcements"):
        announcements = db.list_announcements(limit=3)
        if announcements:
            lines.append("")
            lines.append("**Announcements**")
            for a in announcements:
                lines.append(f"📣 {a['title']}: {a['message']}")

    if include.get("resources"):
        try:
            snap = monitoring.get_resource_snapshot()
            lines.append("")
            lines.append(f"**Resources** — CPU {snap['cpu_percent']}% · RAM {snap['mem_percent']}%")
        except Exception:
            pass

    text = "\n".join(lines)
    if len(text) > 1900:  # Discord's non-Nitro message cap is 2000 chars
        text = text[:1900] + "\n… (truncated)"
    return text


def _try_import_discord():
    try:
        import discord
        from discord.ext import tasks
        return discord, tasks
    except ImportError:
        return None, None


def _make_client_class(discord, tasks):
    """Built lazily, only once discord.py is confirmed importable - this is the only
    place in the module that references the `discord` package, so nothing above it
    needs discord.py installed just to be imported."""

    class StatusBot(discord.Client):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.refresh_loop = tasks.loop(seconds=config.DISCORD_BOT_REFRESH_SECONDS)(self._refresh)

        async def on_ready(self):
            _state["connected"] = True
            _state["user"] = str(self.user)
            _state["last_error"] = None
            print(f"[discord-bot] connected as {self.user}")
            if not self.refresh_loop.is_running():
                self.refresh_loop.start()

        async def on_disconnect(self):
            _state["connected"] = False

        async def on_message(self, message):
            if message.author == self.user:
                return
            if db.get_setting("discordbot_channel_command_enabled", "0") != "1":
                return
            command = db.get_setting("discordbot_command_word", "!status")
            if message.content.strip().lower() != command.strip().lower():
                return
            try:
                await self._post_or_edit(message.channel)
            except Exception as e:
                print(f"[discord-bot] failed to respond in channel {message.channel.id}: {e}")

        async def _post_or_edit(self, channel):
            text = build_status_message(include_settings())
            existing = db.get_discord_status_message(channel.id)
            if existing:
                try:
                    msg = await channel.fetch_message(int(existing["message_id"]))
                    await msg.edit(content=text)
                    return
                except Exception:
                    pass  # tracked message is gone/inaccessible - fall through and post a fresh one
            sent = await channel.send(text)
            db.set_discord_status_message(channel.id, sent.id)

        async def _refresh(self):
            try:
                if db.get_setting("discordbot_update_presence", "0") == "1":
                    overall = _overall_status(db.list_services())
                    await self.change_presence(activity=discord.Activity(
                        type=discord.ActivityType.watching,
                        name=f"{STATUS_ICON[overall]} {STATUS_LABEL[overall]}"))
                if db.get_setting("discordbot_channel_command_enabled", "0") == "1":
                    for row in db.list_discord_status_messages():
                        channel = self.get_channel(int(row["channel_id"]))
                        if channel is None:
                            db.delete_discord_status_message(row["channel_id"])
                            continue
                        try:
                            await self._post_or_edit(channel)
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
    discord, tasks = _try_import_discord()
    if discord is None:
        print("[discord-bot] PORTAL_DISCORD_BOT_TOKEN is set but discord.py isn't installed - "
              "run `pip install discord.py` to enable this optional feature.")
        return

    def _run():
        try:
            intents = discord.Intents.default()
            intents.message_content = True  # required to read the command word in on_message
            client_cls = _make_client_class(discord, tasks)
            client = client_cls(intents=intents)
            client.run(config.DISCORD_BOT_TOKEN)
        except Exception as e:
            _state["connected"] = False
            _state["last_error"] = str(e)
            print(f"[discord-bot] failed to start: {e}")

    threading.Thread(target=_run, daemon=True, name="discord-bot").start()
