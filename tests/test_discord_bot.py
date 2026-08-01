import asyncio
from unittest.mock import AsyncMock, MagicMock

import db
import config
import discord_bot


def _make_interaction(user_id, channel_id=555):
    interaction = MagicMock()
    interaction.user = MagicMock()
    interaction.user.id = user_id
    interaction.user.__str__ = lambda self: f"user#{user_id}"
    interaction.channel_id = channel_id
    interaction.response = AsyncMock()
    interaction.original_response = AsyncMock(return_value=MagicMock(id=999))
    return interaction


def _build_bot():
    """Constructs a real StatusBot (no network call - just __init__)."""
    import discord
    from discord import app_commands
    from discord.ext import tasks
    client_cls = discord_bot._make_client_class(discord, app_commands, tasks)
    return client_cls(intents=discord.Intents.default())


def _build_test_client():
    """Constructs a real StatusBot (no network call - just __init__, which registers
    the slash command) so the actual authorization logic inside the command handler
    can be exercised directly, not just the parsing helpers around it."""
    client = _build_bot()
    command_name = discord_bot.sanitize_command_name(db.get_setting("discordbot_command_name", "status"))
    return client.tree.get_command(command_name)


def _build_snapshot_command():
    client = _build_bot()
    return client.tree.get_command("snapshot")


def _make_guild(guild_id, name="Test Server", channels=None):
    guild = MagicMock()
    guild.id = guild_id
    guild.name = name
    guild.leave = AsyncMock()
    guild.text_channels = channels or []
    return guild


def _make_channel(channel_id, name="general"):
    channel = MagicMock()
    channel.id = channel_id
    channel.name = name
    return channel


def test_channel_ids_parsing_and_normalization(isolated_db):
    assert discord_bot.allowed_channel_ids() == set()  # unset -> unrestricted
    db.set_setting("discordbot_channel_whitelist", "111, 222\n333")
    assert discord_bot.allowed_channel_ids() == {"111", "222", "333"}
    assert discord_bot.normalize_channel_ids("111,222\n333") == "111, 222, 333"


def test_snapshot_guilds_populates_state_from_gateway_cache(isolated_db, monkeypatch):
    bot = _build_bot()
    guild = _make_guild(111, name="Home Lab", channels=[_make_channel(555, "general")])
    # discord.Client.guilds is a read-only property backed by internal connection
    # state - patched at the (per-test, freshly-built) class level rather than set
    # directly on the instance.
    monkeypatch.setattr(type(bot), "guilds", property(lambda self: [guild]), raising=False)

    bot._snapshot_guilds()

    assert discord_bot.get_status()["guilds"] == [
        {"id": "111", "name": "Home Lab", "channels": [{"id": "555", "name": "general"}]}
    ]


def test_on_guild_remove_refreshes_guild_snapshot(isolated_db, monkeypatch):
    bot = _build_bot()
    current_guilds = [_make_guild(111, name="Home Lab")]
    monkeypatch.setattr(type(bot), "guilds", property(lambda self: current_guilds), raising=False)
    bot._snapshot_guilds()
    assert len(discord_bot.get_status()["guilds"]) == 1

    current_guilds.clear()  # discord.py has already updated the cache by the time this fires
    asyncio.run(bot.on_guild_remove(_make_guild(111, name="Home Lab")))
    assert discord_bot.get_status()["guilds"] == []


def test_snapshot_command_rejects_unauthorized_channel(isolated_db):
    db.set_setting("discordbot_channel_command_enabled", "1")
    db.set_setting("discordbot_channel_whitelist", "999")
    command = _build_snapshot_command()

    interaction = _make_interaction(user_id=1, channel_id=555)  # not in the whitelist
    asyncio.run(command.callback(interaction))

    args, kwargs = interaction.response.send_message.call_args
    assert "allowed in this channel" in args[0]
    assert kwargs.get("ephemeral") is True


def test_slash_command_allows_whitelisted_channel(isolated_db):
    db.set_setting("discordbot_channel_command_enabled", "1")
    db.set_setting("discordbot_channel_whitelist", "555")
    command = _build_test_client()

    interaction = _make_interaction(user_id=1, channel_id=555)
    asyncio.run(command.callback(interaction))

    args, kwargs = interaction.response.send_message.call_args
    assert "embed" in kwargs


def test_overall_status_ranks_slow_between_operational_and_degraded():
    assert discord_bot._overall_status([{"status": "operational"}, {"status": "slow"}]) == "slow"
    assert discord_bot._overall_status([{"status": "slow"}, {"status": "degraded"}]) == "degraded"
    assert discord_bot._overall_status([{"status": "operational"}]) == "operational"


def test_overall_status_ignores_flagged_services():
    services = [{"status": "down", "ignore_in_overall_status": 1}, {"status": "operational"}]
    assert discord_bot._overall_status(services) == "operational"


def test_slow_status_has_full_display_plumbing():
    for table in (discord_bot.STATUS_ICON, discord_bot.STATUS_LABEL,
                  discord_bot.PRESENCE_TEXT, discord_bot._EMBED_COLOR_NAME):
        assert "slow" in table


def test_build_status_data_respects_include_toggles(isolated_db):
    db.create_incident({"title": "Test outage", "status": "investigating"})
    db.create_incident({"title": "Old outage", "status": "resolved"})
    db.create_announcement({"title": "Heads up", "message": "Doing work tonight."})

    everything_on = {"services": True, "incidents": True, "announcements": True,
                      "maintenance": True, "resources": {}}
    data = discord_bot.build_status_data(everything_on)
    headings = [h for h, _ in data["sections"]]
    assert "Services" in headings
    assert "Active incident(s)" in headings  # still-open ("investigating") incident
    assert "Recent incidents" in headings  # only the already-resolved one
    assert "Announcements" in headings
    joined = "\n".join(line for _, lines in data["sections"] for line in lines)
    assert "Jellyfin" in joined and "Test outage" in joined and "Heads up" in joined
    assert dict(data["sections"])["Recent incidents"] == ["• [resolved] Old outage"]


def test_build_status_data_active_incident_disappears_once_resolved(isolated_db):
    db.create_incident({"title": "Test outage", "status": "investigating"})
    incident = db.list_incidents()[0]
    include = {"services": False, "incidents": True, "announcements": False,
               "maintenance": False, "resources": {}}

    data = discord_bot.build_status_data(include)
    assert dict(data["sections"])["Active incident(s)"] == ["🚨 [investigating] Test outage"]

    db.update_incident(incident["id"], {"title": "Test outage", "status": "resolved"})
    data = discord_bot.build_status_data(include)
    assert "Active incident(s)" not in dict(data["sections"])


def test_build_status_data_open_incident_not_in_recents(isolated_db):
    """Regression test: an open incident used to appear in both "Active incident(s)"
    and "Recent incidents" (the latter was every incident regardless of status) -
    it should only ever move to "recents" once resolved."""
    db.create_incident({"title": "Ongoing outage", "status": "investigating"})
    include = {"services": False, "incidents": True, "announcements": False,
               "maintenance": False, "resources": {}}

    data = discord_bot.build_status_data(include)
    sections = dict(data["sections"])
    assert sections["Active incident(s)"] == ["🚨 [investigating] Ongoing outage"]
    assert "Recent incidents" not in sections


def test_build_status_data_includes_service_links(isolated_db):
    service = db.list_services()[0]
    db.replace_service_links(service["id"], [("Tailscale", "https://ts.example/jellyfin")])
    include = {"services": True, "incidents": False, "announcements": False,
               "maintenance": False, "resources": {}}
    data = discord_bot.build_status_data(include)
    service_lines = dict(data["sections"])["Services"]
    assert any("[Tailscale](https://ts.example/jellyfin)" in line for line in service_lines)


def test_build_status_data_highload_section(isolated_db, monkeypatch):
    include = {"services": False, "incidents": False, "announcements": False,
               "maintenance": False, "resources": {}, "highload": True}

    monkeypatch.setattr(discord_bot.monitoring, "get_resource_snapshot", lambda: {})
    monkeypatch.setattr(discord_bot.integrations, "evaluate_high_load",
                         lambda snap: {"active": False, "reasons": []})
    data = discord_bot.build_status_data(include)
    assert "High load" not in dict(data["sections"])

    monkeypatch.setattr(discord_bot.integrations, "evaluate_high_load",
                         lambda snap: {"active": True, "reasons": ["CPU 95%"]})
    data = discord_bot.build_status_data(include)
    assert dict(data["sections"])["High load"] == ["⚠ CPU 95%"]

    everything_off = {"services": False, "incidents": False, "announcements": False,
                       "maintenance": False, "resources": {}}
    data = discord_bot.build_status_data(everything_off)
    assert data["sections"] == []
    assert data["overall"] == "operational"


def test_build_status_data_resource_toggles_are_individually_checkable(isolated_db):
    only_cpu = {"services": False, "incidents": False, "announcements": False,
                "maintenance": False, "resources": {"cpu": True, "memory": False, "disks": False,
                                                      "network": False, "gpu": False, "vms": False}}
    data = discord_bot.build_status_data(only_cpu)
    assert len(data["sections"]) == 1
    heading, lines = data["sections"][0]
    assert heading == "Resources"
    assert any("CPU" in line for line in lines)
    assert not any("RAM" in line for line in lines)


def test_include_settings_defaults(isolated_db):
    include = discord_bot.include_settings()
    assert include["services"] is True
    assert include["incidents"] is True
    assert include["announcements"] is True
    assert include["maintenance"] is True
    assert all(v is False for v in include["resources"].values())  # all resource items off by default

    db.set_setting("discordbot_resource_cpu", "1")
    db.set_setting("discordbot_include_services", "0")
    include = discord_bot.include_settings()
    assert include["resources"]["cpu"] is True
    assert include["services"] is False


def test_sanitize_command_name():
    assert discord_bot.sanitize_command_name("!status") == "status"
    assert discord_bot.sanitize_command_name("/status") == "status"
    assert discord_bot.sanitize_command_name("My Cool Command!!") == "mycoolcommand"
    assert discord_bot.sanitize_command_name("") == "status"
    assert discord_bot.sanitize_command_name(None) == "status"
    assert discord_bot.sanitize_command_name("a" * 50) == "a" * 32


def test_build_embed_reflects_overall_status_and_sections(isolated_db):
    import discord  # installed in this dev sandbox for verification; not a hard dependency of the app
    data = discord_bot.build_status_data({"services": True, "incidents": False,
                                           "announcements": False, "maintenance": False, "resources": {}})
    embed = discord_bot.build_embed(discord, data)
    assert "Jellyfin" in embed.fields[0].value
    assert embed.description == discord_bot.PRESENCE_TEXT["operational"]
    assert str(embed.color) == "#2ecc71"  # green


def test_build_embed_shows_placeholder_when_nothing_included(isolated_db):
    import discord
    data = discord_bot.build_status_data({"services": False, "incidents": False,
                                           "announcements": False, "maintenance": False, "resources": {}})
    embed = discord_bot.build_embed(discord, data)
    assert len(embed.fields) == 1
    assert "Nothing to show" in embed.fields[0].name


def test_build_status_message_truncates_long_output(isolated_db):
    for i in range(200):
        db.create_service({"name": f"Service {i}", "url": f"http://svc{i}.example"})
    data = discord_bot.build_status_data({"services": True, "incidents": False,
                                           "announcements": False, "maintenance": False, "resources": {}})
    heading, lines = data["sections"][0]
    joined = "\n".join(lines)
    truncated = discord_bot._truncate(joined, 1024)
    assert len(truncated) <= 1024
    assert truncated.endswith("…")


def test_start_is_noop_when_token_blank(monkeypatch):
    monkeypatch.setattr(config, "DISCORD_BOT_TOKEN", "")
    calls = []
    monkeypatch.setattr(discord_bot, "_try_import_discord", lambda: calls.append("called") or (None, None, None))
    discord_bot.start()
    assert calls == []  # must return before even attempting the import


def test_start_logs_clearly_when_discord_py_missing(monkeypatch, caplog):
    monkeypatch.setattr(config, "DISCORD_BOT_TOKEN", "fake-token")
    monkeypatch.setattr(discord_bot, "_try_import_discord", lambda: (None, None, None))
    with caplog.at_level("WARNING"):
        discord_bot.start()  # must not raise
    assert "discord.py isn't installed" in caplog.text


def test_discord_status_message_db_roundtrip(isolated_db):
    assert db.get_discord_status_message(123) is None
    db.set_discord_status_message(123, 456)
    entry = db.get_discord_status_message(123)
    assert entry["message_id"] == "456"

    db.set_discord_status_message(123, 789)  # overwrite
    assert db.get_discord_status_message(123)["message_id"] == "789"
    assert len(db.list_discord_status_messages()) == 1

    db.delete_discord_status_message(123)
    assert db.get_discord_status_message(123) is None


def test_allowed_user_ids_parsing(isolated_db):
    assert discord_bot.allowed_user_ids() == set()  # unrestricted by default
    db.set_setting("discordbot_allowed_user_ids", "123, 456\n789")
    assert discord_bot.allowed_user_ids() == {"123", "456", "789"}


def test_normalize_user_ids():
    assert discord_bot.normalize_user_ids("123,456\n789") == "123, 456, 789"
    assert discord_bot.normalize_user_ids("  ") == ""
    assert discord_bot.normalize_user_ids(None) == ""


def test_build_snapshot_data_all_clear(isolated_db):
    db.update_service(db.list_services()[0]["id"], {**db.list_services()[0], "status": "operational"})
    data = discord_bot.build_snapshot_data()
    assert data == {"down": [], "incident_count": 0, "maintenance_count": 0}
    assert discord_bot.build_snapshot_text(data) == "✅ All services up. No open incidents or maintenance."


def test_build_snapshot_data_reports_down_services_and_open_incident(isolated_db):
    service = db.list_services()[0]
    db.update_service(service["id"], {**service, "status": "down"})
    db.create_incident({"title": "Test outage", "status": "investigating"})

    data = discord_bot.build_snapshot_data()
    assert data["down"] == [service["name"]]
    assert data["incident_count"] == 1
    assert data["maintenance_count"] == 0
    text = discord_bot.build_snapshot_text(data)
    assert service["name"] in text
    assert "1 open incident(s)" in text


def test_build_snapshot_data_counts_only_in_progress_maintenance(isolated_db):
    sid = db.list_services()[0]["id"]
    # Scheduled but not yet started - must not count as "in progress".
    db.create_maintenance_window({
        "title": "Future", "starts_at": "2099-01-01T00:00", "ends_at": "2099-01-02T00:00",
    }, service_ids=[sid])
    assert discord_bot.build_snapshot_data()["maintenance_count"] == 0

    db.create_maintenance_window({
        "title": "Ongoing", "starts_at": "2000-01-01T00:00", "ends_at": "2099-01-01T00:00",
    }, service_ids=[sid])
    db.process_maintenance_windows()
    assert discord_bot.build_snapshot_data()["maintenance_count"] == 1


def test_registering_both_commands_does_not_collide_when_named_snapshot(isolated_db):
    """An admin who names their main command "snapshot" would otherwise collide with
    the fixed /snapshot command at registration time - must fall back cleanly."""
    db.set_setting("discordbot_command_name", "snapshot")
    client = _build_bot()
    assert client.tree.get_command("status") is not None
    assert client.tree.get_command("snapshot") is not None


def test_snapshot_command_rejects_unauthorized_user(isolated_db):
    db.set_setting("discordbot_channel_command_enabled", "1")
    db.set_setting("discordbot_allowed_user_ids", "111")
    command = _build_snapshot_command()

    interaction = _make_interaction(user_id=222)
    asyncio.run(command.callback(interaction))

    args, kwargs = interaction.response.send_message.call_args
    assert "not authorized" in args[0]
    assert kwargs.get("ephemeral") is True


def test_snapshot_command_replies_with_plain_text_snapshot(isolated_db):
    db.set_setting("discordbot_channel_command_enabled", "1")
    service = db.list_services()[0]
    db.update_service(service["id"], {**service, "status": "down"})
    command = _build_snapshot_command()

    interaction = _make_interaction(user_id=1)
    asyncio.run(command.callback(interaction))

    args, kwargs = interaction.response.send_message.call_args
    assert service["name"] in args[0]
    assert "embed" not in kwargs  # a plain one-shot reply, not an embed


def test_slash_command_rejects_unauthorized_user(isolated_db):
    db.set_setting("discordbot_channel_command_enabled", "1")
    db.set_setting("discordbot_allowed_user_ids", "111")
    command = _build_test_client()

    interaction = _make_interaction(user_id=222)
    asyncio.run(command.callback(interaction))

    interaction.response.send_message.assert_called_once()
    args, kwargs = interaction.response.send_message.call_args
    assert "not authorized" in args[0]
    assert kwargs.get("ephemeral") is True
    assert db.get_discord_status_message(555) is None  # rejected - no message ever tracked


def test_slash_command_allows_authorized_user(isolated_db):
    db.set_setting("discordbot_channel_command_enabled", "1")
    db.set_setting("discordbot_allowed_user_ids", "111")
    command = _build_test_client()

    interaction = _make_interaction(user_id=111)
    asyncio.run(command.callback(interaction))

    interaction.response.send_message.assert_called_once()
    args, kwargs = interaction.response.send_message.call_args
    assert "embed" in kwargs
    assert db.get_discord_status_message(555)["message_id"] == "999"


def test_slash_command_unrestricted_when_allow_list_empty(isolated_db):
    db.set_setting("discordbot_channel_command_enabled", "1")
    # discordbot_allowed_user_ids left unset entirely
    command = _build_test_client()

    interaction = _make_interaction(user_id=999999)
    asyncio.run(command.callback(interaction))

    args, kwargs = interaction.response.send_message.call_args
    assert "embed" in kwargs  # anyone is let through when no allow-list is configured


def test_slash_command_disabled_takes_priority_over_authorization(isolated_db):
    db.set_setting("discordbot_channel_command_enabled", "0")
    db.set_setting("discordbot_allowed_user_ids", "111")
    command = _build_test_client()

    interaction = _make_interaction(user_id=111)  # would be authorized, but feature is off
    asyncio.run(command.callback(interaction))

    args, kwargs = interaction.response.send_message.call_args
    assert "disabled" in args[0]


def test_allowed_guild_ids_parsing(isolated_db):
    assert discord_bot.allowed_guild_ids() == set()  # unrestricted by default
    db.set_setting("discordbot_guild_whitelist", "111, 222\n333")
    assert discord_bot.allowed_guild_ids() == {"111", "222", "333"}


def test_normalize_guild_ids():
    assert discord_bot.normalize_guild_ids("111,222\n333") == "111, 222, 333"
    assert discord_bot.normalize_guild_ids("  ") == ""
    assert discord_bot.normalize_guild_ids(None) == ""


def test_enforce_guild_whitelist_leaves_unlisted_server(isolated_db, caplog):
    db.set_setting("discordbot_guild_whitelist", "111")
    bot = _build_bot()
    guild = _make_guild(222, name="Unwanted Server")

    with caplog.at_level("INFO"):
        left = asyncio.run(bot._enforce_guild_whitelist(guild))

    assert left is True
    guild.leave.assert_awaited_once()
    assert "Unwanted Server" in caplog.text


def test_enforce_guild_whitelist_keeps_listed_server(isolated_db):
    db.set_setting("discordbot_guild_whitelist", "111")
    bot = _build_bot()
    guild = _make_guild(111)

    left = asyncio.run(bot._enforce_guild_whitelist(guild))

    assert left is False
    guild.leave.assert_not_called()


def test_enforce_guild_whitelist_unrestricted_when_empty(isolated_db):
    # discordbot_guild_whitelist left unset entirely
    bot = _build_bot()
    guild = _make_guild(999999)

    left = asyncio.run(bot._enforce_guild_whitelist(guild))

    assert left is False
    guild.leave.assert_not_called()


def test_on_ready_re_enforces_whitelist_for_already_joined_guilds(isolated_db, monkeypatch):
    db.set_setting("discordbot_guild_whitelist", "111")
    bot = _build_bot()
    kept = _make_guild(111)
    unwanted = _make_guild(222)
    monkeypatch.setattr(type(bot), "guilds", property(lambda self: [kept, unwanted]))
    fake_user = MagicMock()
    fake_user.__str__ = lambda self: "TestBot#0001"
    monkeypatch.setattr(type(bot), "user", property(lambda self: fake_user))
    bot.refresh_loop = MagicMock()
    bot.refresh_loop.is_running = MagicMock(return_value=True)  # skip actually starting it

    asyncio.run(bot.on_ready())

    kept.leave.assert_not_called()
    unwanted.leave.assert_awaited_once()
