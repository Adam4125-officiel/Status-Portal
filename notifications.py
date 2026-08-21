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


def channel_summary():
    """What delivery channels exist and whether each one is set up.

    Lives here rather than in the admin route so that adding a channel is a change to
    this module alone: notify() and this list are the two things a new channel has to
    appear in, and keeping them side by side is what stops a channel that sends
    perfectly from being invisible on the admin page (or vice versa)."""
    return [
        {"key": "discord",
         "label": "Discord webhook",
         "description": "Posts to one Discord channel. Separate from the Discord bot - "
                        "a webhook is a one-way URL and needs no bot in your server.",
         "env_var": "PORTAL_DISCORD_WEBHOOK_URL",
         "configured": bool(config.DISCORD_WEBHOOK_URL)},
        {"key": "ntfy",
         "label": "ntfy",
         "description": "Push notification to your phone via ntfy.sh or your own ntfy server.",
         "env_var": "PORTAL_NTFY_URL",
         "configured": bool(config.NTFY_URL)},
    ]


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
