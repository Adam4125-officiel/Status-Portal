"""
app.py — Personal server status portal.
Run with: python app.py
Admin panel: /admin (password is set on first launch)
"""
import io
import logging
import os
import platform
import re
import secrets
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from email.utils import format_datetime
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash, Response, abort, send_file
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from markupsafe import Markup, escape
import requests

import config
import db
import discord_bot
import integrations
import logging_setup
import monitoring
import notifications
import twofactor
import updater

_logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = config.SECRET_KEY
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=config.FORCE_HTTPS_COOKIES,
    MAX_CONTENT_LENGTH=2 * 1024 * 1024,  # 2 MB - plenty for any form on this app
)

if config.BEHIND_PROXY:
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# Reference point for each service's startup_grace_seconds - "time since the portal
# process started", not since the service itself booted (good enough since they're
# expected to start around the same time). monotonic() so a system clock change
# can't skew it.
_APP_START = time.monotonic()

# Service ids currently mid-retry-loop in _check_service_status() (see below) - read
# by index() so a public-page hit during that window can show "retrying" instead of
# silently keeping the last-known status.
_retry_in_progress = set()


@app.after_request
def set_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "script-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; "
        "frame-ancestors 'none'"
    )
    response.headers["Server"] = "status-portal"  # don't advertise the underlying framework/server
    return response


@app.errorhandler(404)
def handle_not_found(e):
    return render_template("error.html", code=404, message="Page not found."), 404


@app.errorhandler(400)
def handle_bad_request(e):
    return render_template("error.html", code=400, message="Invalid or expired form submission. "
                            "Please reload the page and try again."), 400


@app.errorhandler(413)
def handle_too_large(e):
    return render_template("error.html", code=413, message="That file is too large "
                            f"(max {app.config['MAX_CONTENT_LENGTH'] // (1024 * 1024)} MB)."), 413


@app.errorhandler(500)
def handle_server_error(e):
    _logger.exception("Unhandled exception in request %s %s", request.method, request.path)
    return render_template("error.html", code=500, message="Something went wrong."), 500


# ---------------------------------------------------------------------------
# CSRF protection
# ---------------------------------------------------------------------------
# Every state-changing route in this app is a POST under /admin/ (including
# /admin/login itself), relying until now solely on SESSION_COOKIE_SAMESITE=Lax to
# stop cross-site form submissions. This adds a second, independent layer: a
# per-session token embedded in every form (via the csrf_token() Jinja global,
# rendered into a <meta> tag in base.html and injected into every <form> by
# static/js/csrf.js - not hand-added to each of the ~16 templates with a POST form,
# to avoid the very real risk of missing one) and checked against the session on
# every POST under /admin/. Bypassed when app.testing is set (the `client` fixture
# in tests/conftest.py sets this) since the test client posts raw form dicts, never
# simulating the browser-side JS that injects the token - see
# test_csrf_protection_rejects_missing_or_wrong_token for a dedicated test of the
# actual mechanism using a client that does NOT set TESTING.
def _get_csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_hex(32)
        session["csrf_token"] = token
    return token


app.jinja_env.globals["csrf_token"] = _get_csrf_token


# ---------------------------------------------------------------------------
# Custom logo
# ---------------------------------------------------------------------------
# This app's first (and only) file upload - kept deliberately narrow: a fixed
# filename per upload ("logo.<ext>", never the visitor/admin-supplied original
# name) under a dedicated directory, so there's no path-traversal surface and at
# most one logo file ever exists on disk at a time. static/uploads/ is gitignored
# the same way instance/ is - it's runtime-created state, not tracked content.
LOGO_ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "svg", "webp", "ico"}
LOGO_UPLOAD_DIR = os.path.join(app.root_path, "static", "uploads")


@app.template_global()
def asset_url(filename):
    """url_for('static', ...) plus a ?v=<mtime> cache-buster - use this for every
    CSS/JS reference in a template, never bare url_for('static', ...).

    Added 2026-08-10 after a real, user-hit bug that this app's "unzip over the
    old folder" update process makes very easy to reproduce: a released JS change
    (public_history.js switching its pagination from ?offset= to ?before_id=)
    was silently shadowed by the browser's cached copy of the *previous*
    release's file at the identical URL. The stale script kept calling the new
    endpoint with the old parameter, which the server ignored - so every click
    re-returned the newest page and appended the same incidents forever. The
    filename never changes between releases, so the URL has to.

    mtime is a local stat (same class of call as reading a DB row, not the kind
    of slow I/O the no-blocking-in-a-request-handler rule is about), and it
    changes exactly when an updated file is extracted over the old one - which
    is precisely when a cached copy must be invalidated."""
    path = os.path.join(app.root_path, "static", filename)
    try:
        version = int(os.path.getmtime(path))
    except OSError:
        return url_for("static", filename=filename)
    return f"{url_for('static', filename=filename)}?v={version}"


@app.context_processor
def _inject_branding():
    """Exposes the current logo filename/cache-busting version to every template
    (admin included, for the favicon) without every route threading it through
    manually - same idea as csrf_token() being a Jinja global rather than
    hand-passed everywhere. A stale setting pointing at a since-deleted file
    silently falls back to "no logo" rather than a broken <img>/broken favicon."""
    filename = db.get_setting("site_logo_filename", "")
    version = None
    if filename:
        try:
            version = int(os.path.getmtime(os.path.join(LOGO_UPLOAD_DIR, filename)))
        except OSError:
            filename = ""
    return {"site_logo_filename": filename, "site_logo_version": version}


@app.context_processor
def _inject_admin_badges():
    """Unread problem-report count for the admin nav's "Reports" badge - scoped to
    admin pages only (not computed on every public page load) since it's only ever
    displayed there."""
    if not request.path.startswith("/admin/"):
        return {}
    # Reads the update cache only (never checks GitHub here) - a miss or a failed
    # check simply means no badge, exactly like "no unread reports".
    cached = updater.get_cached_update_status()
    return {
        "unread_reports_count": db.count_unread_problem_reports(),
        "update_available": bool(cached and cached.get("update_available")),
    }


@app.before_request
def _check_csrf():
    if app.testing or request.method != "POST" or not request.path.startswith("/admin/"):
        return
    expected = session.get("csrf_token")
    submitted = request.form.get("csrf_token")
    if not expected or not submitted or not secrets.compare_digest(expected, submitted):
        abort(400)


_URL_RE = re.compile(r"(https?://[^\s<]+)")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


@app.template_filter("richtext")
def richtext_filter(text):
    """Free text (announcements, info page) -> minimal, safe HTML.
    Supports **bold**, auto-clickable links, line breaks."""
    if not text:
        return ""
    out = str(escape(text))
    out = _BOLD_RE.sub(r"<strong>\1</strong>", out)
    out = _URL_RE.sub(lambda m: f'<a href="{m.group(1)}" target="_blank" rel="noopener">{m.group(1)}</a>', out)
    out = out.replace("\n", "<br>")
    return Markup(out)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("admin_login", next=request.path))
        return f(*args, **kwargs)
    return wrapper


def is_first_run():
    return db.get_setting("admin_password_hash") is None


def _require_totp(failure_message, redirect_endpoint):
    """Step-up re-authentication gate for this app's most destructive actions (host
    restart/shutdown, restarting the app or the Discord bot, installing an update).

    Returns None when the caller may proceed - either because 2FA isn't enabled at
    all, or because a fresh valid code was supplied - and a ready-to-return redirect
    response when it must not. So every call site is:

        blocked = _require_totp("... cancelled.", "admin_somewhere")
        if blocked:
            return blocked

    Not a decorator: each of these routes validates its own action/component
    parameter *before* asking for a code, and each redirects somewhere different
    with its own wording, none of which a decorator wrapping the whole view could
    express without becoming more configuration than it saves.

    Why this exists as one function rather than inline at each site: the check is
    "does a stolen/replayed session cookie alone suffice to do this?", and three
    hand-maintained copies of that check is three chances for one of them to quietly
    stop matching the others."""
    if not twofactor.is_enabled():
        return None
    code = request.form.get("totp_code", "")
    secret = db.get_setting("admin_totp_secret")
    if twofactor.verify_code(secret, code):
        return None
    flash(failure_message, "error")
    return redirect(url_for(redirect_endpoint))


# Global (not per-IP) login lockout: this is a single-admin app, so there's no
# legitimate concurrent "other users" to inconvenience, and it sidesteps relying on
# a client IP that's only trustworthy when config.BEHIND_PROXY is correctly set.
_login_state = {"failures": 0, "locked_until": 0.0}
LOGIN_LOCKOUT_THRESHOLD = 5
LOGIN_LOCKOUT_SECONDS = 300


def _login_locked():
    return time.time() < _login_state["locked_until"]


def _register_login_failure():
    _login_state["failures"] += 1
    if _login_state["failures"] >= LOGIN_LOCKOUT_THRESHOLD:
        _login_state["locked_until"] = time.time() + LOGIN_LOCKOUT_SECONDS
        _login_state["failures"] = 0


def _register_login_success():
    _login_state["failures"] = 0
    _login_state["locked_until"] = 0.0


# Global (not per-IP, same reasoning as _login_state above) rate limit on the
# public "report a problem" form (see the /report route) - this is the app's first
# public-facing POST route besides /admin/login, so it needs its own light
# anti-abuse rather than relying on the /admin/-scoped CSRF hook, which doesn't
# apply here. No external rate-limiting library is pulled in just for this one
# route - a hand-rolled counter is enough for a personal single-server app.
_report_state = {"count": 0, "window_start": 0.0}
REPORT_RATE_LIMIT = 10
REPORT_RATE_WINDOW_SECONDS = 3600
REPORT_MIN_SECONDS_TO_FILL = 3


def _report_rate_limited():
    now = time.time()
    if now - _report_state["window_start"] > REPORT_RATE_WINDOW_SECONDS:
        _report_state["window_start"] = now
        _report_state["count"] = 0
    return _report_state["count"] >= REPORT_RATE_LIMIT


def _register_report_submission():
    _report_state["count"] += 1


# ---------------------------------------------------------------------------
# Public pages
# ---------------------------------------------------------------------------
def _run_target_label(run_target):
    """Human label for services.run_target ('' / 'host' / 'vm:<name>') - only
    called when the admin has opted a service into showing this publicly
    (show_run_target_public); see admin_services.html for the admin-side
    equivalent, which renders this inline in Jinja instead."""
    if run_target == "host":
        return f"Host ({platform.node()})"
    if run_target.startswith("vm:"):
        return f"VM: {run_target[3:]}"
    return None


def _enrich_services(services):
    open_reports = db.count_open_reports_by_service()
    service_names = {s["id"]: s["name"] for s in services}
    for s in services:
        s["links"] = db.list_service_links(s["id"])
        s["uptime"] = db.get_uptime_percentage(s["id"])
        s["in_grace_period"] = _within_grace_period(s)
        s["retrying"] = s["id"] in _retry_in_progress
        s["open_reports_count"] = open_reports.get(s["id"], 0)
        s["run_target_label"] = _run_target_label(s["run_target"]) if s["show_run_target_public"] else None
        s["dependency_names"] = [
            service_names.get(dep_id, "?") for dep_id in db.get_service_dependencies(s["id"])
        ] if s["show_dependencies_public"] else []
    return services


def _enrich_incidents(incidents):
    for i in incidents:
        i["updates"] = db.list_incident_updates(i["id"])
    return incidents


ADMIN_RESOURCE_VISIBLE = {"cpu": True, "memory": True, "disks": True,
                          "network": True, "gpu": True}

_PUBLIC_RESOURCE_KEYS = ["show_public_cpu", "show_public_memory", "show_public_disks",
                         "show_public_network", "show_public_gpu",
                         "show_public_vms", "show_public_highload", "show_public_jellyfin_tasks"]


def _public_resource_visibility():
    return {key[len("show_public_"):]: db.get_setting(key, "0") == "1" for key in _PUBLIC_RESOURCE_KEYS}


def _public_history_days():
    """Blank/unset (the default) means "show everything", unchanged from before this
    setting existed - an admin has to opt into hiding old resolved incidents, same
    "off by default" convention as every other opt-in behavior toggle in this app."""
    raw = db.get_setting("public_history_days", "")
    return int(raw) if raw.isdigit() else None


HISTORY_PAGE_SIZE = 10

# Upper bound on how many already-displayed incident ids /api/incidents/more will
# accept in one request, purely to keep the URL a sane length - see that route.
SEEN_IDS_LIMIT = 500


# Pre-fill-only defaults for the "New service" form (Settings -> Service defaults) -
# deliberately not a live-cascading override: once a service is created its stored
# column values are normal per-service values like any other, changing these later
# never retroactively affects services that already exist. See ROADMAP.md.
SERVICE_DEFAULT_FIELDS = ["slow_threshold_ms", "startup_grace_seconds", "retry_count", "retry_interval_seconds"]


def _service_defaults():
    defaults = {key: db.get_setting(f"service_default_{key}", "") for key in SERVICE_DEFAULT_FIELDS}
    defaults["retry_interval_seconds"] = defaults["retry_interval_seconds"] or "5"
    defaults["auto_incident"] = db.get_setting("service_default_auto_incident", "1") == "1"
    defaults["api_health_mode"] = db.get_setting("service_default_api_health_mode", "off")
    if defaults["api_health_mode"] not in db.API_HEALTH_MODES:
        defaults["api_health_mode"] = "off"
    return defaults


# The public page's reorderable content blocks - the topbar/status-hero/footer stay
# fixed (they're page chrome, not content). Each key maps 1:1 to a
# templates/sections/<key>.html partial, which owns its own "is there anything to
# show" guard - index() doesn't filter this list by content, it's included
# unconditionally in whatever order is configured, same as today's fixed order.
PUBLIC_SECTIONS = [
    ("announcements", "Announcements"),
    ("services", "Services"),
    ("incidents", "Incidents & maintenance"),
    ("info", "Practical info"),
    ("resources", "Server resources"),
    ("vms", "Virtual machines"),
    ("jellyfin_activity", "Jellyfin activity"),
]
_DEFAULT_SECTION_ORDER = [key for key, _ in PUBLIC_SECTIONS]


def _public_section_order():
    """Admin-configured order, stored as a comma-separated settings value. Any
    stored key that's no longer recognized is silently dropped, and any valid key
    missing from the stored value (e.g. a new section added after the admin last
    saved this) is appended at the end in its default position - so nothing
    silently disappears from the page just because the setting predates it."""
    raw = db.get_setting("public_layout_order", "")
    stored = [k.strip() for k in raw.split(",") if k.strip()]
    valid_keys = {key for key, _ in PUBLIC_SECTIONS}
    order = [k for k in stored if k in valid_keys]
    order += [k for k in _DEFAULT_SECTION_ORDER if k not in order]
    return order


# Integration statuses are checked in the background (see run_health_checks) and cached
# here, keyed by integration id. Nothing in the request path ever calls
# integrations.fetch_integration_status() directly - a slow/unreachable integration
# (confirmed: ~10s for an unresponsive *Arr host, due to its v3->v1 fallback, each with
# a 5s timeout) would otherwise block every single page load, including every public
# auto-refresh cycle. That was a real bug: the public page appeared to be "stuck
# refreshing" because it was, in fact, blocked on a slow outbound HTTP call every time.
_integration_status_cache = {}

def _integration_severity(status):
    if not status["reachable"]:
        return "crit"
    if any(i["level"] == "error" for i in status["issues"]):
        return "crit"
    if status["issues"]:
        return "warn"
    return "ok"


def _refresh_integration_cache():
    for integ in db.list_integrations():
        if not integ["enabled"]:
            continue
        previous = _integration_status_cache.get(integ["id"])
        try:
            status = integrations.fetch_integration_status(integ)
        except Exception as e:
            status = {"reachable": False, "version": None, "issues": [], "error": str(e)}
        _integration_status_cache[integ["id"]] = {"status": status, "checked_at": db.now_iso()}
        if integ["auto_incident"] and integ["service_id"] and previous is not None:
            linked_service = db.get_service(integ["service_id"])
            if not linked_service or not _within_grace_period(linked_service):
                _handle_integration_incident_lifecycle(integ, previous["status"]["reachable"], status["reachable"])

    # High-load detection wants a couple of Jellyfin-specific signals (active
    # transcode count, running scheduled tasks like trickplay generation) beyond
    # plain reachability - refreshed here (background loop) for the same reason as
    # everything else in this function: never queried live from a request handler.
    jellyfin = next((i for i in db.list_integrations() if i["kind"] == "jellyfin" and i["enabled"]), None)
    if jellyfin:
        try:
            integrations.refresh_jellyfin_activity_cache(jellyfin["base_url"], jellyfin["api_key"])
        except Exception:
            _logger.exception("Jellyfin activity refresh failed")


def _attach_integration_status(services):
    """Reads the cache only - see the module-level note above for why."""
    for s in services:
        linked = db.list_integrations_for_service(s["id"])
        entry = _integration_status_cache.get(linked[0]["id"]) if linked else None
        s["integration_status"] = entry["status"] if entry else None
        s["integration_severity"] = _integration_severity(entry["status"]) if entry else None
    return services


def _group_services(services):
    """Groups services by group_name, preserving sort_order within a group and the
    order groups first appear. Ungrouped services (group_name == '') share a group
    with an empty name, which the template renders without a subheading - so setups
    that never use grouping look exactly as before."""
    groups = []
    index = {}
    for s in services:
        name = (s.get("group_name") or "").strip()
        if name not in index:
            index[name] = {"name": name, "services": []}
            groups.append(index[name])
        index[name]["services"].append(s)
    return groups


@app.route("/")
def index():
    services = _attach_integration_status(_enrich_services(db.list_services()))
    groups = _group_services(services)
    announcements = db.list_announcements(limit=10)
    incidents = _enrich_incidents(db.list_incidents(limit=8, max_age_days=_public_history_days()))
    # Distinguishes "no incidents exist" from "incidents exist but are all older
    # than public_history_days" - the unfiltered existence check only runs when
    # the filtered list is already empty, so this adds no query in the common case.
    incidents_hidden = not incidents and bool(db.list_incidents(limit=1))
    maintenance_windows = db.list_public_maintenance_windows()
    info = db.get_info_page()
    overall = compute_overall_status(services)
    site_name = db.get_setting("site_name", "Server")
    visible = _public_resource_visibility()
    show_any_resource = any(visible[k] for k in ("cpu", "memory", "disks", "network", "gpu"))
    # High-load detection needs a live snapshot even if none of the resource cards
    # themselves are set to display - the badge is a separate toggle from them.
    snapshot = monitoring.get_resource_snapshot() if (show_any_resource or visible["highload"]) else None
    vms = monitoring.get_cached_vm_snapshot() if visible["vms"] else []
    high_load = integrations.evaluate_high_load(snapshot) if (visible["highload"] and snapshot) \
        else {"active": False, "reasons": []}
    jellyfin_activity = integrations.get_cached_jellyfin_activity() if visible["jellyfin_tasks"] else None
    return render_template("index.html", services=services, groups=groups, announcements=announcements,
                            incidents=incidents, incidents_hidden=incidents_hidden,
                            maintenance_windows=maintenance_windows, info=info, overall=overall,
                            refresh_seconds=config.PUBLIC_REFRESH_SECONDS,
                            resource_refresh_seconds=config.RESOURCE_REFRESH_SECONDS,
                            site_name=site_name, visible=visible, show_any_resource=show_any_resource,
                            snapshot=snapshot, vms=vms, high_load=high_load, jellyfin_activity=jellyfin_activity,
                            section_order=_public_section_order(), repo_url=updater.REPO_URL)


@app.route("/api/status")
def api_status():
    services = _enrich_services(db.list_services())
    incidents = _enrich_incidents(db.list_incidents(limit=8, max_age_days=_public_history_days()))
    announcements = db.list_announcements(limit=10)
    return jsonify({
        "site_name": db.get_setting("site_name", "Server"),
        "overall": compute_overall_status(services),
        "services": services,
        "announcements": announcements,
        "incidents": incidents,
        "maintenance_windows": db.list_public_maintenance_windows(),
    })


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
    seen = [int(part) for part in raw.split(",") if part.strip().isdigit()]
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
    offset = request.args.get("offset", type=int, default=0)
    windows = db.list_ended_maintenance_windows(limit=HISTORY_PAGE_SIZE, offset=offset)
    return render_template("sections/_maintenance_fragment.html", windows=windows, history=True)


def compute_overall_status(services):
    """A service with ignore_in_overall_status set still shows its own real status on
    its own card - it's only excluded here, from the aggregate banner. E.g. an
    ignored service that's down alongside otherwise-healthy services must still show
    "All Services Up" overall. Any place this precedence list is duplicated
    (discord_bot._overall_status()) needs the same filter - grep for "degraded"
    across the codebase before assuming every status-aware spot is covered."""
    services = [s for s in services if not s.get("ignore_in_overall_status")]
    if not services:
        return "operational"
    statuses = [s["status"] for s in services]
    if "down" in statuses:
        return "down"
    if "degraded" in statuses:
        return "degraded"
    if "maintenance" in statuses:
        return "maintenance"
    if "slow" in statuses:
        return "slow"
    return "operational"


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
    for i in db.list_incidents(limit=20):
        title = f"{i['service_names']}: " if i["service_names"] else ""
        title += f"[{i['status']}] {i['title']}"
        description = i["description"] or ""
        updates = db.list_incident_updates(i["id"])
        if updates:
            description += "\n\n" + "\n".join(f"({u['status']}) {u['message']}" for u in updates)
        entries.append({"title": title, "description": description,
                         "date": i["resolved_at"] or i["started_at"], "guid": f"incident-{i['id']}"})
    for a in db.list_announcements(limit=20):
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


@app.route("/report", methods=["GET", "POST"])
def report_problem():
    """Public "report a problem" form - deliberately separate from the admin-authored
    incident/maintenance system (this is a visitor telling the admin something looks
    wrong, not the admin recording a known outage). The app's first public POST route
    besides /admin/login, so it isn't covered by the /admin/-scoped CSRF hook - CSRF
    doesn't meaningfully apply here anyway (there's no authenticated session/privilege
    being exercised, so a cross-site submission achieves nothing an attacker couldn't
    already do by POSTing directly), but it does need its own light anti-abuse:
    a honeypot field, a minimum-time-to-fill check, and a global rate limit
    (see _report_rate_limited above)."""
    services = db.list_services()
    preselect_service_id = request.args.get("service_id", type=int)
    site_name = db.get_setting("site_name", "Server")
    if request.method == "POST":
        # A real visitor never fills this field - it's hidden from sighted users via
        # CSS, not display:none/hidden (which some bots skip). Silently "succeed"
        # without writing a row, so a bot gets no signal it was caught.
        if request.form.get("website"):
            flash("Thanks — your report has been submitted.", "success")
            return redirect(url_for("report_problem"))
        rendered_at = session.get("report_form_rendered_at", 0)
        if time.time() - rendered_at < REPORT_MIN_SECONDS_TO_FILL:
            flash("That was fast — please wait a moment and try again.", "error")
            return redirect(url_for("report_problem"))
        if _report_rate_limited():
            flash("Too many reports submitted recently — please try again later.", "error")
            return redirect(url_for("report_problem"))
        message = request.form.get("message", "").strip()
        if not message:
            flash("Please describe the problem.", "error")
            return render_template("report.html", services=services, preselect_service_id=preselect_service_id,
                                    site_name=site_name)
        contact = request.form.get("contact", "").strip()[:200]
        service_id = request.form.get("service_id", type=int)
        service = db.get_service(service_id) if service_id else None
        db.create_problem_report(message[:2000], contact, service["id"] if service else None)
        _register_report_submission()
        prefix = f"{service['name']}: " if service else ""
        notifications.notify("Problem reported", f"{prefix}{message[:200]}")
        flash("Thanks — your report has been submitted.", "success")
        return redirect(url_for("report_problem"))
    session["report_form_rendered_at"] = time.time()
    return render_template("report.html", services=services, preselect_service_id=preselect_service_id,
                            site_name=site_name)


@app.route("/admin/reports")
@login_required
def admin_reports():
    return render_template("admin_reports.html", reports=db.list_problem_reports(), active="reports")


@app.route("/admin/reports/<int:rid>/status", methods=["POST"])
@login_required
def admin_report_status(rid):
    status = request.form.get("status", "reviewed")
    db.update_problem_report_status(rid, status if status in ("new", "reviewed", "resolved") else "reviewed")
    flash("Report updated.", "success")
    return redirect(url_for("admin_reports"))


@app.route("/admin/reports/<int:rid>/delete", methods=["POST"])
@login_required
def admin_report_delete(rid):
    db.delete_problem_report(rid)
    flash("Report deleted.", "success")
    return redirect(url_for("admin_reports"))


@app.route("/admin/reports/<int:rid>/create-incident", methods=["POST"])
@login_required
def admin_report_create_incident(rid):
    report = db.get_problem_report(rid)
    if not report:
        flash("Report not found.", "error")
        return redirect(url_for("admin_reports"))
    service_ids = [report["service_id"]] if report["service_id"] else []
    title = report["message"][:80] + ("…" if len(report["message"]) > 80 else "")
    description = f'Reported via the public "Report a problem" form.\n\n{report["message"]}'
    iid = db.create_incident({"title": title, "description": description, "status": "investigating"}, service_ids)
    db.update_problem_report_status(rid, "resolved")
    flash("Incident created from report.", "success")
    return redirect(url_for("admin_incident_edit", iid=iid))


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    # Cheap check (a single os.path.exists()) on every hit, so a host-level 2FA
    # reset (see twofactor.py) takes effect the moment someone next loads this
    # page - no app restart needed.
    twofactor.check_and_process_reset_flag()
    first_run = is_first_run()
    awaiting_totp = session.get("awaiting_totp", False)

    if request.method == "POST":
        if not first_run and _login_locked():
            flash("Too many failed attempts. Try again in a few minutes.", "error")
            return render_template("login.html", first_run=first_run, awaiting_totp=awaiting_totp)

        # Second step of a 2FA login: the password was already verified to get
        # here (this flag can only be set below, server-side, in this same
        # session) - only the code is checked now, not the password again.
        if awaiting_totp:
            code = request.form.get("totp_code", "")
            secret = db.get_setting("admin_totp_secret")
            if secret and twofactor.verify_code(secret, code):
                _register_login_success()
                session.pop("awaiting_totp", None)
                session["logged_in"] = True
                nxt = session.pop("login_next", None) or url_for("admin_dashboard")
                return redirect(nxt)
            _register_login_failure()
            flash("Incorrect code.", "error")
            return render_template("login.html", first_run=False, awaiting_totp=True)

        password = request.form.get("password", "")
        if first_run:
            confirm = request.form.get("confirm", "")
            if len(password) < 6:
                flash("Password must be at least 6 characters.", "error")
            elif password != confirm:
                flash("Passwords do not match.", "error")
            else:
                db.set_setting("admin_password_hash", generate_password_hash(password))
                session["logged_in"] = True
                return redirect(url_for("admin_dashboard"))
        else:
            stored = db.get_setting("admin_password_hash")
            if stored and check_password_hash(stored, password):
                if twofactor.is_enabled():
                    # Login isn't complete yet - don't reset the failure counter
                    # or set logged_in until the code step also succeeds.
                    session["awaiting_totp"] = True
                    session["login_next"] = request.args.get("next") or url_for("admin_dashboard")
                    return render_template("login.html", first_run=False, awaiting_totp=True)
                _register_login_success()
                session["logged_in"] = True
                nxt = request.args.get("next") or url_for("admin_dashboard")
                return redirect(nxt)
            _register_login_failure()
            flash("Incorrect password.", "error")
    return render_template("login.html", first_run=first_run, awaiting_totp=awaiting_totp)


@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Admin dashboard
# ---------------------------------------------------------------------------
@app.route("/admin")
@login_required
def admin_dashboard():
    return redirect(url_for("admin_services"))


# ---- Services ----
@app.route("/admin/services")
@login_required
def admin_services():
    services = db.list_services()
    return render_template("admin_services.html", services=services, hostname=platform.node(), active="services")


@app.route("/admin/services/new", methods=["GET", "POST"])
@login_required
def admin_service_new():
    if request.method == "POST":
        data = dict(request.form)
        data["manual_override"] = 1 if request.form.get("manual_override") else 0
        data["auto_check"] = 1 if request.form.get("auto_check") else 0
        data["auto_incident"] = 1 if request.form.get("auto_incident") else 0
        data["ignore_in_overall_status"] = 1 if request.form.get("ignore_in_overall_status") else 0
        data["show_report_button"] = 1 if request.form.get("show_report_button") else 0
        data["show_run_target_public"] = 1 if request.form.get("show_run_target_public") else 0
        data["show_dependencies_public"] = 1 if request.form.get("show_dependencies_public") else 0
        db.create_service(data)
        flash("Service added.", "success")
        return redirect(url_for("admin_services"))
    return render_template("admin_service_form.html", service=None, defaults=_service_defaults(),
                            vms=monitoring.get_cached_vm_snapshot(), hostname=platform.node(), active="services")


@app.route("/admin/services/<int:service_id>/edit", methods=["GET", "POST"])
@login_required
def admin_service_edit(service_id):
    service = db.get_service(service_id)
    if not service:
        flash("Service not found.", "error")
        return redirect(url_for("admin_services"))
    if request.method == "POST":
        data = dict(request.form)
        data["manual_override"] = 1 if request.form.get("manual_override") else 0
        data["auto_check"] = 1 if request.form.get("auto_check") else 0
        data["auto_incident"] = 1 if request.form.get("auto_incident") else 0
        data["ignore_in_overall_status"] = 1 if request.form.get("ignore_in_overall_status") else 0
        data["show_report_button"] = 1 if request.form.get("show_report_button") else 0
        data["show_run_target_public"] = 1 if request.form.get("show_run_target_public") else 0
        data["show_dependencies_public"] = 1 if request.form.get("show_dependencies_public") else 0
        db.update_service(service_id, data)
        labels = request.form.getlist("link_label")
        urls = request.form.getlist("link_url")
        links = [(label.strip(), url.strip()) for label, url in zip(labels, urls) if label.strip() and url.strip()]
        db.replace_service_links(service_id, links)
        depends_on_ids = [int(i) for i in request.form.getlist("depends_on") if i.isdigit()]
        db.set_service_dependencies(service_id, depends_on_ids)
        flash("Service updated.", "success")
        return redirect(url_for("admin_services"))
    links = db.list_service_links(service_id)
    dependency_ids = db.get_service_dependencies(service_id)
    other_services = [s for s in db.list_services() if s["id"] != service_id]
    return render_template("admin_service_form.html", service=service, links=links,
                            vms=monitoring.get_cached_vm_snapshot(), hostname=platform.node(),
                            dependency_ids=dependency_ids, other_services=other_services, active="services")


@app.route("/admin/services/<int:service_id>/delete", methods=["POST"])
@login_required
def admin_service_delete(service_id):
    db.delete_service(service_id)
    flash("Service deleted.", "success")
    return redirect(url_for("admin_services"))


# ---- Announcements ----
@app.route("/admin/announcements")
@login_required
def admin_announcements():
    announcements = db.list_announcements()
    return render_template("admin_announcements.html", announcements=announcements, active="announcements")


@app.route("/admin/announcements/new", methods=["GET", "POST"])
@login_required
def admin_announcement_new():
    if request.method == "POST":
        data = dict(request.form)
        data["pinned"] = 1 if request.form.get("pinned") else 0
        db.create_announcement(data)
        flash("Announcement published.", "success")
        return redirect(url_for("admin_announcements"))
    return render_template("admin_announcement_form.html", announcement=None, active="announcements")


@app.route("/admin/announcements/<int:aid>/edit", methods=["GET", "POST"])
@login_required
def admin_announcement_edit(aid):
    announcement = db.get_announcement(aid)
    if not announcement:
        flash("Announcement not found.", "error")
        return redirect(url_for("admin_announcements"))
    if request.method == "POST":
        data = dict(request.form)
        data["pinned"] = 1 if request.form.get("pinned") else 0
        db.update_announcement(aid, data)
        flash("Announcement updated.", "success")
        return redirect(url_for("admin_announcements"))
    return render_template("admin_announcement_form.html", announcement=announcement, active="announcements")


@app.route("/admin/announcements/<int:aid>/delete", methods=["POST"])
@login_required
def admin_announcement_delete(aid):
    db.delete_announcement(aid)
    flash("Announcement deleted.", "success")
    return redirect(url_for("admin_announcements"))


# ---- Incidents ----
@app.route("/admin/incidents")
@login_required
def admin_incidents():
    incidents = db.list_incidents()
    services = db.list_services()
    return render_template("admin_incidents.html", incidents=incidents, services=services, active="incidents")


@app.route("/admin/incidents/new", methods=["GET", "POST"])
@login_required
def admin_incident_new():
    services = db.list_services()
    if request.method == "POST":
        service_ids = request.form.getlist("service_id")
        db.create_incident(request.form, service_ids)
        names = [db.get_service(sid)["name"] for sid in service_ids if db.get_service(sid)]
        prefix = f"{', '.join(names)}: " if names else ""
        notifications.notify("Incident opened", f"{prefix}{request.form.get('title', '')}")
        flash("Incident recorded.", "success")
        return redirect(url_for("admin_incidents"))
    return render_template("admin_incident_form.html", incident=None, services=services, active="incidents")


@app.route("/admin/incidents/<int:iid>/edit", methods=["GET", "POST"])
@login_required
def admin_incident_edit(iid):
    incident = db.get_incident(iid)
    services = db.list_services()
    if not incident:
        flash("Incident not found.", "error")
        return redirect(url_for("admin_incidents"))
    if request.method == "POST":
        service_ids = request.form.getlist("service_id")
        db.update_incident(iid, request.form, service_ids)
        flash("Incident updated.", "success")
        return redirect(url_for("admin_incidents"))
    updates = db.list_incident_updates(iid)
    return render_template("admin_incident_form.html", incident=incident, services=services,
                            updates=updates, active="incidents")


@app.route("/admin/incidents/<int:iid>/delete", methods=["POST"])
@login_required
def admin_incident_delete(iid):
    db.delete_incident(iid)
    flash("Incident deleted.", "success")
    return redirect(url_for("admin_incidents"))


@app.route("/admin/incidents/<int:iid>/updates", methods=["POST"])
@login_required
def admin_incident_add_update(iid):
    incident = db.get_incident(iid)
    if not incident:
        flash("Incident not found.", "error")
        return redirect(url_for("admin_incidents"))
    message = request.form.get("message", "").strip()
    status = request.form.get("status", incident["status"])
    if message:
        db.create_incident_update(iid, message, status)
        db.update_incident(iid, {
            "service_id": incident["service_id"],
            "title": incident["title"],
            "description": incident["description"],
            "status": status,
        })
        notifications.notify(f"Incident update — {status}", f"{incident['title']}: {message}")
        flash("Update posted.", "success")
    return redirect(url_for("admin_incident_edit", iid=iid))


# ---- Maintenance windows ----
@app.route("/admin/maintenance")
@login_required
def admin_maintenance():
    windows = db.list_maintenance_windows()
    return render_template("admin_maintenance.html", windows=windows, active="maintenance")


@app.route("/admin/maintenance/new", methods=["GET", "POST"])
@login_required
def admin_maintenance_new():
    services = db.list_services()
    if request.method == "POST":
        service_ids = request.form.getlist("service_id")
        if not service_ids:
            flash("Check at least one service.", "error")
            return render_template("admin_maintenance_form.html", services=services, active="maintenance")
        db.create_maintenance_window(request.form, service_ids)
        # Applies immediately rather than waiting for the next health-check cycle (up
        # to PORTAL_CHECK_INTERVAL_SECONDS later) - matters most for a window whose
        # start time is already in the past (e.g. "this has actually been going on
        # since two days ago, I forgot to log it"), which should flip the service to
        # maintenance right now, not minutes from now.
        _process_maintenance_and_notify()
        flash("Maintenance window scheduled.", "success")
        return redirect(url_for("admin_maintenance"))
    return render_template("admin_maintenance_form.html", services=services, active="maintenance")


@app.route("/admin/maintenance/<int:mid>/edit", methods=["GET", "POST"])
@login_required
def admin_maintenance_edit(mid):
    window = db.get_maintenance_window(mid)
    services = db.list_services()
    if not window:
        flash("Maintenance window not found.", "error")
        return redirect(url_for("admin_maintenance"))
    if request.method == "POST":
        # Once a window is already applied, its service selector is disabled
        # client-side (see admin_maintenance_form.html) - a disabled <select> submits
        # nothing at all, so pass service_ids=None (leave associated services alone)
        # rather than reading an empty list and wiping every association.
        service_ids = request.form.getlist("service_id") if not window["applied"] else None
        if service_ids is not None and not service_ids:
            flash("Check at least one service.", "error")
            return render_template("admin_maintenance_form.html", services=services, window=window, active="maintenance")
        db.update_maintenance_window(mid, request.form, service_ids)
        _process_maintenance_and_notify()
        flash("Maintenance window updated.", "success")
        return redirect(url_for("admin_maintenance"))
    return render_template("admin_maintenance_form.html", services=services, window=window, active="maintenance")


@app.route("/admin/maintenance/<int:mid>/delete", methods=["POST"])
@login_required
def admin_maintenance_delete(mid):
    db.delete_maintenance_window(mid)
    flash("Maintenance window removed.", "success")
    return redirect(url_for("admin_maintenance"))


# ---- Info page ----
@app.route("/admin/info", methods=["GET", "POST"])
@login_required
def admin_info():
    if request.method == "POST":
        db.set_info_page(request.form.get("content", ""))
        flash("Info page updated.", "success")
        return redirect(url_for("admin_info"))
    content = db.get_info_page()
    return render_template("admin_info.html", content=content, active="info")


# ---- Resources ----
@app.route("/admin/resources")
@login_required
def admin_resources():
    snapshot = monitoring.get_resource_snapshot()
    vms = monitoring.get_cached_vm_snapshot()
    return render_template("admin_resources.html", snapshot=snapshot, vms=vms, visible=ADMIN_RESOURCE_VISIBLE,
                            refresh_seconds=config.RESOURCE_REFRESH_SECONDS,
                            totp_enabled=twofactor.is_enabled(), active="resources")


@app.route("/admin/resources/host-control", methods=["POST"])
@login_required
def admin_host_control():
    """Restarts/shuts down the host machine itself - see monitoring.control_host()
    for why this is safe from an injection standpoint (zero user-controlled input:
    exactly two fixed actions). The blast radius comes entirely from what the
    action *does*, not from anything an attacker could smuggle into it - login +
    CSRF (already required on every admin POST) plus the typed client-side
    confirmation in admin_resources.html are the mitigations for that.

    Step-up authentication: if 2FA is enabled, a fresh code is required here even
    though the session is already logged in - a stolen/replayed session cookie
    alone must not be enough to trigger this specific action, given what it does."""
    action = request.form.get("action", "")
    if action not in ("restart", "shutdown"):
        flash("Unknown host action.", "error")
        return redirect(url_for("admin_resources"))
    blocked = _require_totp("Incorrect or missing 2FA code - host action cancelled.",
                            "admin_resources")
    if blocked:
        return blocked
    success, message = monitoring.control_host(action)
    flash(message, "success" if success else "error")
    return redirect(url_for("admin_resources"))


@app.route("/admin/resources/vm-control", methods=["POST"])
@login_required
def admin_vm_control():
    name = request.form.get("name", "")
    action = request.form.get("action", "")
    if action not in ("start", "stop", "restart"):
        flash("Unknown VM action.", "error")
        return redirect(url_for("admin_resources"))
    success, message = monitoring.control_vm(name, action)
    flash(message, "success" if success else "error")
    return redirect(url_for("admin_resources"))


# ---- System (this app's own process/components - separate from Resources above,
# which is about the host machine's hardware) ----
def _restart_process():
    """Replaces the running process image in place via os.execv - same PID, works
    identically whether launched as `python app.py`, `python serve_waitress.py`, or
    either wrapped in a systemd unit/Task Scheduler entry, and needs no supervisor
    process (unlike a fork+exit approach). Delayed briefly on a background thread so
    the triggering HTTP response has a moment to actually reach the browser first -
    same shape as monitoring.control_host(), but restarting this Python process
    instead of shelling out to the OS to restart the whole machine."""
    def _do():
        time.sleep(1)
        os.execv(sys.executable, [sys.executable] + sys.argv)
    threading.Thread(target=_do, daemon=True).start()


@app.route("/admin/system")
@login_required
def admin_system():
    return render_template("admin_system.html", discord_status=discord_bot.get_status(),
                            discord_configured=bool(config.DISCORD_BOT_TOKEN),
                            totp_enabled=twofactor.is_enabled(), active="system")


@app.route("/admin/system/restart", methods=["POST"])
@login_required
def admin_system_restart():
    """Restarts either the whole app process or just the Discord bot connection -
    see _restart_process()/discord_bot.restart() for what each actually does.
    Same step-up 2FA reasoning as admin_host_control(): a stolen/replayed session
    cookie alone must not be enough to trigger this, given a full-app restart
    briefly takes the whole portal offline and a bot restart interrupts anyone
    mid-conversation with it."""
    component = request.form.get("component", "")
    if component not in ("app", "discord-bot"):
        flash("Unknown restart target.", "error")
        return redirect(url_for("admin_system"))
    blocked = _require_totp("Incorrect or missing 2FA code - restart cancelled.",
                            "admin_system")
    if blocked:
        return blocked
    if component == "app":
        _restart_process()
        flash("Restarting the app now - this page will be briefly unreachable.", "success")
    else:
        if not config.DISCORD_BOT_TOKEN:
            flash("Discord bot isn't configured (PORTAL_DISCORD_BOT_TOKEN not set).", "error")
            return redirect(url_for("admin_system"))
        discord_bot.restart()
        flash("Discord bot restarted.", "success")
    return redirect(url_for("admin_system"))


# ---- About / self-update (see updater.py for everything that actually happens) ----
@app.route("/admin/about")
@login_required
def admin_about():
    """Reads the update-check cache only - never checks GitHub inline. The cache is
    refreshed by the background health-check loop (see run_health_checks) on its own
    much longer TTL, same pattern as _integration_status_cache. A cache miss (nothing
    checked yet this process, or checking disabled) renders as "not checked yet"
    rather than blocking the page on an outbound call."""
    return render_template(
        "admin_about.html",
        version=config.VERSION,
        version_display=config.VERSION_DISPLAY,
        is_git_checkout=config.IS_GIT_CHECKOUT,
        update_status=updater.get_cached_update_status(),
        channel=updater.get_channel(),
        channels=updater.CHANNELS,
        check_enabled=updater.update_check_enabled(),
        check_interval_hours=round(config.UPDATE_CHECK_INTERVAL_SECONDS / 3600, 1),
        inapp_update_enabled=config.ENABLE_INAPP_UPDATE,
        repo_url=updater.REPO_URL,
        releases_url=updater.RELEASES_PAGE_URL,
        backups=list(reversed(updater.list_backups()))[:5],
        python_version=sys.version.split()[0],
        platform_name=f"{platform.system()} {platform.release()}".strip(),
        db_path=db.DB_PATH,
        log_path=os.path.join(config.APP_ROOT, "instance", "logs", "app.log"),
        app_root=config.APP_ROOT,
        totp_enabled=twofactor.is_enabled(),
        active="about",
    )


@app.route("/admin/about/check", methods=["POST"])
@login_required
def admin_about_check():
    """The sanctioned exception to the no-slow-I/O-in-a-request-handler rule: an
    explicit one-shot admin action the admin knows will take a moment, exactly like
    the integrations "Check now" button. Never fires automatically."""
    result = updater.refresh_update_cache_if_stale(force=True)
    if result and result["ok"]:
        if result["update_available"]:
            flash(f"Update available: {result['current']} → {result['latest']}.", "success")
        elif result["ahead"]:
            flash(f"You're running {result['current']}, newer than the latest "
                  f"{result['channel']} release ({result['latest']}).", "success")
        else:
            flash(f"Up to date ({result['current']}).", "success")
    else:
        flash(f"Couldn't check for updates: {result['error'] if result else 'unknown error'}", "error")
    return redirect(url_for("admin_about"))


@app.route("/admin/about/settings", methods=["POST"])
@login_required
def admin_about_settings():
    channel = request.form.get("update_channel", "")
    if channel not in updater.CHANNELS:
        flash("Unknown update channel.", "error")
        return redirect(url_for("admin_about"))
    db.set_setting("update_channel", channel)
    db.set_setting("update_check_enabled", "1" if request.form.get("update_check_enabled") else "0")
    # The cached result belongs to the old channel - drop it so the page doesn't show
    # a stale "latest stable" reading next to a freshly-selected unstable channel.
    updater._update_cache["result"] = None
    updater._update_cache["refreshed_monotonic"] = None
    flash("Update preferences saved.", "success")
    return redirect(url_for("admin_about"))


@app.route("/admin/about/update", methods=["POST"])
@login_required
def admin_update():
    """Installs the latest release and restarts the app into it.

    This is the most powerful button in the app - it installs and then executes new
    code - so it is gated at least as heavily as the host restart/shutdown control:
    login + CSRF (both automatic for any POST under /admin/), a typed confirmation
    client-side, and step-up 2FA requiring a fresh code even on an already
    authenticated session. It additionally honours config.ENABLE_INAPP_UPDATE, which
    lives in an env var rather than a DB setting precisely so an attacker who owns
    the admin panel can't just switch it back on.

    Runs updater.perform_update() synchronously: a deliberate, documented instance
    of the "explicit one-shot admin action the user knows will be slow" exception,
    same as the integration Check-now button, not an automatic background path.
    Restarting reuses _restart_process() rather than inventing a second mechanism."""
    if not config.ENABLE_INAPP_UPDATE:
        flash("In-app updates are disabled (PORTAL_ENABLE_INAPP_UPDATE=false). "
              "Use the update.py script over SSH instead.", "error")
        return redirect(url_for("admin_about"))
    if config.IS_GIT_CHECKOUT:
        flash("This is a git checkout, not an installed release - updating would overwrite "
              "tracked files. Use `git pull` instead.", "error")
        return redirect(url_for("admin_about"))
    blocked = _require_totp("Incorrect or missing 2FA code - update cancelled.",
                            "admin_about")
    if blocked:
        return blocked

    lines = []

    def progress(message):
        lines.append(message)
        _logger.info("[update] %s", message)

    try:
        result = updater.perform_update(progress=progress)
    except updater.UpdateError as e:
        _logger.error("In-app update failed: %s", e)
        flash(f"Update failed: {e}", "error")
        return redirect(url_for("admin_about"))
    except Exception as e:
        _logger.exception("In-app update crashed")
        flash(f"Update failed unexpectedly: {e} (see instance/logs/app.log)", "error")
        return redirect(url_for("admin_about"))

    if not result["applied"]:
        flash(f"Nothing to update - {result['reason']}.", "success")
        return redirect(url_for("admin_about"))

    # Written before the restart so the next successful start can confirm it came up
    # on the new version - and so `python update.py rollback` knows which backup to
    # use if it doesn't. See updater.write_pending_marker() for what this can and
    # cannot detect.
    updater.write_pending_marker(result["backup"], result["latest"])
    flash(f"Updated {result['current']} → {result['latest']}. Restarting now - this page will be "
          f"briefly unreachable. If it doesn't come back, run "
          f"`python update.py rollback` on the server.", "success")
    _restart_process()
    return redirect(url_for("admin_about"))


# ---- Integrations (read-only Jellyfin/Jellyseerr/*Arr status) ----
@app.route("/admin/integrations")
@login_required
def admin_integrations():
    configured = db.list_integrations()
    statuses = {i["id"]: _integration_status_cache.get(i["id"]) for i in configured if i["enabled"]}
    return render_template("admin_integrations.html", integrations=configured, statuses=statuses,
                            check_interval=config.CHECK_INTERVAL_SECONDS, active="integrations")


@app.route("/admin/integrations/<int:iid>/check", methods=["POST"])
@login_required
def admin_integration_check_now(iid):
    integration = db.get_integration(iid)
    if not integration:
        flash("Integration not found.", "error")
        return redirect(url_for("admin_integrations"))
    try:
        status = integrations.fetch_integration_status(integration)
    except Exception as e:
        status = {"reachable": False, "version": None, "issues": [], "error": str(e)}
    _integration_status_cache[iid] = {"status": status, "checked_at": db.now_iso()}
    flash("Reachable." if status["reachable"] else f"Unreachable: {status['error']}",
          "success" if status["reachable"] else "error")
    return redirect(url_for("admin_integrations"))


@app.route("/admin/integrations/new", methods=["GET", "POST"])
@login_required
def admin_integration_new():
    if request.method == "POST":
        data = dict(request.form)
        data["enabled"] = 1 if request.form.get("enabled") else 0
        data["show_on_public"] = 1 if request.form.get("show_on_public") else 0
        data["auto_incident"] = 1 if request.form.get("auto_incident") else 0
        db.create_integration(data)
        flash("Integration added.", "success")
        return redirect(url_for("admin_integrations"))
    return render_template("admin_integration_form.html", integration=None, services=db.list_services(),
                            active="integrations")


@app.route("/admin/integrations/<int:iid>/edit", methods=["GET", "POST"])
@login_required
def admin_integration_edit(iid):
    integration = db.get_integration(iid)
    if not integration:
        flash("Integration not found.", "error")
        return redirect(url_for("admin_integrations"))
    if request.method == "POST":
        data = dict(request.form)
        data["enabled"] = 1 if request.form.get("enabled") else 0
        data["show_on_public"] = 1 if request.form.get("show_on_public") else 0
        data["auto_incident"] = 1 if request.form.get("auto_incident") else 0
        db.update_integration(iid, data)
        flash("Integration updated.", "success")
        return redirect(url_for("admin_integrations"))
    return render_template("admin_integration_form.html", integration=integration, services=db.list_services(),
                            active="integrations")


# ---- Add-new wizard (service, integration, or both) ----
@app.route("/admin/new")
@login_required
def admin_new_picker():
    return render_template("admin_new_picker.html", active="services")


@app.route("/admin/new/combined", methods=["GET", "POST"])
@login_required
def admin_new_combined():
    if request.method == "POST":
        # Every field admin_service_form.html exposes (Advanced settings section
        # below, collapsed by default) is now collected here too, the same way
        # admin_service_new() does it - previously this only ever sent
        # name/icon/description/url/group_name, so db.create_service() silently
        # fell back to its own hardcoded literals instead of the admin's
        # configured Service defaults, and settings like retry/threshold/grace/
        # api-health-mode were unreachable without a follow-up edit.
        url = request.form.get("url", "").strip()
        service_data = dict(request.form)
        service_data["url"] = url
        service_data["manual_override"] = 1 if request.form.get("manual_override") else 0
        service_data["auto_check"] = 1 if request.form.get("auto_check") else 0
        service_data["auto_incident"] = 1 if request.form.get("auto_incident") else 0
        service_data["ignore_in_overall_status"] = 1 if request.form.get("ignore_in_overall_status") else 0
        service_data["show_report_button"] = 1 if request.form.get("show_report_button") else 0
        service_id = db.create_service(service_data)
        db.create_integration({
            "name": request.form.get("name", ""),
            "kind": request.form.get("kind", "arr"),
            "base_url": url,
            "api_key": request.form.get("api_key", ""),
            "enabled": 1,
            "service_id": service_id,
            "show_on_public": 1 if request.form.get("show_on_public") else 0,
            # Named differently from the service's own "auto_incident" checkbox
            # above (same form, would otherwise collide) - see the template.
            "auto_incident": 1 if request.form.get("check_auto_incident") else 0,
        })
        flash("Service and status check created.", "success")
        return redirect(url_for("admin_services"))
    return render_template("admin_new_combined.html", defaults=_service_defaults(), active="services")


@app.route("/admin/integrations/<int:iid>/delete", methods=["POST"])
@login_required
def admin_integration_delete(iid):
    db.delete_integration(iid)
    flash("Integration deleted.", "success")
    return redirect(url_for("admin_integrations"))


# ---- Settings ----
@app.route("/admin/settings", methods=["GET", "POST"])
@login_required
def admin_settings():
    if request.method == "POST":
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        stored = db.get_setting("admin_password_hash")
        if not check_password_hash(stored, current):
            flash("Current password is incorrect.", "error")
        elif len(new) < 6:
            flash("New password must be at least 6 characters.", "error")
        elif new != confirm:
            flash("Passwords do not match.", "error")
        else:
            db.set_setting("admin_password_hash", generate_password_hash(new))
            flash("Password changed.", "success")
    section_order = _public_section_order()
    section_labels = dict(PUBLIC_SECTIONS)
    return render_template("admin_settings.html", check_interval=config.CHECK_INTERVAL_SECONDS,
                            refresh_seconds=config.PUBLIC_REFRESH_SECONDS,
                            site_name=db.get_setting("site_name", "Server"),
                            show_public=_public_resource_visibility(),
                            highload_thresholds=integrations.high_load_thresholds(),
                            discord_configured=bool(config.DISCORD_WEBHOOK_URL),
                            ntfy_configured=bool(config.NTFY_URL),
                            section_order=section_order, section_labels=section_labels,
                            service_defaults=_service_defaults(),
                            public_history_days=db.get_setting("public_history_days", ""),
                            lowdisk_percent_threshold=db.get_setting("lowdisk_percent_threshold", ""),
                            active="settings")


@app.route("/admin/settings/general", methods=["POST"])
@login_required
def admin_settings_general():
    db.set_setting("site_name", request.form.get("site_name", "").strip() or "Server")
    for key in _PUBLIC_RESOURCE_KEYS:
        db.set_setting(key, "1" if request.form.get(key) else "0")
    for key, default in integrations.HIGHLOAD_DEFAULTS.items():
        raw = request.form.get(f"highload_{key}", "").strip()
        db.set_setting(f"highload_{key}", raw if raw.isdigit() else default)
    layout_order = request.form.get("layout_order", "").strip()
    if layout_order:
        db.set_setting("public_layout_order", layout_order)
    history_days = request.form.get("public_history_days", "").strip()
    db.set_setting("public_history_days", history_days if history_days.isdigit() else "")
    lowdisk = request.form.get("lowdisk_percent_threshold", "").strip()
    db.set_setting("lowdisk_percent_threshold", lowdisk if lowdisk.isdigit() else "")
    for key in SERVICE_DEFAULT_FIELDS:
        raw = request.form.get(f"service_default_{key}", "").strip()
        db.set_setting(f"service_default_{key}", raw if raw.isdigit() else "")
    db.set_setting("service_default_auto_incident", "1" if request.form.get("service_default_auto_incident") else "0")
    api_mode = request.form.get("service_default_api_health_mode", "off")
    db.set_setting("service_default_api_health_mode", api_mode if api_mode in db.API_HEALTH_MODES else "off")
    flash("Settings updated.", "success")
    return redirect(url_for("admin_settings"))


@app.route("/admin/settings/test-notification", methods=["POST"])
@login_required
def admin_settings_test_notification():
    if not config.DISCORD_WEBHOOK_URL and not config.NTFY_URL:
        flash("No notification channel configured - set PORTAL_DISCORD_WEBHOOK_URL or PORTAL_NTFY_URL first.", "error")
    else:
        notifications.notify("Test notification",
                              "This is a test notification from the Status Portal admin panel.")
        flash("Test notification sent - check your configured channel(s).", "success")
    return redirect(url_for("admin_settings"))


@app.route("/admin/settings/backup-db")
@login_required
def admin_settings_backup_db():
    if not os.path.isfile(db.DB_PATH):
        abort(404)
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_db_path = os.path.join(tmp_dir, "portal.db")
        db.backup_to_file(tmp_db_path)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(tmp_db_path, arcname="portal.db")
    buffer.seek(0)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return send_file(buffer, mimetype="application/zip", as_attachment=True,
                      download_name=f"portal-backup-{stamp}.zip")


@app.route("/admin/settings/logo", methods=["POST"])
@login_required
def admin_settings_logo():
    file = request.files.get("logo")
    if not file or not file.filename:
        flash("Choose a file to upload.", "error")
        return redirect(url_for("admin_settings"))
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in LOGO_ALLOWED_EXTENSIONS:
        flash("Unsupported file type - use PNG, JPG, SVG, WEBP, or ICO.", "error")
        return redirect(url_for("admin_settings"))
    os.makedirs(LOGO_UPLOAD_DIR, exist_ok=True)
    filename = secure_filename(f"logo.{ext}")
    # A re-upload in a different format (e.g. .png replacing an old .svg) would
    # otherwise leave the old file orphaned on disk alongside the new one, since
    # the filename itself (and therefore the on-disk path) changes with it.
    old = db.get_setting("site_logo_filename", "")
    if old and old != filename:
        try:
            os.remove(os.path.join(LOGO_UPLOAD_DIR, old))
        except OSError:
            pass
    file.save(os.path.join(LOGO_UPLOAD_DIR, filename))
    db.set_setting("site_logo_filename", filename)
    flash("Logo updated.", "success")
    return redirect(url_for("admin_settings"))


@app.route("/admin/settings/logo/remove", methods=["POST"])
@login_required
def admin_settings_logo_remove():
    filename = db.get_setting("site_logo_filename", "")
    if filename:
        try:
            os.remove(os.path.join(LOGO_UPLOAD_DIR, filename))
        except OSError:
            pass
        db.set_setting("site_logo_filename", "")
    flash("Logo removed.", "success")
    return redirect(url_for("admin_settings"))


# ---- Two-factor authentication ----
@app.route("/admin/2fa")
@login_required
def admin_2fa():
    return render_template("admin_2fa.html", totp_enabled=twofactor.is_enabled(), active="2fa")


@app.route("/admin/2fa/enable", methods=["GET", "POST"])
@login_required
def admin_2fa_enable():
    if request.method == "POST":
        secret = session.get("pending_totp_secret")
        code = request.form.get("totp_code", "")
        if secret and twofactor.verify_code(secret, code):
            db.set_setting("admin_totp_secret", secret)
            db.set_setting("admin_totp_enabled", "1")
            session.pop("pending_totp_secret", None)
            flash("Two-factor authentication enabled.", "success")
            return redirect(url_for("admin_2fa"))
        flash("Incorrect code - scan the QR code again (or re-enter the manual "
              "key) and try once more.", "error")

    if request.method == "GET" and request.args.get("new"):
        session.pop("pending_totp_secret", None)
    if not session.get("pending_totp_secret"):
        # Covers the first visit, an explicit "generate a different code", and the
        # defensive case of a POST arriving with no pending secret at all (session
        # expired between GET and POST, or a direct POST) - generate a fresh one
        # rather than crashing on a missing key.
        session["pending_totp_secret"] = twofactor.generate_secret()

    secret = session["pending_totp_secret"]
    uri = twofactor.provisioning_uri(secret, account_name=db.get_setting("site_name", "admin"))
    return render_template("admin_2fa_enable.html", secret=secret,
                            qr_svg=twofactor.qr_code_svg(uri), active="2fa")


@app.route("/admin/2fa/disable", methods=["POST"])
@login_required
def admin_2fa_disable():
    code = request.form.get("totp_code", "")
    secret = db.get_setting("admin_totp_secret")
    if secret and twofactor.verify_code(secret, code):
        db.set_setting("admin_totp_secret", "")
        db.set_setting("admin_totp_enabled", "0")
        flash("Two-factor authentication disabled.", "success")
    else:
        flash("Incorrect code - 2FA was not disabled.", "error")
    return redirect(url_for("admin_2fa"))


# ---- Discord bot (separate from the simple webhook notifications above) ----
@app.route("/admin/discord-bot", methods=["GET", "POST"])
@login_required
def admin_discord_bot():
    if request.method == "POST":
        raw_name = request.form.get("command_name", "").strip()
        db.set_setting("discordbot_command_name", discord_bot.sanitize_command_name(raw_name))
        db.set_setting("discordbot_update_presence", "1" if request.form.get("update_presence") else "0")
        db.set_setting("discordbot_channel_command_enabled",
                       "1" if request.form.get("channel_command_enabled") else "0")
        for key in discord_bot.INCLUDE_KEYS:
            db.set_setting(f"discordbot_include_{key}", "1" if request.form.get(f"include_{key}") else "0")
        for key in discord_bot.RESOURCE_KEYS:
            db.set_setting(f"discordbot_resource_{key}", "1" if request.form.get(f"resource_{key}") else "0")
        db.set_setting("discordbot_allowed_user_ids",
                        discord_bot.normalize_user_ids(request.form.get("allowed_user_ids", "")))
        db.set_setting("discordbot_guild_whitelist",
                        discord_bot.normalize_guild_ids(request.form.get("guild_whitelist", "")))
        flash("Discord bot settings saved. Restart the app if the command name changed.", "success")
        return redirect(url_for("admin_discord_bot"))
    include = discord_bot.include_settings()
    return render_template("admin_discord_bot.html",
                            token_configured=bool(config.DISCORD_BOT_TOKEN),
                            status=discord_bot.get_status(),
                            refresh_seconds=config.DISCORD_BOT_REFRESH_SECONDS,
                            command_name=db.get_setting("discordbot_command_name", "status"),
                            update_presence=db.get_setting("discordbot_update_presence", "0") == "1",
                            channel_command_enabled=db.get_setting(
                                "discordbot_channel_command_enabled", "0") == "1",
                            include=include,
                            resources=include["resources"],
                            allowed_user_ids=db.get_setting("discordbot_allowed_user_ids", ""),
                            guild_whitelist=db.get_setting("discordbot_guild_whitelist", ""),
                            active="discord-bot")


@app.route("/admin/discord-bot/guilds", methods=["GET", "POST"])
@login_required
def admin_discord_bot_guilds():
    if request.method == "POST":
        db.set_setting("discordbot_channel_whitelist",
                        discord_bot.normalize_channel_ids(request.form.get("channel_whitelist", "")))
        flash("Channel whitelist saved.", "success")
        return redirect(url_for("admin_discord_bot_guilds"))
    status = discord_bot.get_status()
    return render_template("admin_discord_bot_guilds.html",
                            token_configured=bool(config.DISCORD_BOT_TOKEN),
                            connected=status["connected"], guilds=status["guilds"],
                            channel_whitelist=db.get_setting("discordbot_channel_whitelist", ""),
                            active="discord-bot")


# ---------------------------------------------------------------------------
# Background health check
# ---------------------------------------------------------------------------
def _process_maintenance_and_notify():
    for event in db.process_maintenance_windows():
        service_name = event["service"]["name"]
        window_title = event["window"]["title"]
        if event["event"] == "maintenance_started":
            notifications.notify("Maintenance started", f"{service_name}: {window_title}")
        else:
            notifications.notify("Maintenance ended", f"{service_name}: {window_title}")


def _check_status_for_response(r, elapsed_ms, slow_threshold_ms):
    """A response was received at all, so the service is reachable - only a 5xx
    (actual server-side error) counts against it. Anything else the server sends
    back, including a 401/403 auth challenge (e.g. Bazarr's login prompt) or a 404,
    means it answered and is up. 'Slow' is purely a latency observation layered on
    top of an otherwise-healthy response, never on top of a 5xx.

    502 specifically means "down", not "degraded": unlike a 500/503/504 (the
    service itself is up but erroring/overloaded), a 502 means whatever's in
    front of it (reverse proxy, gateway) couldn't even reach it - functionally
    equivalent to a connection failure from the visitor's perspective."""
    if r.status_code == 502:
        return "down"
    if r.status_code >= 500:
        return "degraded"
    if slow_threshold_ms and elapsed_ms > slow_threshold_ms:
        return "slow"
    return "operational"


def _within_grace_period(service):
    """True while a service is still inside its configured post-startup grace
    window (time since this process started, not since the service itself booted -
    good enough since they're expected to start around the same time)."""
    grace = service.get("startup_grace_seconds") or 0
    return grace > 0 and (time.monotonic() - _APP_START) < grace


def _run_single_check(check_url, slow_threshold_ms):
    """One HTTP attempt against check_url. Returns (status, elapsed_ms)."""
    start = time.time()
    try:
        r = requests.get(check_url, timeout=5)
        elapsed_ms = int((time.time() - start) * 1000)
        return _check_status_for_response(r, elapsed_ms, slow_threshold_ms), elapsed_ms
    except requests.RequestException:
        return "down", None


def _check_service_status(service):
    """Runs a service's health check, retrying only a 'down' result - a degraded/
    slow/operational result is never worth retrying, and only 'down' ever opens an
    auto-incident (see _handle_incident_lifecycle), so retrying anything else would
    just delay the loop for no benefit. `retry_count` extra attempts are made
    (0 = disabled, the historical single-attempt behavior), spaced
    `retry_interval_seconds` apart; the first non-down result wins immediately, so a
    service that recovers seconds later never triggers a spurious incident. Runs
    inline in the background health-check thread - not a request handler, so
    blocking here for the retry window is the intended tradeoff, not a bug.

    While actually mid-retry (sleeping between attempts), the service's id is kept
    in _retry_in_progress so a public-page hit during that window can show
    "retrying" instead of silently keeping the last-known status - narrow (only
    matters if a request lands in the same seconds-long window), but real, since
    this loop can block for up to retry_count * retry_interval_seconds."""
    status, elapsed_ms = _run_single_check(service["check_url"], service["slow_threshold_ms"])
    retries_left = service.get("retry_count") or 0
    interval = service.get("retry_interval_seconds") or 0
    try:
        while status == "down" and retries_left > 0:
            _retry_in_progress.add(service.get("id"))
            time.sleep(interval)
            status, elapsed_ms = _run_single_check(service["check_url"], service["slow_threshold_ms"])
            retries_left -= 1
    finally:
        _retry_in_progress.discard(service.get("id"))
    return status, elapsed_ms


def _merge_api_health(status, api_health_mode, integration_reachable):
    """Folds a linked integration's cached API reachability into a service's own
    web-check status, producing the single final status used for both the public
    display and _handle_incident_lifecycle below - deliberately not two separate
    decisions, to preserve the "one status feeds both" invariant documented on
    _handle_incident_lifecycle (a past bug here already came from letting a status
    write and an incident-lifecycle decision drift apart).

    integration_reachable is None when there's nothing to act on (api_health_mode
    is "off", or this service has no enabled+publicly-shown linked integration) -
    the web-check status passes through unchanged. "down" mode always wins outright
    once the integration is unreachable; "degrade" only ever raises operational/slow
    up to degraded, never overrides an already-worse status like "down"."""
    if api_health_mode == "off" or integration_reachable is None or integration_reachable:
        return status
    if api_health_mode == "down":
        return "down"
    if api_health_mode == "degrade" and status in ("operational", "slow"):
        return "degraded"
    return status


def _linked_integration_reachable(service_id):
    """None = nothing to act on (no enabled+publicly-shown integration linked to
    this service, or it hasn't been checked yet); True/False = that integration's
    last cached reachability. Reuses the same enabled+show_on_public integrations
    already surfaced on this service's own public card (db.list_integrations_for_service)
    on purpose - api_health_mode only makes sense in relation to an integration a
    visitor can actually see the sub-badge for, not a hidden one, so a status bump
    never appears to come from nowhere."""
    linked = db.list_integrations_for_service(service_id)
    if not linked:
        return None
    cached = _integration_status_cache.get(linked[0]["id"])
    if not cached:
        return None
    return cached["status"]["reachable"]


def _merge_dependency_health(status, dependency_statuses):
    """Folds this service's direct dependencies' status into its own, same
    non-destructive precedence and "one status feeds both display and incident
    lifecycle" reasoning as _merge_api_health above: only ever raises
    operational/slow up to degraded, never overrides an already-worse down/
    maintenance. Only a dependency that's actually 'down' triggers this - a merely
    degraded dependency is still serving requests, not a strong enough signal to
    cascade further.

    Direct dependencies only, not transitive - deliberately. Resolving a chain
    would need real cycle detection and topological ordering that nothing here
    currently needs; a one-hop-only merge can't infinite-loop even if two services
    are configured to depend on each other."""
    if status not in ("operational", "slow"):
        return status
    if "down" in dependency_statuses:
        return "degraded"
    return status


def _lowdisk_threshold():
    raw = db.get_setting("lowdisk_percent_threshold", "")
    return int(raw) if raw.isdigit() else None


def _check_low_disk_space(snapshot):
    """Edge-triggered: notifies once when a disk crosses the threshold, and once
    when it recovers - never every cycle while it stays low, which would spam a
    notification every CHECK_INTERVAL_SECONDS forever. Notification-only, no
    incident - incidents are inherently tied to a service via the join table, and
    disk space isn't a service.

    State is persisted via db.get/set_low_disk_alert_state() (SQLite, not just
    in-process memory) specifically so a portal restart while a disk is still low
    doesn't re-fire the "low disk space" notification - this app is meant to
    survive its own restart cleanly (see CLAUDE.md)."""
    threshold = _lowdisk_threshold()
    if not threshold:
        return
    low_disks = {d["path"] for d in monitoring.evaluate_low_disk(snapshot, threshold)}
    for d in snapshot.get("disks", []):
        path = d["path"]
        was_low = db.get_low_disk_alert_state(path)
        is_low = path in low_disks
        if is_low and not was_low:
            notifications.notify("Low disk space",
                                  f"{d['display_name']}: {d['percent']}% used ({d['free_gb']} GB free)")
        elif was_low and not is_low:
            notifications.notify("Disk space back to normal", f"{d['display_name']} is no longer low on space.")
        db.set_low_disk_alert_state(path, is_low)


def run_health_checks():
    while True:
        try:
            # Runs before the per-service checks below, so a window that just started
            # (setting manual_override=1) is already in effect for this same cycle's
            # loop - otherwise a service being intentionally taken down for maintenance
            # could get a spurious auto-incident opened in the same instant its window begins.
            _process_maintenance_and_notify()
            services = db.list_services()
            # A fixed pre-loop snapshot, not a live re-query mid-loop - otherwise
            # whether a dependency's fresh-this-cycle status is visible would
            # silently depend on iteration order (already processed vs. not yet).
            # Caps dependency staleness at one check interval, same as everything
            # else here already tolerates.
            status_by_id = {row["id"]: row["status"] for row in services}
            for s in services:
                if not s["auto_check"] or s["manual_override"] or not s["check_url"]:
                    continue
                previous_status = s["status"]
                status, elapsed_ms = _check_service_status(s)
                status = _merge_api_health(status, s.get("api_health_mode", "off"),
                                            _linked_integration_reachable(s["id"]))
                dependency_statuses = [status_by_id.get(dep_id) for dep_id in db.get_service_dependencies(s["id"])]
                status = _merge_dependency_health(status, dependency_statuses)
                db.update_service_status_from_check(s["id"], status, elapsed_ms)
                db.record_status_history(s["id"], status, elapsed_ms)
                # Status/response time are recorded above regardless - only the
                # auto-incident escalation is held back during the grace window, so a
                # service that's still booting doesn't get an incident opened on it.
                if s["auto_incident"] and not _within_grace_period(s):
                    _handle_incident_lifecycle(s, previous_status, status)
            _refresh_integration_cache()
            _check_low_disk_space(monitoring.get_resource_snapshot())
            # Reuses this existing background thread rather than starting another one:
            # it no-ops until its own (much longer) TTL has elapsed, so a 120s health
            # -check interval doesn't turn into a GitHub API call every 120s. The
            # About page only ever reads the cache this populates.
            updater.refresh_update_cache_if_stale()
        except Exception:
            _logger.exception("health-check loop error")
        time.sleep(config.CHECK_INTERVAL_SECONDS)


def _handle_incident_lifecycle(service, previous_status, new_status):
    """Auto-opens an incident whenever a service is down and doesn't already have one
    open, and auto-resolves it the moment it recovers. Only hard 'down' transitions
    ever resolve - 'degraded' is left for a human to judge, since a single slow
    response isn't necessarily an incident.

    The open side is deliberately level-triggered (checks new_status alone, not
    previous_status != "down") rather than edge-triggered - idempotency already
    comes from the get_open_auto_incident_for_service() guard below, and an
    edge-trigger silently breaks for a service whose status was already 'down' the
    last time this function *would* have run had a startup grace period not
    suppressed the call: services.status still gets written to 'down' every cycle
    during grace (status/response time are recorded regardless), so by the time the
    grace period elapses, previous_status is already 'down' and an edge-trigger
    would never see a fresh transition again - the service could stay down forever
    with no incident ever opening. Caught by live-testing the grace period feature
    against a real always-refusing HTTP server (2026-07-23), not by unit tests."""
    if new_status == "down":
        if not db.get_open_auto_incident_for_service(service["id"]):
            incident_id = db.create_auto_incident(
                service["id"], f"{service['name']} is unreachable", "investigating")
            db.create_incident_update(
                incident_id, "Automatic health check could not reach this service.", "investigating")
            notifications.notify("Incident opened", f"{service['name']} is unreachable.")
    elif new_status != "down" and previous_status == "down":
        incident = db.get_open_auto_incident_for_service(service["id"])
        if incident:
            notifications.notify("Incident resolved", f"{service['name']} has recovered.")
            db.update_incident(incident["id"], {
                "service_id": service["id"],
                "title": incident["title"],
                "description": incident["description"],
                "status": "resolved",
            })
            db.create_incident_update(
                incident["id"], "Automatic health check confirmed this service has recovered.", "resolved")


def _handle_integration_incident_lifecycle(integration, previous_reachable, new_reachable):
    """Same auto-open/auto-resolve pattern as _handle_incident_lifecycle, but driven by
    an integration's reachability (Jellyfin/*Arr/Jellyseerr) instead of a service's own
    health check. Shares the same incident slot as the linked service (via
    get_open_auto_incident_for_service/create_auto_incident) - if the service's own
    health check already opened one, this won't open a second.

    Same level-triggered-open reasoning as _handle_incident_lifecycle applies here -
    _integration_status_cache is also updated every cycle regardless of grace, so an
    edge-trigger on previous_reachable would have the same "never opens after a
    grace-suppressed cycle" bug."""
    service_id = integration["service_id"]
    if new_reachable is False:
        if not db.get_open_auto_incident_for_service(service_id):
            service = db.get_service(service_id)
            name = service["name"] if service else integration["name"]
            incident_id = db.create_auto_incident(
                service_id, f"{name} is unreachable", "investigating")
            db.create_incident_update(
                incident_id, f"Automatic status check for '{integration['name']}' could not reach it.",
                "investigating")
            notifications.notify("Incident opened", f"{name}: status check '{integration['name']}' failed.")
    elif new_reachable is True and previous_reachable is False:
        incident = db.get_open_auto_incident_for_service(service_id)
        if incident:
            service = db.get_service(service_id)
            name = service["name"] if service else integration["name"]
            notifications.notify("Incident resolved", f"{name} has recovered.")
            db.update_incident(incident["id"], {
                "service_id": service_id,
                "title": incident["title"],
                "description": incident["description"],
                "status": "resolved",
            })
            db.create_incident_update(
                incident["id"], f"Automatic status check for '{integration['name']}' confirmed recovery.",
                "resolved")


def start_background_checker():
    t = threading.Thread(target=run_health_checks, daemon=True)
    t.start()


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging_setup.init_logging()
    db.init_db()
    # If the previous shutdown was an in-app update restarting into a new version,
    # this is where that gets confirmed (or reported as not having taken effect).
    updater.check_pending_marker()
    start_background_checker()
    monitoring.start_background_refresh(config.RESOURCE_REFRESH_SECONDS)
    discord_bot.start()
    # debug must stay False whenever this is reachable outside localhost.
    app.run(host="0.0.0.0", port=5000, debug=False)
