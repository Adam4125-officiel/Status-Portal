"""
notifications.py — best-effort outbound push notifications (Discord webhook, ntfy,
email). Fire-and-forget: any failure (unreachable, misconfigured, timeout, encoding
oddities) is caught and logged, never raised - a notification failing must never break
the action that triggered it, whether that's the background health-check loop or an
admin submitting a form.

Adding a channel means touching exactly two things in this file, and they are kept
adjacent on purpose: `notify()` (so it actually sends) and `channel_summary()` (so the
admin page knows it exists). A channel that sends perfectly but is invisible on
/admin/notifications, or vice versa, is the failure mode that split causes.
"""
import html
import logging
import smtplib
from email.message import EmailMessage

import requests

import config

_logger = logging.getLogger(__name__)

TIMEOUT = 5


def email_recipients():
    """The admin alert recipients, from PORTAL_SMTP_TO. Comma-separated, blanks
    dropped, so a trailing comma or a stray space isn't an empty address."""
    return [address.strip() for address in config.SMTP_TO.split(",") if address.strip()]


def email_configured():
    """Email needs three things to work at all. A half-filled block counts as "not set
    up" rather than as a channel that fails on every send - the admin page can then say
    so, instead of the log filling with the same error every time a service blips."""
    return bool(config.SMTP_HOST and config.SMTP_FROM and email_recipients())


def channel_summary():
    """What delivery channels exist and whether each one is set up.

    Lives here rather than in the admin route so that adding a channel is a change to
    this module alone - see the module docstring."""
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
        {"key": "email",
         "label": "Email",
         "description": "Sent through your own SMTP server or provider. Needs a host, a "
                        "from-address and at least one recipient before it counts as set up.",
         "env_var": "PORTAL_SMTP_HOST",
         "configured": email_configured()},
    ]


def notify(title, message):
    """Sends `title`/`message` to every configured channel. No-op if none are set."""
    if config.DISCORD_WEBHOOK_URL:
        _send_discord(title, message)
    if config.NTFY_URL:
        _send_ntfy(title, message)
    if email_configured():
        send_email(title, message)


def _send_discord(title, message):
    try:
        requests.post(config.DISCORD_WEBHOOK_URL,
                      json={"content": f"**{title}**\n{message}"}, timeout=TIMEOUT)
    except Exception as e:
        _logger.warning("Discord webhook failed: %s", e)


def _send_ntfy(title, message):
    # Title kept in the body rather than a request header - ntfy requires non-ASCII
    # header values to be RFC 2047-encoded, and a service/incident name with an accent
    # or emoji would otherwise raise before the request is even sent.
    try:
        requests.post(config.NTFY_URL, data=f"{title}\n{message}".encode("utf-8"), timeout=TIMEOUT)
    except Exception as e:
        _logger.warning("ntfy failed: %s", e)


def build_email(subject, message, recipients):
    """A multipart/alternative message: plain text first, then a minimal HTML part.

    The HTML is built here with html.escape() rather than through a Jinja template on
    purpose. This module deliberately doesn't import Flask - it's called from the
    background health-check thread, where there is no request or app context to render
    a template in - and an email body is small enough that a template engine buys
    nothing. Every interpolated value is escaped; nothing here is ever marked safe.

    Plain text is set first because in multipart/alternative the *last* part is the
    preferred one, so this offers HTML to clients that want it and text to those that
    don't, which is the right way round."""
    email = EmailMessage()
    email["Subject"] = subject
    email["From"] = config.SMTP_FROM
    email["To"] = ", ".join(recipients)
    email.set_content(f"{subject}\n\n{message}\n\n-- \nSent by your Status Portal.")
    email.add_alternative(f"""<html><body style="font-family: -apple-system, Segoe UI, Roboto, sans-serif;
   line-height: 1.5; color: #1a1d24;">
  <h2 style="margin: 0 0 12px; font-size: 18px;">{html.escape(subject)}</h2>
  <p style="margin: 0 0 16px; white-space: pre-wrap;">{html.escape(message)}</p>
  <p style="margin: 0; font-size: 12px; color: #6b7280;">Sent by your Status Portal.</p>
</body></html>""", subtype="html")
    return email


def send_email(subject, message, recipients=None):
    """Sends one email. `recipients` defaults to the admin alert list, and is a
    parameter so per-user notifications can reuse this without going near PORTAL_SMTP_TO.

    Best-effort like every other channel here: returns True/False rather than raising,
    so a caller that wants to record delivery can, and one that doesn't can ignore it."""
    recipients = recipients if recipients is not None else email_recipients()
    if not (config.SMTP_HOST and config.SMTP_FROM and recipients):
        return False
    email = build_email(subject, message, recipients)
    try:
        with _smtp_connection() as smtp:
            if config.SMTP_USERNAME:
                smtp.login(config.SMTP_USERNAME, config.SMTP_PASSWORD)
            smtp.send_message(email)
        return True
    except Exception as e:
        # Including the recipients would put addresses in the log on every failure;
        # the count is enough to tell "one address is wrong" from "SMTP is down".
        _logger.warning("Email to %d recipient(s) failed: %s", len(recipients), e)
        return False


def _smtp_connection():
    """The connection, per PORTAL_SMTP_SECURITY.

    'ssl' wraps the socket from the start (implicit TLS, usually port 465); 'starttls'
    connects in the clear and upgrades (the common case, port 587); anything else is
    unencrypted, which is only reasonable for a relay on the same machine or LAN.
    An unrecognised value is treated as starttls rather than silently downgrading to
    plaintext - failing to connect is a far better outcome than quietly sending
    credentials in the clear."""
    if config.SMTP_SECURITY == "ssl":
        return smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT,
                                 timeout=config.SMTP_TIMEOUT_SECONDS)
    smtp = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT,
                         timeout=config.SMTP_TIMEOUT_SECONDS)
    if config.SMTP_SECURITY != "none":
        smtp.starttls()
    return smtp
