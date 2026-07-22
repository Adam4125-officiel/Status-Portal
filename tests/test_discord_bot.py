import db
import config
import discord_bot


def test_build_status_message_respects_include_toggles(isolated_db):
    db.create_incident({"title": "Test outage", "status": "investigating"})
    db.create_announcement({"title": "Heads up", "message": "Doing work tonight."})

    everything_on = {"services": True, "incidents": True, "announcements": True,
                      "maintenance": True, "resources": False}
    text = discord_bot.build_status_message(everything_on)
    assert "Jellyfin" in text
    assert "Test outage" in text
    assert "Heads up" in text

    everything_off = {"services": False, "incidents": False, "announcements": False,
                       "maintenance": False, "resources": False}
    text = discord_bot.build_status_message(everything_off)
    assert "Jellyfin" not in text
    assert "Test outage" not in text
    assert "Heads up" not in text
    assert "status" in text.lower()  # the header line is always present


def test_build_status_message_truncates_long_output(isolated_db):
    for i in range(200):
        db.create_service({"name": f"Service {i}", "url": f"http://svc{i}.example"})
    text = discord_bot.build_status_message({"services": True, "incidents": False,
                                              "announcements": False, "maintenance": False,
                                              "resources": False})
    assert len(text) <= 1920
    assert text.endswith("(truncated)")


def test_include_settings_defaults(isolated_db):
    include = discord_bot.include_settings()
    assert include == {"services": True, "incidents": True, "announcements": True,
                        "maintenance": True, "resources": False}

    db.set_setting("discordbot_include_resources", "1")
    db.set_setting("discordbot_include_services", "0")
    include = discord_bot.include_settings()
    assert include["resources"] is True
    assert include["services"] is False


def test_start_is_noop_when_token_blank(monkeypatch):
    monkeypatch.setattr(config, "DISCORD_BOT_TOKEN", "")
    calls = []
    monkeypatch.setattr(discord_bot, "_try_import_discord", lambda: calls.append("called") or (None, None))
    discord_bot.start()
    assert calls == []  # must return before even attempting the import


def test_start_logs_clearly_when_discord_py_missing(monkeypatch, capsys):
    monkeypatch.setattr(config, "DISCORD_BOT_TOKEN", "fake-token")
    monkeypatch.setattr(discord_bot, "_try_import_discord", lambda: (None, None))
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
