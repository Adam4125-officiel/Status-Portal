"""
seerr_alerts.py — "somebody is waiting for you to approve something."

Polls Jellyseerr/Overseerr for requests awaiting a decision, keeps the count where the
admin panel can read it, and direct-messages the configured Discord users when a *new*
one appears.

Why this is its own module
--------------------------
It needs both `integrations` (to ask Seerr) and `discord_bot` (to deliver), and
`discord_bot` already imports `integrations` - so this cannot live in either without
creating an import cycle. A thin layer above both is the shape that works, and it's
where the next "watch something, tell someone" job belongs too (the stuck-download
alert on the roadmap is the same shape).

Why the notified-ids list is persisted
--------------------------------------
The alert is edge-triggered: it fires once, when a request first appears, and never
again while it sits there waiting. That state has to survive a restart, or every
restart would re-announce every request still pending - which is precisely the moment
an admin is least inclined to forgive a notification storm. Same reasoning as the
low-disk alert state, and the same storage: the settings table, not a module global.

The delivery limitation, stated plainly
---------------------------------------
A Discord bot may only DM a user who shares a server with it and who allows DMs from
server members. A correctly-configured user ID can still be undeliverable. That is
Discord's rule, not something this code can work around, so failures are recorded
against the task with the reason rather than being retried forever.
"""
import json
import logging

import requests

import db
import discord_bot
import integrations
import scheduler
import user_notify

_logger = logging.getLogger(__name__)

TASK_NAME = "seerr_approvals"

COUNT_SETTING = "seerr_pending_count"
REQUEST_STATES_SETTING = "seerr_request_media_states"
CHECKED_SETTING = "seerr_pending_checked_at"
NOTIFIED_SETTING = "seerr_notified_request_ids"
DM_ENABLED_SETTING = "seerr_dm_enabled"

# How many already-announced request ids to remember. Enough that a request sitting in
# the queue for a long time is never re-announced, bounded so the settings row can't
# grow without limit. Ids are numeric and increasing, so the oldest are the safe ones
# to forget.
MAX_REMEMBERED_IDS = 500


def seerr_integration():
    """The Jellyseerr/Overseerr integration to poll, or None. First enabled one, same
    convention as jellyfin_auth.auth_integration()'s fallback."""
    return next((i for i in db.list_integrations()
                 if i["kind"] == "jellyseerr" and i["enabled"]), None)


def dm_enabled():
    """Off by default. Enabling it makes the bot message people unprompted, which is a
    different thing from answering a slash command and should be chosen deliberately."""
    return db.get_setting(DM_ENABLED_SETTING, "0") == "1"


def pending_count():
    """What the last poll found, for the admin panel. Persisted rather than cached in
    memory so a restart doesn't blank it until the next run."""
    raw = db.get_setting(COUNT_SETTING, "")
    return int(raw) if raw.isdigit() else 0


def last_checked_at():
    return db.get_setting(CHECKED_SETTING, "") or None


def _notified_ids():
    raw = db.get_setting(NOTIFIED_SETTING, "")
    if not raw:
        return []
    try:
        stored = json.loads(raw)
    except ValueError:
        _logger.warning("Stored Seerr notified-id list isn't valid JSON; starting over")
        return []
    return [i for i in stored if isinstance(i, int)] if isinstance(stored, list) else []


def _remember_ids(ids):
    db.set_setting(NOTIFIED_SETTING, json.dumps(sorted(ids)[-MAX_REMEMBERED_IDS:]))


def _arrived_suffix(count):
    return f"; {count} request(s) now available" if count else ""


def format_alert(request):
    """One pending request as a DM. Plain text, not an embed: an embed in a DM buys
    nothing and build_embed() is deliberately the only function in discord_bot that
    touches discord.py's types."""
    who = f" for {request['requested_by']}" if request["requested_by"] else ""
    kind = f" ({request['media_type']})" if request["media_type"] else ""
    return (f"**Request awaiting approval**\n"
            f"> {request['title']}{kind}{who}")


# Seerr's media status 5 means "available" - the moment a request stops being a request
# and becomes something the person can actually watch, which is the only transition
# worth interrupting somebody for.
MEDIA_AVAILABLE = 5


def _stored_request_states():
    raw = db.get_setting(REQUEST_STATES_SETTING, "")
    if not raw:
        return {}
    try:
        stored = json.loads(raw)
    except ValueError:
        return {}
    return stored if isinstance(stored, dict) else {}


def track_request_progress(integration):
    """Queues a notification for whoever asked, when something they requested arrives.

    Edge-triggered against the previously-seen status per request, persisted for the
    same reason the announced-id list is: a restart must not re-announce everything
    currently sitting at "available".

    Resolving the requester is deliberately strict. Seerr gives the request's own user
    id; that maps to a Jellyfin account only through a link the user established from
    Seerr's `jellyfinUserId`. No link, no notification - guessing by name or email would
    eventually tell the wrong person about someone else's request."""
    try:
        requests_list = integrations.fetch_seerr_requests(
            integration["base_url"], integration["api_key"], limit=50)
    except (requests.RequestException, ValueError) as e:
        _logger.info("Could not read Seerr requests for progress tracking: %s", e)
        return 0

    previous = _stored_request_states()
    current, queued = {}, 0
    for entry in requests_list:
        if entry["id"] is None:
            continue
        key = str(entry["id"])
        status = entry.get("media_status")
        current[key] = status
        was = previous.get(key)
        # `was is None` means this request is new to us - a first sighting of something
        # already available must not fire, or enabling the feature would announce the
        # entire back catalogue at once.
        if was is None or status != MEDIA_AVAILABLE or was == MEDIA_AVAILABLE:
            continue
        user_id = db.user_id_for_seerr_user(entry.get("requested_by_id"))
        if not user_id:
            continue
        user_notify.notify_user(
            user_id, "request_update",
            f"{entry['title']} is now available",
            f"Something you requested has arrived: {entry['title']}.")
        queued += 1

    db.set_setting(REQUEST_STATES_SETTING, json.dumps(current))
    return queued


def run_approval_check():
    """Body of the `seerr_approvals` scheduled task."""
    integration = seerr_integration()
    if integration is None:
        raise scheduler.TaskSkipped(
            "No enabled Jellyseerr/Overseerr integration - add one under Integrations.")

    try:
        pending, total = integrations.fetch_seerr_pending(
            integration["base_url"], integration["api_key"])
    except (requests.RequestException, ValueError) as e:
        # Raised, so the scheduler records a failed run with the reason intact. The
        # stored count is deliberately left alone: a failed poll means "unknown", and
        # overwriting a real count with 0 would quietly say "nothing to approve".
        raise RuntimeError(f"Could not read pending requests: {e}")

    db.set_setting(COUNT_SETTING, str(total))
    db.set_setting(CHECKED_SETTING, db.now_iso())

    # Same poll, second question: has anything somebody asked for actually turned up?
    # Failures here are logged and skipped rather than raised - the approval count is
    # this task's primary job and must not be lost because progress tracking failed.
    arrived = track_request_progress(integration)

    known = set(_notified_ids())
    fresh = [r for r in pending if r["id"] is not None and r["id"] not in known]

    if not dm_enabled() or not discord_bot.dm_user_ids():
        # Still remember what's pending, so switching DMs on later announces what
        # arrives *next* rather than dumping the entire existing backlog into someone's
        # inbox at once.
        _remember_ids(known | {r["id"] for r in pending if r["id"] is not None})
        return f"{total} awaiting approval{_arrived_suffix(arrived)}. Discord DMs are off."

    if not fresh:
        return f"{total} awaiting approval, nothing new{_arrived_suffix(arrived)}."

    delivered, failures = 0, []
    announced = set()
    for request in fresh:
        sent, errors = discord_bot.broadcast_dm(format_alert(request))
        delivered += sent
        failures.extend(errors)
        if sent:
            # Only remembered once it actually reached somebody. A request nobody was
            # told about must stay "new", so a disconnected bot means a delayed alert
            # rather than a silently swallowed one.
            announced.add(request["id"])
    _remember_ids(known | announced)

    message = (f"{total} awaiting approval; announced {len(announced)} new "
               f"({delivered} DM(s) sent){_arrived_suffix(arrived)}")
    if failures:
        reasons = "; ".join(sorted({error for _, error in failures}))
        return f"{message}. {len(failures)} DM(s) failed: {reasons}"
    return message + "."


scheduler.register(
    TASK_NAME,
    "Seerr approval check",
    "Asks Jellyseerr/Overseerr how many requests are waiting for you to approve them, "
    "and direct-messages the configured Discord users when a new one arrives. The "
    "count is admin-only - it's operational information, not a status signal.",
    run_approval_check,
    default_interval_minutes=10,
)
