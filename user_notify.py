"""
user_notify.py — telling the person who asked, rather than only telling the admin.

The portal already pushes to the admin (Discord webhook, ntfy, email). This is the
other direction: the visitor whose problem report got a reply, whose report became an
incident, whose Seerr request moved on, or who asked to hear about maintenance.

Three things here are load-bearing and easy to get wrong.

**Nothing is ever sent from the request that triggered it.** An admin clicking "reply"
writes one row to `notification_queue` and returns; the `user_notifications` scheduled
task drains it. That is the same rule every other outbound call in this app follows,
and it's why a slow SMTP server can't make the admin panel hang.

**Matching a Jellyfin user to a Seerr user fails closed.** Seerr can import Jellyfin
accounts, and when it has, each Seerr user carries the Jellyfin id it came from. That
real link is the only one followed. Matching on email or username instead would
eventually deliver one person's notifications to another - so an unmatched user is
simply asked for their details instead, which is a mild inconvenience rather than a
privacy failure.

**Writing back to Seerr is opt-in, one user, two fields, one button.** It is the only
call in this application that modifies another service, and it must never happen as a
side effect of a sync or a background task.
"""
import logging

import requests

import config
import db
import discord_bot
import integrations
import notifications
import scheduler

_logger = logging.getLogger(__name__)

TASK_NAME = "user_notifications"

# Which preference gates which event. A queued row whose user has since switched the
# relevant preference off is dropped rather than sent - the preference is checked at
# delivery time, not at enqueue time, so turning something off silences what's already
# waiting too.
EVENT_PREFERENCE = {
    "report_reply": "notify_own_reports",
    "report_incident": "notify_own_reports",
    "maintenance": "notify_service_events",
    "request_update": "notify_requests",
}


def is_enabled():
    """Off unless the admin has switched per-user notifications on. Enabling it means
    the portal starts messaging visitors, which has to be deliberate."""
    return db.get_setting("user_notifications_enabled", "0") == "1"


# ---------------------------------------------------------------------------
# Queueing (called from request handlers - one INSERT, never a network call)
# ---------------------------------------------------------------------------
def notify_user(user_id, event, subject, body):
    """Queues a notification for one Jellyfin user. Safe to call from anywhere,
    including a request handler, and a no-op when the feature is off."""
    if not user_id or not is_enabled():
        return None
    if event not in EVENT_PREFERENCE:
        raise ValueError(f"Unknown notification event: {event}")
    return db.enqueue_notification(user_id, event, subject, body)


def notify_service_subscribers(event, subject, body, exclude_user_id=None):
    """Queues the same message for everyone opted into service events.

    Used for maintenance, which isn't attributable to one person. Reads the opted-in
    ids in a single query rather than every user's preferences one at a time."""
    if not is_enabled():
        return 0
    queued = 0
    for user_id in db.users_opted_into(EVENT_PREFERENCE[event]):
        if user_id and user_id != exclude_user_id:
            db.enqueue_notification(user_id, event, subject, body)
            queued += 1
    return queued


# ---------------------------------------------------------------------------
# Where a person's contact details come from
# ---------------------------------------------------------------------------
def seerr_integration():
    return next((i for i in db.list_integrations()
                 if i["kind"] == "jellyseerr" and i["enabled"]), None)


def find_seerr_account(jellyfin_user_id):
    """The Seerr account belonging to this Jellyfin user, or None.

    Only ever follows Seerr's own `jellyfinUserId`. If Seerr hasn't imported Jellyfin
    accounts there is no link to follow, and this returns None rather than guessing -
    see the module docstring. Network failures also return None: "we couldn't check"
    and "there's no link" both correctly lead to asking the person directly."""
    integration = seerr_integration()
    if integration is None or not jellyfin_user_id:
        return None
    try:
        users = integrations.fetch_seerr_users(integration["base_url"], integration["api_key"])
    except (requests.RequestException, ValueError) as e:
        _logger.info("Could not read Seerr users while looking for a link: %s", e)
        return None
    return next((u for u in users if u["jellyfin_user_id"] == str(jellyfin_user_id)), None)


def contact_for(user_id):
    """(email, discord_id) for a user: whatever they've saved here.

    Deliberately reads only this app's stored preferences and never calls Seerr. This
    runs once per queued notification in a background task; going out to Seerr for each
    one would be both slow and a way for a Seerr outage to stop delivery entirely.
    Seerr is consulted once, when the account page offers to fill these in."""
    prefs = db.get_user_preferences(user_id)
    return prefs["notify_email"], prefs["notify_discord_id"]


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------
def deliver(row):
    """Sends one queued notification. Returns (ok, detail).

    "Nowhere to send it" counts as done, not as a failure to retry: the person has no
    contact details or has switched the relevant preference off, and neither will be
    fixed by trying again in two minutes."""
    preference = EVENT_PREFERENCE.get(row["event"])
    prefs = db.get_user_preferences(row["user_id"])
    if preference and not prefs.get(preference):
        return True, "recipient has this switched off"

    email, discord_id = prefs["notify_email"], prefs["notify_discord_id"]
    if not email and not discord_id:
        return True, "no contact details"

    delivered, errors = [], []
    if discord_id:
        ok, error = discord_bot.send_dm(discord_id, f"**{row['subject']}**\n{row['body']}")
        delivered.append("discord") if ok else errors.append(f"discord: {error}")
    if email:
        if notifications.send_email(row["subject"], row["body"], recipients=[email]):
            delivered.append("email")
        else:
            errors.append("email: send failed (see the log)")

    if delivered:
        # Partial success counts as sent. Retrying would re-deliver to the channel that
        # already worked, which is worse than one missing copy on the other.
        return True, ", ".join(delivered) + ("; " + "; ".join(errors) if errors else "")
    return False, "; ".join(errors) or "no channel available"


def run_delivery_task():
    """Body of the `user_notifications` scheduled task."""
    if not is_enabled():
        raise scheduler.TaskSkipped(
            "Per-user notifications are switched off - enable them under Notifications.")

    pending = db.pending_notifications()
    if not pending:
        db.prune_notification_queue()
        return "Nothing waiting."

    sent, failed = 0, 0
    for row in pending:
        try:
            ok, detail = deliver(row)
        except Exception as e:            # noqa: BLE001 - one bad row must not stop the drain
            _logger.exception("Delivering notification %s failed", row["id"])
            ok, detail = False, str(e)
        if ok:
            db.mark_notification_sent(row["id"])
            sent += 1
        else:
            db.mark_notification_failed(row["id"], detail)
            failed += 1
    db.prune_notification_queue()

    message = f"Delivered {sent}."
    if failed:
        message += (f" {failed} failed and will be retried "
                    f"(up to {db.MAX_NOTIFICATION_ATTEMPTS} attempts).")
    return message


scheduler.register(
    TASK_NAME,
    "Per-user notifications",
    "Delivers queued notifications to the people they're about - a reply to their "
    "problem report, their report becoming an incident, a request of theirs moving on, "
    "or maintenance they asked to hear about. Sending happens here rather than in the "
    "request that triggered it, so nothing in the admin panel ever waits on an email.",
    run_delivery_task,
    default_interval_minutes=2,
)
