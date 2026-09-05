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
ISSUE_STATES_SETTING = "seerr_issue_states"

# How many already-announced request ids to remember. Enough that a request sitting in
# the queue for a long time is never re-announced, bounded so the settings row can't
# grow without limit. Ids are numeric and increasing, so the oldest are the safe ones
# to forget.
MAX_REMEMBERED_IDS = 500


def seerr_integration():
    """Whichever Seerr the admin selected - see integrations.seerr_integration().

    Deliberately delegated rather than re-implemented here: three copies of "the first
    enabled one" is three chances for the search page, the notifier and the diagnostic
    to end up talking to different servers without anyone noticing."""
    return integrations.seerr_integration()


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


def _previous_state(was):
    """Reads one request's previously-stored state, tolerating the old shape.

    Before the approved/declined tracking below was added, a stored value was just the
    bare media_status int. A stored settings row from before this change must not crash
    the next poll - it's read as "no request_status recorded yet" instead, which is the
    same as a fresh sighting for that one field."""
    if isinstance(was, dict):
        return was.get("media_status"), was.get("request_status")
    return was, None


def track_request_progress(integration):
    """Queues a notification for whoever asked, when something they requested arrives,
    or when it's approved or declined.

    Edge-triggered against the previously-seen state per request, persisted for the
    same reason the announced-id list is: a restart must not re-announce everything
    currently sitting at "available"/"approved".

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
        media_status = entry.get("media_status")
        request_status = entry.get("request_status_key")
        current[key] = {"media_status": media_status, "request_status": request_status}
        was_media, was_request = _previous_state(previous.get(key))
        user_id = None

        # `was_media is None` means this request is new to us - a first sighting of
        # something already available must not fire, or enabling the feature would
        # announce the entire back catalogue at once. Same reasoning for request_status.
        if was_media is not None and media_status == MEDIA_AVAILABLE and was_media != MEDIA_AVAILABLE:
            user_id = db.user_id_for_seerr_user(entry.get("requested_by_id"))
            if user_id:
                user_notify.notify_user(
                    user_id, "request_update",
                    f"{entry['title']} is now available",
                    f"Something you requested has arrived: {entry['title']}.")
                queued += 1
        elif (was_request == "pending" and request_status in ("approved", "declined")):
            user_id = db.user_id_for_seerr_user(entry.get("requested_by_id"))
            if user_id:
                verb = "approved" if request_status == "approved" else "declined"
                user_notify.notify_user(
                    user_id, "seerr_event",
                    f"Your request for {entry['title']} was {verb}",
                    f"Your request for {entry['title']} has been {verb}.")
                queued += 1

    db.set_setting(REQUEST_STATES_SETTING, json.dumps(current))
    return queued


def _stored_issue_states():
    raw = db.get_setting(ISSUE_STATES_SETTING, "")
    if not raw:
        return {}
    try:
        stored = json.loads(raw)
    except ValueError:
        return {}
    return stored if isinstance(stored, dict) else {}


def format_issue_alert(issue, is_new):
    """One issue update as a DM, plain text like format_alert() - see there for why."""
    verb = "New issue" if is_new else f"Issue update ({issue['status']})"
    who = f" from {issue['created_by_name']}" if issue["created_by_name"] else ""
    return f"**{verb}: {issue['issue_type']}**\n> {issue['title']}{who}"


def track_issue_updates(integration):
    """Queues a notification for the person who opened an issue when it's updated, and
    DMs the admin(s) about any new or changed issue - admin-directed, so it goes
    through discord_bot.broadcast_dm() (gated by dm_enabled(), same as an approval
    alert) rather than a per-user preference, which doesn't apply to messages
    addressed to the admin.

    Same edge-triggered/persisted-state shape as track_request_progress(): a restart
    must not re-announce every issue that was already open."""
    try:
        issues = integrations.fetch_seerr_issues(integration["base_url"], integration["api_key"])
    except (requests.RequestException, ValueError) as e:
        _logger.info("Could not read Seerr issues for update tracking: %s", e)
        return 0, []

    previous = _stored_issue_states()
    current, admin_alerts = {}, []
    for issue in issues:
        key = str(issue["id"])
        current[key] = {"status": issue["status"], "comment_count": issue["comment_count"]}
        was = previous.get(key)
        is_new = was is None
        changed = was is not None and (was.get("status") != issue["status"]
                                        or was.get("comment_count") != issue["comment_count"])
        if not is_new and not changed:
            continue

        admin_alerts.append(format_issue_alert(issue, is_new))
        if not is_new:
            # A brand-new issue is already implicitly "reported by" its creator - only
            # an update (status change or new comment) is news to them specifically.
            user_id = db.user_id_for_seerr_user(issue["created_by_id"])
            if user_id:
                user_notify.notify_user(
                    user_id, "seerr_event", f"Update on your report: {issue['title']}",
                    f"Your issue on {issue['title']} is now {issue['status']}.")

    db.set_setting(ISSUE_STATES_SETTING, json.dumps(current))
    return len(admin_alerts), admin_alerts


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
        #
        # TaskUnavailable rather than a bare RuntimeError so the log gets one line
        # saying what Seerr answered, instead of a chained traceback through this
        # module that reads like the portal crashed. Seerr going 502 behind a tunnel
        # for a minute is not a portal fault and shouldn't look like one.
        raise scheduler.TaskUnavailable(f"Could not read pending requests: {e}")

    db.set_setting(COUNT_SETTING, str(total))
    db.set_setting(CHECKED_SETTING, db.now_iso())

    # Same poll, more questions: has anything somebody asked for actually turned up or
    # been decided, and has anything happened on an issue? Failures in either are
    # logged and skipped rather than raised - the approval count is this task's primary
    # job and must not be lost because one of these two failed.
    arrived = track_request_progress(integration)
    issue_count, issue_alerts = track_issue_updates(integration)

    known = set(_notified_ids())
    fresh = [r for r in pending if r["id"] is not None and r["id"] not in known]

    if not dm_enabled() or not discord_bot.dm_user_ids():
        # Still remember what's pending, so switching DMs on later announces what
        # arrives *next* rather than dumping the entire existing backlog into someone's
        # inbox at once. Issue state is tracked unconditionally inside
        # track_issue_updates() itself, so there's nothing extra to remember here.
        _remember_ids(known | {r["id"] for r in pending if r["id"] is not None})
        return (f"{total} awaiting approval{_arrived_suffix(arrived)}"
                f"{f'; {issue_count} issue update(s)' if issue_count else ''}. "
                f"Discord DMs are off.")

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
    for alert in issue_alerts:
        sent, errors = discord_bot.broadcast_dm(alert)
        delivered += sent
        failures.extend(errors)

    if not fresh and not issue_alerts:
        return f"{total} awaiting approval, nothing new{_arrived_suffix(arrived)}."

    message = (f"{total} awaiting approval; announced {len(announced)} new"
               f"{f', {issue_count} issue update(s)' if issue_count else ''} "
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
