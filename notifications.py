"""
notifications.py — best-effort outbound push notifications (Discord webhook, ntfy).
Fire-and-forget: any failure (unreachable, misconfigured, timeout, encoding oddities)
is caught and logged, never raised - a notification failing must never break the
action that triggered it, whether that's the background health-check loop or an
admin submitting a form.
"""
import requests
import config

TIMEOUT = 5


def notify(title, message):
    """Sends `title`/`message` to every configured channel. No-op if none are set."""
    if config.DISCORD_WEBHOOK_URL:
        _send_discord(title, message)
    if config.NTFY_URL:
        _send_ntfy(title, message)


def _send_discord(title, message):
    try:
        requests.post(config.DISCORD_WEBHOOK_URL,
                      json={"content": f"**{title}**\n{message}"}, timeout=TIMEOUT)
    except Exception as e:
        print(f"[notifications] Discord webhook failed: {e}")


def _send_ntfy(title, message):
    # Title kept in the body rather than a request header - ntfy requires non-ASCII
    # header values to be RFC 2047-encoded, and a service/incident name with an accent
    # or emoji would otherwise raise before the request is even sent.
    try:
        requests.post(config.NTFY_URL, data=f"{title}\n{message}".encode("utf-8"), timeout=TIMEOUT)
    except Exception as e:
        print(f"[notifications] ntfy failed: {e}")
