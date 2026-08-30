"""
app.py — Personal server status portal.
Run with: python app.py
Admin panel: /admin (password is set on first launch)
"""
import gzip
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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash, Response, abort, send_file, g
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from markupsafe import Markup, escape
import requests

import config
import db
import discord_bot
import integrations
import jellyfin_auth
import logging_setup
import media_search
import monitoring
import notifications
import scheduler
import seerr_alerts
import twofactor
import updater
import user_notify
import version_checks

_logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = config.SECRET_KEY
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=config.FORCE_HTTPS_COOKIES,
    MAX_CONTENT_LENGTH=2 * 1024 * 1024,  # 2 MB - plenty for any form on this app
    # Admin sessions are marked permanent (see _start_admin_session) so the cookie
    # carries an explicit Max-Age instead of being a browser-session cookie. That
    # distinction is what made session behavior differ per device: a browser-session
    # cookie dies when the browser is closed (desktop) but lives forever on a device
    # whose browser is never closed (phone/tablet), which is exactly the
    # "logged out on one device, logged in forever on another" report. With an
    # explicit Max-Age every device follows the same rule.
    PERMANENT_SESSION_LIFETIME=timedelta(days=config.SESSION_COOKIE_MAX_AGE_DAYS),
    # Every static URL in this app is cache-busted (asset_url()'s ?v=<mtime> for
    # CSS/JS, site_logo_version for the logo), so a long max-age can never serve a
    # stale asset - it just stops the browser re-validating a dozen files on every
    # auto-refresh reload. See asset_url() for why the busting is mandatory.
    SEND_FILE_MAX_AGE_DEFAULT=timedelta(days=30),
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
        # image.tmdb.org is the one exception, for search-result posters - same class
        # of scoped, named-host exception as the two fonts.google* hosts above, not a
        # general relaxation. Jellyfin-only results (no TMDB poster_path) stay
        # text-only rather than proxying Jellyfin's own image endpoint through a
        # request handler, which the no-live-outbound-I/O rule elsewhere exists to
        # avoid.
        "img-src 'self' data: https://image.tmdb.org; "
        "script-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; "
        "frame-ancestors 'none'"
    )
    response.headers["Server"] = "status-portal"  # don't advertise the underlying framework/server
    return response


# text/html and the JSON API are the two payload types the audit measured: a
# representative public page compresses from ~24KB to ~2.8KB, /api/status from ~14KB
# to ~1KB. Worth doing because the public page reloads itself every
# PUBLIC_REFRESH_SECONDS (60s by default) for every visitor - a recurring cost, not a
# one-off page load.
_COMPRESSIBLE_MIMETYPES = {"text/html", "application/json", "application/rss+xml"}
# Below this, gzip's own overhead (headers, checksum) can net worse than sending the
# original bytes - not worth the CPU cycles either way at this size.
_COMPRESS_MIN_BYTES = 500


@app.after_request
def _compress_response(response):
    """Gzip-compresses eligible response bodies. No static-file serving route is
    touched at all - direct_passthrough excludes send_file()'s conditional/
    range-request mode, which this must not interfere with, and neither
    static/uploads nor the DB backup download are in _COMPRESSIBLE_MIMETYPES to
    begin with.

    A client that doesn't advertise gzip support gets the exact same uncompressed
    body as before this existed - nothing here changes what's rendered, only
    whether the bytes on the wire are compressed."""
    if (response.direct_passthrough
            or response.mimetype not in _COMPRESSIBLE_MIMETYPES
            or "gzip" not in request.headers.get("Accept-Encoding", "")
            or "Content-Encoding" in response.headers
            or response.content_length is None
            or response.content_length < _COMPRESS_MIN_BYTES):
        return response
    response.set_data(gzip.compress(response.get_data(), compresslevel=6))
    response.headers["Content-Encoding"] = "gzip"
    # So a cache sitting in front of this app (a reverse proxy, a CDN - see
    # config.BEHIND_PROXY) never serves a gzip response to a client that didn't ask
    # for one, or vice versa.
    response.headers["Vary"] = "Accept-Encoding"
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
    # request.max_content_length, not the app-wide config value: the database-restore
    # endpoint raises its own limit for that one request, and quoting 2 MB at someone
    # whose 30 MB upload was actually allowed would be actively confusing.
    limit = getattr(request, "max_content_length", None) or app.config["MAX_CONTENT_LENGTH"]
    return render_template("error.html", code=413, message="That file is too large "
                            f"(max {limit // (1024 * 1024)} MB)."), 413


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
    return f"{url_for('static', filename=filename)}?v={version}{_asset_cache_salt()}"


# A suffix appended to every asset URL's ?v=, bumped by the admin panel's
# clear-caches button. mtime alone covers the normal case (a file changed, so its URL
# changes), but not the ones where a browser is holding a copy that mtime can't
# distinguish: a rollback to an older release, a file restored with its timestamp
# intact, or a proxy/CDN caching more aggressively than expected. This is the manual
# escape hatch for "I shipped a fix and their browser still runs the old file".
# Stored in settings so it survives a restart - otherwise a restart would hand every
# browser back the URL it already has cached, undoing the bump.
_asset_salt = {"value": None}


def _asset_cache_salt():
    if _asset_salt["value"] is None:
        raw = db.get_setting("asset_cache_salt", "")
        _asset_salt["value"] = f"-{raw}" if raw else ""
    return _asset_salt["value"]


def _bump_asset_cache_salt():
    # Random rather than a timestamp: two clicks inside the same second would produce
    # an identical timestamp, i.e. a "cache bust" that hands the browser back the URL
    # it already has - precisely the failure this button exists to rule out.
    raw = secrets.token_hex(4)
    db.set_setting("asset_cache_salt", raw)
    _asset_salt["value"] = f"-{raw}"


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
def _inject_portal_user():
    """Who (if anyone) is signed in, plus whether the feature is on at all - needed
    by the public topbar, the report form and the sign-in page itself, so it goes
    here rather than being threaded through every route by hand (same reasoning as
    csrf_token() being a Jinja global)."""
    user = session.get("portal_user")
    # Two extra reads, and only for a signed-in visitor: the theme they've chosen
    # (rendered into <html> so a device that has never seen this portal doesn't flash
    # the wrong colours before JavaScript runs) and whether an admin reply is waiting
    # (the only thing that tells them to go and look).
    theme = db.get_user_preferences(user["id"])["theme"] if user else "auto"
    return {"portal_user": user,
            "user_auth_enabled": jellyfin_auth.is_enabled(),
            "report_needs_login": _report_login_required(),
            "user_theme": theme if theme in db.USER_THEMES else "auto",
            "unseen_replies": db.count_unseen_replies(user["id"]) if user else 0}


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
    # New reports *plus* unread replies from reporters. Both are "someone is waiting
    # on you" and both live behind the same Reports tab, so one badge covers them -
    # and without counting replies, the admin would simply never learn that anybody
    # had answered, which would make the conversation one-directional in practice.
    return {
        "unread_reports_count": db.count_unread_problem_reports() + db.count_unseen_user_messages(),
        "update_available": bool(cached and cached.get("update_available")),
        # How many *Arr apps the last version check found behind. Reads one stored
        # setting - never checks anything here, same rule as the update badge above.
        "arr_updates_count": version_checks.updates_available(),
        # Requests waiting on the admin to approve them. Also a stored value, never a
        # live call - and admin-only, which the /admin/ guard above already ensures.
        "seerr_pending_count": seerr_alerts.pending_count(),
    }


# ---------------------------------------------------------------------------
# Admin session lifetime
# ---------------------------------------------------------------------------
# Idle timeout in hours. 0 (or a blank/invalid value) means "no idle timeout" - the
# session then lasts until the cookie's own Max-Age
# (config.SESSION_COOKIE_MAX_AGE_DAYS) runs out, which is still a real, uniform
# expiry rather than the old "forever on some devices, never on others".
DEFAULT_SESSION_TIMEOUT_HOURS = 12
MAX_SESSION_TIMEOUT_HOURS = config.SESSION_COOKIE_MAX_AGE_DAYS * 24

# The session's last_seen stamp is only rewritten once per this many seconds. Writing
# it on literally every request would re-sign and re-send the cookie on every single
# hit (including the public page's auto-refresh) for no benefit - a minute of
# granularity is far finer than any timeout worth configuring.
SESSION_TOUCH_INTERVAL_SECONDS = 60


def _session_timeout_seconds():
    """None = no idle timeout configured. Clamped to MAX_SESSION_TIMEOUT_HOURS
    because anything beyond the cookie's own Max-Age is a promise this can't keep."""
    raw = db.get_setting("admin_session_timeout_hours", str(DEFAULT_SESSION_TIMEOUT_HOURS))
    hours = int(raw) if raw.isdigit() else DEFAULT_SESSION_TIMEOUT_HOURS
    if hours <= 0:
        return None
    return min(hours, MAX_SESSION_TIMEOUT_HOURS) * 3600


def _start_admin_session():
    """The one place a login becomes a logged-in session. Marks it permanent (so the
    cookie gets an explicit Max-Age instead of dying with the browser) and stamps
    last_seen so the idle clock starts now. Every place that logs someone in goes
    through this - three hand-maintained copies is three chances for one to drift."""
    session.permanent = True
    session["logged_in"] = True
    session["last_seen"] = time.time()


# Restoring the database means uploading one, and a real portal.db is far larger than
# the 2 MB app-wide cap that exists for the logo upload. Flask 3.1 lets that cap be
# raised for one request instead of for the whole app, which is the difference between
# "the restore endpoint accepts a database" and "every form on the site, including the
# public report form, now accepts 128 MB". This is a cap on the *upload itself*
# (typically a zip, so compressed) - see MAX_EXTRACTED_DB_BYTES below for the
# separate, larger cap on the database's own uncompressed size once extracted.
DB_RESTORE_MAX_BYTES = 128 * 1024 * 1024


# Opens the request-scoped pooled DB connection (see db.py's get_db()/
# begin_request_scope()) before anything else in this request touches the database -
# registered first, ahead of the ordering-sensitive pair below, so every db.py call
# for the rest of the request benefits, including from _enforce_session_timeout and
# _enforce_user_session's own settings reads. Harmless either way since get_db()
# always has a working fallback; this is purely about not leaving a handful of
# early calls opening their own connection when they didn't have to.
@app.before_request
def _open_scoped_db():
    db.begin_request_scope()


@app.teardown_appcontext
def _close_scoped_db(exception=None):
    """Guaranteed to run once per request by Flask, even if a before_request hook or
    the view itself raised - closing the real connection here (not relying on
    anything inside the request to do it) is what makes db.begin_request_scope()
    safe to open unconditionally above."""
    db.end_request_scope()


# Registration order matters: Flask runs before_request hooks in the order they were
# defined, and _check_csrf below reads request.form - which parses the body, and would
# therefore hit the *old* limit and 413 a perfectly good upload before the view that
# raises the limit ever runs. This has to be first among the ordering-sensitive group.
# (Same class of ordering constraint as _enforce_session_timeout needing to precede
# _check_csrf; see CLAUDE.md.)
@app.before_request
def _allow_large_upload_for_restore():
    if request.method == "POST" and request.path == "/admin/about/restore-db":
        request.max_content_length = DB_RESTORE_MAX_BYTES


@app.before_request
def _enforce_session_timeout():
    """Server-side idle expiry, and the sliding-window touch that feeds it.

    Registered *before* _check_csrf on purpose: an admin POST arriving on an expired
    session must redirect to the login page, not fail the CSRF check with a bare 400
    (the token lives in the very session being cleared here, so ordering them the
    other way round turns "your session expired" into an unexplained error page).

    Server-side rather than relying on the cookie's Max-Age alone: a cookie's expiry
    attribute isn't covered by the signature, so a client that simply keeps sending
    an "expired" cookie would otherwise stay logged in indefinitely."""
    if not session.get("logged_in"):
        return
    session.permanent = True
    timeout = _session_timeout_seconds()
    now = time.time()
    last_seen = session.get("last_seen")
    if timeout is not None and last_seen is not None and now - last_seen > timeout:
        session.clear()
        if request.path.startswith("/admin/"):
            flash("Your session expired after a period of inactivity. Please sign in again.", "error")
            return redirect(url_for("admin_login", next=request.path))
        # A public page doesn't need a redirect - it renders identically signed out.
        return
    if last_seen is None or now - last_seen > SESSION_TOUCH_INTERVAL_SECONDS:
        session["last_seen"] = now


@app.before_request
def _enforce_user_session():
    """The visitor-session equivalent of _enforce_session_timeout above, kept
    completely separate from it: this reads and clears its own session keys and
    never so much as looks at `logged_in`.

    Registered *between* the admin timeout hook and _check_csrf, which preserves the
    ordering rule those two depend on (see _enforce_session_timeout's docstring)
    while making sure an expired visitor session is gone before anything downstream
    treats it as valid.

    Two independent reasons a session ends here:

    * idle timeout, checked server-side against session["portal_user_last_seen"] for
      the same reason the admin one is - a cookie's expiry attribute isn't covered
      by the signature, so a client that keeps sending an "expired" cookie would
      otherwise stay signed in forever;
    * the user no longer being valid in the cached Jellyfin list, which is what
      makes the sync task an actual revocation mechanism. Jellyfin is never
      contacted here - an outage must not sign anybody out, and an unpopulated cache
      never invalidates anyone (see jellyfin_auth.session_user_still_valid)."""
    user = session.get("portal_user")
    if not user:
        return
    session.permanent = True
    now = time.time()
    timeout = _user_session_timeout_seconds()
    last_seen = session.get("portal_user_last_seen")
    if timeout is not None and last_seen is not None and now - last_seen > timeout:
        _end_user_session()
        return
    if not jellyfin_auth.session_user_still_valid(user.get("id", "")):
        _logger.info("Ended the portal session for '%s' - no longer a valid Jellyfin user",
                      user.get("name", "?"))
        _end_user_session()
        return
    if last_seen is None or now - last_seen > SESSION_TOUCH_INTERVAL_SECONDS:
        session["portal_user_last_seen"] = now


# POST paths outside /admin/ that still need CSRF protection. /report is
# conditional rather than listed here - see _csrf_required_for().
_CSRF_PROTECTED_PUBLIC_PATHS = {"/login", "/account", "/account/theme", "/search/request"}


def _csrf_required_for(path, method):
    """Which POSTs the check below applies to.

    Everything under /admin/ always, as before - that's what makes a new admin route
    protected for free. Beyond that:

    * /login establishes a session, so a cross-site POST to it is session fixation.
    * /report is protected *only while somebody is signed in*. Its documented
      exemption rested on it exercising no authenticated privilege, which stops
      being true the moment reports are attributable to a user - but on an install
      that hasn't enabled sign-in, nothing has changed and the form keeps working
      without JavaScript (the token is injected by static/js/csrf.js).

    Adding another public POST route means making a deliberate decision here;
    tests/test_conventions.py reads this function to make sure of that."""
    if method != "POST":
        return False
    if path.startswith("/admin/") or path.startswith("/account") or path in _CSRF_PROTECTED_PUBLIC_PATHS:
        return True
    if path == "/report":
        return bool(session.get("portal_user"))
    return False


@app.before_request
def _check_csrf():
    if app.testing or not _csrf_required_for(request.path, request.method):
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


# ---------------------------------------------------------------------------
# Visitor sessions (Jellyfin-backed) - a second, entirely separate identity
# ---------------------------------------------------------------------------
# Deliberately built as a parallel system rather than an extension of the admin
# login, and the separation is structural, not a convention anyone has to remember:
#
# * different session keys. `logged_in` is the admin's and is set in exactly one
#   place (_start_admin_session); nothing here ever writes it, so no amount of
#   getting the visitor flow wrong can produce an admin session.
# * different decorator. login_required gates /admin/ and only ever reads
#   `logged_in`; user_login_required gates the visitor-facing routes and only ever
#   reads `portal_user`. Neither consults the other's key.
# * different lifetimes, different lockout counters, different logout routes.
#
# A signed-in Jellyfin user is a visitor with a name, not a lesser admin. What's
# stored is the minimum that identity needs: Jellyfin's user id (stable across a
# rename, which is why sessions key off it rather than the username), the display
# name, and whether Jellyfin considers them an administrator - that last one is
# groundwork for the per-content visibility work on the ROADMAP, and is deliberately
# *not* consulted anywhere in this app today. No password and no Jellyfin access
# token ever goes in here: the session cookie is signed, not encrypted.
def _contact_prompt_url_if_needed(user_id):
    """Where to send someone after signing in: the contact prompt if they have nowhere
    to be reached and haven't waved it away, otherwise None (carry on as before)."""
    try:
        return url_for("user_contact_prompt") if user_notify.needs_contact_details(user_id) else None
    except Exception:
        # A prompt is a nicety; it must never be the reason a sign-in fails.
        _logger.exception("Could not decide whether to prompt for contact details")
        return None


def _start_user_session(user):
    session.permanent = True
    session["portal_user"] = {
        "id": user["id"],
        "name": user["name"],
        "jellyfin_admin": bool(user.get("is_administrator")),
        "authenticated_at": time.time(),
    }
    session["portal_user_last_seen"] = time.time()


def _end_user_session():
    """Pops only this feature's own keys. Never session.clear() - an admin who is
    also signed in as a Jellyfin user in the same browser must not be logged out of
    the admin panel by a visitor-side timeout."""
    session.pop("portal_user", None)
    session.pop("portal_user_last_seen", None)


def current_user():
    return session.get("portal_user")


def user_login_required(f):
    """Gate for visitor-facing routes. Reads `portal_user` and nothing else - in
    particular, being the admin does not satisfy it, because the two are answers to
    different questions and conflating them is how "admin implies user" quietly
    becomes "user implies admin" later."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("portal_user"):
            flash("Please sign in with your Jellyfin account first.", "error")
            return redirect(url_for("user_login", next=request.full_path.rstrip("?")))
        return f(*args, **kwargs)
    return wrapper


# Separate from _login_state on purpose: a burst of failed Jellyfin sign-ins must
# not lock the admin out of their own panel, and vice versa. Same global (not
# per-IP) shape and the same reasoning as the two counters it sits beside.
_user_login_state = {"failures": 0, "locked_until": 0.0}
USER_LOGIN_LOCKOUT_THRESHOLD = 10
USER_LOGIN_LOCKOUT_SECONDS = 300


def _user_login_locked():
    return time.time() < _user_login_state["locked_until"]


def _register_user_login_failure():
    """Counts credential rejections only. An unreachable Jellyfin is deliberately
    *not* counted: it isn't a failed guess, and letting an outage fill the counter
    would lock everybody out for five minutes after the server came back."""
    _user_login_state["failures"] += 1
    if _user_login_state["failures"] >= USER_LOGIN_LOCKOUT_THRESHOLD:
        _user_login_state["locked_until"] = time.time() + USER_LOGIN_LOCKOUT_SECONDS
        _user_login_state["failures"] = 0


def _register_user_login_success():
    _user_login_state["failures"] = 0
    _user_login_state["locked_until"] = 0.0


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
    stop matching the others.

    Shares _login_state (the same counter the login page's own TOTP step uses)
    rather than keeping a separate one - a stolen session cookie is exactly the
    threat this function exists for, and an unthrottled code-guessing loop against
    it would otherwise make the "fresh code required" guarantee above hollow. This
    deliberately means 5 wrong guesses here also locks the login page for 5
    minutes, and vice versa: one counter protecting one identity, not two."""
    if not twofactor.is_enabled():
        return None
    if _login_locked():
        flash("Too many incorrect attempts - try again in a few minutes.", "error")
        return redirect(url_for(redirect_endpoint))
    code = request.form.get("totp_code", "")
    secret = db.get_setting("admin_totp_secret")
    if twofactor.verify_code(secret, code):
        _register_login_success()
        return None
    _register_login_failure()
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


# Wording aimed at the person who filed the report, not at the admin triaging it:
# "new" is meaningless to a reporter, "not looked at yet" isn't.
REPORT_STATUS_LABELS = {
    "new": "Waiting to be looked at",
    "reviewed": "Being looked into",
    "resolved": "Closed",
}


def _report_login_required():
    """Whether /report needs a signed-in Jellyfin user.

    Gated on jellyfin_auth.is_enabled() as well as its own setting, deliberately: an
    install that hasn't set up Jellyfin sign-in must behave exactly as it always
    has, rather than having its report form silently become unreachable behind a
    login that doesn't exist. Defaults to on once sign-in *is* enabled.

    Worth being honest about the trade-off this makes, because it isn't free: while
    Jellyfin is down, nobody who hasn't already signed in can sign in - so nobody
    new can report the outage, which is one of the times a status page is most
    useful. That's why it's a setting an admin can turn off, and why the admin page
    says so in as many words rather than burying it."""
    return jellyfin_auth.is_enabled() and db.get_setting("report_requires_login", "1") == "1"


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


# Uptime percentages, cached briefly. Two separate wins stack here: one grouped
# query instead of one per service (see db.get_uptime_percentages), and then not
# re-running even that on every hit. A 30-day uptime figure can only change when the
# background checker records a new result - once every CHECK_INTERVAL_SECONDS, 120s
# by default - and one more sample moves a 30-day percentage by well under 0.1%, so
# a minute of staleness is invisible where a per-page-load scan of the whole history
# table is not. The public page reloads itself every 60s per visitor; this is the
# difference between that costing a full aggregate each time and costing nothing.
UPTIME_CACHE_TTL_SECONDS = 60
_uptime_cache = {"value": {}, "fetched_at": 0.0}
_uptime_cache_lock = threading.Lock()


def _cached_uptime_percentages():
    now = time.monotonic()
    with _uptime_cache_lock:
        if _uptime_cache["fetched_at"] and now - _uptime_cache["fetched_at"] < UPTIME_CACHE_TTL_SECONDS:
            return _uptime_cache["value"]
    # Deliberately computed outside the lock: this is an idempotent read, so two
    # threads racing on a cold cache just do the same harmless work twice - far
    # better than every request queueing behind whichever one got there first.
    value = db.get_uptime_percentages()
    with _uptime_cache_lock:
        _uptime_cache["value"] = value
        _uptime_cache["fetched_at"] = now
    return value


def _enrich_services(services):
    open_reports = db.count_open_reports_by_service()
    uptimes = _cached_uptime_percentages()
    service_names = {s["id"]: s["name"] for s in services}
    links_by_service = db.list_service_links_for_services([s["id"] for s in services])
    for s in services:
        s["links"] = links_by_service[s["id"]]
        s["uptime"] = uptimes.get(s["id"])
        s["in_grace_period"] = _within_grace_period(s)
        s["retrying"] = s["id"] in _retry_in_progress
        s["open_reports_count"] = open_reports.get(s["id"], 0)
        s["run_target_label"] = _run_target_label(s["run_target"]) if s["show_run_target_public"] else None
        s["dependency_names"] = [
            service_names.get(dep_id, "?") for dep_id in db.get_service_dependencies(s["id"])
        ] if s["show_dependencies_public"] else []
    return services


def _enrich_incidents(incidents):
    updates_by_incident = db.list_incident_updates_for_incidents([i["id"] for i in incidents])
    for i in incidents:
        i["updates"] = updates_by_incident[i["id"]]
    return incidents


ADMIN_RESOURCE_VISIBLE = {"cpu": True, "memory": True, "disks": True,
                          "network": True, "gpu": True}

_PUBLIC_RESOURCE_KEYS = ["show_public_cpu", "show_public_memory", "show_public_disks",
                         "show_public_network", "show_public_gpu",
                         "show_public_vms", "show_public_highload", "show_public_jellyfin_tasks"]

# The Media section's four parts, each independently switchable. All default to OFF:
# unlike the resource cards, these say something about what is in (or on its way into)
# the library and who asked for it, which is a different kind of disclosure and has to
# be opted into rather than appearing the moment an integration is configured.
_PUBLIC_MEDIA_KEYS = ["show_public_calendar", "show_public_requests",
                      "show_public_downloads", "show_public_indexers"]


def _public_media_visibility():
    """Memoised on Flask's `g`, same pattern as _request_snapshot() below - every
    public page calls this (and _public_resource_visibility()) from more than one of
    its availability predicate/context builder/summary builder, and each one used to
    re-read the same handful of settings rows from scratch (measured: 8 of the ~114
    SQLite connections a single '/' render made, all re-fetching the same 4 rows)."""
    if not hasattr(g, "_public_media_visibility"):
        g._public_media_visibility = {key[len("show_public_"):]: db.get_setting(key, "0") == "1"
                                       for key in _PUBLIC_MEDIA_KEYS}
    return g._public_media_visibility


def _media_requires_login():
    """Whether the Media section is for signed-in visitors only.

    Defaults to on, and only *means* anything while Jellyfin sign-in is enabled - with
    no sign-in configured there is no such thing as a signed-in visitor, so enforcing it
    would make the section unreachable for everyone rather than restricted. Same shape
    as report_requires_login."""
    return db.get_setting("media_requires_login", "1") == "1"


def _media_visible_to(user):
    """Whether this viewer may see the Media section at all."""
    if not jellyfin_auth.is_enabled():
        return True
    return bool(user) if _media_requires_login() else True


def _public_resource_visibility():
    """Memoised on Flask's `g` - see _public_media_visibility()'s docstring just
    above for why (same pattern, same reason: measured at 56 of the ~114 SQLite
    connections a single '/' render made, all re-fetching the same 8 rows)."""
    if not hasattr(g, "_public_resource_visibility"):
        g._public_resource_visibility = {key[len("show_public_"):]: db.get_setting(key, "0") == "1"
                                          for key in _PUBLIC_RESOURCE_KEYS}
    return g._public_resource_visibility


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
    defaults["run_target"] = db.get_setting("service_default_run_target", "")
    defaults["show_run_target_public"] = db.get_setting("service_default_show_run_target_public", "0") == "1"
    defaults["show_dependencies_public"] = db.get_setting("service_default_show_dependencies_public", "0") == "1"
    return defaults


# The public page's reorderable content blocks - the topbar/status-hero/footer stay
# fixed (they're page chrome, not content). Each key maps 1:1 to a
# templates/sections/<key>.html partial, which owns its own "is there anything to
# show" guard - index() doesn't filter this list by content, it's included
# unconditionally in whatever order is configured, same as today's fixed order.
# The main page's own content blocks, in the order an admin can rearrange them.
#
# Deliberately short. The public page used to carry nine of these in one scroll -
# announcements, services, incidents, info, resources, VMs, Jellyfin activity, media
# and search - which buried the thing almost every visitor actually came for ("is it
# working?") under everything else. What's left here is that question; the rest moved
# to pages of their own (PUBLIC_PAGES below) and appears here only as a one-line
# summary linking through.
PUBLIC_SECTIONS = [
    ("announcements", "Announcements"),
    ("services", "Services"),
    ("incidents", "Incidents & maintenance"),
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
    integrations_list = db.list_integrations()
    for integ in integrations_list:
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
    jellyfin = next((i for i in integrations_list if i["kind"] == "jellyfin" and i["enabled"]), None)
    if jellyfin:
        try:
            integrations.refresh_jellyfin_activity_cache(jellyfin["base_url"], jellyfin["api_key"])
        except Exception:
            _logger.exception("Jellyfin activity refresh failed")


def _attach_integration_status(services):
    """Reads the cache only - see the module-level note above for why."""
    linked_by_service = db.list_public_integrations_by_service()
    for s in services:
        linked = linked_by_service.get(s["id"], [])
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


# ---------------------------------------------------------------------------
# The public sub-pages
# ---------------------------------------------------------------------------
# Each of these was a block on the main page and is now a page of its own. The context
# builder for each is shared between its page *and* the main page's summary line, which
# is the point: the visibility rules (show_public_*, media_requires_login) are applied
# once, in one place, so a sub-page cannot become a way around a setting that hides the
# corresponding block. A page whose builder reports nothing to show is not linked and
# 404s if requested directly - the same answer a visitor would get if it didn't exist,
# rather than an empty page confirming the feature is there but switched off.
def _request_snapshot():
    """One resource snapshot per request, memoised on Flask's `g`.

    The main page can want it twice - once for the high-load badge and once for the
    "Server resources" summary line - and a snapshot is the single most expensive thing
    a public page does (it reads every disk, and falls back to a blocking CPU sample
    when monitoring's cache is stale). Taking it twice for one page is pure waste."""
    if not hasattr(g, "_resource_snapshot"):
        g._resource_snapshot = monitoring.get_resource_snapshot()
    return g._resource_snapshot


def _resources_context():
    visible = _public_resource_visibility()
    show_any = any(visible[k] for k in ("cpu", "memory", "disks", "network", "gpu"))
    if not show_any:
        return None
    return {"visible": visible, "snapshot": _request_snapshot(), "show_any_resource": show_any}


def _vms_context():
    visible = _public_resource_visibility()
    if not visible["vms"]:
        return None
    vms = monitoring.get_cached_vm_snapshot()
    return {"visible": visible, "vms": vms}


def _activity_context():
    visible = _public_resource_visibility()
    if not visible["jellyfin_tasks"]:
        return None
    activity = integrations.get_cached_jellyfin_activity()
    # "Nothing is happening" is a real answer and the page has to be able to say it, so
    # this returns a context even when both lists are empty - unlike the other builders,
    # where empty means "not configured" and the page shouldn't exist at all.
    return {"visible": visible, "jellyfin_activity": activity,
            "activity_idle": not activity.get("running_tasks") and not activity.get("transcoding")}


def _media_context():
    media_visible = _public_media_visibility()
    if not any(media_visible.values()) or not _media_visible_to(current_user()):
        return None
    return {"media": integrations.get_cached_media(), "media_visible": media_visible}


def _info_context():
    info = db.get_info_page()
    if not (info or "").strip():
        return None
    return {"info": info}


def _high_load():
    """The high-load badge, which stays on the main page even though the resource cards
    moved: it answers "is something wrong right now", which is the main page's job. Its
    toggle is separate from the cards', so it needs its own snapshot when they're off."""
    visible = _public_resource_visibility()
    if not visible["highload"]:
        return {"active": False, "reasons": []}
    snapshot = _request_snapshot()
    return integrations.evaluate_high_load(snapshot) if snapshot else {"active": False, "reasons": []}


# Whether a page exists for this viewer, answered *without* building its content.
#
# This split is load-bearing for performance, not tidiness. The nav appears on every
# public page, and building it by calling each page's context builder meant every page
# load ran a full resource snapshot - 200ms+, and worse when monitoring's CPU cache is
# stale and get_resource_snapshot() falls back to a blocking sample. Navigating to
# Jellyfin activity was paying for a disk and CPU poll it never displays, which is
# what made those pages look like they'd hung.
#
# Each predicate reads settings only. They must stay cheap: anything that talks to the
# filesystem, the network or psutil belongs in the context builder, not here.
def _resources_available():
    visible = _public_resource_visibility()
    return any(visible[k] for k in ("cpu", "memory", "disks", "network", "gpu"))


def _vms_available():
    return _public_resource_visibility()["vms"]


def _activity_available():
    return _public_resource_visibility()["jellyfin_tasks"]


def _media_available():
    return any(_public_media_visibility().values()) and _media_visible_to(current_user())


def _info_available():
    return bool((db.get_info_page() or "").strip())


def _resources_summary(ctx):
    snapshot = ctx.get("snapshot") or {}
    bits = []
    if ctx["visible"].get("cpu") and snapshot.get("cpu_percent") is not None:
        bits.append(f"CPU {snapshot['cpu_percent']}%")
    if ctx["visible"].get("memory") and (snapshot.get("memory") or {}).get("percent") is not None:
        bits.append(f"memory {snapshot['memory']['percent']}%")
    if ctx["visible"].get("disks") and snapshot.get("disks"):
        bits.append(f"{len(snapshot['disks'])} disk(s)")
    return " · ".join(bits)


def _vms_summary(ctx):
    vms = ctx.get("vms") or []
    if not vms:
        return ""
    running = sum(1 for vm in vms if (vm.get("state") or "").lower() == "running")
    return f"{running} of {len(vms)} running"


def _activity_summary(ctx):
    activity = ctx.get("jellyfin_activity") or {}
    bits = []
    if activity.get("transcoding"):
        bits.append(f"{activity['transcoding']} transcoding")
    tasks = activity.get("running_tasks") or []
    if tasks:
        bits.append(f"{len(tasks)} task(s) running")
    return " · ".join(bits) if bits else "Idle right now"


def _media_summary(ctx):
    media, visible = ctx["media"], ctx["media_visible"]
    bits = []
    if visible.get("downloads") and media.get("downloads"):
        bits.append(f"{len(media['downloads'])} downloading")
    if visible.get("calendar") and media.get("calendar"):
        bits.append(f"{len(media['calendar'])} coming soon")
    if visible.get("requests") and media.get("requests"):
        bits.append(f"{len(media['requests'])} request(s)")
    return " · ".join(bits)


# key -> (label, endpoint, availability predicate, context builder, summary builder).
# The summary is the line shown on the main page; returning a falsy summary still links
# the page, it just has nothing to say about it yet.
PUBLIC_PAGES = [
    ("resources", "Server resources", "public_resources", _resources_available,
     _resources_context, _resources_summary),
    ("vms", "Virtual machines", "public_vms", _vms_available,
     _vms_context, _vms_summary),
    ("activity", "Jellyfin activity", "public_activity", _activity_available,
     _activity_context, _activity_summary),
    ("media", "Media activity", "public_media", _media_available,
     _media_context, _media_summary),
    ("info", "Practical info", "public_info", _info_available,
     _info_context, lambda ctx: "How to connect, and what to do when something's wrong"),
]


def _public_page_links(include_summaries=False):
    """Which sub-pages a visitor can see right now, for the nav and the summaries.

    Availability comes from the cheap predicate; the (possibly expensive) context is
    built only when a summary is actually wanted, i.e. on the main page. "Is it linked"
    and "does it render" still agree, because the page route applies the same predicate
    before rendering - a link to a page that would 404 is worse than no link."""
    links = []
    for key, label, endpoint, available, build_context, summarise in PUBLIC_PAGES:
        if not available():
            continue
        entry = {"key": key, "label": label, "url": url_for(endpoint)}
        if include_summaries:
            try:
                context = build_context()
                entry["summary"] = (summarise(context) or "") if context else ""
            except Exception:
                # A summary is decoration; it must never be the reason a page 500s.
                _logger.exception("Could not summarise public page '%s'", key)
                entry["summary"] = ""
        links.append(entry)
    return links


def _render_public_page(key):
    """Shared body of every sub-page route."""
    entry = next((p for p in PUBLIC_PAGES if p[0] == key), None)
    if entry is None:
        abort(404)
    _, label, _, available, build_context, _ = entry
    context = build_context() if available() else None
    if context is None:
        # Switched off, or not visible to this viewer. 404 rather than an empty page:
        # "this doesn't exist here" is the honest answer, and an empty page would
        # confirm the feature exists and is merely hidden.
        abort(404)
    return render_template(f"public/{key}.html", page_label=label, active_page=key,
                            site_name=db.get_setting("site_name", "Server"),
                            nav_links=_public_page_links(),
                            # The nav is shared, so everything it renders has to be
                            # passed everywhere it appears - without this the Search tab
                            # existed only on the page that happened to compute it.
                            search_enabled=bool(current_user()) and media_search.is_available(),
                            refresh_seconds=config.PUBLIC_REFRESH_SECONDS,
                            repo_url=updater.REPO_URL, **context)


@app.route("/resources")
def public_resources():
    return _render_public_page("resources")


@app.route("/vms")
def public_vms():
    return _render_public_page("vms")


@app.route("/activity")
def public_activity():
    return _render_public_page("activity")


@app.route("/media")
def public_media():
    return _render_public_page("media")


@app.route("/info")
def public_info():
    return _render_public_page("info")


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
    overall = compute_overall_status(services)
    site_name = db.get_setting("site_name", "Server")
    # Signed-in visitors only, and only when there's something to search. Both halves
    # matter: without sign-in the box would expose the whole library to anyone who can
    # load the page, and without an integration it would be a box that does nothing.
    search_enabled = bool(current_user()) and media_search.is_available()
    return render_template("index.html", services=services, groups=groups, announcements=announcements,
                            incidents=incidents, incidents_hidden=incidents_hidden,
                            maintenance_windows=maintenance_windows, overall=overall,
                            refresh_seconds=config.PUBLIC_REFRESH_SECONDS,
                            site_name=site_name,
                            high_load=_high_load(),
                            # Both the nav and the one-line summaries come from the same
                            # builders the pages themselves use.
                            nav_links=_public_page_links(),
                            page_summaries=_public_page_links(include_summaries=True),
                            search_enabled=search_enabled, active_page="status",
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


def _notify_async(title, message):
    """Fires notifications.notify() on its own one-shot daemon thread instead of
    inline - same shape as _send_announcement_discord()/_restart_process()'s delayed
    action, applied here because notify() itself can block on up to three sequential
    outbound calls (Discord webhook, ntfy, SMTP - each with its own multi-second
    timeout) and several of its callers are request handlers, including /report,
    which is reachable by anyone with no login at all.

    notify() is already fire-and-forget from every caller's point of view - nothing
    reads a return value or shows delivery status in the response - so moving the
    call off the request thread changes nothing about what happens, only how long
    the request waits for it to."""
    threading.Thread(target=notifications.notify, args=(title, message), daemon=True).start()


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
    (see _report_rate_limited above).

    Optionally gated behind a signed-in Jellyfin user (see _report_login_required).
    When it is, the CSRF exemption above no longer applies either - the form is then
    exercising an authenticated identity, which is exactly the thing that exemption
    was justified by the absence of."""
    if _report_login_required() and not current_user():
        flash("Please sign in with your Jellyfin account to report a problem.", "error")
        return redirect(url_for("user_login", next=request.full_path.rstrip("?")))
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
            return render_template("report.html", services=services,
                                    preselect_service_id=preselect_service_id, site_name=site_name,
                                    prefill_contact=request.form.get("contact", ""))
        contact = request.form.get("contact", "").strip()[:200]
        service_id = request.form.get("service_id", type=int)
        service = db.get_service(service_id) if service_id else None
        user = current_user()
        db.create_problem_report(message[:2000], contact, service["id"] if service else None,
                                  reporter_user=(user or {}).get("name", "")[:100],
                                  reporter_user_id=(user or {}).get("id", ""))
        _register_report_submission()
        prefix = f"{service['name']}: " if service else ""
        who = f" (from {user['name']})" if user else ""
        _notify_async("Problem reported", f"{prefix}{message[:200]}{who}")
        flash("Thanks — your report has been submitted.", "success")
        return redirect(url_for("report_problem"))
    session["report_form_rendered_at"] = time.time()
    user = current_user()
    return render_template("report.html", services=services, preselect_service_id=preselect_service_id,
                            site_name=site_name,
                            prefill_contact=db.get_user_preferences(user["id"])["contact"] if user else "")


@app.route("/admin/reports")
@login_required
def admin_reports():
    reports = db.attach_report_messages(db.list_problem_reports())
    # Opening this page is what marks reporters' replies as read, mirroring what the
    # account page does for the admin's own messages.
    db.mark_report_messages_seen([r["id"] for r in reports], "user")
    return render_template("admin_reports.html", reports=reports, active="reports")


@app.route("/admin/reports/<int:rid>/status", methods=["POST"])
@login_required
def admin_report_status(rid):
    status = request.form.get("status", "reviewed")
    db.update_problem_report_status(rid, status if status in ("new", "reviewed", "resolved") else "reviewed")
    flash("Report updated.", "success")
    return redirect(url_for("admin_reports"))


@app.route("/admin/reports/<int:rid>/reply", methods=["POST"])
@login_required
def admin_report_reply(rid):
    """Answers the reporter. Only visible to them, on their own account page - a
    reply is a message to one person, not something to publish next to the service
    card, and the report's own text was never shown publicly either.

    Saving an empty reply clears it, which is the only way to retract something said
    by mistake."""
    report = db.get_problem_report(rid)
    if not report:
        flash("Report not found.", "error")
        return redirect(url_for("admin_reports"))
    if not db.add_report_message(rid, "admin", request.form.get("reply", "")[:2000]):
        flash("Write something first.", "error")
    elif not report["reporter_user_id"]:
        flash("Reply saved — but this report was submitted anonymously, so nobody can "
              "see it. Use the contact details if there are any.", "error")
    else:
        # Queued, not sent: this is one INSERT and the delivery task does the rest, so
        # a slow SMTP server can never make this button hang. A no-op unless the admin
        # has switched per-user notifications on and the reporter has somewhere to be
        # reached, which is why the flash below doesn't promise anything about it.
        user_notify.notify_user(
            report["reporter_user_id"], "report_reply",
            "Your problem report got a reply",
            f'The admin replied to the report you filed:\n\n"{report["message"][:200]}"\n\n'
            "Open your account page on the status portal to read it and reply.")
        flash(f"Reply sent. {report['reporter_user']} will see it on their account page.",
              "success")
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
    # Carry the reporter into the incident. Without this the one place the admin
    # acts on a report is the one place it loses who sent it, which is exactly when
    # "who do I go back to about this" matters.
    source = (f'Reported by Jellyfin user "{report["reporter_user"]}" via the public '
              '"Report a problem" form.' if report["reporter_user"]
              else 'Reported anonymously via the public "Report a problem" form.')
    description = f'{source}\n\n{report["message"]}'
    iid = db.create_incident({"title": title, "description": description, "status": "investigating"}, service_ids)
    db.update_problem_report_status(rid, "resolved")
    # Remembered so the reporter's account page can show what became of their report,
    # and keep showing that incident's current status as it progresses.
    db.set_problem_report_incident(rid, iid)
    user_notify.notify_user(
        report["reporter_user_id"], "report_incident",
        "Your report is now a tracked incident",
        f'The problem you reported has been turned into an incident: "{title}"\n\n'
        "You can follow its progress on the status page.")
    flash("Incident created from report.", "success")
    return redirect(url_for("admin_incident_edit", iid=iid))


# ---------------------------------------------------------------------------
# Visitor sign-in (Jellyfin-backed). Entirely separate from /admin/login below,
# which is untouched by this feature.
# ---------------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def user_login():
    """Signs a visitor in against Jellyfin's own credentials.

    The password is checked live, on every sign-in, and is never stored. The locally
    cached user list is deliberately *not* an alternative path in here: it holds no
    password material, so "validate against the cache instead" would mean accepting
    any username that appears in it. When Jellyfin can't be reached, this refuses -
    and says so, rather than claiming the password was wrong, which would send
    someone off to reset a password that was fine. Sessions that already exist are
    unaffected by that outage; see _enforce_user_session().

    This route calls out to Jellyfin inline, which is the sanctioned exception to
    the no-slow-I/O-in-a-request-handler rule (an explicit one-shot action whose
    entire purpose is the answer) - and it's why the timeout is its own tunable
    config value rather than the shared 5s used for background health checks."""
    if not jellyfin_auth.is_enabled():
        abort(404)
    next_url = _safe_next_url(request.args.get("next") or request.form.get("next"))
    if session.get("portal_user"):
        return redirect(next_url or url_for("index"))

    if request.method == "POST":
        if _user_login_locked():
            flash("Too many failed sign-ins. Try again in a few minutes.", "error")
            return render_template("user_login.html", next_url=next_url)
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if not username or not password:
            flash("Enter your Jellyfin username and password.", "error")
            return render_template("user_login.html", next_url=next_url)

        result = jellyfin_auth.authenticate(username, password)
        if result["ok"]:
            _register_user_login_success()
            _start_user_session(result["user"])
            _logger.info("Jellyfin user '%s' signed in", result["user"]["name"])
            # Asked once, and only when there's genuinely nowhere to reach them. An
            # explicit ?next= still wins - somebody who clicked a link and got bounced
            # through sign-in should land where they were going.
            prompt = None if next_url else _contact_prompt_url_if_needed(result["user"]["id"])
            return redirect(prompt or next_url or url_for("index"))

        reason = result.get("reason")
        if reason == "unreachable":
            # Not counted as a failed attempt - an outage isn't a guess, and letting
            # it fill the lockout counter would lock everyone out for five minutes
            # after Jellyfin came back.
            flash("Can't reach Jellyfin right now, so sign-in isn't available. "
                  "This isn't a problem with your password - please try again shortly.", "error")
        elif reason == "disabled":
            _register_user_login_failure()
            flash("That Jellyfin account is disabled.", "error")
        elif reason == "not_allowed":
            # Correct credentials, refused by this portal's own access list. Worded
            # so the person knows it isn't their password or their Jellyfin account -
            # neither of which they can fix to get in here.
            _register_user_login_failure()
            flash("Your access to this portal has been turned off by the administrator. "
                  "Your Jellyfin account itself is unaffected.", "error")
        elif reason == "not_configured":
            flash("Jellyfin sign-in isn't configured on this portal.", "error")
        else:
            _register_user_login_failure()
            flash("Incorrect username or password.", "error")

    return render_template("user_login.html", next_url=next_url)


# The 8 checkbox names the account-settings form submits, shared by the visitor's own
# POST handler and the admin-viewing-a-user's-account route (see
# admin_user_account()) - one place naming the fields so the two can't drift apart.
def _save_account_prefs(user_id, form):
    db.set_user_preferences(
        user_id, theme=form.get("theme", "auto"), contact=form.get("contact", "").strip()[:200],
        notify_email=form.get("notify_email", "").strip()[:200],
        notify_discord_id=form.get("notify_discord_id", "").strip()[:32],
        notify_email_reports=bool(form.get("notify_email_reports")),
        notify_email_requests=bool(form.get("notify_email_requests")),
        notify_email_maintenance=bool(form.get("notify_email_maintenance")),
        notify_discord_reports=bool(form.get("notify_discord_reports")),
        notify_discord_requests=bool(form.get("notify_discord_requests")),
        notify_discord_maintenance=bool(form.get("notify_discord_maintenance")),
        notify_discord_seerr_events=bool(form.get("notify_discord_seerr_events")),
        notify_email_announcements=bool(form.get("notify_email_announcements")))


@app.route("/account", methods=["GET", "POST"])
@user_login_required
def user_account():
    """A signed-in visitor's own page: their preferences, and what became of the
    problem reports they filed.

    The reports half is the point. Before this, submitting a report was a black hole -
    no way to see whether anyone had looked at it, whether it became an incident, or
    whether the admin had said anything back. Everything shown here already existed in
    the database; the only new fact is the admin's reply.

    Scoped strictly to the signed-in user's own reports, by Jellyfin user id (see
    db.list_reports_for_user, which refuses a blank id so anonymous reports can never
    be handed to whoever asks first)."""
    user = current_user()
    user_id = user["id"]

    if request.method == "POST":
        _save_account_prefs(user_id, request.form)
        flash("Your settings have been saved.", "success")
        # saved=1 tells the page to bring this browser's own stored theme into line
        # with what was just saved - otherwise the setting would appear to do nothing
        # on the very device it was changed from, because a local choice made with
        # the floating toggle takes precedence over the account-level default.
        return redirect(url_for("user_account", saved=1))

    # Opening the page is what marks the admin's messages as read; the write is
    # skipped entirely when there's nothing to mark, which is the common case.
    db.mark_replies_seen(user_id)
    prefs = db.get_user_preferences(user_id)
    # Looked up once, on page load, rather than per notification: the delivery task
    # reads only what's stored here, so a Seerr outage can't stop notifications going
    # out - it can only stop this page offering to prefill the fields.
    seerr_account = user_notify.find_seerr_account(user_id) if user_notify.is_enabled() else None
    # Auto-populate rather than waiting for the manual "Use these details here" button:
    # only when *both* fields are still blank here, so this never overwrites a choice
    # the person already made (including a value this same fill-in wrote last time,
    # which is exactly what stops it from repeating on every subsequent visit).
    if seerr_account and not prefs["notify_email"] and not prefs["notify_discord_id"] \
            and (seerr_account["email"] or seerr_account["discord_id"]):
        user_notify.adopt_seerr_contact(user_id, seerr_account)
        prefs = db.get_user_preferences(user_id)
        flash("Filled in your contact details from Seerr — edit or clear them below.", "success")
    return render_template("account.html",
                            reports=db.attach_report_messages(db.list_reports_for_user(user_id)),
                            prefs=prefs,
                            notifications_enabled=user_notify.is_enabled(),
                            seerr_account=seerr_account,
                            seerr_configured=user_notify.seerr_integration() is not None,
                            themes=db.USER_THEMES,
                            just_saved=bool(request.args.get("saved")),
                            site_name=db.get_setting("site_name", "Server"),
                            report_statuses=REPORT_STATUS_LABELS)


@app.route("/account/reports/<int:rid>/reply", methods=["POST"])
@user_login_required
def user_report_reply(rid):
    """The reporter's side of the conversation.

    Ownership is checked against the report's stored Jellyfin user id, not against
    anything supplied by the request, so a guessed report id gets the same answer as
    a nonexistent one - deliberately not distinguishing the two, since "that report
    exists but isn't yours" is itself information about other people's reports.

    No extra rate limiting beyond the length cap: unlike /report, which is open to
    anonymous visitors and carries a honeypot, a timing check and a global limit
    precisely because of that, everything here is attributable to a signed-in
    Jellyfin account the admin can block outright from /admin/users.

    Replying deliberately does *not* reopen a closed report. A status that changed
    itself underneath the admin would be surprising; the unread badge on the admin's
    Reports tab is the signal, and reopening is their call."""
    user = current_user()
    report = db.get_problem_report(rid)
    if not report or report["reporter_user_id"] != user["id"]:
        flash("That report could not be found.", "error")
        return redirect(url_for("user_account"))
    if db.add_report_message(rid, "user", request.form.get("body", "")[:2000]):
        flash("Your reply has been sent.", "success")
    else:
        flash("Write something first.", "error")
    return redirect(url_for("user_account"))


@app.route("/account/contact", methods=["GET", "POST"])
@user_login_required
def user_contact_prompt():
    """A one-time, skippable ask for somewhere to send notifications.

    Shown right after signing in when the person has no contact details anywhere - which
    is common, because Seerr doesn't require them either. Skipping is a real answer and
    is remembered: being asked the same question on every sign-in is how a prompt turns
    into an annoyance people learn to click past."""
    user = current_user()
    if request.method == "POST":
        if request.form.get("skip"):
            db.set_user_preferences(user["id"], contact_prompt_dismissed=True)
            return redirect(url_for("index"))
        ok, message = user_notify.save_contact(
            user["id"],
            email=request.form.get("notify_email", "").strip()[:200],
            discord_id=request.form.get("notify_discord_id", "").strip()[:32])
        # Asked and answered either way - a failed write-back to Seerr still saved the
        # value here, so there's no reason to keep asking.
        db.set_user_preferences(user["id"], contact_prompt_dismissed=True)
        flash(message or "Saved.", "success" if ok else "error")
        return redirect(url_for("index"))

    if not user_notify.needs_contact_details(user["id"]):
        return redirect(url_for("user_account"))
    return render_template("contact_prompt.html",
                            site_name=db.get_setting("site_name", "Server"),
                            user=user, nav_links=_public_page_links(),
                            search_enabled=bool(current_user()) and media_search.is_available(),
                            refresh_seconds=config.PUBLIC_REFRESH_SECONDS,
                            repo_url=updater.REPO_URL, active_page=None)


@app.route("/account/seerr/import", methods=["POST"])
@user_login_required
def user_account_import_seerr_contact():
    """Copies the contact details Seerr already holds into this portal.

    A read, and one the visitor asked for by pressing a button - the details are shown
    on the page first, so nobody's address appears in their settings without them
    having seen where it came from."""
    user = current_user()
    account = user_notify.find_seerr_account(user["id"])
    if not account:
        flash("Couldn't find a Seerr account linked to your Jellyfin login.", "error")
        return redirect(url_for("user_account"))
    user_notify.adopt_seerr_contact(user["id"], account)
    flash("Copied your contact details from Seerr.", "success")
    return redirect(url_for("user_account"))


@app.route("/account/seerr/push", methods=["POST"])
@user_login_required
def user_account_push_seerr_contact():
    """Sends this portal's contact details back to the visitor's Seerr account.

    **The only place this application writes to another service.** It is therefore an
    explicit button, pressed by the person whose details they are, affecting only their
    own Seerr user and only the two contact fields - never a sync, never a side effect
    of saving something else, and never something a background task can reach."""
    user = current_user()
    prefs = db.get_user_preferences(user["id"])
    integration = user_notify.seerr_integration()
    account = user_notify.find_seerr_account(user["id"])
    if not integration or not account:
        flash("Couldn't find a Seerr account linked to your Jellyfin login.", "error")
        return redirect(url_for("user_account"))
    try:
        integrations.push_seerr_contact(integration["base_url"], integration["api_key"],
                                         account["id"],
                                         email=prefs["notify_email"] or None,
                                         discord_id=prefs["notify_discord_id"] or None)
    except (requests.RequestException, ValueError) as e:
        _logger.warning("Could not push contact details to Seerr: %s", e)
        flash(f"Couldn't update Seerr: {e}", "error")
        return redirect(url_for("user_account"))
    db.set_user_preferences(user["id"], seerr_user_id=account["id"])
    user_notify._invalidate_seerr_account_cache(user["id"])
    flash("Your Seerr account has been updated with these details.", "success")
    return redirect(url_for("user_account"))


# ---------------------------------------------------------------------------
# Unified search (signed-in visitors only)
# ---------------------------------------------------------------------------
# Per *session*, not process-global like the login and report limiters. Those defend a
# route open to anonymous strangers, where a shared counter is the point; this one is
# behind a Jellyfin sign-in, so the meaningful unit is "this person", and a global
# counter would let one enthusiastic searcher lock everybody else out.
# Typing "dune" shouldn't search TMDB for "d". Enforced on both sides: the client to
# avoid the request, the server because the client can't be trusted to.
MIN_LIVE_QUERY_LENGTH = 3


def _search_rate_limited():
    now = time.time()
    if now - session.get("search_window_start", 0) > config.SEARCH_RATE_WINDOW_SECONDS:
        session["search_window_start"] = now
        session["search_count"] = 0
    return session.get("search_count", 0) >= config.SEARCH_RATE_LIMIT


def _register_search():
    session["search_count"] = session.get("search_count", 0) + 1


@app.route("/search")
@user_login_required
def search():
    """The one request handler in this app that makes a live outbound call with no
    cache in front of it at all, by necessity rather than convenience.

    A search query isn't known until someone types it, so there's nothing to
    pre-fetch into a cache the way almost every other read here works - see
    media_search.py for the safety machinery that makes this carve-out acceptable.
    (A handful of other routes also make live outbound calls - a sanctioned
    one-shot admin action like "Check now" or a host/VM control, or a lookup this
    app deliberately keeps a short cache in front of, like the /account page's
    Seerr contact lookup - but none of those are an *uncached* live call the way
    every search keystroke here is.)"""
    user = current_user()
    query = request.args.get("q", "").strip()[:100]
    outcome = {"results": [], "errors": {}, "available": media_search.is_available()}
    limited = False

    if query:
        if _search_rate_limited():
            limited = True
        else:
            _register_search()
            outcome = media_search.search(query, jellyfin_user_id=user["id"])

    return render_template("search.html", query=query, outcome=outcome,
                            rate_limited=limited,
                            can_request=media_search.seerr_integration() is not None,
                            jellyfin_url=media_search.jellyfin_item_url,
                            min_query_length=MIN_LIVE_QUERY_LENGTH,
                            site_name=db.get_setting("site_name", "Server"))


@app.route("/search/live")
@user_login_required
def search_live():
    """The results block alone, for the incremental search that runs while typing.

    A server-rendered HTML fragment rather than JSON, matching /api/incidents/more and
    the maintenance history: this app has exactly one JSON API (/api/status, for
    external consumers) and no client-side templating anywhere, so a fragment keeps the
    live results and the submitted results provably identical - they're the same
    template.

    Behind the same sign-in and the same per-session rate limit as /search, because it
    makes exactly the same outbound calls."""
    user = current_user()
    query = request.args.get("q", "").strip()[:100]
    outcome = {"results": [], "errors": {}, "available": media_search.is_available()}
    limited = False

    # Below the minimum, answer with nothing rather than searching two APIs for "a".
    # The client enforces this too; this is the half that can't be bypassed.
    if len(query) >= MIN_LIVE_QUERY_LENGTH:
        if _search_rate_limited():
            limited = True
        else:
            _register_search()
            outcome = media_search.search(query, jellyfin_user_id=user["id"])

    return render_template("sections/_search_results.html", query=query, outcome=outcome,
                            rate_limited=limited,
                            can_request=media_search.seerr_integration() is not None,
                            jellyfin_url=media_search.jellyfin_item_url)


@app.route("/search/detail/<media_type>/<int:tmdb_id>")
@user_login_required
def search_detail(media_type, tmdb_id):
    """A single result's full detail - poster, overview, genres, runtime, rating -
    reached by clicking a title in the search results. Counts against the same
    per-session rate limit as searching, since it's another live outbound call.

    in_library/jellyfin_id/requested arrive as query params from the results link
    rather than being re-derived here - this route has no independent way to know a
    single TMDB id's Jellyfin/Seerr status without a second live search, and they're
    read-only display info, not anything this route writes."""
    if _search_rate_limited():
        flash("You've made a lot of requests just now - give it a minute.", "error")
        return redirect(url_for("search", q=request.args.get("q", "")))
    _register_search()
    detail = media_search.detail(media_type, tmdb_id)
    if not detail:
        return render_template("error.html", code=404,
                               message="Couldn't find that title."), 404
    item = {
        "title": detail["title"], "year": detail["year"], "media_type": media_type,
        "tmdb_id": tmdb_id, "poster_path": detail["poster_path"],
        "in_library": request.args.get("in_library") == "1",
        "jellyfin_id": request.args.get("jellyfin_id") or "",
        "requested": request.args.get("requested") == "1",
    }
    return render_template("search_detail.html", item=item, detail=detail,
                            query=request.args.get("q", ""),
                            can_request=media_search.seerr_integration() is not None,
                            jellyfin_url=media_search.jellyfin_item_url,
                            site_name=db.get_setting("site_name", "Server"))


@app.route("/search/request/configure")
@user_login_required
def search_request_configure():
    """Shows what a request will actually contain before submitting it, rather than
    submitting with Seerr's silent defaults - a season picker for everyone signed in,
    plus root folder/quality profile/tags only when the browser is *also* signed in as
    the portal admin (session["logged_in"]), since those reveal server filesystem
    paths an ordinary visitor has no business seeing."""
    media_type = request.args.get("media_type", "")
    tmdb_id = request.args.get("tmdb_id", "")
    is_admin = bool(session.get("logged_in"))
    config = media_search.request_configuration(media_type, tmdb_id, include_admin_fields=is_admin)
    if not config:
        return render_template("error.html", code=404, message="Couldn't find that title."), 404
    return render_template("search_request_configure.html", config=config,
                            media_type=media_type, tmdb_id=tmdb_id,
                            query=request.args.get("q", ""), is_admin=is_admin,
                            site_name=db.get_setting("site_name", "Server"))


@app.route("/search/request", methods=["POST"])
@user_login_required
def search_request():
    """Asks Seerr for something on the signed-in visitor's behalf.

    A write against another service, so it's a POST, it's CSRF-protected (see
    _csrf_required_for), it requires a signed-in visitor, and it counts against the same
    per-session rate limit as searching."""
    user = current_user()
    if _search_rate_limited():
        flash("You've made a lot of requests just now - give it a minute.", "error")
        return redirect(url_for("search", q=request.form.get("q", "")))
    _register_search()
    media_type = request.form.get("media_type", "")
    seasons = ([int(s) for s in request.form.getlist("seasons") if s.isdigit()]
               if media_type == "tv" else None)
    root_folder = request.form.get("root_folder") or None
    profile_id = request.form.get("profile_id") or None
    tags = request.form.getlist("tags") or None
    # Root folder/profile/tags are only ever honoured when the browser is also signed
    # in as the portal admin - the configuration page never renders those fields for
    # anyone else, but a forged POST must not be able to smuggle them in regardless of
    # what the form actually showed.
    if not session.get("logged_in"):
        root_folder = profile_id = tags = None
    ok, message = media_search.request(media_type, request.form.get("tmdb_id", ""),
                                        user["id"], user.get("name", ""),
                                        seasons=seasons, root_folder=root_folder,
                                        profile_id=profile_id, tags=tags)
    flash(message, "success" if ok else "error")
    return redirect(url_for("search", q=request.form.get("q", "")))


@app.route("/account/theme", methods=["POST"])
@user_login_required
def user_account_theme():
    """Endpoint for the floating light/dark toggle, so a signed-in user's choice
    follows them to their other devices instead of living only in the browser that
    made it. Deliberately tiny and separate from the settings form above: it must not
    be able to touch any other preference, and it answers with no body because the
    page has already applied the change locally."""
    db.set_user_preferences(current_user()["id"], theme=request.form.get("theme", "auto"))
    return ("", 204)


@app.route("/logout")
def user_logout():
    """Ends only the visitor session - see _end_user_session() for why this must not
    be session.clear()."""
    _end_user_session()
    flash("You have been signed out.", "success")
    return redirect(url_for("index"))


def _safe_next_url(raw):
    """Only ever a path on this site. A `next` parameter that can name another host
    is an open redirect, and this one is reachable without any authentication at
    all. Anything that isn't a single-slash-prefixed relative path is discarded
    rather than sanitised - there's no legitimate case here for the difference."""
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return None
    return raw


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
                _start_admin_session()
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
                _start_admin_session()
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
                _start_admin_session()
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
    return render_template("admin_announcements.html", announcements=announcements,
                            sends=db.list_announcement_sends(), active="announcements",
                            **_announcement_channel_context())


def _announcement_channel_context():
    return {"discord_channel_configured": bool(db.get_setting("discordbot_announcement_channel_id", "")),
            "email_notify_enabled": user_notify.is_enabled()}


def _send_announcement_discord(send_id, channel_id, text):
    """Runs on its own one-shot thread, the same shape _restart_process() uses for a
    delayed one-off action - the caller returns immediately, and this records the
    real outcome once Discord actually answers (or times out) rather than making the
    admin's page load wait on a network round trip they didn't ask to watch."""
    ok, detail = discord_bot.send_channel_message(channel_id, text)
    db.set_announcement_send_detail(send_id, "Posted." if ok else f"Failed: {detail}")


def _dispatch_announcement_send(aid, title, message, channels):
    """Fans one announcement out over the requested channels and returns a list of
    short status notes for the flash message. Shared by the create/edit form's
    "Publish and send" and the list page's "Send" button, so there's exactly one
    implementation of what a send actually does.

    Email fans out per-recipient through the existing queue+task machinery
    (notify_service_subscribers(), respecting each user's own opt-out); Discord is one
    post to one configured channel, not a DM broadcast, so it can't hit Discord's
    per-DM rate limits the way messaging every user individually would."""
    recipient_count = 0
    notes = []
    if "email" in channels:
        if not user_notify.is_enabled():
            notes.append("email: per-user notifications are switched off — see Notifications.")
        else:
            recipient_count = user_notify.notify_service_subscribers("announcement", title, message)
            notes.append(f"email: queued for {recipient_count} recipient(s).")

    send_id = db.record_announcement_send(aid, title, ",".join(channels), recipient_count)

    if "discord" in channels:
        channel_id = db.get_setting("discordbot_announcement_channel_id", "")
        if not channel_id:
            notes.append("discord: no announcement channel configured — see Discord bot → Discord servers.")
            db.set_announcement_send_detail(send_id, "No announcement channel configured.")
        else:
            text = f"**{title}**\n{message}"
            threading.Thread(target=_send_announcement_discord,
                             args=(send_id, channel_id, text), daemon=True).start()
            notes.append("discord: posting now — check the send history below shortly.")
    return notes


@app.route("/admin/announcements/new", methods=["GET", "POST"])
@login_required
def admin_announcement_new():
    if request.method == "POST":
        data = dict(request.form)
        data["pinned"] = 1 if request.form.get("pinned") else 0
        aid = db.create_announcement(data)
        channels = [c for c in request.form.getlist("channels") if c in ("email", "discord")]
        if channels:
            notes = _dispatch_announcement_send(aid, data["title"], data["message"], channels)
            flash("Announcement published. " + " ".join(notes), "success")
        else:
            flash("Announcement published.", "success")
        return redirect(url_for("admin_announcements"))
    return render_template("admin_announcement_form.html", announcement=None, active="announcements",
                            **_announcement_channel_context())


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
        channels = [c for c in request.form.getlist("channels") if c in ("email", "discord")]
        if channels:
            notes = _dispatch_announcement_send(aid, data["title"], data["message"], channels)
            flash("Announcement updated. " + " ".join(notes), "success")
        else:
            flash("Announcement updated.", "success")
        return redirect(url_for("admin_announcements"))
    return render_template("admin_announcement_form.html", announcement=announcement, active="announcements",
                            **_announcement_channel_context())


@app.route("/admin/announcements/<int:aid>/delete", methods=["POST"])
@login_required
def admin_announcement_delete(aid):
    db.delete_announcement(aid)
    flash("Announcement deleted.", "success")
    return redirect(url_for("admin_announcements"))


@app.route("/admin/announcements/<int:aid>/send", methods=["POST"])
@login_required
def admin_announcement_send(aid):
    """Re-sends an already-published announcement - the list page's counterpart to
    "Publish and send" on the create/edit form, for a typo fixed and re-sent, or sent
    by email first and Discord later."""
    announcement = db.get_announcement(aid)
    if not announcement:
        flash("Announcement not found.", "error")
        return redirect(url_for("admin_announcements"))

    channels = [c for c in request.form.getlist("channels") if c in ("email", "discord")]
    if not channels:
        flash("Choose at least one channel to send on.", "error")
        return redirect(url_for("admin_announcements"))

    notes = _dispatch_announcement_send(aid, announcement["title"], announcement["message"], channels)
    flash(" ".join(notes), "success")
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
        _notify_async("Incident opened", f"{prefix}{request.form.get('title', '')}")
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
        _notify_async(f"Incident update — {status}", f"{incident['title']}: {message}")
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
    """Same step-up reasoning as admin_host_control above: a stolen/replayed
    session cookie alone must not be enough to stop or restart a VM."""
    name = request.form.get("name", "")
    action = request.form.get("action", "")
    if action not in ("start", "stop", "restart"):
        flash("Unknown VM action.", "error")
        return redirect(url_for("admin_resources"))
    blocked = _require_totp("Incorrect or missing 2FA code - VM action cancelled.",
                            "admin_resources")
    if blocked:
        return blocked
    success, message = monitoring.control_vm(name, action)
    flash(message, "success" if success else "error")
    return redirect(url_for("admin_resources"))


# ---- System (this app's own process/components - separate from Resources above,
# which is about the host machine's hardware) ----
def _release_dev_server_socket():
    """Closes the listening socket Werkzeug's development server deliberately leaks
    across exec, so a restart can rebind the port.

    Only ever does anything under `python app.py`. `werkzeug.serving.run_simple()`
    calls `socket.set_inheritable(True)` on its listening socket and exports the
    descriptor as WERKZEUG_SERVER_FD, so that its auto-reloader can hand the same
    bound port to a child process. That socket therefore survives os.execv - but the
    re-executed process only *adopts* WERKZEUG_SERVER_FD when the reloader is active,
    which it never is here (debug=False). The result was a portal that restarted
    straight into "Address already in use" and died: same PID gone, nothing listening,
    and no trace anywhere but the console.

    Production runs `serve_waitress.py`, and waitress never marks its socket
    inheritable, so that path was always fine - which is exactly why this went
    unnoticed. Anyone following the README's `python app.py` had a restart button, a
    self-updater and (now) a database restore that all took the portal down for good.

    Failure here is deliberately swallowed: not being able to close a socket must never
    be the reason a restart doesn't happen, and on the waitress path there is nothing
    to close in the first place."""
    raw_fd = os.environ.pop("WERKZEUG_SERVER_FD", None)
    if raw_fd is None:
        return
    try:
        os.close(int(raw_fd))
    except (ValueError, OSError):
        _logger.info("Could not close the inherited development-server socket", exc_info=True)


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
        # After the sleep, so the response the admin is waiting on has already gone out
        # over this socket's accepted connection.
        _release_dev_server_socket()
        os.execv(sys.executable, [sys.executable] + sys.argv)
    threading.Thread(target=_do, daemon=True).start()


@app.route("/admin/system")
@login_required
def admin_system():
    caches, connections = _cache_inventory()
    return render_template("admin_system.html", discord_status=discord_bot.get_status(),
                            discord_configured=bool(config.DISCORD_BOT_TOKEN),
                            totp_enabled=twofactor.is_enabled(), caches=caches,
                            connections=connections, cache_clear_note=CACHE_CLEAR_NOTE,
                            active="system")


CACHE_CLEAR_NOTE = ("Everything here is derived data that rebuilds itself - the "
                    "background checker refills it within one check cycle, and "
                    "nothing stored in the database is touched.")


def _cache_inventory():
    """What's currently held in memory, for the System page's overview. Read-only,
    and cheap: every entry is a len() over a dict this process already has.

    The integration rows double as the "is it actually reachable" view - that
    reachability *is* the cached value, so there's nothing extra to query for it."""
    integration_names = {i["id"]: i["name"] for i in db.list_integrations()}
    entries = [
        {"name": "Uptime percentages",
         "entries": len(_uptime_cache["value"]),
         "detail": f"services (recomputed every {UPTIME_CACHE_TTL_SECONDS}s)",
         "updated_at": None},
        {"name": "Integration status checks",
         "entries": len(_integration_status_cache),
         "detail": "integrations polled", "updated_at": None},
        {"name": "Update check (GitHub releases)",
         "entries": 1 if updater._update_cache["result"] else 0,
         "detail": "result cached", "updated_at": None},
        {"name": "Jellyfin activity",
         "entries": len(integrations.get_cached_jellyfin_activity()["running_tasks"]),
         "detail": "running tasks", "updated_at": None},
        {"name": "Seerr account lookups",
         "entries": len(user_notify._seerr_account_cache),
         "detail": f"users (recomputed every {user_notify.SEERR_ACCOUNT_CACHE_TTL_SECONDS}s)",
         "updated_at": None},
    ] + monitoring.cache_summary()
    # Reachability per integration, straight out of the cache above.
    connections = [
        {"name": integration_names.get(iid, f"integration {iid}"),
         "reachable": entry["status"]["reachable"],
         "checked_at": entry["checked_at"]}
        for iid, entry in sorted(_integration_status_cache.items())
    ]
    return entries, connections


def _clear_all_caches():
    """Drops every in-memory cache in the app and bumps the static-asset cache-buster
    so browsers re-fetch CSS/JS too.

    Each module clears its own globals (monitoring/integrations/updater) rather than
    this function reaching into them - a cache added over there should not need an
    edit over here to be covered by this button."""
    _uptime_cache["value"] = {}
    _uptime_cache["fetched_at"] = 0.0
    _integration_status_cache.clear()
    integrations.clear_caches()
    monitoring.clear_caches()
    updater.clear_update_cache()
    user_notify.clear_caches()
    _bump_asset_cache_salt()


@app.route("/admin/system/clear-caches", methods=["POST"])
@login_required
def admin_system_clear_caches():
    """Deliberately not behind _require_totp(), unlike the restart buttons next to it:
    nothing is destroyed and nothing goes offline - the worst case is one slightly
    emptier page until the background loop's next tick refills things."""
    _clear_all_caches()
    flash("Cached data cleared. Fresh values are fetched on the next background "
          "check, and browsers will re-download the page's CSS/JS.", "success")
    return redirect(url_for("admin_system"))


def _all_static_asset_urls():
    """Every CSS/JS file this app serves, as cache-busted URLs. Used by the
    clear-browser-cache page to re-fetch each one with `cache: 'reload'`, which is
    what actually replaces a browser's stored copy - a directory listing rather than
    a hand-maintained list, so a file added later can't be quietly left behind."""
    urls = []
    for subdir in ("css", "js"):
        directory = os.path.join(app.root_path, "static", subdir)
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        urls += [asset_url(f"{subdir}/{name}") for name in names
                 if name.endswith((".css", ".js"))]
    logo = db.get_setting("site_logo_filename", "")
    if logo:
        urls.append(url_for("static", filename=f"uploads/{logo}"))
    return urls


@app.route("/admin/system/clear-browser-cache", methods=["POST"])
@login_required
def admin_system_clear_browser_cache():
    """Clears *this* browser's own cached copy of the site - a different thing from
    the server-side button next to it, and worth keeping separate rather than merging
    the two.

    The server-side one changes what every visitor is *asked* to download (it bumps
    the ?v= salt, so their next request is for a URL they've never seen). This one
    only reaches the browser that clicked it, because that's all a web page can
    reach - there is no way to reach into someone else's browser and evict a file.
    Which is exactly why both exist: this one is for "I'm looking at a stale page
    right now", the other is for "make sure nobody else is".

    Two mechanisms, because neither is sufficient alone:

    - The Clear-Site-Data response header is the standards-based way, and it clears
      things a page cannot touch itself. Chrome and Edge only honor it in a secure
      context, and this portal is very often served over plain HTTP on a LAN or
      Tailscale, so it can't be relied on here.
    - The page's own script then re-does what it can reach (Cache Storage, service
      workers, DOM storage) and re-fetches every asset with `cache: 'reload'`, which
      forces a network fetch and replaces the stored copy.

    Deliberately NOT sending the "cookies" directive: that would clear the session
    cookie and sign the admin out as a side effect of a cache action."""
    theme = request.form.get("theme", "")
    response = Response(render_template("admin_clear_browser_cache.html",
                                         assets=_all_static_asset_urls(), theme=theme,
                                         active="system"))
    response.headers["Clear-Site-Data"] = '"cache", "storage"'
    return response


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
def _update_check_task_view():
    """The `update_check` task's row as the admin page sees it, or None if updater.py
    somehow didn't register it (a build with the task removed shouldn't 500 here)."""
    task = scheduler.get_task(updater.TASK_NAME)
    return scheduler.task_view(task) if task else None


def _update_check_schedule_label():
    """How often the check runs, in words, for the About page - which shouldn't have
    to know that a schedule can be either an interval or a daily time."""
    view = _update_check_task_view()
    if not view:
        return "on a schedule"
    if view["schedule_kind"] == "daily":
        return f"daily at {view['daily_at']} UTC"
    minutes = view["interval_minutes"] or 0
    if minutes >= 120 and minutes % 60 == 0:
        return f"every {minutes // 60}h"
    return f"every {minutes} min"


def _set_update_check_enabled(enabled):
    """Flips the scheduled task on or off without touching its schedule."""
    view = _update_check_task_view()
    if not view:
        return
    scheduler.save_schedule(updater.TASK_NAME, enabled=enabled,
                             schedule_kind=view["schedule_kind"],
                             interval_minutes=view["interval_minutes"],
                             daily_at=view["daily_at"])


@app.route("/admin/about")
@login_required
def admin_about():
    """Reads the update-check cache only - never checks GitHub inline. The cache is
    refreshed by the `update_check` scheduled task (see /admin/tasks), same
    read-the-cache pattern as _integration_status_cache. A cache miss (nothing checked
    yet this process, or checking disabled) renders as "not checked yet" rather than
    blocking the page on an outbound call."""
    return render_template(
        "admin_about.html",
        version=config.VERSION,
        version_display=config.VERSION_DISPLAY,
        is_git_checkout=config.IS_GIT_CHECKOUT,
        update_status=updater.get_cached_update_status(),
        channel=updater.get_channel(),
        channels=updater.CHANNELS,
        check_enabled=updater.update_check_enabled(),
        check_schedule=_update_check_schedule_label(),
        inapp_update_enabled=config.ENABLE_INAPP_UPDATE,
        repo_url=updater.REPO_URL,
        releases_url=updater.RELEASES_PAGE_URL,
        backups=list(reversed(updater.list_backups()))[:5],
        db_backups=list_db_safety_backups(),
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
    # Writes the scheduled task's own enabled flag rather than a second setting beside
    # it: /admin/about and /admin/tasks are two views of one row, so they cannot drift
    # into disagreeing about whether the portal checks for updates. The rest of the
    # schedule is preserved - this checkbox is an on/off switch, not a reschedule.
    _set_update_check_enabled(bool(request.form.get("update_check_enabled")))
    # The cached result belongs to the old channel - drop it so the page doesn't show
    # a stale "latest stable" reading next to a freshly-selected unstable channel.
    updater.clear_update_cache()
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


# ---------------------------------------------------------------------------
# Restoring the database from a backup zip
# ---------------------------------------------------------------------------
# The counterpart to Settings' "download a backup" button, and by some distance the
# most destructive thing in this admin panel: it replaces the entire live database with
# an uploaded file. It lives on the About page rather than in Settings because someone
# reaching for a restore is usually recovering from something and will look next to the
# update-rollback machinery first.
#
# Do not confuse the two kinds of backup on that page, and do not let the UI confuse
# them either. updater.py's backups are **application code**, taken to undo a bad
# update, and contain no data. These are **database** snapshots and contain nothing
# else. Neither can restore the other.
DB_SAFETY_BACKUP_DIR = os.path.join(config.APP_ROOT, "instance", "db_backups")
KEEP_DB_SAFETY_BACKUPS = 5

# An extracted database is capped separately from the upload itself, so a small zip
# that expands to an enormous file (a zip bomb) is refused before it is written out.
#
# Genuinely independent from DB_RESTORE_MAX_BYTES, not just named differently - a
# real SQLite database routinely compresses well (repeated status/timestamp text in
# status_history especially), which is the entire point of accepting a zip instead
# of requiring a bare .db upload: it lets a database larger than the raw upload cap
# through. Reusing DB_RESTORE_MAX_BYTES here (as this used to do) defeated that -
# a real backup that zipped down to well under the upload cap could still have its
# *uncompressed* size rejected by this check, at whatever compression ratio the
# database happened to hit. Confirmed in the wild: a 140MB database zipping to 30MB
# (a real, legitimate backup, nowhere near the 64MB upload cap) was refused here
# because 140MB > the old 64MB extraction cap.
MAX_EXTRACTED_DB_BYTES = 512 * 1024 * 1024


def _write_uploaded_database(upload, dest_path):
    """The uploaded file -> a plain .db at `dest_path`. Returns None, or a reason.

    Accepts either the zip the backup button produces or a bare .db, because an admin
    who unzipped it to look inside should not be told their own backup is invalid.

    Nothing here inspects the *contents*; that's db.validate_backup_file()'s job. This
    only gets the bytes safely onto disk - which for a zip means never trusting the
    declared size, and never trusting a member name (a member called `../../app.py` is
    the classic zip-slip, avoided here by never joining a member name to a path at all:
    the single member is streamed to a filename we chose)."""
    filename = (upload.filename or "").lower()
    try:
        if filename.endswith(".zip"):
            with zipfile.ZipFile(upload.stream) as zf:
                members = [m for m in zf.infolist()
                            if not m.is_dir() and m.filename.lower().endswith(".db")]
                if not members:
                    return "That zip doesn't contain a .db file."
                if len(members) > 1:
                    return f"That zip contains {len(members)} .db files - expected exactly one."
                member = members[0]
                if member.file_size > MAX_EXTRACTED_DB_BYTES:
                    return (f"The database inside that zip is too large "
                            f"({member.file_size // (1024 * 1024)} MB).")
                written = 0
                with zf.open(member) as src, open(dest_path, "wb") as out:
                    while True:
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        # Checked against what was actually read, not against the
                        # header's declared file_size, which a hostile zip can lie about.
                        written += len(chunk)
                        if written > MAX_EXTRACTED_DB_BYTES:
                            return "The database inside that zip is too large."
                        out.write(chunk)
        else:
            upload.save(dest_path)
    except zipfile.BadZipFile:
        return "That file isn't a readable zip."
    except OSError as e:
        return f"Could not read the uploaded file: {e}"
    return None


def _remove_sqlite_sidecars(path):
    """Deletes the -wal/-shm files SQLite creates beside `path`, if any.

    Opening a WAL-mode database creates them even for a read-only connection, so simply
    validating an uploaded backup leaves two files next to the staged copy. Best-effort:
    failing to tidy up must never turn a successful restore into a reported failure."""
    if not path:
        return
    for suffix in ("-wal", "-shm"):
        try:
            os.remove(path + suffix)
        except OSError:
            pass


def _db_safety_snapshot():
    """A consistent snapshot of the database as it is *right now*, taken before it is
    replaced. This is the whole reason a bad restore isn't unrecoverable, so it happens
    after validation (no point snapshotting for a file we're going to reject) and before
    a single byte of the live database is touched.

    Uses db.backup_to_file(), i.e. SQLite's own online backup API - a plain file copy
    could catch a torn write from the background health-check thread."""
    os.makedirs(DB_SAFETY_BACKUP_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = os.path.join(DB_SAFETY_BACKUP_DIR, f"portal-before-restore-{stamp}.db")
    db.backup_to_file(path)
    return path


def list_db_safety_backups():
    """Newest first. A local os.listdir + stat, the same cheap class of call as
    asset_url()'s getmtime - not the kind of slow outbound I/O the no-blocking-in-a-
    request-handler rule is about, and not worth a cache."""
    try:
        names = [n for n in os.listdir(DB_SAFETY_BACKUP_DIR) if n.endswith(".db")]
    except OSError:
        return []
    entries = []
    for name in names:
        path = os.path.join(DB_SAFETY_BACKUP_DIR, name)
        try:
            stat = os.stat(path)
        except OSError:
            continue
        entries.append({"name": name, "path": path,
                        "size_mb": round(stat.st_size / (1024 * 1024), 2),
                        "created_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()})
    return sorted(entries, key=lambda e: e["created_at"], reverse=True)


def _prune_db_safety_backups():
    """Keeps the newest KEEP_DB_SAFETY_BACKUPS. Reads the module constant inside the
    function rather than as a default argument, so a monkeypatched value in a test is
    actually honoured - a default arg binds at def time, the same trap that made
    updater._prune_backups pass for the wrong reason once."""
    for entry in list_db_safety_backups()[KEEP_DB_SAFETY_BACKUPS:]:
        try:
            os.remove(entry["path"])
        except OSError:
            _logger.warning("Could not prune old database snapshot %s", entry["name"])


@app.route("/admin/about/restore-db", methods=["POST"])
@login_required
def admin_restore_db():
    """Replaces the live database with an uploaded backup, then restarts.

    Behind _require_totp() like the other actions where a stolen session cookie alone
    must not be enough (host restart/shutdown, app restart, self-update) - this one
    replaces every piece of data the portal holds, so it belongs in that set rather than
    beside the harmless cache buttons.

    The order below is the safety machinery and is not rearrangeable:
      1. write the upload to a temp file - the live database is untouched;
      2. validate it is a well-formed SQLite database *and* one of ours;
      3. snapshot the current database, so a regretted restore is still recoverable;
      4. atomically replace, dealing with the WAL sidecars (see db.restore_from_file);
      5. restart, because every existing connection still points at the old file.
    """
    blocked = _require_totp("Enter your 2FA code to restore the database.", "admin_about")
    if blocked:
        return blocked

    upload = request.files.get("backup")
    if not upload or not upload.filename:
        flash("Choose a backup file to restore.", "error")
        return redirect(url_for("admin_about"))

    # Staged next to the database rather than in the system temp directory, so the final
    # os.replace() is a rename within one filesystem (atomic) rather than a cross-device
    # copy - and so a large upload can't fill a small /tmp.
    os.makedirs(os.path.dirname(db.DB_PATH), exist_ok=True)
    fd, staged = tempfile.mkstemp(prefix="restore-", suffix=".db",
                                   dir=os.path.dirname(db.DB_PATH))
    os.close(fd)
    snapshot = None
    installed = None
    try:
        error = _write_uploaded_database(upload, staged)
        if error is None:
            error = db.validate_backup_file(staged)
        if error:
            flash(f"Restore refused: {error} Your database has not been touched.", "error")
            return redirect(url_for("admin_about"))

        try:
            snapshot = _db_safety_snapshot()
        except Exception as e:
            _logger.exception("Could not snapshot the database before restoring")
            flash(f"Restore aborted: couldn't back up your current database first ({e}). "
                  "Nothing has been changed.", "error")
            return redirect(url_for("admin_about"))

        try:
            db.restore_from_file(staged)
        except Exception as e:
            _logger.exception("Database restore failed")
            flash(f"Restore failed: {e}. Your previous database was saved to "
                  f"{snapshot} before the attempt.", "error")
            return redirect(url_for("admin_about"))
        # The staged file has been renamed onto the live path, so it must not be
        # deleted below - but its sidecars still need clearing, hence the separate flag
        # rather than just dropping the name.
        installed = staged
        staged = None
        _prune_db_safety_backups()
    finally:
        if staged and os.path.exists(staged):
            try:
                os.remove(staged)
            except OSError:
                pass
        # Validating the upload opens it with SQLite, and opening a WAL-mode database -
        # which every backup of this app is - creates -wal/-shm sidecars beside it. The
        # rename above moves only the main file, so without this every restore left two
        # orphaned files in instance/ forever. Found by looking at the directory after a
        # release re-test, not by any test.
        _remove_sqlite_sidecars(installed or staged)

    _logger.warning("Database restored from an uploaded backup; previous database saved to %s",
                     os.path.basename(snapshot))
    flash(f"Database restored. Your previous database was saved as "
          f"{os.path.basename(snapshot)} in instance/db_backups/. Restarting now - this "
          f"page will be briefly unreachable.", "success")
    _restart_process()
    return redirect(url_for("admin_about"))


# ---- Integrations (read-only Jellyfin/Jellyseerr/*Arr status) ----
@app.route("/admin/integrations")
@login_required
def admin_integrations():
    configured = db.list_integrations()
    statuses = {i["id"]: _integration_status_cache.get(i["id"]) for i in configured if i["enabled"]}
    return render_template("admin_integrations.html", integrations=configured, statuses=statuses,
                            check_interval=config.CHECK_INTERVAL_SECONDS,
                            version_check=version_checks.get_results(),
                            seerr_choices=[i for i in db.list_integrations()
                                            if i["kind"] == "jellyseerr" and i["enabled"]],
                            seerr_chosen=db.get_setting(integrations.SEERR_INTEGRATION_SETTING, ""),
                            seerr_in_use=integrations.seerr_integration(),
                            # Popped, so the report shows once after the button is
                            # pressed rather than sticking around looking like live state.
                            seerr_diagnosis=session.pop("seerr_diagnosis", None),
                            version_task=scheduler.task_view(scheduler.get_task(version_checks.TASK_NAME)),
                            active="integrations")


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


@app.route("/admin/integrations/seerr-instance", methods=["POST"])
@login_required
def admin_seerr_instance():
    """Which Seerr backs search, requests, approval alerts and contact sync.

    Same shape as the Jellyfin instance that backs user sign-in. It matters once more
    than one exists: before this, each feature independently took "the first enabled
    one", so the Integrations page could be diagnosing one server while search talked
    to another."""
    raw = request.form.get("seerr_integration_id", "").strip()
    db.set_setting(integrations.SEERR_INTEGRATION_SETTING, raw if raw.isdigit() else "")
    integrations.clear_caches()
    flash("Seerr instance saved.", "success")
    return redirect(url_for("admin_integrations"))


@app.route("/admin/integrations/<int:iid>/diagnose", methods=["POST"])
@login_required
def admin_integration_diagnose(iid):
    """Runs Seerr's health check and its search call back to back and reports both.

    A sanctioned explicit-slow-action, exactly like "Check now" beside it. It exists
    because "search says Seerr is down, this page says it's up" is a genuinely confusing
    pair of facts, and the two come from different calls with different dependencies -
    the health endpoint is local to Seerr, search proxies to TMDB. Rather than guess
    which difference mattered, this measures both against the real instance."""
    integration = db.get_integration(iid)
    if not integration or integration["kind"] != "jellyseerr":
        flash("Search diagnostics only apply to a Jellyseerr/Overseerr integration.", "error")
        return redirect(url_for("admin_integrations"))
    # The admin's own failing search term, so the diagnostic reproduces the actual
    # problem rather than proving that the word "test" works. A 400 that only happens
    # for real queries is a 400 about the query.
    query = request.form.get("query", "").strip()[:100] or "test"
    report = integrations.diagnose_seerr(integration["base_url"], integration["api_key"], query)
    session["seerr_diagnosis"] = report
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
        service_data["show_run_target_public"] = 1 if request.form.get("show_run_target_public") else 0
        service_data["show_dependencies_public"] = 1 if request.form.get("show_dependencies_public") else 0
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
    return render_template("admin_new_combined.html", defaults=_service_defaults(),
                            vms=monitoring.get_cached_vm_snapshot(), hostname=platform.node(), active="services")


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
                            show_media=_public_media_visibility(),
                            media_calendar_days=integrations.calendar_days(),
                            max_calendar_days=integrations.MAX_CALENDAR_DAYS,
                            media_requires_login=_media_requires_login(),
                            refresh_seconds=config.PUBLIC_REFRESH_SECONDS,
                            site_name=db.get_setting("site_name", "Server"),
                            show_public=_public_resource_visibility(),
                            highload_thresholds=integrations.high_load_thresholds(),
                            section_order=section_order, section_labels=section_labels,
                            service_defaults=_service_defaults(),
                            public_history_days=db.get_setting("public_history_days", ""),
                            lowdisk_percent_threshold=db.get_setting("lowdisk_percent_threshold", ""),
                            admin_session_timeout_hours=db.get_setting(
                                "admin_session_timeout_hours", str(DEFAULT_SESSION_TIMEOUT_HOURS)),
                            max_session_timeout_hours=MAX_SESSION_TIMEOUT_HOURS,
                            session_cookie_max_age_days=config.SESSION_COOKIE_MAX_AGE_DAYS,
                            status_history_retention_days=db.get_setting(
                                "status_history_retention_days", str(DEFAULT_HISTORY_RETENTION_DAYS)),
                            vms=monitoring.get_cached_vm_snapshot(), hostname=platform.node(),
                            active="settings")


@app.route("/admin/settings/general", methods=["POST"])
@login_required
def admin_settings_general():
    db.set_setting("site_name", request.form.get("site_name", "").strip() or "Server")
    for key in _PUBLIC_RESOURCE_KEYS:
        db.set_setting(key, "1" if request.form.get(key) else "0")
    for key in _PUBLIC_MEDIA_KEYS:
        db.set_setting(key, "1" if request.form.get(key) else "0")
    calendar_days = request.form.get("media_calendar_days", "").strip()
    db.set_setting("media_calendar_days",
                    calendar_days if calendar_days.isdigit() else str(integrations.DEFAULT_CALENDAR_DAYS))
    db.set_setting("media_requires_login", "1" if request.form.get("media_requires_login") else "0")
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
    # Both stored as plain digit strings; a non-numeric submission falls back to the
    # default rather than being stored blank, since neither has a meaningful
    # "unset" state the way the two optional thresholds above do.
    timeout_hours = request.form.get("admin_session_timeout_hours", "").strip()
    db.set_setting("admin_session_timeout_hours",
                    timeout_hours if timeout_hours.isdigit() else str(DEFAULT_SESSION_TIMEOUT_HOURS))
    retention = request.form.get("status_history_retention_days", "").strip()
    db.set_setting("status_history_retention_days",
                    retention if (retention.isdigit() and int(retention) > 0)
                    else str(DEFAULT_HISTORY_RETENTION_DAYS))
    for key in SERVICE_DEFAULT_FIELDS:
        raw = request.form.get(f"service_default_{key}", "").strip()
        db.set_setting(f"service_default_{key}", raw if raw.isdigit() else "")
    db.set_setting("service_default_auto_incident", "1" if request.form.get("service_default_auto_incident") else "0")
    api_mode = request.form.get("service_default_api_health_mode", "off")
    db.set_setting("service_default_api_health_mode", api_mode if api_mode in db.API_HEALTH_MODES else "off")
    db.set_setting("service_default_run_target", request.form.get("service_default_run_target", "").strip())
    db.set_setting("service_default_show_run_target_public",
                    "1" if request.form.get("service_default_show_run_target_public") else "0")
    db.set_setting("service_default_show_dependencies_public",
                    "1" if request.form.get("service_default_show_dependencies_public") else "0")
    flash("Settings updated.", "success")
    return redirect(url_for("admin_settings"))


# ---- Notifications ----
@app.route("/admin/notifications", methods=["GET", "POST"])
@login_required
def admin_notifications():
    """The hub for everything notification-related, which used to be scattered: the
    channel status lived in a paragraph at the bottom of Settings, and the Discord bot
    had its own top-level nav entry unrelated to either. Grouping them means "how do I
    get told about this" has one answer."""
    if request.method == "POST":
        db.set_setting(notifications.RECIPIENTS_SETTING,
                        notifications.normalize_recipients(request.form.get("recipients", "")))
        flash("Email recipients saved.", "success")
        return redirect(url_for("admin_notifications"))
    channels = notifications.channel_summary()
    return render_template("admin_notifications.html", channels=channels,
                            any_configured=any(c["configured"] for c in channels),
                            recipients=", ".join(notifications.email_recipients()),
                            email_host_configured=bool(config.SMTP_HOST),
                            legacy_env_recipients=bool(config.SMTP_TO)
                                and not db.get_setting(notifications.RECIPIENTS_SETTING, "").strip(),
                            active="notifications")


@app.route("/admin/notifications/users", methods=["GET", "POST"])
@login_required
def admin_user_notifications():
    """The master switch for per-user notifications, plus what the delivery queue is
    doing, plus the portal-wide baseline every unconfigured user starts from. The
    per-*person* settings deliberately aren't here: what someone wants to be told
    about, and where, is theirs to set on their own account page - "Default"/
    "Override" below are two different, explicit ways to reach past that."""
    if request.method == "POST":
        db.set_setting("user_notifications_enabled",
                        "1" if request.form.get("enabled") else "0")
        db.set_setting("seerr_email_events_enabled",
                        "1" if request.form.get("seerr_email_events_enabled") else "0")
        flash("Per-user notification settings saved.", "success")
        return redirect(url_for("admin_user_notifications"))

    task = scheduler.get_task(user_notify.TASK_NAME)
    channels = notifications.channel_summary()
    return render_template(
        "admin_user_notifications.html",
        enabled=user_notify.is_enabled(),
        seerr_email_events_enabled=user_notify.seerr_email_enabled(),
        queue=db.notification_queue_summary(),
        recent=db.recent_notifications(),
        email_ready=next((c["configured"] for c in channels if c["key"] == "email"), False),
        bot_ready=bool(config.DISCORD_BOT_TOKEN) and discord_bot.get_status()["connected"],
        bot_token_configured=bool(config.DISCORD_BOT_TOKEN),
        seerr_configured=user_notify.seerr_integration() is not None,
        max_attempts=db.MAX_NOTIFICATION_ATTEMPTS,
        task=scheduler.task_view(task) if task else None,
        toggle_fields=db.NOTIFICATION_TOGGLE_FIELDS,
        toggle_labels=db.NOTIFICATION_TOGGLE_LABELS,
        defaults=db.notification_defaults(),
        active="user-notifications")


@app.route("/admin/notifications/users/defaults", methods=["POST"])
@login_required
def admin_notification_defaults():
    """Saves the portal-wide baseline for each toggle - what a user who has never set
    their own preferences starts with. Never touches anyone who already has, which is
    exactly what makes this the non-destructive half of the pair; see
    admin_notification_override() for the other one."""
    for field in db.NOTIFICATION_TOGGLE_FIELDS:
        db.set_setting(f"{db.NOTIFY_DEFAULT_SETTING_PREFIX}{field}",
                        "1" if request.form.get(field) else "0")
    flash("Default notification settings saved. This only applies to new or "
          "unconfigured users - it does not change anyone's existing choice.", "success")
    return redirect(url_for("admin_user_notifications"))


@app.route("/admin/notifications/users/override", methods=["POST"])
@login_required
def admin_notification_override():
    """Force-applies one already-saved default to every existing user right now,
    discarding their individual choice for that one setting. Reads the *saved* default
    rather than trusting a value posted alongside the button, so this can never apply
    something other than what the admin sees checked on this page - and reads it fresh
    with defaults()[field] rather than trusting stale form state to describe what
    "the default" currently is."""
    field = request.form.get("field", "")
    if field not in db.NOTIFICATION_TOGGLE_FIELDS:
        flash("Unknown setting.", "error")
        return redirect(url_for("admin_user_notifications"))
    value = db.notification_defaults()[field]
    db.override_user_preference(field, value)
    label = db.NOTIFICATION_TOGGLE_LABELS[field]
    flash(f"Applied \"{label[0]}: {label[1]}\" = {'on' if value else 'off'} to every "
          "existing user, discarding their own choice for this one setting.", "success")
    return redirect(url_for("admin_user_notifications"))


@app.route("/admin/notifications/seerr", methods=["GET", "POST"])
@login_required
def admin_seerr_alerts():
    """Seerr approval alerts: how many requests are waiting, and who gets DM'd when a
    new one arrives.

    Under Notifications rather than Integrations because the interesting part is the
    delivery - the count is one number, the DM configuration is the feature. The count
    is deliberately admin-only and never appears on the public page: it's operational
    information about a queue, not a signal about whether anything is working."""
    if request.method == "POST":
        db.set_setting(seerr_alerts.DM_ENABLED_SETTING,
                        "1" if request.form.get("dm_enabled") else "0")
        db.set_setting("discordbot_dm_user_ids",
                        discord_bot.normalize_dm_user_ids(request.form.get("dm_user_ids", "")))
        flash("Seerr alert settings saved.", "success")
        return redirect(url_for("admin_seerr_alerts"))

    task = scheduler.get_task(seerr_alerts.TASK_NAME)
    return render_template(
        "admin_seerr_alerts.html",
        integration=seerr_alerts.seerr_integration(),
        pending_count=seerr_alerts.pending_count(),
        last_checked_at=seerr_alerts.last_checked_at(),
        dm_enabled=seerr_alerts.dm_enabled(),
        dm_user_ids=db.get_setting("discordbot_dm_user_ids", ""),
        bot_token_configured=bool(config.DISCORD_BOT_TOKEN),
        bot_status=discord_bot.get_status(),
        task=scheduler.task_view(task) if task else None,
        active="seerr-alerts")


@app.route("/admin/notifications/test", methods=["POST"])
@login_required
def admin_notifications_test():
    """Goes through notifications.notify() with a canned payload - the exact dispatch
    path a real incident alert takes, not a parallel one, so this confirms the whole
    chain rather than just that a setting is non-empty. notify() is deliberately
    fire-and-forget (it catches and logs every failure and never raises), so this
    can't report per-channel success; the admin checks their channel, which is the
    point of the button."""
    notifications.notify("Test notification",
                          "This is a test from your status portal. If you can read this, "
                          "notifications are working.")
    flash("Test notification sent. Check your channel(s) - delivery failures are "
          "logged, not reported back here.", "success")
    return redirect(url_for("admin_notifications"))


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
    # max_age=0 overrides SEND_FILE_MAX_AGE_DEFAULT: this is a point-in-time
    # download, not a versioned static asset, and must never be re-served from cache.
    return send_file(buffer, mimetype="application/zip", as_attachment=True,
                      download_name=f"portal-backup-{stamp}.zip", max_age=0)


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


# ---- User accounts (Jellyfin-backed sign-in for visitors - completely separate
# from the single-admin password login above; see jellyfin_auth.py) ----
DEFAULT_USER_SESSION_TIMEOUT_HOURS = 168  # a week


def _user_session_timeout_seconds():
    """None = no idle timeout. Clamped to the same upper bound as the admin session
    for the same reason: anything beyond the cookie's own Max-Age is a promise this
    can't keep. Defaults far longer than the admin's, deliberately - a visitor
    signing in to file a problem report is not holding privileged access, and making
    them re-authenticate every twelve hours would just make the feature annoying."""
    raw = db.get_setting("user_session_timeout_hours", str(DEFAULT_USER_SESSION_TIMEOUT_HOURS))
    hours = int(raw) if raw.isdigit() else DEFAULT_USER_SESSION_TIMEOUT_HOURS
    if hours <= 0:
        return None
    return min(hours, MAX_SESSION_TIMEOUT_HOURS) * 3600


@app.route("/admin/users")
@login_required
def admin_users():
    return render_template("admin_users.html",
                            contacts=db.list_seerr_contacts(),
                            contacts_synced_at=db.seerr_contacts_synced_at(),
                            notifications_enabled=user_notify.is_enabled(),
                            summary=jellyfin_auth.status_summary(),
                            jellyfin_integrations=[i for i in db.list_integrations()
                                                   if i["kind"] == "jellyfin" and i["enabled"]],
                            selected_integration_id=db.get_setting("jellyfin_auth_integration_id", ""),
                            report_requires_login=db.get_setting("report_requires_login", "1") == "1",
                            user_session_timeout_hours=db.get_setting(
                                "user_session_timeout_hours", str(DEFAULT_USER_SESSION_TIMEOUT_HOURS)),
                            max_session_timeout_hours=MAX_SESSION_TIMEOUT_HOURS,
                            users=db.list_jellyfin_users(),
                            blocked_count=sum(1 for u in db.list_jellyfin_users()
                                              if not u["portal_allowed"]),
                            sync_task_name=jellyfin_auth.TASK_NAME,
                            active="users")


@app.route("/admin/users/settings", methods=["POST"])
@login_required
def admin_users_settings():
    db.set_setting("jellyfin_auth_enabled", "1" if request.form.get("jellyfin_auth_enabled") else "0")
    chosen = request.form.get("jellyfin_auth_integration_id", "").strip()
    db.set_setting("jellyfin_auth_integration_id", chosen if chosen.isdigit() else "")
    db.set_setting("report_requires_login", "1" if request.form.get("report_requires_login") else "0")
    timeout_hours = request.form.get("user_session_timeout_hours", "").strip()
    db.set_setting("user_session_timeout_hours",
                    timeout_hours if timeout_hours.isdigit() else str(DEFAULT_USER_SESSION_TIMEOUT_HOURS))
    flash("User account settings updated.", "success")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<user_id>/access", methods=["POST"])
@login_required
def admin_user_access(user_id):
    """Blocks or unblocks one Jellyfin user's access to *this portal*. Never touches
    their Jellyfin account - this portal has no business disabling someone's media
    server login, and an admin who wants that does it in Jellyfin (where the next
    sync will pick it up on its own).

    Takes effect immediately: _enforce_user_session re-checks this on every request,
    so blocking someone ends the session they are sitting in rather than waiting for
    it to expire."""
    user = db.get_jellyfin_user(user_id)
    if not user:
        flash("No such user in the cached Jellyfin user list.", "error")
        return redirect(url_for("admin_users"))
    allow = request.form.get("allow") == "1"
    db.set_jellyfin_user_allowed(user_id, allow)
    _logger.info("Portal access %s for Jellyfin user '%s'",
                  "granted" if allow else "blocked", user["name"])
    flash(f"{user['name']} can {'now sign in' if allow else 'no longer sign in'} to this portal.",
          "success")
    return redirect(url_for("admin_users"))


# ---- Discord bot (separate from the simple webhook notifications above) ----
@app.route("/admin/users/<user_id>/contact", methods=["POST"])
@login_required
def admin_user_contact(user_id):
    """Fills in a Jellyfin user's email/Discord ID on their behalf.

    Seerr is where this data lives, so this writes through to Seerr rather than keeping
    a private copy - see user_notify.save_contact(). It exists because Seerr often
    simply doesn't have these filled in, and an admin who knows the answer shouldn't
    have to go and set it up in another app first."""
    user = db.get_jellyfin_user(user_id)
    next_url = _safe_next_url(request.form.get("next")) or url_for("admin_users")
    if not user:
        flash("No such user in the cached Jellyfin user list.", "error")
        return redirect(next_url)
    ok, message = user_notify.save_contact(
        user_id,
        email=request.form.get("notify_email", "").strip()[:200],
        discord_id=request.form.get("notify_discord_id", "").strip()[:32])
    flash(f"{user['name']}: {message}" if message else f"Saved for {user['name']}.",
          "success" if ok else "error")
    return redirect(next_url)


@app.route("/admin/users/<user_id>/account", methods=["GET", "POST"])
@login_required
def admin_user_account(user_id):
    """The admin's view of one visitor's own account.html page - the "better
    alternative" to a separate admin-only settings grid: one template, one set of
    fields, so the two audiences can't drift apart. Reuses _save_account_prefs() (the
    visitor's own POST handler's logic) and user_notify.adopt_seerr_contact() (the
    auto-fill/manual-import logic, on GET below) rather than re-implementing either.

    The report thread is deliberately not shown here - /admin/reports is already the
    admin's UI for that, and reusing this page for it would be exactly the second UI
    this route exists to avoid."""
    target = db.get_jellyfin_user(user_id)
    if not target:
        return render_template("error.html", code=404,
                               message="No such user in the cached Jellyfin user list."), 404

    if request.method == "POST":
        _save_account_prefs(user_id, request.form)
        flash(f"Saved settings for {target['name']}.", "success")
        return redirect(url_for("admin_user_account", user_id=user_id))

    prefs = db.get_user_preferences(user_id)
    seerr_account = user_notify.find_seerr_account(user_id) if user_notify.is_enabled() else None
    # Same auto-fill user_account() does for a visitor's own first visit - this route
    # used to only reach adopt_seerr_contact() via the manual "Use these details here"
    # button, which meant an admin browsing many users always had one extra click per
    # user even though the visitor-facing page already did this automatically. Same
    # "both fields still blank" guard, so it still only ever fires once per user and
    # never overwrites a choice (theirs or a previous auto-fill) already on file.
    if seerr_account and not prefs["notify_email"] and not prefs["notify_discord_id"] \
            and (seerr_account["email"] or seerr_account["discord_id"]):
        user_notify.adopt_seerr_contact(user_id, seerr_account)
        prefs = db.get_user_preferences(user_id)
        flash(f"Filled in {target['name']}'s contact details from Seerr — edit or clear them below.",
              "success")
    return render_template("account.html",
                            admin_viewing=True,
                            target_user=target,
                            reports=[],
                            prefs=prefs,
                            notifications_enabled=user_notify.is_enabled(),
                            seerr_account=seerr_account,
                            seerr_configured=user_notify.seerr_integration() is not None,
                            themes=db.USER_THEMES,
                            just_saved=False,
                            site_name=db.get_setting("site_name", "Server"),
                            report_statuses=REPORT_STATUS_LABELS)


@app.route("/admin/users/<user_id>/seerr/import", methods=["POST"])
@login_required
def admin_user_account_import_seerr(user_id):
    """The admin-viewing-a-user's-account equivalent of the visitor's own "Use these
    details here" button - same underlying write (user_notify.adopt_seerr_contact()),
    triggered by the admin instead of the person themselves."""
    target = db.get_jellyfin_user(user_id)
    if not target:
        flash("No such user in the cached Jellyfin user list.", "error")
        return redirect(url_for("admin_users"))
    account = user_notify.find_seerr_account(user_id)
    if not account:
        flash(f"Couldn't find a Seerr account linked to {target['name']}'s Jellyfin login.", "error")
    else:
        user_notify.adopt_seerr_contact(user_id, account)
        flash(f"Copied {target['name']}'s contact details from Seerr.", "success")
    return redirect(url_for("admin_user_account", user_id=user_id))


def _send_test_notification(user_id, channel):
    """Shared body of the two test-notification routes below. Runs synchronously - the
    same sanctioned one-shot-admin-action exception admin_notifications_test() already
    uses - so the flash can report the real outcome (e.g. "Discord refused the DM")
    rather than "queued, check back later"."""
    target = db.get_jellyfin_user(user_id)
    if not target:
        flash("No such user in the cached Jellyfin user list.", "error")
        return redirect(url_for("admin_users"))
    site_name = db.get_setting("site_name", "Server")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    ok, detail = user_notify.send_direct(
        user_id, channel, "Test notification",
        f"This is a test notification from {site_name}, sent at {stamp}.")
    label = "Discord DM" if channel == "discord" else "email"
    if ok:
        flash(f"Test {label} to {target['name']} sent." + (f" {detail}" if detail else ""),
              "success")
    else:
        flash(f"Test {label} to {target['name']} failed: {detail}", "error")
    return redirect(url_for("admin_user_account", user_id=user_id))


@app.route("/admin/users/<user_id>/test/discord", methods=["POST"])
@login_required
def admin_user_test_discord(user_id):
    return _send_test_notification(user_id, "discord")


@app.route("/admin/users/<user_id>/test/email", methods=["POST"])
@login_required
def admin_user_test_email(user_id):
    return _send_test_notification(user_id, "email")


@app.route("/admin/users/<user_id>/message", methods=["POST"])
@login_required
def admin_user_message(user_id):
    """A free-text message to one person, sent right now on the admin's chosen
    channel. Uses the same user_notify.send_direct() the test-notification buttons
    do, for the same reason: an explicit one-to-one admin action, so it bypasses the
    recipient's own channel preferences (it only fails when there's no contact detail
    for the chosen channel at all) and runs synchronously so the flash can report the
    real outcome. No history is kept - unlike the announcement send log, this was
    scoped as a one-off, not something an admin needs to look back on later."""
    target = db.get_jellyfin_user(user_id)
    if not target:
        flash("No such user in the cached Jellyfin user list.", "error")
        return redirect(url_for("admin_users"))
    channel = request.form.get("channel", "")
    body = request.form.get("body", "").strip()
    if channel not in ("discord", "email"):
        flash("Choose a channel to send on.", "error")
        return redirect(url_for("admin_user_account", user_id=user_id))
    if not body:
        flash("Message can't be empty.", "error")
        return redirect(url_for("admin_user_account", user_id=user_id))
    site_name = db.get_setting("site_name", "Server")
    ok, detail = user_notify.send_direct(user_id, channel, f"Message from {site_name}", body)
    label = "Discord DM" if channel == "discord" else "email"
    if ok:
        flash(f"Message sent to {target['name']} by {label}." + (f" {detail}" if detail else ""),
              "success")
    else:
        flash(f"Message to {target['name']} by {label} failed: {detail}", "error")
    return redirect(url_for("admin_user_account", user_id=user_id))


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
        db.set_setting("discordbot_announcement_channel_id",
                        request.form.get("announcement_channel_id", "").strip())
        flash("Channel settings saved.", "success")
        return redirect(url_for("admin_discord_bot_guilds"))
    status = discord_bot.get_status()
    return render_template("admin_discord_bot_guilds.html",
                            token_configured=bool(config.DISCORD_BOT_TOKEN),
                            connected=status["connected"], guilds=status["guilds"],
                            channel_whitelist=db.get_setting("discordbot_channel_whitelist", ""),
                            announcement_channel_id=db.get_setting("discordbot_announcement_channel_id", ""),
                            # Reached from the Discord bot page's own button rather than
                            # from the nav, so it highlights its parent - a sub-page with
                            # no nav entry of its own must not leave the nav showing
                            # nothing selected.
                            active="discord-bot")


# ---- Scheduled tasks (see scheduler.py - the framework; the tasks themselves are
# registered by whichever module owns them) ----
@app.route("/admin/tasks")
@login_required
def admin_tasks():
    return render_template("admin_tasks.html", tasks=scheduler.list_task_views(),
                            loops=scheduler.list_loop_views(),
                            tick_seconds=config.SCHEDULER_TICK_SECONDS, active="tasks")


@app.route("/admin/tasks/<name>/save", methods=["POST"])
@login_required
def admin_task_save(name):
    if scheduler.get_task(name) is None:
        flash("No such scheduled task.", "error")
        return redirect(url_for("admin_tasks"))
    kind = request.form.get("schedule_kind", "interval")
    interval = request.form.get("interval_minutes", type=int) or 60
    daily_at = request.form.get("daily_at", "03:00").strip() or "03:00"
    scheduler.save_schedule(name, enabled=bool(request.form.get("enabled")),
                             schedule_kind=kind, interval_minutes=interval, daily_at=daily_at)
    flash("Schedule saved.", "success")
    return redirect(url_for("admin_tasks"))


@app.route("/admin/tasks/<name>/run", methods=["POST"])
@login_required
def admin_task_run(name):
    """Runs the task synchronously, inside the request. That is the same sanctioned
    exception to the no-slow-I/O-in-a-request-handler rule as the integrations "Check
    now" button and perform_update(): an explicit one-shot action the admin knowingly
    triggered, where the whole point is getting the result back in the response.
    Moving it to a background thread would just mean staring at a page that says
    nothing happened yet."""
    if scheduler.get_task(name) is None:
        flash("No such scheduled task.", "error")
        return redirect(url_for("admin_tasks"))
    status, message = scheduler.run_task(name, trigger="manual")
    category = {"success": "success", "skipped": "error", "busy": "error"}.get(status, "error")
    flash(f"{scheduler.get_task(name).label}: {status}{' - ' + message if message else ''}", category)
    return redirect(url_for("admin_tasks"))


# ---------------------------------------------------------------------------
# Background health check
# ---------------------------------------------------------------------------
def _process_maintenance_and_notify():
    """Also called directly from admin_maintenance_new()/admin_maintenance_edit() (a
    request handler), not just from the health-check loop below - db.process_
    maintenance_windows() above already applied the actual status flip by the time
    this loop runs, so backgrounding only the notify() call doesn't delay that; see
    those routes' own comments for why the flip itself has to stay synchronous."""
    for event in db.process_maintenance_windows():
        service_name = event["service"]["name"]
        window_title = event["window"]["title"]
        started = event["event"] == "maintenance_started"
        title = "Maintenance started" if started else "Maintenance ended"
        _notify_async(title, f"{service_name}: {window_title}")
        # And the visitors who asked to hear about service events. Queued from this
        # background thread exactly as it would be from a request handler - the queue
        # is what keeps delivery off whichever thread noticed the event.
        user_notify.notify_service_subscribers(
            "maintenance",
            f"{title}: {service_name}",
            (f"{service_name} is now under maintenance ({window_title})."
             if started else
             f"Maintenance on {service_name} has finished ({window_title})."))


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


# How long individual health-check results are kept (settings-page tunable).
# 30 days is all get_uptime_percentages() ever reads; the extra headroom is there so
# an admin can go look at more history than the uptime figure covers if they want to.
DEFAULT_HISTORY_RETENTION_DAYS = 90

def _history_retention_days():
    raw = db.get_setting("status_history_retention_days", str(DEFAULT_HISTORY_RETENTION_DAYS))
    return int(raw) if raw.isdigit() and int(raw) > 0 else DEFAULT_HISTORY_RETENTION_DAYS


def _prune_status_history_task():
    """Body of the `prune_status_history` scheduled task.

    status_history is the one table where staying unbounded would be user-visible
    immediately, not just eventually: every query against it runs on a public page
    load, so this is what keeps the covering index doing an index scan over a bounded
    range instead of an ever-growing one. (notification_queue is also pruned on a
    schedule - db.prune_notification_queue() - but it's read from an admin-only
    background task, not a public page, so its growth was never the same class of
    problem.)

    This used to be a "have 24 hours passed?" check inside the health-check loop,
    tracked in a module-level float. Being a real task instead means the schedule
    survives a restart (a portal restarted twice a day would otherwise never prune at
    all, because the in-process clock reset every time) and that an admin can see when
    it last ran and how much it removed."""
    days = _history_retention_days()
    deleted = db.prune_status_history(days)
    if deleted:
        _logger.info("Pruned %d status_history rows older than %d days", deleted, days)
        return f"Removed {deleted} check result(s) older than {days} days."
    # A normal success, not a TaskSkipped: there was nothing to do because the table
    # is already inside its retention window, which is the job working, not the job
    # being unconfigured.
    return f"Nothing to remove - no check results are older than {days} days."


scheduler.register(
    "prune_status_history",
    "Prune old check results",
    "Deletes individual health-check results older than the retention window set "
    "under Settings. Every public page load reads this table, so keeping it trimmed "
    "is what stops the page getting slower every week.",
    _prune_status_history_task,
    default_schedule_kind="daily",
    default_daily_at="03:30",
)


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


def _check_and_record_service(s, status_by_id):
    """One service's full check-and-record - the per-service body of the loop below,
    pulled out so it can be submitted to a thread pool. Caught and logged here rather
    than left to the loop's own try/except: a bounded pool means other services'
    checks are already running concurrently regardless, so one service raising must
    not cost the rest of this cycle's results the way it would in a plain
    sequential loop with a single try/except wrapped around the whole thing."""
    try:
        if not s["auto_check"] or s["manual_override"] or not s["check_url"]:
            return
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
    except Exception:
        _logger.exception("health check failed for service '%s'", s.get("name", "?"))


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
            # Bounded to config.HEALTH_CHECK_WORKERS rather than one thread per
            # service - a service with retry_count set can hold its own slot for
            # retry_count * retry_interval_seconds seconds, and fully sequential
            # checking meant a handful of unreachable services could push a single
            # cycle well past CHECK_INTERVAL_SECONDS. Each service's dependency
            # lookup still reads the same pre-loop status_by_id snapshot regardless
            # of which worker thread runs it or in what order, which is what keeps
            # this safe to parallelize without dependency results depending on
            # thread scheduling.
            with ThreadPoolExecutor(max_workers=config.HEALTH_CHECK_WORKERS) as pool:
                # list() to actually wait for every submitted check before moving on
                # to the integration/disk checks below, which is what preserves this
                # loop's existing "everything for this cycle finishes before the
                # next one starts" shape.
                list(pool.map(lambda s: _check_and_record_service(s, status_by_id), services))
            _refresh_integration_cache()
            _check_low_disk_space(monitoring.get_resource_snapshot())
            # Pruning old history and checking GitHub for releases used to be tacked
            # on here, each with its own hand-rolled "is it due yet" check. Both are
            # scheduled tasks now (see /admin/tasks) - this loop is back to being only
            # the things that genuinely have to happen on the health-check cycle:
            # maintenance windows first, then every service's status, then the
            # integration cache that _merge_api_health() reads from.
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


# The health-check thread, held only so /admin/tasks can report whether it is still
# alive. threading.excepthook already logs a thread dying from something outside its
# own try/except (see logging_setup.py), but "services just stopped updating" is
# exactly the symptom nobody thinks to look in a log file for.
_health_thread = {"thread": None}


def _health_thread_alive():
    thread = _health_thread["thread"]
    return None if thread is None else thread.is_alive()


def start_background_checker():
    t = threading.Thread(target=run_health_checks, daemon=True, name="health-checks")
    t.start()
    _health_thread["thread"] = t
    return t


# ---------------------------------------------------------------------------
# The recurring jobs that are deliberately *not* scheduled tasks
# ---------------------------------------------------------------------------
# Registered read-only so /admin/tasks can answer "what does this portal do on a
# timer" completely, rather than listing the two thirds of the answer that happen to
# be controllable. Each one's reason for staying a plain loop is on the entry itself;
# the general argument is in scheduler.BackgroundLoop's docstring.
scheduler.register_loop(
    "health_checks",
    "Service health checks",
    "Requests every service's check URL, records the result, applies maintenance "
    "windows, and opens or resolves automatic incidents. Not a task you can switch "
    "off here on purpose: turning it off would also turn off incident detection, "
    "which is the portal's whole job.",
    interval_seconds=config.CHECK_INTERVAL_SECONDS,
    configured_by="PORTAL_CHECK_INTERVAL_SECONDS",
    is_alive=_health_thread_alive,
)
scheduler.register_loop(
    "resource_polling",
    "Resource polling",
    "Samples CPU, memory, disks and network into the cache the Resources page reads, "
    "plus the Windows-only queries (Hyper-V VMs, temperatures). Runs far faster than "
    "the scheduler's tick, so the scheduler could not drive it even if it were "
    "listed as a task.",
    interval_seconds=config.RESOURCE_REFRESH_SECONDS,
    configured_by="PORTAL_RESOURCE_REFRESH_SECONDS",
    is_alive=monitoring.refresh_thread_alive,
)
scheduler.register_loop(
    "discord_bot_refresh",
    "Discord bot refresh",
    "Updates the bot's presence and re-edits its tracked live status message. Runs "
    "inside discord.py's own event loop, where it belongs - driving it from here "
    "would mean scheduling coroutines across threads for no benefit.",
    interval_seconds=config.DISCORD_BOT_REFRESH_SECONDS,
    configured_by="PORTAL_DISCORD_BOT_REFRESH_SECONDS",
    is_alive=lambda: discord_bot.get_status()["connected"] if config.DISCORD_BOT_TOKEN else None,
)


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging_setup.init_logging()
    db.init_db()
    # If the previous shutdown was an in-app update restarting into a new version,
    # this is where that gets confirmed (or reported as not having taken effect).
    updater.check_pending_marker()
    start_background_checker()
    monitoring.start_background_refresh(config.RESOURCE_REFRESH_SECONDS)
    scheduler.start()
    discord_bot.start()
    # debug must stay False whenever this is reachable outside localhost.
    app.run(host="0.0.0.0", port=5000, debug=False)
