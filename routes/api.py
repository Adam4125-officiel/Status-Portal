"""Machine-readable and embeddable public endpoints: /api/status, the "load more"
fragments, the notification delegation API, the display-device API, the SVG badges
and the RSS feed. See routes/__init__.py for how this module attaches to the app."""
import logging
import secrets
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import format_datetime

from flask import Response, abort, jsonify, render_template, request
from markupsafe import escape

import db
import device_api
import integrations
import monitoring
import notifications
import user_notify
from app import (
    DEVICE_API_KEY_SETTING,
    HISTORY_PAGE_SIZE,
    NOTIFY_API_KEY_SETTING,
    SEEN_IDS_LIMIT,
    _enrich_incidents,
    _enrich_services,
    _public_history_days,
    _request_snapshot,
    app,
    compute_overall_status,
    device_api_enabled,
)

# Logs under app.py's logger name, as these views did before they moved here.
_logger = logging.getLogger("app")


@app.route("/api/status")
def api_status():
    services = _enrich_services(db.list_services())
    incidents = _enrich_incidents(db.list_incidents(limit=8, max_age_days=_public_history_days()))
    announcements = db.list_active_announcements(limit=10)
    return jsonify({
        "site_name": db.get_setting("site_name", "Server"),
        "overall": compute_overall_status(services),
        "services": [_api_service_view(s) for s in services],
        "announcements": announcements,
        "incidents": incidents,
        "maintenance_windows": db.list_public_maintenance_windows(),
    })


# Every key /api/status publishes per service. An allow-list on purpose: a column added
# to `services` later must not become public by default. External dashboards read these
# keys, so none may disappear. check_url and check_type are never published; run_target only when
# show_run_target_public is set (see _api_service_view).
_API_SERVICE_KEYS = frozenset({
    "api_health_mode", "auto_check", "auto_incident", "description", "group_name", "icon",
    "id", "ignore_in_overall_status", "last_checked", "manual_override", "name",
    "response_ms", "retry_count", "retry_interval_seconds", "show_dependencies_public",
    "show_report_button", "show_run_target_public", "slow_threshold_ms", "sort_order",
    "startup_grace_seconds", "status", "url",
    # computed per request
    "links", "uptime", "in_grace_period", "retrying", "open_reports_count",
    "run_target_label", "dependency_names",
})


def _api_service_view(service):
    """A service row as /api/status publishes it: only the keys in _API_SERVICE_KEYS,
    plus the raw run_target when show_run_target_public says so - the same opt-in the
    HTML page applies via run_target_label. check_url (the LAN address the portal
    probes) is never published. External dashboards read this endpoint, so a key must
    not go missing."""
    view = {k: v for k, v in service.items() if k in _API_SERVICE_KEYS}
    if service.get("show_run_target_public") and "run_target" in service:
        view["run_target"] = service["run_target"]
    return view


@app.route("/api/incidents/more")
def api_incidents_more():
    """HTML-fragment endpoint (not JSON, unlike /api/status above) backing the
    public page's "Load more incidents" button - this app has no client-side
    templating anywhere, so returning a ready-to-insert fragment matches that
    convention instead of introducing one just for this.

    Deliberately does NOT apply the max_age_days filter the initial page load
    uses - "load more" exists specifically to reveal incidents the initial view
    hid for being old, so re-applying the same filter here would make anything
    past the age cutoff permanently unreachable (a real bug, caught 2026-08-10:
    reported by the user testing a 3-day cutoff with 2 old + 1 recent incident,
    where the 2 old ones never appeared and "load more" always came back empty).

    Pagination is by the `seen` list - the ids the client is already displaying -
    rather than an offset or an id cursor; see db.list_incidents() for why both
    of those lost or duplicated data here. `seen` is capped at SEEN_IDS_LIMIT
    purely to bound the URL length; a personal status portal is never going to
    have a page rendering more incidents than that, and the cap failing closed
    (below) is safe rather than silently wrong.

    A `seen` key entirely missing from the query string fails closed with an
    empty response rather than returning "page 1". That case is never a real
    "load more" click - the button always sends the key - it's a stale cached
    copy of an older public_history.js still sending the previous release's
    ?offset= parameter, which the server would otherwise answer with the newest
    page over and over, appending the same incidents forever (exactly the
    runaway duplication a user hit after updating). An empty response makes such
    a button remove itself.

    A `seen` key that IS present but empty (?seen=) is different and legitimate:
    it's a real click on a page where nothing is currently visible (e.g. every
    incident is hidden by public_history_days - see index()'s incidents_hidden),
    so there's nothing yet to list in the button's data-seen-selector. Treating
    that the same as a missing key would make the "all hidden" empty state's
    load-more button permanently non-functional."""
    if "seen" not in request.args:
        return ""
    raw = request.args.get("seen", "")
    # Same skip-what-isn't-an-id rule as before, through db.parse_int(): "²" passed
    # isdigit() and then failed int(), and a 23-digit id overflowed SQLite - both 500s.
    seen = [n for n in (db.parse_int(part) for part in raw.split(",")) if n is not None]
    if len(seen) > SEEN_IDS_LIMIT:
        return ""
    incidents = _enrich_incidents(db.list_incidents(limit=HISTORY_PAGE_SIZE, exclude_ids=seen))
    return render_template("sections/_incidents_fragment.html", incidents=incidents)


@app.route("/api/maintenance/history")
def api_maintenance_history():
    """HTML-fragment endpoint backing the public page's maintenance history list -
    ended windows are never shown on the initial page load at all (see
    db.list_ended_maintenance_windows), only ever paged in here on request.

    Deliberately does NOT apply the max_age_days filter, same reasoning as
    api_incidents_more() above - the whole point of this list is to reveal
    history an admin-configured age cutoff would otherwise hide, so filtering it
    here too would make old windows permanently unreachable. Plain numeric
    offset pagination is safe here (unlike incidents) because every call into
    this endpoint uses the exact same unfiltered query - there's no
    filtered-vs-unfiltered mismatch to cause the offset to drift."""
    # ASCII digits only, clamped to what SQLite can bind: "²" and a 23-digit offset
    # were both unauthenticated 500s. Anything else (negative included) is page one.
    offset = db.parse_int(request.args.get("offset"), default=0)
    windows = db.list_ended_maintenance_windows(limit=HISTORY_PAGE_SIZE, offset=offset)
    return render_template("sections/_maintenance_fragment.html", windows=windows, history=True)


# ---------------------------------------------------------------------------
# Notification delegation API - lets a sibling app (currently: gamesportal) deliver
# its own notifications through this portal's existing Discord/email machinery
# instead of building its own. See CLAUDE.md's "Cross-repo: gamesportal" section.
#
# Authenticated with a static shared secret (NOTIFY_API_KEY_SETTING, see
# /admin/notifications) checked with secrets.compare_digest against the X-Api-Key
# header - deliberately NOT covered by the session-cookie CSRF mechanism
# (_csrf_required_for()): CSRF defends against a browser's *ambient* session
# credentials being exploited by another site, and there is no session here at all -
# a cross-site page cannot make a victim's browser send a custom X-Api-Key header it
# doesn't know, so the key itself is the entire defence. See
# tests/test_conventions.py's public_post_exceptions for where this is made explicit.
# ---------------------------------------------------------------------------
NOTIFY_API_SUBJECT_MAX_LENGTH = 200
NOTIFY_API_BODY_MAX_LENGTH = 2000


def _check_api_key(setting_name):
    """None when the request's X-Api-Key header matches the key stored under
    `setting_name`, otherwise a ready-to-return 401 response - same call pattern as
    _require_totp(). One implementation for every machine-to-machine key, so a second
    key can't quietly end up with a weaker comparison than the first."""
    stored = db.get_setting(setting_name, "")
    submitted = request.headers.get("X-Api-Key", "")
    if not stored or not submitted or not secrets.compare_digest(stored, submitted):
        return jsonify({"error": "Missing or invalid API key"}), 401
    return None


def _check_notify_api_key():
    return _check_api_key(NOTIFY_API_KEY_SETTING)


@app.route("/api/notify/admin", methods=["POST"])
def api_notify_admin():
    """Routes straight into notifications.notify() - the same admin fan-out
    (Discord webhook, ntfy, admin email list) a real incident/maintenance alert
    already uses. No new delivery code: this is just an authenticated front door onto
    the existing function, which is itself fire-and-forget and never raises - so a
    200 here means "accepted and dispatched," not "confirmed delivered", exactly like
    the "Send test notification" button."""
    blocked = _check_notify_api_key()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    subject = str(data.get("subject") or "").strip()[:NOTIFY_API_SUBJECT_MAX_LENGTH]
    body = str(data.get("body") or "").strip()[:NOTIFY_API_BODY_MAX_LENGTH]
    if not subject or not body:
        return jsonify({"error": "subject and body are required"}), 400
    notifications.notify(subject, body)
    return jsonify({"status": "sent"}), 200


@app.route("/api/notify/user", methods=["POST"])
def api_notify_user():
    """Queues a per-user notification via user_notify.notify_user() - delivered later
    by the existing user_notifications scheduled task, not from this request, same as
    every other per-user notification in this app. An unmatched/unlinked Jellyfin
    user, or one with every applicable channel switched off, is not an error here any
    more than it is anywhere else in this system - it resolves through the normal
    user_notify.contact_for()/EVENT_CHANNEL_PREFERENCE machinery and "nowhere to send
    it" is reported back as such, not as a failure."""
    blocked = _check_notify_api_key()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    jellyfin_user_id = str(data.get("jellyfin_user_id") or "").strip()
    event = str(data.get("event") or "").strip()
    subject = str(data.get("subject") or "").strip()[:NOTIFY_API_SUBJECT_MAX_LENGTH]
    body = str(data.get("body") or "").strip()[:NOTIFY_API_BODY_MAX_LENGTH]
    if not jellyfin_user_id or not event or not subject or not body:
        return jsonify({"error": "jellyfin_user_id, event, subject and body are required"}), 400
    if event not in user_notify.EVENT_CHANNEL_PREFERENCE:
        return jsonify({"error": f"Unknown event: {event}"}), 400
    if not user_notify.is_enabled():
        return jsonify({"status": "not_queued",
                        "reason": "Per-user notifications are switched off on this portal"}), 200
    notification_id = user_notify.notify_user(jellyfin_user_id, event, subject, body)
    return jsonify({"status": "queued", "notification_id": notification_id}), 200


# ---------------------------------------------------------------------------
# Display-device API - a compact, versioned, key-authenticated JSON summary for small
# wall/desk displays (Status-ESP, an ESP8266 with ~35 KB of free RAM). See CLAUDE.md's
# "Display device API" section and device_api.py for the shape and its size bounds.
#
# Deliberately NOT /api/status: that one is public, large, and published for external
# dashboards (a pinned key set), while this one has to stay under 4 KB and is allowed to
# carry data the public pages hide (server resources) - which is exactly why it needs a
# key. The key is its own secret, separate from the notification key above: this one
# can only read, that one can make the portal post to Discord and send email, and a
# display sitting on a shelf shouldn't hold the stronger credential.
#
# A GET, so the session-cookie CSRF mechanism has nothing to say about it (and
# tests/test_conventions.py only audits POST routes); there is no session either way,
# the X-Api-Key header is the whole credential.
# ---------------------------------------------------------------------------
@app.route("/api/device/summary")
def api_device_summary():
    """Everything here comes from the database or an existing background-refreshed
    cache; nothing waits on the network. `?sections=` limits it to some of
    device_api.SECTIONS, and the work for a section nobody asked for is not done - the
    resource snapshot is the most expensive thing the public pages do."""
    if not device_api_enabled():
        abort(404)
    blocked = _check_api_key(DEVICE_API_KEY_SETTING)
    if blocked:
        return blocked
    sections = device_api.parse_sections(request.args.get("sections"))
    if sections is None:
        return jsonify({"error": "Unknown sections", "valid": list(device_api.ALL_SECTIONS)}), 400
    # `detail=full` (the Jelly Portal plugin) gives the site name and the services, incidents,
    # maintenance and announcements sections room to breathe; anything but the exact value `full`
    # is the compact answer a display gets. See device_api.FULL.
    detail_full = device_api.parse_detail(request.args.get("detail"))

    services = db.list_services()
    data = {}
    if "incidents" in sections:
        data["open_incident_count"] = db.count_open_incidents()
        data["incidents"] = db.list_incidents(limit=device_api.incident_limit(detail_full), open_only=True)
    if "maintenance" in sections:
        data["maintenance"] = db.list_public_maintenance_windows()
    if "announcements" in sections:
        data["announcements"] = db.list_active_announcements()
    if "resources" in sections:
        try:
            # Not gated by the show_public_* switches: this is a keyed endpoint, and the
            # holder of the key is the admin's own display. The public pages' switches
            # decide what *visitors* see; the key decides what this sees.
            data["resources"] = _request_snapshot()
        except Exception:
            # One failing section must not take the rest of the summary down with it;
            # the response says "unavailable" (null) rather than leaving it out.
            _logger.exception("Could not read the resource snapshot for the device API")
    if "vms" in sections:
        try:
            # The background-refreshed cache the public /vms page reads, no PowerShell here.
            # Like the resources, not gated by show_public_vms: the key is the gate.
            data["vms"] = monitoring.get_cached_vm_snapshot()
        except Exception:
            _logger.exception("Could not read the VM list for the device API")

    with_jellyfin = request.args.get("jellyfin", "").strip() == "1"
    summary = device_api.build_summary(
        sections, now=datetime.now(timezone.utc), site=db.get_setting("site_name", "Server"),
        overall=compute_overall_status(services), services=services,
        # `services=all` also lists the operational services (Status-ESP pages through them).
        all_services=(request.args.get("services", "").strip().lower() == "all"),
        # `resources=all` also sends every disk and the GPUs (Status-ESP pages through them).
        all_resources=(request.args.get("resources", "").strip().lower() == "all"),
        # `jellyfin=1` adds what Jellyfin is doing, read from the background-refreshed cache
        # (no outbound call); it comes with any `sections`.
        with_jellyfin=with_jellyfin,
        jellyfin=integrations.get_cached_jellyfin_activity() if with_jellyfin else None,
        detail_full=detail_full, **data)
    response = Response(device_api.dumps(summary), mimetype="application/json")
    response.headers["Content-Type"] = "application/json; charset=utf-8"
    # A display polls this to learn what is true *now*.
    response.headers["Cache-Control"] = "no-store"
    return response


STATUS_BADGE_LABEL = {"operational": "operational", "degraded": "degraded",
                       "maintenance": "maintenance", "down": "down", "slow": "slow"}
STATUS_BADGE_COLOR = {"operational": "#3ddc97", "degraded": "#ffb545",
                       "maintenance": "#a08bff", "down": "#ff5470", "slow": "#ffb545"}


def _render_badge_svg(label, value, color):
    """Hand-rolled shields.io-style two-segment pill - no external service call (this
    stays fully self-contained/offline, consistent with the rest of the app), no
    external font/library dependency. Character-count width estimate is approximate
    but good enough for short status words."""
    label = str(escape(label))
    value = str(escape(value))
    label_width = len(label) * 6 + 20
    value_width = len(value) * 6 + 20
    total_width = label_width + value_width
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{total_width}" height="20" role="img" aria-label="{label}: {value}">
  <linearGradient id="s" x2="0" y2="100%">
    <stop offset="0" stop-color="#bbb" stop-opacity=".1"/>
    <stop offset="1" stop-opacity=".1"/>
  </linearGradient>
  <clipPath id="r"><rect width="{total_width}" height="20" rx="3" fill="#fff"/></clipPath>
  <g clip-path="url(#r)">
    <rect width="{label_width}" height="20" fill="#2b2f3a"/>
    <rect x="{label_width}" width="{value_width}" height="20" fill="{color}"/>
    <rect width="{total_width}" height="20" fill="url(#s)"/>
  </g>
  <g fill="#fff" text-anchor="middle" font-family="DejaVu Sans,Verdana,Geneva,sans-serif" font-size="11">
    <text x="{label_width / 2}" y="14">{label}</text>
    <text x="{label_width + value_width / 2}" y="14">{value}</text>
  </g>
</svg>'''


@app.route("/badge.svg")
def badge_overall():
    overall = compute_overall_status(db.list_services())
    svg = _render_badge_svg(db.get_setting("site_name", "status"),
                             STATUS_BADGE_LABEL[overall], STATUS_BADGE_COLOR[overall])
    return Response(svg, mimetype="image/svg+xml", headers={"Cache-Control": "no-cache"})


@app.route("/badge/<int:service_id>.svg")
def badge_service(service_id):
    service = db.get_service(service_id)
    if not service:
        return Response("Service not found", status=404, mimetype="text/plain")
    svg = _render_badge_svg(service["name"], STATUS_BADGE_LABEL[service["status"]],
                             STATUS_BADGE_COLOR[service["status"]])
    return Response(svg, mimetype="image/svg+xml", headers={"Cache-Control": "no-cache"})


def _rss_date(iso_str):
    """Converts one of this app's ISO 8601 timestamps to the RFC 822 format RSS
    requires. Falls back to 'now' for anything unparseable rather than failing the
    whole feed over one bad row."""
    try:
        dt = datetime.fromisoformat(iso_str)
    except (ValueError, TypeError):
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return format_datetime(dt)


@app.route("/feed.xml")
def feed():
    """RSS 2.0 feed of incidents + announcements, so they can be followed in a feed
    reader instead of checking the page or setting up Discord/ntfy. Built with
    ElementTree (not string concatenation) so titles/messages are escaped correctly
    regardless of what characters an admin puts in them."""
    site_name = db.get_setting("site_name", "Server")
    base_url = request.url_root.rstrip("/")

    entries = []
    incidents = db.list_incidents(limit=20)
    updates_by_incident = db.list_incident_updates_for_incidents([i["id"] for i in incidents])
    for i in incidents:
        title = f"{i['service_names']}: " if i["service_names"] else ""
        title += f"[{i['status']}] {i['title']}"
        description = i["description"] or ""
        updates = updates_by_incident[i["id"]]
        if updates:
            description += "\n\n" + "\n".join(f"({u['status']}) {u['message']}" for u in updates)
        entries.append({"title": title, "description": description,
                         "date": i["resolved_at"] or i["started_at"], "guid": f"incident-{i['id']}"})
    # Active only: an announcement scheduled for next week is unpublished content, and
    # a feed is as public as the page is.
    for a in db.list_active_announcements(limit=20):
        entries.append({"title": f"Announcement: {a['title']}", "description": a["message"],
                         "date": a["created_at"], "guid": f"announcement-{a['id']}"})
    entries.sort(key=lambda e: e["date"], reverse=True)

    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = f"{site_name} status"
    ET.SubElement(channel, "link").text = f"{base_url}/"
    ET.SubElement(channel, "description").text = f"Incidents and announcements for {site_name}."
    for entry in entries[:30]:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = entry["title"]
        ET.SubElement(item, "description").text = entry["description"]
        ET.SubElement(item, "pubDate").text = _rss_date(entry["date"])
        guid = ET.SubElement(item, "guid", isPermaLink="false")
        guid.text = f"{base_url}/#{entry['guid']}"

    xml_bytes = ET.tostring(rss, encoding="utf-8", xml_declaration=True)
    return Response(xml_bytes, mimetype="application/rss+xml")
