import config
import notifications


def test_notify_does_nothing_when_unconfigured(monkeypatch):
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "")
    monkeypatch.setattr(config, "NTFY_URL", "")
    calls = []
    monkeypatch.setattr(notifications.requests, "post", lambda *a, **k: calls.append((a, k)))
    notifications.notify("Title", "Message")
    assert calls == []


def test_notify_sends_discord_payload(monkeypatch):
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "https://discord.example/webhook")
    monkeypatch.setattr(config, "NTFY_URL", "")
    calls = []
    monkeypatch.setattr(notifications.requests, "post", lambda *a, **k: calls.append((a, k)))
    notifications.notify("Incident opened", "Jellyfin is unreachable.")
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] == "https://discord.example/webhook"
    assert "Incident opened" in kwargs["json"]["content"]
    assert "Jellyfin is unreachable." in kwargs["json"]["content"]


def test_notify_sends_ntfy_payload(monkeypatch):
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "")
    monkeypatch.setattr(config, "NTFY_URL", "https://ntfy.sh/my-topic")
    calls = []
    monkeypatch.setattr(notifications.requests, "post", lambda *a, **k: calls.append((a, k)))
    notifications.notify("Incident resolved", "Jellyfin has recovered.")
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] == "https://ntfy.sh/my-topic"
    assert b"Incident resolved" in kwargs["data"]
    assert b"Jellyfin has recovered." in kwargs["data"]


def test_notify_swallows_exceptions(monkeypatch):
    monkeypatch.setattr(config, "DISCORD_WEBHOOK_URL", "https://discord.example/webhook")
    monkeypatch.setattr(config, "NTFY_URL", "https://ntfy.sh/my-topic")

    def _boom(*a, **k):
        raise ConnectionError("nope")

    monkeypatch.setattr(notifications.requests, "post", _boom)
    notifications.notify("Title", "Message")  # must not raise
