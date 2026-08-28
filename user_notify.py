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

# Which preference gates which event, per channel. A queued row whose user has since
# switched the relevant preference off is dropped rather than sent - the preference is
# checked at delivery time, not at enqueue time, so turning something off silences
# what's already waiting too. A channel mapped to None is never used for that event
# regardless of whether the person has contact details there - "seerr_event" is
# Discord-only, there is no email equivalent to gate.
EVENT_CHANNEL_PREFERENCE = {
    "report_reply": {"email": "notify_email_reports", "discord": "notify_discord_reports"},
    "report_incident": {"email": "notify_email_reports", "discord": "notify_discord_reports"},
    "maintenance": {"email": "notify_email_maintenance", "discord": "notify_discord_maintenance"},
    "request_update": {"email": "notify_email_requests", "discord": "notify_discord_requests"},
    "seerr_event": {"email": None, "discord": "notify_discord_seerr_events"},
}


# Events sourced from Seerr, which already emails its own users about these directly -
# see seerr_email_enabled() below. Both map to an email preference today only through
# request_update (seerr_event's own email slot is already None in the table above), but
# this set is checked by event, not by column, so a future email preference added for
# seerr_event would automatically be covered by the same switch without touching this
# list again.
SEERR_SOURCED_EVENTS = {"request_update", "seerr_event"}


def seerr_email_enabled():
    """Off by default - Seerr already sends its own email for these, so this portal's
    copy would otherwise double up on every install upgrading into this feature.
    Discord DMs are unaffected; this only ever suppresses the email channel."""
    return db.get_setting("seerr_email_events_enabled", "0") == "1"


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
    if event not in EVENT_CHANNEL_PREFERENCE:
        raise ValueError(f"Unknown notification event: {event}")
    return db.enqueue_notification(user_id, event, subject, body)


def notify_service_subscribers(event, subject, body, exclude_user_id=None):
    """Queues the same message for everyone opted into this event via at least one
    channel.

    Used for maintenance, which isn't attributable to one person. Reads the opted-in
    ids in a single query rather than every user's preferences one at a time - opted in
    via *either* channel, since deliver() decides per-channel which one(s) actually
    fire for a given row."""
    if not is_enabled():
        return 0
    columns = [c for c in EVENT_CHANNEL_PREFERENCE[event].values() if c]
    queued = 0
    for user_id in db.users_opted_into(*columns):
        if user_id and user_id != exclude_user_id:
            db.enqueue_notification(user_id, event, subject, body)
            queued += 1
    return queued


# ---------------------------------------------------------------------------
# Where a person's contact details come from
# ---------------------------------------------------------------------------
def seerr_integration():
    """Whichever Seerr the admin selected - see integrations.seerr_integration().

    Deliberately delegated rather than re-implemented here: three copies of "the first
    enabled one" is three chances for the search page, the notifier and the diagnostic
    to end up talking to different servers without anyone noticing."""
    return integrations.seerr_integration()


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
        users = integrations.fetch_seerr_users(integration["base_url"], integration["api_key"],
                                                with_notification_settings=True)
    except (requests.RequestException, ValueError) as e:
        _logger.info("Could not read Seerr users while looking for a link: %s", e)
        return None
    return next((u for u in users if u["jellyfin_user_id"] == str(jellyfin_user_id)), None)


def adopt_seerr_contact(user_id, account):
    """Copies a linked Seerr account's email/Discord ID into this portal's own copy.

    One write, shared by the manual "Use these details here" button and the automatic
    first-load fill-in on the account page - both are "take what Seerr already has",
    the only difference is whether a person clicked something to trigger it."""
    db.set_user_preferences(user_id,
                             notify_email=account["email"],
                             notify_discord_id=account["discord_id"],
                             seerr_user_id=account["id"])


def contact_for(user_id):
    """(email, discord_id) for a user, from the local caches only.

    Never calls Seerr. This runs once per queued notification in a background task, so
    going out to Seerr each time would be slow *and* would let a Seerr outage stop
    delivery to people whose details are perfectly well known.

    Precedence is "what was entered here" over "what Seerr last said", which sounds
    backwards for a system where Seerr is the source of truth - but anything entered
    here is written straight to Seerr and mirrored into the cache, so the two agree.
    The exception is a write-back that failed, and in that case the value the person
    actually typed is the better one to use."""
    prefs = db.get_user_preferences(user_id)
    cached = db.get_seerr_contact(user_id) or {}
    return (prefs["notify_email"] or cached.get("email", ""),
            prefs["notify_discord_id"] or cached.get("discord_id", ""))


def save_contact(jellyfin_user_id, email=None, discord_id=None):
    """Stores a contact detail and pushes it to Seerr. Returns (ok, message).

    Seerr is where this data belongs - people fill it in once there, and Seerr uses it
    for its own notifications - so anything entered in this portal is written back
    rather than kept as a private second copy that drifts. It's still stored locally
    too: the delivery task reads the local copy, and it must keep working when Seerr is
    down or when the account isn't linked at all.

    A failed write-back is reported, not swallowed, but does not undo the local save -
    losing what somebody just typed because another service was unreachable would be
    the worse outcome."""
    fields = {}
    if email is not None:
        fields["notify_email"] = email.strip()
    if discord_id is not None:
        fields["notify_discord_id"] = discord_id.strip()
    if not fields:
        return True, ""
    db.set_user_preferences(jellyfin_user_id, **fields)

    integration = seerr_integration()
    account = find_seerr_account(jellyfin_user_id) if integration else None
    if not account:
        return True, ("Saved here. Your Jellyfin account isn't linked to a Seerr one, so "
                      "there's nowhere to copy it to.")
    try:
        integrations.push_seerr_contact(
            integration["base_url"], integration["api_key"], account["id"],
            email=fields.get("notify_email"), discord_id=fields.get("notify_discord_id"))
    except (requests.RequestException, ValueError) as e:
        _logger.warning("Saved contact details locally but could not push them to Seerr: %s", e)
        return False, f"Saved here, but Seerr couldn't be updated ({e})."

    db.set_user_preferences(jellyfin_user_id, seerr_user_id=account["id"])
    db.upsert_seerr_contact(jellyfin_user_id, account["id"],
                             email=fields.get("notify_email"),
                             discord_id=fields.get("notify_discord_id"),
                             display_name=account["display_name"])
    return True, "Saved, and copied to your Seerr account."


def needs_contact_details(user_id):
    """Whether this person has nowhere to be reached, and hasn't said they'd rather not
    be asked. Drives the one-time prompt after signing in."""
    if not is_enabled():
        return False
    if db.get_user_preferences(user_id).get("contact_prompt_dismissed"):
        return False
    email, discord_id = contact_for(user_id)
    return not (email or discord_id)


def sync_seerr_contacts():
    """Body of the `seerr_contact_sync` scheduled task.

    Refreshes the local mirror of what Seerr holds. Same shape and same reasoning as the
    Jellyfin user sync: replaced wholesale on success, left completely alone on failure,
    and only users with a real jellyfinUserId link are stored - matching by name or email
    would eventually attach one person's contact details to another."""
    integration = seerr_integration()
    if integration is None:
        raise scheduler.TaskSkipped(
            "No enabled Jellyseerr/Overseerr integration - add one under Integrations.")
    # Discord IDs live on a per-user sub-resource, not the user list, so the sync has to
    # ask for them explicitly - that is exactly why email synced and Discord never did.
    users = integrations.fetch_seerr_users(integration["base_url"], integration["api_key"],
                                            with_notification_settings=True)
    linked = [{"jellyfin_user_id": u["jellyfin_user_id"], "seerr_user_id": u["id"],
               "display_name": u["display_name"], "email": u["email"],
               "discord_id": u["discord_id"]}
              for u in users if u["jellyfin_user_id"]]
    db.replace_seerr_contacts(linked)
    with_contact = sum(1 for c in linked if c["email"] or c["discord_id"])
    return (f"Cached {len(linked)} linked Seerr account(s) of {len(users)}; "
            f"{with_contact} have contact details.")


scheduler.register(
    "seerr_contact_sync",
    "Seerr contact sync",
    "Mirrors the email and Discord ID each linked Seerr account holds, so per-user "
    "notifications can be delivered without calling Seerr - and keep working while it's "
    "down. Only accounts Seerr has linked to a Jellyfin user are stored.",
    sync_seerr_contacts,
    default_interval_minutes=60,
)


# ---------------------------------------------------------------------------
# One-off admin-initiated sends (test notifications, a direct message) - always
# immediate, always bypassing the recipient's channel preferences. Both are explicit
# one-to-one admin actions, not automated events, so the only thing that can stop one
# is the person having no contact detail on that channel at all. Callers run these
# synchronously in the request, the same sanctioned exception admin_notifications_test()
# already uses: both discord_bot.send_dm() and notifications.send_email() carry their
# own hard timeouts, so this is bounded, and the admin gets the real error back
# ("Discord refused the DM...") instead of a queued row they'd have to go check.
# ---------------------------------------------------------------------------
def send_direct(user_id, channel, subject, body):
    """Sends one message to one person on one channel right now. Returns (ok, detail).

    channel is 'discord' or 'email'. Never touches EVENT_CHANNEL_PREFERENCE - this is
    not a queued, preference-gated notification."""
    email, discord_id = contact_for(user_id)
    if channel == "discord":
        if not discord_id:
            return False, "This person has no Discord ID on file."
        return discord_bot.send_dm(discord_id, f"**{subject}**\n{body}")
    if channel == "email":
        if not email:
            return False, "This person has no email address on file."
        if notifications.send_email(subject, body, recipients=[email]):
            return True, f"Sent to {email}."
        return False, "Send failed (see the log)."
    raise ValueError(f"Unknown channel: {channel}")


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------
def deliver(row):
    """Sends one queued notification. Returns (ok, detail).

    Each channel is gated independently against its own preference column (see
    EVENT_CHANNEL_PREFERENCE) - someone who wants an event by email but not Discord (or
    the other way around) must get exactly that, not both or neither. "Nowhere to send
    it" counts as done, not as a failure to retry: the person has no contact details or
    has every applicable channel switched off, and neither is fixed by trying again in
    two minutes."""
    channel_prefs = EVENT_CHANNEL_PREFERENCE.get(row["event"], {})
    prefs = db.get_user_preferences(row["user_id"])
    email, discord_id = prefs["notify_email"], prefs["notify_discord_id"]

    email_pref = channel_prefs.get("email")
    discord_pref = channel_prefs.get("discord")
    send_email = bool(email) and email_pref is not None and prefs.get(email_pref)
    if row["event"] in SEERR_SOURCED_EVENTS and not seerr_email_enabled():
        send_email = False
    send_discord = bool(discord_id) and discord_pref is not None and prefs.get(discord_pref)

    if not send_email and not send_discord:
        if not email and not discord_id:
            return True, "no contact details"
        return True, "recipient has this switched off"

    delivered, errors = [], []
    if send_discord:
        ok, error = discord_bot.send_dm(discord_id, f"**{row['subject']}**\n{row['body']}")
        delivered.append("discord") if ok else errors.append(f"discord: {error}")
    if send_email:
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
