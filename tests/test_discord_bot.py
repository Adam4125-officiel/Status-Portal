import db
import config
import discord_bot


def test_build_status_data_respects_include_toggles(isolated_db):
    db.create_incident({"title": "Test outage", "status": "investigating"})
    db.create_announcement({"title": "Heads up", "message": "Doing work tonight."})

    everything_on = {"services": True, "incidents": True, "announcements": True,
                      "maintenance": True, "resources": {}}
    data = discord_bot.build_status_data(everything_on)
    headings = [h for h, _ in data["sections"]]
    assert "Services" in headings
    assert "Recent incidents" in headings
    assert "Announcements" in headings
    joined = "\n".join(line for _, lines in data["sections"] for line in lines)
    assert "Jellyfin" in joined and "Test outage" in joined and "Heads up" in joined

    everything_off = {"services": False, "incidents": False, "announcements": False,
                       "maintenance": False, "resources": {}}
    data = discord_bot.build_status_data(everything_off)
    assert data["sections"] == []
    assert data["overall"] == "operational"


def test_build_status_data_resource_toggles_are_individually_checkable(isolated_db):
    only_cpu = {"services": False, "incidents": False, "announcements": False,
                "maintenance": False, "resources": {"cpu": True, "memory": False, "disks": False,
                                                      "disk_io": False, "network": False,
                                                      "gpu": False, "vms": False}}
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


def test_start_logs_clearly_when_discord_py_missing(monkeypatch, capsys):
    monkeypatch.setattr(config, "DISCORD_BOT_TOKEN", "fake-token")
    monkeypatch.setattr(discord_bot, "_try_import_discord", lambda: (None, None, None))
    discord_bot.start()  # must not raise
    out = capsys.readouterr().out
    assert "discord.py isn't installed" in out


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
