"""
app.py — Personal server status portal.
Run with: python app.py
Admin panel: /admin (password is set on first launch)
"""
import gzip
import io
import ipaddress
import logging
import os
import platform
import re
import secrets
import sys
import tempfile
import threading
import time
import unicodedata
import zipfile
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import (
    Flask,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from markupsafe import Markup, escape
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

import config
import db
import device_api
import discord_bot
import health
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


@app.after_request
def set_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "font-src 'self'; "
        # image.tmdb.org is the one exception, for search-result posters - a scoped,
        # named-host exception, not a general relaxation. Jellyfin-only results (no
        # TMDB poster_path) stay text-only rather than proxying Jellyfin's own image
        # endpoint through a request handler, which the no-live-outbound-I/O rule
        # elsewhere exists to avoid.
        "img-src 'self' data: https://image.tmdb.org; "
        "script-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        # Not covered by default-src: without these an injected <base> or <form> can
        # redirect relative URLs or form posts to another site. No form in the app
        # posts off-site.
        "base-uri 'self'; "
        "form-action 'self'; "
        "object-src 'none'"
    )
    response.headers["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
    )
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    # An uploaded SVG can carry a script and the CSP above allows inline script. As an
    # <img> or favicon it is inert; opened directly it would run on the portal's
    # origin. sandbox gives it an opaque origin and no scripts, and does not affect
    # <img> rendering.
    if request.path.startswith("/static/uploads/"):
        response.headers["Content-Security-Policy"] = "sandbox"
    response.headers["Server"] = "status-portal"  # don't advertise the underlying framework/server
    _apply_cache_policy(response)
    return response


# Pages that are per-visitor or carry a form: never kept by the browser or anything in
# between, whoever asks. Matched as a path or a path prefix followed by "/".
_NEVER_CACHED_PATHS = ("/admin", "/account", "/search", "/login", "/report")
# How long a browser or a CDN may keep an anonymous public page. Short enough that a
# status change shows within one refresh; long enough that a burst of visitors (or a
# CDN serving the page while the origin is down) costs one render.
PUBLIC_CACHE_SECONDS = 15


def _apply_cache_policy(response):
    """Cache-Control for HTML, in three tiers.

    * Under _NEVER_CACHED_PATHS: `no-store`, so the back button, or the next person on
      a shared machine, can't bring an admin or account page back after logout.
      Overrides the send_file() default too, which is what the two point-in-time
      downloads under /admin/ want anyway.
    * Any other page, for a request carrying a session cookie or leaving with one:
      `private, no-cache` - never a shared cache (the page can show a name, a sign-out
      button, a CSRF token), and the browser revalidates, which keeps the polled
      fragment's 304.
    * The rest - an anonymous GET of a public page that set no cookie (base.html
      renders the CSRF token only when a session or a form needs it) - is
      `public, max-age=PUBLIC_CACHE_SECONDS`, which lets Cloudflare serve it, even
      while this server is down. `Vary: Cookie` alone does not stop Cloudflare
      serving it to a signed-in visitor: its cache rule must bypass on the `session`
      cookie (docs/claude/sessions-caching-db.md).

    Static files and non-HTML responses keep whatever they already had."""
    path = request.path
    if any(path == p or path.startswith(p + "/") for p in _NEVER_CACHED_PATHS):
        response.headers["Cache-Control"] = "no-store"
        return
    if path.startswith("/static/") or response.mimetype != "text/html":
        return
    response.vary.add("Cookie")
    if (app.config["SESSION_COOKIE_NAME"] in request.cookies or session or session.modified
            or "Set-Cookie" in response.headers):
        response.headers["Cache-Control"] = "private, no-cache"
    elif request.method in ("GET", "HEAD") and response.status_code in (200, 304):
        response.headers["Cache-Control"] = f"public, max-age={PUBLIC_CACHE_SECONDS}"


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


@app.after_request
def _etag_public_fragment(response):
    """The polled live region of a public page (`?fragment=1`): an ETag over the rendered
    body so an unchanged page costs a 304 with no body. Cache-Control is left to
    _apply_cache_policy(), like any other page. Registered after
    _compress_response on purpose: Flask runs these in reverse order, so the tag is
    computed over the plain HTML - gzip's header carries a timestamp, which would make
    the tag change every second."""
    if (request.method == "GET" and request.args.get("fragment") == "1"
            and response.status_code == 200 and response.mimetype == "text/html"
            and not response.direct_passthrough
            and not request.path.startswith(("/admin", "/account"))):
        response.add_etag()
        response.make_conditional(request)
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
# The existing groups for the service forms' picker. A global the partial calls itself, so the
# three places that render it (new, edit, wizard) cannot forget to pass the list - and no other
# page pays for the query.
app.jinja_env.globals["service_groups"] = db.list_service_groups


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


def _show_public_version():
    """Whether the public footer shows the exact version and the project link. On by
    default (an unset setting reads as on) so existing installs don't change; the
    About page always shows the version to the admin regardless."""
    return db.get_setting("show_public_version", "1") == "1"


@app.context_processor
def _inject_portal_version():
    """Exposes the running version to every template (currently just the public
    footer) without threading it through every route that renders public_base.html -
    same reasoning as _inject_branding()/csrf_token() being globals rather than
    hand-passed. VERSION_DISPLAY, not bare VERSION, so a +dev source checkout reads
    as one - same value the About page already shows the admin."""
    return {"portal_version_display": config.VERSION_DISPLAY,
            "show_public_version": _show_public_version()}


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


def _is_public_fragment():
    """True for `GET <public page>?fragment=1`: the page renders only its live region,
    which static/js/public_refresh.js swaps in place instead of reloading the page."""
    return request.method == "GET" and request.args.get("fragment") == "1"


# The status data counts as stale once the last health-check cycle finished more than
# this many check intervals ago (the cycle stalled, or the checker thread died).
STALE_AFTER_INTERVALS = 3


def _stale_data_minutes():
    """Minutes since the last finished health-check cycle when that is more than
    STALE_AFTER_INTERVALS intervals ago, else None. No recorded cycle (a fresh install
    that has not finished its first one) is not stale: there is nothing to compare."""
    raw = db.get_setting(health.LAST_HEALTH_CYCLE_SETTING)
    if not raw:
        return None
    try:
        last = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - last).total_seconds()
    if age <= STALE_AFTER_INTERVALS * config.CHECK_INTERVAL_SECONDS:
        return None
    return int(age // 60)


@app.context_processor
def _inject_public_chrome():
    """What public_base.html renders around every public page - the nav tabs, the
    refresh interval, the footer's repo link - so no route has to
    thread it through by hand (the bug class CLAUDE.md used to warn about: a page that
    forgot one of them simply lacked the tab). Values a route passes explicitly win.
    Skipped on admin and kiosk pages, which never render it; the nav itself is only
    settings-level predicates, no I/O."""
    chrome = {"site_name": db.get_setting("site_name", "Server"),
              "refresh_seconds": config.PUBLIC_REFRESH_SECONDS,
              "repo_url": updater.REPO_URL, "active_page": None,
              "public_fragment": _is_public_fragment(), "data_stale": False}
    if request.path == "/admin" or request.path.startswith(("/admin/", "/kiosk")):
        return chrome
    try:
        chrome["nav_links"] = _public_page_links()
        chrome["search_enabled"] = _search_available()
        stale_minutes = _stale_data_minutes()
        chrome["data_stale"] = stale_minutes is not None
        chrome["data_stale_minutes"] = stale_minutes
    except Exception:
        # An error page must still render when the nav can't be built.
        _logger.exception("Could not build the public nav")
    return chrome


@app.context_processor
def _inject_admin_badges():
    """Unread problem-report count for the admin nav's "Reports" badge - scoped to
    admin pages only (not computed on every public page load) since it's only ever
    displayed there."""
    if request.path != "/admin" and not request.path.startswith("/admin/"):
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
    hours = db.parse_int(raw, DEFAULT_SESSION_TIMEOUT_HOURS)
    if hours <= 0:
        return None
    return min(hours, MAX_SESSION_TIMEOUT_HOURS) * 3600


# Admin session revocation. A Flask session is a signed cookie, so nothing on the
# server can invalidate a copy of one: logging out only clears the browser's own
# copy, and a stolen cookie kept working after a password change. Every admin
# session is stamped with this setting's value at login, and _enforce_session_timeout
# rejects one whose stamp no longer matches. Rotating it therefore signs out every
# other admin session at once. It rotates only on a password change and on enabling
# or disabling 2FA; logout stays local to the browser that clicked it.
#
# Unset reads as "", which is also what a session stamped before this existed
# compares as - so upgrading signs nobody out, and the first rotation revokes
# everything older.
ADMIN_SESSION_EPOCH_SETTING = "admin_session_epoch"

# What survives an admin login's session.clear(). The admin keys are what the clear
# is for (nothing from before authentication carries into the admin session, and the
# CSRF token is regenerated); the visitor's Jellyfin sign-in and its per-session
# state are a separate identity and aren't the admin login's to end.
_VISITOR_SESSION_KEYS = ("portal_user", "portal_user_last_seen", "recent_requests",
                          "report_form_rendered_at")
# What a revoked admin session loses. Deliberately not session.clear(), for the same
# reason: a revoked admin cookie in a browser also signed in as a visitor keeps the
# visitor session. csrf_token stays because visitor forms use it too.
_ADMIN_SESSION_KEYS = ("logged_in", "last_seen", "admin_epoch", "awaiting_totp",
                        "login_next", "pending_totp_secret", "seerr_diagnosis")


def _end_admin_session():
    """Drops the admin keys and nothing else, so a browser also signed in as a Jellyfin
    visitor stays signed in as one. Used by the idle expiry and /admin/logout; see
    _ADMIN_SESSION_KEYS."""
    for key in _ADMIN_SESSION_KEYS:
        session.pop(key, None)


def _admin_session_epoch():
    return db.get_setting(ADMIN_SESSION_EPOCH_SETTING, "")


def _rotate_admin_session_epoch():
    """Signs out every admin session except the one making this request, which is
    re-stamped so that changing the password doesn't sign out the person who
    changed it."""
    epoch = secrets.token_hex(16)
    db.set_setting(ADMIN_SESSION_EPOCH_SETTING, epoch)
    if session.get("logged_in"):
        session["admin_epoch"] = epoch


def _start_admin_session():
    """The one place a login becomes a logged-in session. Clears whatever the session
    held before authenticating (visitor keys aside - see _VISITOR_SESSION_KEYS), which
    also rotates the CSRF token; marks it permanent (so the cookie gets an explicit
    Max-Age instead of dying with the browser); stamps last_seen so the idle clock
    starts now; and stamps the revocation epoch. Every place that logs someone in
    goes through this - three hand-maintained copies is three chances for one to
    drift."""
    visitor = {key: session[key] for key in _VISITOR_SESSION_KEYS if key in session}
    session.clear()
    session.update(visitor)
    session.permanent = True
    session["logged_in"] = True
    session["last_seen"] = time.time()
    session["admin_epoch"] = _admin_session_epoch()


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
    # Revoked by a password or 2FA change made elsewhere (see
    # ADMIN_SESSION_EPOCH_SETTING). Same redirect as an expiry, and here for the same
    # ordering reason: a POST must reach the login page, not a CSRF 400.
    if session.get("admin_epoch", "") != _admin_session_epoch():
        _end_admin_session()
        if request.path.startswith("/admin/"):
            return redirect(url_for("admin_login", next=request.path))
        return
    session.permanent = True
    timeout = _session_timeout_seconds()
    now = time.time()
    last_seen = session.get("last_seen")
    if timeout is not None and last_seen is not None and now - last_seen > timeout:
        _end_admin_session()
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
_CSRF_PROTECTED_PUBLIC_PATHS = {"/login", "/logout", "/account", "/account/theme", "/search/request"}


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


PASSWORD_RESET_FLAG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "instance", "RESET_PASSWORD")


def _check_and_process_password_reset_flag():
    """Host-level password recovery, modelled on twofactor.check_and_process_reset_flag():
    an empty file at instance/RESET_PASSWORD, seen on a hit of /admin/login, erases the
    admin password hash, signs out every admin session and deletes itself. The portal is
    then back at first-run setup (which _first_run_setup_allowed() keeps to the local
    network). Leaves 2FA alone."""
    if not os.path.exists(PASSWORD_RESET_FLAG_PATH):
        return False
    db.delete_setting("admin_password_hash")
    _rotate_admin_session_epoch()
    try:
        os.remove(PASSWORD_RESET_FLAG_PATH)
    except OSError:
        _logger.exception("Password reset flag file found but could not be removed")
    _logger.warning("Admin password erased via host-level RESET_PASSWORD flag file")
    return True


# Tailscale hands out addresses from here (RFC 6598 shared space), and ipaddress
# doesn't count it as private, but a tailnet is exactly the kind of "my own network"
# this portal is set up from.
_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")
# Headers a reverse proxy or tunnel adds. Without BEHIND_PROXY, their presence means
# the real client is somewhere this app can't see - cloudflared running on the same
# host connects from 127.0.0.1 for every visitor on the internet.
_FORWARDING_HEADERS = ("X-Forwarded-For", "X-Real-IP", "Forwarded", "CF-Connecting-IP")


def _first_run_setup_allowed():
    """Whether this request may set the admin password on a portal that has none.

    Whoever reaches the login page first on a fresh install (a new Docker volume, a
    reinstall) claims the admin account, and with a tunnel up that can be anyone on
    the internet. So first-run setup is only offered to a client on this machine or
    a private, link-local or Tailscale address. With BEHIND_PROXY set,
    request.remote_addr is already the proxy's X-Forwarded-For answer (see ProxyFix
    above); without it, any forwarding header means "can't tell", which refuses."""
    if not config.BEHIND_PROXY and any(h in request.headers for h in _FORWARDING_HEADERS):
        return False
    try:
        ip = ipaddress.ip_address(request.remote_addr or "")
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return (ip.is_loopback or ip.is_private or ip.is_link_local
            or (ip.version == 4 and ip in _CGNAT_NETWORK))


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


# Caps how many Jellyfin sign-ins may be in flight at once, so a burst of slow ones
# can't hold every waitress thread (same third-of-the-pool reasoning as
# media_search._outbound_slots). The lockout counter only moves after Jellyfin
# answers, so it can't stop a simultaneous burst by itself.
USER_LOGIN_MAX_CONCURRENT = max(1, config.WAITRESS_THREADS // 3)
_user_login_slots = threading.BoundedSemaphore(USER_LOGIN_MAX_CONCURRENT)


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


# The 90-day strip on each public card. Same reasoning as the uptime figure above: a
# day's bar changes at most once per check, so it is built once a minute for every
# service rather than per request.
_uptime_strip_cache = {"value": {}, "fetched_at": 0.0}
_uptime_strip_cache_lock = threading.Lock()


def _day_label(day, checks, down, maintenance):
    counted = checks - maintenance
    if counted <= 0:
        return ("maint", "planned maintenance") if checks else ("nodata", "no data")
    pct = round((counted - down) / counted * 100, 1)
    # Green only for a day with no failed check at all; one blip is amber, a bad day red.
    css = "ok" if down == 0 else ("warn" if pct >= 95 else "crit")
    return css, f"{pct}% uptime"


def _build_uptime_strips(daily):
    """{service_id: [{"day", "css", "label"}, ...]} - 90 entries, oldest first, ending
    today (UTC). Every service that has any row in the window gets a strip; the rest
    are absent and their cards show no strip at all."""
    today = datetime.now(timezone.utc).date()
    days = [(today - timedelta(days=n)).isoformat()
            for n in range(db.UPTIME_STRIP_DAYS - 1, -1, -1)]
    strips = {}
    for sid, rows in daily.items():
        strip = []
        for day in days:
            counts = rows.get(day)
            css, label = _day_label(day, *counts) if counts else ("nodata", "no data")
            strip.append({"day": day, "css": css, "label": f"{day}: {label}"})
        strips[sid] = strip
    return strips


def _cached_uptime_strips():
    now = time.monotonic()
    with _uptime_strip_cache_lock:
        if (_uptime_strip_cache["fetched_at"]
                and now - _uptime_strip_cache["fetched_at"] < UPTIME_CACHE_TTL_SECONDS):
            return _uptime_strip_cache["value"]
    value = _build_uptime_strips(db.get_daily_history())
    with _uptime_strip_cache_lock:
        _uptime_strip_cache["value"] = value
        _uptime_strip_cache["fetched_at"] = now
    return value


def _enrich_services(services):
    open_reports = db.count_open_reports_by_service()
    uptimes = _cached_uptime_percentages()
    strips = _cached_uptime_strips()
    service_names = {s["id"]: s["name"] for s in services}
    links_by_service = db.list_service_links_for_services([s["id"] for s in services])
    # One grouped query, and only for the services that publish their dependencies -
    # nothing is resolved for a service that hasn't opted in.
    deps_by_service = db.list_dependencies_for_services(
        [s["id"] for s in services if s["show_dependencies_public"]])
    for s in services:
        s["links"] = links_by_service[s["id"]]
        s["uptime"] = uptimes.get(s["id"])
        s["uptime_strip"] = strips.get(s["id"])
        s["in_grace_period"] = health._within_grace_period(s)
        s["retrying"] = s["id"] in health._retry_in_progress
        s["open_reports_count"] = open_reports.get(s["id"], 0)
        s["run_target_label"] = _run_target_label(s["run_target"]) if s["show_run_target_public"] else None
        s["dependency_names"] = [
            service_names.get(dep_id, "?") for dep_id in deps_by_service[s["id"]]
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
    its availability predicate/context builder, and each one used to
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
    return db.parse_int(db.get_setting("public_history_days", ""))


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
# to tabs of their own (PUBLIC_PAGES below).
PUBLIC_SECTIONS = [
    ("announcements", "Announcements"),
    ("services", "Services"),
    ("incidents", "Incidents"),
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


def _integration_severity(status):
    if not status["reachable"]:
        return "crit"
    if any(i["level"] == "error" for i in status["issues"]):
        return "crit"
    if status["issues"]:
        return "warn"
    return "ok"


def _attach_integration_status(services):
    """Reads the cache only - see the module-level note above for why."""
    linked_by_service = db.list_public_integrations_by_service()
    for s in services:
        linked = linked_by_service.get(s["id"], [])
        entry = health._integration_status_cache.get(linked[0]["id"]) if linked else None
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
# Each of these was a block on the main page and is now a tab of its own. The same
# predicate gates its route *and* its nav link, which is the point: the visibility rules (show_public_*, media_requires_login) are applied
# once, in one place, so a sub-page cannot become a way around a setting that hides the
# corresponding block. A page whose builder reports nothing to show is not linked and
# 404s if requested directly - the same answer a visitor would get if it didn't exist,
# rather than an empty page confirming the feature is there but switched off.
def _request_snapshot():
    """One resource snapshot per request, memoised on Flask's `g`.

    The main page's high-load badge and the Server tab's cards can both want it, and a
    snapshot is the single most expensive thing
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


def _search_available():
    """The library search a signed-in visitor gets at the head of the Media tab."""
    return bool(current_user()) and media_search.is_available()


def _media_context():
    media_visible = _public_media_visibility()
    show_media = any(media_visible.values()) and _media_visible_to(current_user())
    if not show_media and not _search_available():
        return None
    return {"media": integrations.get_cached_media() if show_media else None,
            "media_visible": media_visible}


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
    # Either the activity sections or, for a signed-in visitor, the search box.
    return (any(_public_media_visibility().values()) and _media_visible_to(current_user())) \
        or _search_available()


def _info_available():
    return bool((db.get_info_page() or "").strip())


def _server_available():
    return _resources_available() or _vms_available() or _activity_available()


def _server_context():
    """The Server tab: resources, VMs and Jellyfin activity as sections of one page.

    Each section keeps its own visibility rule (its own predicate and builder); the page
    exists when at least one of them does. A section that is off is simply absent, and
    its builder is not run, so a visitor who may only see the VMs doesn't make the
    server take a resource snapshot."""
    context = {"visible": _public_resource_visibility()}
    for key, available, build in (("resources", _resources_available, _resources_context),
                                  ("vms", _vms_available, _vms_context),
                                  ("activity", _activity_available, _activity_context)):
        section = build() if available() else None
        context[f"show_{key}"] = section is not None
        context.update(section or {})
    return context if any(context[f"show_{k}"] for k in ("resources", "vms", "activity")) else None


# key -> (label, endpoint, availability predicate, context builder). These are the
# public tabs after Status. Server holds three sections (resources, vms, activity) whose
# old addresses redirect to their anchors; see _SERVER_SECTIONS.
PUBLIC_PAGES = [
    ("server", "Server", "public_server", _server_available, _server_context),
    ("media", "Media activity", "public_media", _media_available, _media_context),
    ("info", "Practical info", "public_info", _info_available, _info_context),
]


def _public_page_links():
    """Which tabs a visitor can see right now, for the nav.

    Availability comes from the cheap predicate only; the page route applies the same
    predicate before rendering - a link to a page that would 404 is worse than no
    link."""
    return [{"key": key, "label": label, "url": url_for(endpoint)}
            for key, label, endpoint, available, _ in PUBLIC_PAGES if available()]


def _render_public_page(key):
    """Shared body of every sub-page route."""
    entry = next((p for p in PUBLIC_PAGES if p[0] == key), None)
    if entry is None:
        abort(404)
    _, label, _, available, build_context = entry
    context = build_context() if available() else None
    if context is None:
        # Switched off, or not visible to this viewer. 404 rather than an empty page:
        # "this doesn't exist here" is the honest answer, and an empty page would
        # confirm the feature exists and is merely hidden.
        abort(404)
    return render_template(f"public/{key}.html", page_label=label, active_page=key,
                            site_name=db.get_setting("site_name", "Server"),
                            **context)

# ---------------------------------------------------------------------------
# Kiosk mode
# ---------------------------------------------------------------------------
# A full-screen, chrome-free display for a wall-mounted TV or a tablet left running
# in a corner - one view at a time, rotating on a timer, with the data underneath it
# refreshed on the ordinary public-page cycle.
#
# Two things about the shape of this are deliberate:
#
# 1. It reuses PUBLIC_PAGES' (predicate, context builder) split rather than inventing
#    its own visibility rules. A kiosk view is gated by *both* the admin's kiosk
#    checkbox and the existing show_public_* setting for the same data - otherwise
#    ticking "Virtual machines" here would publish VM names on a wall display that the
#    admin had deliberately switched off for /vms, and kiosk would have become a way
#    around a public-visibility setting. Nothing on /kiosk is visible to a visitor who
#    could not already see it on the public pages.
# 2. It refreshes by polling for a server-rendered HTML fragment rather than by
#    reloading the page the way main.js does. A full reload on a TV means a visible
#    flash every refresh *and* throws the rotation back to the first view mid-cycle,
#    which on a 60s refresh and a 20s rotation means views 2 and 3 are never reached.
#    The fragment endpoint is the same convention /api/incidents/more and
#    /admin/logs/tail already use: server-rendered HTML, not JSON.
KIOSK_DEFAULT_ROTATION_SECONDS = 20
# Floor and ceiling on the rotation timer. Below the floor a view is gone before it can
# be read from across a room; above the ceiling the display has effectively stopped
# rotating, which is better expressed by ticking one view.
KIOSK_MIN_ROTATION_SECONDS = 5
KIOSK_MAX_ROTATION_SECONDS = 300
# How many incidents one kiosk view shows. There is no "load more" on a wall display,
# so this is the whole list rather than a first page.
KIOSK_INCIDENT_LIMIT = 6
_KIOSK_DEFAULT_VIEWS = ["services", "incidents"]


def _kiosk_services_context():
    """Always has something to say - "no services configured yet" is itself the answer
    a status display exists to give, which is also what makes this the safe fallback
    when every other view is switched off or empty."""
    services = _attach_integration_status(_enrich_services(db.list_services()))
    return {"services": services, "groups": _group_services(services),
            "overall": compute_overall_status(services)}


def _kiosk_incidents_context():
    return {"incidents": _enrich_incidents(
                db.list_incidents(limit=KIOSK_INCIDENT_LIMIT, max_age_days=_public_history_days())),
            "maintenance_windows": db.list_public_maintenance_windows()}


def _kiosk_announcements_context():
    announcements = db.list_active_announcements(limit=5)
    return {"announcements": announcements} if announcements else None


def _kiosk_vms_context():
    """None when there are no VMs, unlike /vms' own builder, which renders an empty
    table. A page a visitor navigated to can afford to say "nothing here"; a view that
    rotates onto a wall for 20 seconds cannot."""
    context = _vms_context()
    return context if context and context["vms"] else None


# key -> (label, availability predicate, context builder). Same contract as
# PUBLIC_PAGES: the predicate reads settings only and answers "may this be shown at
# all", the builder answers "is there anything to show right now" by returning None.
KIOSK_VIEWS = [
    ("services", "Services", lambda: True, _kiosk_services_context),
    ("incidents", "Incidents & maintenance", lambda: True, _kiosk_incidents_context),
    ("announcements", "Announcements", lambda: True, _kiosk_announcements_context),
    ("vms", "Virtual machines", _vms_available, _kiosk_vms_context),
    ("resources", "Server resources", _resources_available, _resources_context),
]
KIOSK_VIEW_LABELS = {key: label for key, label, _, _ in KIOSK_VIEWS}


def kiosk_enabled():
    """Public helper: the master switch, read by both kiosk routes and by index() to
    decide whether to offer the link at all."""
    return db.get_setting("kiosk_enabled", "0") == "1"


def _kiosk_rotation_seconds():
    seconds = db.parse_int(db.get_setting("kiosk_rotation_seconds", ""), KIOSK_DEFAULT_ROTATION_SECONDS)
    return max(KIOSK_MIN_ROTATION_SECONDS, min(KIOSK_MAX_ROTATION_SECONDS, seconds))


def _kiosk_selected_views():
    """Which views the admin ticked.

    Deliberately *not* _public_section_order()'s behaviour of appending valid keys
    missing from the stored value. That list is an ordering of blocks that all show
    anyway, so a new one appearing at the bottom is right; this one is an inclusion
    list, and silently adding a view nobody ticked would put data on a wall display
    the admin never asked to publish. A never-saved setting (None, not "") falls back
    to the defaults; an explicitly emptied one stays empty and takes the fallback in
    _kiosk_rendered_views()."""
    raw = db.get_setting("kiosk_views", None)
    if raw is None:
        return list(_KIOSK_DEFAULT_VIEWS)
    valid = set(KIOSK_VIEW_LABELS)
    return [key.strip() for key in raw.split(",") if key.strip() in valid]


def _kiosk_rendered_views():
    """The views to actually put on screen, each already rendered to HTML.

    Rendered here rather than by including 'kiosk/<key>.html' from a loop because each
    view has its own context, and Jinja's include shares the caller's. Merging all five
    contexts into one namespace would work today only because their keys happen not to
    collide, which is not a property worth depending on.

    Order is KIOSK_VIEWS' own, not the stored setting's - the checkboxes say which
    views, not in what sequence, and a second reorder UI for five items would be cost
    without benefit.

    Falls back to Services when nothing qualifies: every view excluded, or every ticked
    view currently empty, must still show the thing the display exists for rather than
    a blank screen on somebody's wall."""
    selected = set(_kiosk_selected_views())
    views = []
    for key, label, available, build_context in KIOSK_VIEWS:
        if key not in selected or not available():
            continue
        try:
            context = build_context()
        except Exception:
            # One broken view must not take the whole display down - the same
            # reasoning as the one in every public builder.
            _logger.exception("Could not build kiosk view '%s'", key)
            continue
        if context:
            views.append({"key": key, "label": label,
                          "html": render_template(f"kiosk/{key}.html", **context)})
    if not views:
        views.append({"key": "services", "label": KIOSK_VIEW_LABELS["services"],
                      "html": render_template("kiosk/services.html", **_kiosk_services_context())})
    return views


def _kiosk_view_choices():
    """The kiosk view checkboxes for the Settings page.

    Each carries whether its underlying public-visibility setting currently allows it,
    so the form can say that a ticked view still won't appear. Without that, an admin
    who ticks "Virtual machines" while show_public_vms is off sees a setting that
    silently does nothing - which reads as a bug rather than as the deliberate refusal
    to let kiosk publish what the public pages don't."""
    selected = set(_kiosk_selected_views())
    return [{"key": key, "label": label, "selected": key in selected, "available": available()}
            for key, label, available, _ in KIOSK_VIEWS]


# The display-device API's settings; the endpoint itself is in routes/api.py, and the
# admin page that sets them (admin_device) reads them too.
DEVICE_API_KEY_SETTING = "device_api_key"
DEVICE_API_ENABLED_SETTING = "device_api_enabled"


def device_api_enabled():
    """On only when the admin switched it on *and* a key exists. A switch with no key
    behind it would be an endpoint that answers 401 to everybody, which still tells a
    stranger the feature is there; 404 is what an install that never had it says."""
    return (db.get_setting(DEVICE_API_ENABLED_SETTING, "0") == "1"
            and bool(db.get_setting(DEVICE_API_KEY_SETTING, "")))


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


# The 10 checkbox names the account-settings form submits, shared by the visitor's own
# POST handler and the admin-viewing-a-user's-account route (see
# admin_user_account()) - one place naming the fields so the two can't drift apart.
def _save_account_prefs(user_id, form):
    """Saves the form; returns the list of error messages for contact fields that were
    invalid (those fields are left as they were, everything else is saved)."""
    email = form.get("notify_email", "").strip()[:200]
    discord_id = form.get("notify_discord_id", "").strip()[:32]
    errors = user_notify.contact_errors(email=email, discord_id=discord_id)
    contact_fields = {}
    if "notify_email" not in errors:
        contact_fields["notify_email"] = email
    if "notify_discord_id" not in errors:
        contact_fields["notify_discord_id"] = discord_id
    db.set_user_preferences(
        user_id, theme=form.get("theme", "auto"),
        **contact_fields,
        notify_email_reports=bool(form.get("notify_email_reports")),
        notify_email_requests=bool(form.get("notify_email_requests")),
        notify_email_maintenance=bool(form.get("notify_email_maintenance")),
        notify_discord_reports=bool(form.get("notify_discord_reports")),
        notify_discord_requests=bool(form.get("notify_discord_requests")),
        notify_discord_maintenance=bool(form.get("notify_discord_maintenance")),
        notify_discord_seerr_events=bool(form.get("notify_discord_seerr_events")),
        notify_email_announcements=bool(form.get("notify_email_announcements")),
        notify_email_gamesportal=bool(form.get("notify_email_gamesportal")),
        notify_discord_gamesportal=bool(form.get("notify_discord_gamesportal")))
    return list(errors.values())


def _safe_next_url(raw):
    """Only ever a path on this site. A `next` parameter that can name another host
    is an open redirect, and this one is reachable without any authentication at
    all. Anything that isn't a single-slash-prefixed relative path is discarded
    rather than sanitised - there's no legitimate case here for the difference.

    Backslashes and control characters are refused too: browsers read `/\\host` as
    `//host`, and a tab or newline can be stripped on the way to the same result.
    Werkzeug happens to percent-encode both in the Location header today, but that's
    an implementation detail to not depend on."""
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return None
    if "\\" in raw or any(unicodedata.category(ch) == "Cc" for ch in raw):
        return None
    return raw


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------
# How long a verified password stays good for the 2FA code step.
AWAITING_TOTP_SECONDS = 300


def _awaiting_totp():
    """True while the session holds a verified password that is still waiting for its
    code. The flag is the time the password was accepted; past AWAITING_TOTP_SECONDS
    (or in a session that stored a bare True before it was timestamped) it is dropped
    and the login falls back to the password step."""
    stamp = session.get("awaiting_totp")
    if not stamp:
        return False
    if (isinstance(stamp, bool) or not isinstance(stamp, (int, float))
            or time.time() - stamp > AWAITING_TOTP_SECONDS):
        session.pop("awaiting_totp", None)
        session.pop("login_next", None)
        return False
    return True


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    # Cheap check (a single os.path.exists()) on every hit, so a host-level 2FA
    # reset (see twofactor.py) takes effect the moment someone next loads this
    # page - no app restart needed.
    twofactor.check_and_process_reset_flag()
    _check_and_process_password_reset_flag()
    first_run = is_first_run()
    awaiting_totp = _awaiting_totp()

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
                # Read before _start_admin_session(), which clears the session.
                # Re-checked here as well as where it's stored, so a session written
                # before that check existed can't still carry an off-site target.
                nxt = _safe_next_url(session.pop("login_next", None)) or url_for("admin_dashboard")
                _start_admin_session()
                return redirect(nxt)
            _register_login_failure()
            flash("Incorrect code.", "error")
            return render_template("login.html", first_run=False, awaiting_totp=True)

        password = request.form.get("password", "")
        if first_run:
            confirm = request.form.get("confirm", "")
            if not _first_run_setup_allowed():
                _logger.warning("Refused first-run admin password setup from %s", request.remote_addr)
                flash("For safety, the admin password can only be set for the first time "
                      "from this machine or your local network.", "error")
            elif len(password) < 6:
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
                    session["awaiting_totp"] = time.time()
                    session["login_next"] = (_safe_next_url(request.args.get("next"))
                                             or url_for("admin_dashboard"))
                    return render_template("login.html", first_run=False, awaiting_totp=True)
                _register_login_success()
                _start_admin_session()
                nxt = _safe_next_url(request.args.get("next")) or url_for("admin_dashboard")
                return redirect(nxt)
            _register_login_failure()
            flash("Incorrect password.", "error")
    return render_template("login.html", first_run=first_run, awaiting_totp=awaiting_totp)


@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    _end_admin_session()
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Admin dashboard
# ---------------------------------------------------------------------------
@app.route("/admin")
@login_required
def admin_dashboard():
    return render_template("admin_overview.html", items=_overview_items(), active="overview")


def _overview_items():
    """What on this portal wants the admin's attention, as (state, text, endpoint, link
    text) rows, worst first, plus an all-clear list. Reads the database, the in-memory
    caches and stored settings only - never a live call - so the landing page costs
    the same as any other admin page.

    `state` is "crit", "warn" or "ok"; an "ok" row is a check that passed, shown so the
    page says what it looked at rather than merely being empty."""
    rows = []

    def add(state, text, endpoint, link_text):
        rows.append((state, text, endpoint, link_text))

    def plural(n, word):
        return f"{n} {word}" if n == 1 else f"{n} {word}s"

    services = db.list_services()
    down = [s["name"] for s in services if s["status"] == "down"]
    degraded = [s["name"] for s in services if s["status"] == "degraded"]
    open_incidents = db.count_open_incidents()
    if down:
        add("crit", f"Down: {', '.join(down)}", "admin_services", "Services")
    if degraded:
        add("warn", f"Degraded: {', '.join(degraded)}", "admin_services", "Services")
    if open_incidents:
        add("warn", f"{plural(open_incidents, 'open incident')}", "admin_incidents", "Incidents")
    if not (down or degraded or open_incidents):
        add("ok", "Every service is up and no incident is open", "admin_services", "Services")

    unread = db.count_unread_problem_reports() + db.count_unseen_user_messages()
    if unread:
        add("warn", f"{plural(unread, 'unread report or reply')}", "admin_reports", "Reports")
    pending = seerr_alerts.pending_count()
    if pending:
        add("warn", f"{plural(pending, 'Seerr request')} waiting for approval",
            "admin_seerr_alerts", "Seerr approvals")

    cached = updater.get_cached_update_status()
    if cached and cached.get("update_available"):
        add("warn", "A portal update is available", "admin_about", "Updates & backups")
    arr_updates = version_checks.updates_available()
    if arr_updates:
        add("warn", f"{plural(arr_updates, 'app')} behind the latest version",
            "admin_integrations", "Integrations")

    failed_tasks = [t["label"] for t in scheduler.list_task_views() if t["last_status"] == "failed"]
    if failed_tasks:
        add("crit", f"Last run failed: {', '.join(failed_tasks)}", "admin_tasks", "Scheduled tasks")

    cycle = db.get_setting(health.LAST_HEALTH_CYCLE_SETTING)
    age = None
    if cycle:
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(cycle)).total_seconds()
        except ValueError:
            age = None
    # Three missed cycles, not one: a cycle that runs long is normal, a silent one is not.
    if age is None:
        add("warn", "No health-check cycle has finished yet", "admin_tasks", "Scheduled tasks")
    elif age > 3 * config.CHECK_INTERVAL_SECONDS:
        add("crit", f"The last health-check cycle finished {int(age // 60)} minutes ago",
            "admin_tasks", "Scheduled tasks")
    else:
        add("ok", f"Last health-check cycle finished {int(age // 60)} minutes ago",
            "admin_tasks", "Scheduled tasks")

    unreachable = [i["name"] for i in db.list_integrations() if i["enabled"]
                   and (health._integration_status_cache.get(i["id"]) or {}).get("status", {}).get(
                       "reachable") is False]
    if unreachable:
        add("crit", f"Unreachable: {', '.join(unreachable)}", "admin_integrations", "Integrations")

    failed_notifications = db.notification_queue_summary()["failed"]
    if failed_notifications:
        add("warn", f"{plural(failed_notifications, 'notification')} given up on after repeated failures",
            "admin_user_notifications", "Per-user notifications")

    findings = _security_checks()
    if findings:
        add("warn", f"{plural(len(findings), 'security check')} to look at",
            "admin_security", "Security")
    else:
        add("ok", "Nothing to flag on the Security page", "admin_security", "Security")

    order = {"crit": 0, "warn": 1, "ok": 2}
    return sorted(rows, key=lambda r: order[r[0]])


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
# The daily `db_backup` task writes into the same folder under its own prefix. Listing
# and pruning are per prefix, so neither kind of copy can ever push out the other.
SAFETY_BACKUP_PREFIX = "portal-before-restore-"
SCHEDULED_BACKUP_PREFIX = "portal-scheduled-"
KEEP_SCHEDULED_DB_BACKUPS = 7

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
    path = os.path.join(DB_SAFETY_BACKUP_DIR, f"{SAFETY_BACKUP_PREFIX}{stamp}.db")
    db.backup_to_file(path)
    return path


def list_db_safety_backups(prefix=None):
    """Newest first. `prefix` selects the kind of copy; the default is the
    "before a restore" ones. A local os.listdir + stat, the same cheap class of call as
    asset_url()'s getmtime - not the kind of slow outbound I/O the no-blocking-in-a-
    request-handler rule is about, and not worth a cache."""
    try:
        prefix = SAFETY_BACKUP_PREFIX if prefix is None else prefix
        names = [n for n in os.listdir(DB_SAFETY_BACKUP_DIR)
                 if n.endswith(".db") and n.startswith(prefix)]
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


def _prune_db_safety_backups(prefix=None, keep=None):
    """Keeps the newest KEEP_DB_SAFETY_BACKUPS (of the given prefix). Reads the module constant inside the
    function rather than as a default argument, so a monkeypatched value in a test is
    actually honoured - a default arg binds at def time, the same trap that made
    updater._prune_backups pass for the wrong reason once."""
    keep = KEEP_DB_SAFETY_BACKUPS if keep is None else keep
    for entry in list_db_safety_backups(prefix)[keep:]:
        try:
            os.remove(entry["path"])
        except OSError:
            _logger.warning("Could not prune old database snapshot %s", entry["name"])


def _db_backup_task():
    """Body of the `db_backup` scheduled task: a consistent snapshot of the database
    into instance/db_backups/, keeping the newest KEEP_SCHEDULED_DB_BACKUPS. Only
    "portal-scheduled-*" files are ever pruned - the before-restore copies are not
    this task's to delete."""
    os.makedirs(DB_SAFETY_BACKUP_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = os.path.join(DB_SAFETY_BACKUP_DIR, f"{SCHEDULED_BACKUP_PREFIX}{stamp}.db")
    try:
        db.backup_to_file(path)
    except Exception:
        for suffix in ("", "-wal", "-shm", "-journal"):
            try:
                os.remove(path + suffix)
            except OSError:
                pass
        raise
    _prune_db_safety_backups(SCHEDULED_BACKUP_PREFIX, KEEP_SCHEDULED_DB_BACKUPS)
    return f"Saved {os.path.basename(path)}."


scheduler.register(
    "db_backup",
    "Back up the database",
    "Saves a consistent copy of the database to instance/db_backups/ every day and "
    "keeps the newest seven. The database holds all settings, integrations and "
    "history, so this is the copy to reach for if the disk or the file is damaged.",
    _db_backup_task,
    default_schedule_kind="daily",
    default_daily_at="03:00",
)


# ---- Settings ----
def _change_admin_password(redirect_endpoint):
    """The password-change form, shared by /admin/security and the older POST to
    /admin/settings (kept so existing bookmarks and scripts still work). Returns a
    redirect when it must not proceed (2FA gate), otherwise None; either way the
    outcome has been flashed."""
    current = request.form.get("current_password", "")
    new = request.form.get("new_password", "")
    confirm = request.form.get("confirm_password", "")
    stored = db.get_setting("admin_password_hash")
    # Same counter as the login page, _require_totp() and /admin/2fa/disable: with
    # a stolen session cookie this form was an unthrottled password-guessing loop.
    if _login_locked():
        flash("Too many incorrect attempts - try again in a few minutes.", "error")
    elif not check_password_hash(stored, current):
        _register_login_failure()
        flash("Current password is incorrect.", "error")
    elif len(new) < 6:
        flash("New password must be at least 6 characters.", "error")
    elif new != confirm:
        flash("Passwords do not match.", "error")
    else:
        # With 2FA on, changing the password also needs a current code: it is the
        # action that signs the real admin out and leaves a cookie thief in.
        blocked = _require_totp("Incorrect or missing 2FA code - password not changed.",
                                redirect_endpoint)
        if blocked:
            return blocked
        if not twofactor.is_enabled():
            _register_login_success()
        db.set_setting("admin_password_hash", generate_password_hash(new))
        # The natural reaction to a suspected compromise, so it has to actually
        # lock out a copied cookie - every other admin session ends here.
        _rotate_admin_session_epoch()
        flash("Password changed.", "success")
    return None


@app.route("/admin/settings", methods=["GET", "POST"])
@login_required
def admin_settings():
    if request.method == "POST":
        # The password form moved to /admin/security; this POST is kept for old callers.
        return _change_admin_password("admin_security") or redirect(url_for("admin_security"))
    section_order = _public_section_order()
    section_labels = dict(PUBLIC_SECTIONS)
    return render_template("admin_settings.html", check_interval=config.CHECK_INTERVAL_SECONDS,
                            kiosk_enabled=kiosk_enabled(),
                            show_public_version=_show_public_version(),
                            kiosk_rotation_seconds=_kiosk_rotation_seconds(),
                            kiosk_min_rotation=KIOSK_MIN_ROTATION_SECONDS,
                            kiosk_max_rotation=KIOSK_MAX_ROTATION_SECONDS,
                            kiosk_view_choices=_kiosk_view_choices(),
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
                            tls_expiry_alert_days=db.get_setting("tls_expiry_alert_days", ""),
                            status_history_retention_days=db.get_setting(
                                "status_history_retention_days", str(DEFAULT_HISTORY_RETENTION_DAYS)),
                            vms=monitoring.get_cached_vm_snapshot(), hostname=platform.node(),
                            active="settings")


def _setting_digits(raw, fallback):
    """A submitted number as the string stored for it, parsed by db.parse_int(), or
    `fallback` when it isn't one. Checking with str.isdigit() alone used to store
    values like "²", which every later read then failed to int() - on the public
    page, for some of them."""
    value = db.parse_int(raw)
    return fallback if value is None else str(value)


@app.route("/admin/settings/general", methods=["POST"])
@login_required
def admin_settings_general():
    db.set_setting("site_name", request.form.get("site_name", "").strip() or "Server")
    for key in _PUBLIC_RESOURCE_KEYS:
        db.set_setting(key, "1" if request.form.get(key) else "0")
    for key in _PUBLIC_MEDIA_KEYS:
        db.set_setting(key, "1" if request.form.get(key) else "0")
    db.set_setting("media_calendar_days", _setting_digits(
        request.form.get("media_calendar_days"), str(integrations.DEFAULT_CALENDAR_DAYS)))
    db.set_setting("media_requires_login", "1" if request.form.get("media_requires_login") else "0")
    layout_order = request.form.get("layout_order", "").strip()
    if layout_order:
        db.set_setting("public_layout_order", layout_order)
    db.set_setting("show_public_version", "1" if request.form.get("show_public_version") else "0")
    db.set_setting("kiosk_enabled", "1" if request.form.get("kiosk_enabled") else "0")
    db.set_setting("kiosk_rotation_seconds", _setting_digits(
        request.form.get("kiosk_rotation_seconds"), str(KIOSK_DEFAULT_ROTATION_SECONDS)))
    # getlist() reads identically from repeated checkboxes as it would from a
    # <select multiple>, which is why the picker is a checkbox list. Whitelisted
    # against the declared views so only a real view key is ever stored, and stored
    # even when empty - "" is an admin who unticked everything (and gets the Services
    # fallback), which _kiosk_selected_views() has to be able to tell apart from a
    # setting that was never saved at all.
    db.set_setting("kiosk_views", ",".join(
        key for key in request.form.getlist("kiosk_views") if key in KIOSK_VIEW_LABELS))
    db.set_setting("public_history_days", _setting_digits(request.form.get("public_history_days"), ""))
    flash("Public page settings saved.", "success")
    return redirect(url_for("admin_settings") + "#public-page")


# Each settings form writes only its own keys: a checkbox that is absent from the POST
# reads as "unchecked", so a shared handler would reset the other forms' boxes.
@app.route("/admin/settings/monitoring", methods=["POST"])
@login_required
def admin_settings_monitoring():
    for key, default in integrations.HIGHLOAD_DEFAULTS.items():
        db.set_setting(f"highload_{key}", _setting_digits(request.form.get(f"highload_{key}"), default))
    db.set_setting("lowdisk_percent_threshold",
                    _setting_digits(request.form.get("lowdisk_percent_threshold"), ""))
    db.set_setting("tls_expiry_alert_days",
                    _setting_digits(request.form.get("tls_expiry_alert_days"), ""))
    retention = db.parse_int(request.form.get("status_history_retention_days"))
    db.set_setting("status_history_retention_days",
                    str(retention) if retention else str(DEFAULT_HISTORY_RETENTION_DAYS))
    for key in SERVICE_DEFAULT_FIELDS:
        db.set_setting(f"service_default_{key}",
                        _setting_digits(request.form.get(f"service_default_{key}"), ""))
    db.set_setting("service_default_auto_incident", "1" if request.form.get("service_default_auto_incident") else "0")
    api_mode = request.form.get("service_default_api_health_mode", "off")
    db.set_setting("service_default_api_health_mode", api_mode if api_mode in db.API_HEALTH_MODES else "off")
    db.set_setting("service_default_run_target", request.form.get("service_default_run_target", "").strip())
    db.set_setting("service_default_show_run_target_public",
                    "1" if request.form.get("service_default_show_run_target_public") else "0")
    db.set_setting("service_default_show_dependencies_public",
                    "1" if request.form.get("service_default_show_dependencies_public") else "0")
    flash("Monitoring settings saved.", "success")
    return redirect(url_for("admin_settings") + "#monitoring")


@app.route("/admin/settings/security", methods=["POST"])
@login_required
def admin_settings_security():
    # Both stored as plain digit strings; a non-numeric submission falls back to the
    # default rather than being stored blank, since neither has a meaningful
    # "unset" state the way the two optional thresholds above do.
    db.set_setting("admin_session_timeout_hours", _setting_digits(
        request.form.get("admin_session_timeout_hours"), str(DEFAULT_SESSION_TIMEOUT_HOURS)))
    flash("Security settings saved.", "success")
    return redirect(url_for("admin_security") + "#session")


def _security_checks():
    """Findings for the Security page's checks panel, as (message, link endpoint or
    None, link text). Empty means nothing to flag. Reads this request, settings and the
    update cache only - never a live call."""
    findings = []
    if not twofactor.is_enabled():
        findings.append(("Two-factor authentication is off, so the password alone opens "
                         "this panel.", "admin_2fa", "Set it up"))
    over_https = request.is_secure or request.headers.get(
        "X-Forwarded-Proto", "").split(",")[0].strip().lower() == "https"
    if over_https and not config.FORCE_HTTPS_COOKIES:
        findings.append(("This page was reached over HTTPS but the session cookie is not "
                         "marked Secure. Set PORTAL_FORCE_HTTPS_COOKIES=true.", None, ""))
    if not config.BEHIND_PROXY and any(h in request.headers for h in _FORWARDING_HEADERS):
        findings.append(("Proxy headers (X-Forwarded-For and the like) arrived but "
                         "PORTAL_BEHIND_PROXY is not set, so every visitor looks like "
                         "the proxy and the sign-in lockout and rate limits see one client. "
                         "Set PORTAL_BEHIND_PROXY=true if a proxy or tunnel is in front.",
                         None, ""))
    cached = updater.get_cached_update_status()
    if cached and cached.get("update_available"):
        findings.append(("A newer version is available.", "admin_about", "Open Updates"))
    return findings


@app.route("/admin/security", methods=["GET", "POST"])
@login_required
def admin_security():
    """Everything about who can get into /admin, on one page. The pieces already
    existed but were scattered (password and session timeout in Settings, 2FA in its
    own menu entry, the two API keys on Channels and Display device); the handlers
    they post to are unchanged."""
    if request.method == "POST":
        return _change_admin_password("admin_security") or redirect(url_for("admin_security"))
    return render_template("admin_security.html", totp_enabled=twofactor.is_enabled(),
                            admin_session_timeout_hours=db.get_setting(
                                "admin_session_timeout_hours", str(DEFAULT_SESSION_TIMEOUT_HOURS)),
                            max_session_timeout_hours=MAX_SESSION_TIMEOUT_HOURS,
                            session_cookie_max_age_days=config.SESSION_COOKIE_MAX_AGE_DAYS,
                            notify_api_key=db.get_setting(NOTIFY_API_KEY_SETTING, ""),
                            device_api_key=db.get_setting(DEVICE_API_KEY_SETTING, ""),
                            findings=_security_checks(), active="security")


@app.route("/admin/security/sign-out-others", methods=["POST"])
@login_required
def admin_security_sign_out_others():
    """Ends every admin session except this one. The epoch rotation used to happen only
    as a side effect of a password or 2FA change. Behind the 2FA code for the same
    reason the password change is: a copied cookie must not be able to do it."""
    blocked = _require_totp("Incorrect or missing 2FA code - nobody was signed out.",
                            "admin_security")
    if blocked:
        return blocked
    _rotate_admin_session_epoch()
    flash("Every other admin session has been signed out.", "success")
    return redirect(url_for("admin_security"))


# ---- Notifications ----
# The shared secret external services (currently: the gamesportal integration) present
# via X-Api-Key to POST /api/notify/admin and /api/notify/user - see those routes below
# and CLAUDE.md's "Cross-repo: gamesportal" section. Deliberately a DB setting rather
# than a PORTAL_* env var: it's generated and shown by this app, not typed in by the
# admin, and the standard config split reserves env vars for things read from .env -
# same reasoning as asset_cache_salt/the CSRF token being settings-table/session rather
# than config.py.
NOTIFY_API_KEY_SETTING = "notify_api_key"


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
                            notify_api_key=db.get_setting(NOTIFY_API_KEY_SETTING, ""),
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


def _key_page(default_endpoint):
    """Where a key regeneration returns to: the Security page when the button was
    pressed there (a hidden `back=security` field), otherwise the page that owns the
    key. Whitelisted rather than a free `next`, so it can't be an open redirect."""
    if request.form.get("back") == "security":
        return url_for("admin_security") + "#api-keys"
    return url_for(default_endpoint)


@app.route("/admin/notifications/api-key/regenerate", methods=["POST"])
@login_required
def admin_notifications_regenerate_api_key():
    """Generates a fresh key for /api/notify/* and throws the old one away outright -
    there is no "current + previous" grace period, since the only caller (currently:
    gamesportal) has to be handed the new value out of band anyway before it can use
    it again. Shown in plain text on the page afterwards, same as this is the only
    place it's ever displayed - there's nothing else in this admin panel to mask it
    from."""
    db.set_setting(NOTIFY_API_KEY_SETTING, secrets.token_hex(24))
    flash("New API key generated. The old key stopped working immediately - update "
          "anything using it.", "success")
    return redirect(_key_page("admin_notifications"))


@app.route("/admin/device", methods=["GET", "POST"])
@login_required
def admin_device():
    """The switch, the key and the setup hints for the display-device API. Its own
    page rather than a panel on Channels or Settings: it is neither a notification nor
    one more checkbox in the big settings form (whose nested-<form> trap a key
    regeneration button would walk straight into), and "where do I configure the
    display" deserves one answer."""
    if request.method == "POST":
        enable = bool(request.form.get("device_api_enabled"))
        db.set_setting(DEVICE_API_ENABLED_SETTING, "1" if enable else "0")
        if enable and not db.get_setting(DEVICE_API_KEY_SETTING, ""):
            # Turning it on with nothing to authenticate against would be an endpoint
            # that 404s while the page says it is on.
            db.set_setting(DEVICE_API_KEY_SETTING, secrets.token_hex(24))
            flash("Display device API enabled, and a key generated.", "success")
        else:
            flash("Display device API enabled." if enable else "Display device API disabled.",
                  "success")
        return redirect(url_for("admin_device"))
    example, example_bytes = device_api.example_json()
    example_full, example_full_bytes = device_api.example_full_json()
    return render_template("admin_device.html", example_vms=device_api.example_vms_json(),
                            example_full=example_full, example_full_bytes=example_full_bytes,
                            example_full_query=device_api.FULL_EXAMPLE_QUERY,
                            full=device_api.FULL, compact=device_api.COMPACT,
                            max_bytes_full=device_api.MAX_BYTES_FULL,
                            max_bytes_full_sections=device_api.MAX_BYTES_FULL_SECTIONS,
                            device_api_enabled=db.get_setting(DEVICE_API_ENABLED_SETTING, "0") == "1",
                            device_api_key=db.get_setting(DEVICE_API_KEY_SETTING, ""),
                            port=config.PORT, example=example, example_bytes=example_bytes,
                            max_bytes=device_api.MAX_BYTES, active="device")


@app.route("/admin/device/key/regenerate", methods=["POST"])
@login_required
def admin_device_regenerate_key():
    """A fresh key, and the old one is dead immediately - no grace period, for the same
    reason as the notification key: the display has to be handed the new value by hand
    before it can use it again. Independent of notify_api_key; rotating one never
    touches the other."""
    db.set_setting(DEVICE_API_KEY_SETTING, secrets.token_hex(24))
    flash("New display device key generated. The old key stopped working immediately - "
          "enter the new one on the display.", "success")
    return redirect(_key_page("admin_device"))


@app.route("/admin/settings/backup-db", methods=["POST"])
@login_required
def admin_settings_backup_db():
    """The backup holds the TOTP seed and every integration API key, so a session
    cookie alone must not be enough to take it: POST behind _require_totp(), like the
    restore."""
    blocked = _require_totp("Enter your 2FA code to download a backup.", "admin_about")
    if blocked:
        return blocked
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
    # Refused outright while 2FA is already on, for GET and POST alike. Otherwise a
    # stolen session cookie alone could enrol the attacker's own authenticator over
    # the admin's, then pass every _require_totp() step-up with it. Switching to a
    # different device means disabling first, which does need a current code. The
    # 2FA page never links here while enabled, so the real admin never sees this.
    if twofactor.is_enabled():
        flash("Two-factor authentication is already enabled. Disable it first to "
              "enrol a different authenticator.", "error")
        return redirect(url_for("admin_2fa"))

    if request.method == "POST":
        secret = session.get("pending_totp_secret")
        code = request.form.get("totp_code", "")
        if secret and twofactor.verify_code(secret, code):
            db.set_setting("admin_totp_secret", secret)
            db.set_setting("admin_totp_enabled", "1")
            session.pop("pending_totp_secret", None)
            _rotate_admin_session_epoch()
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
    # Same counter as the login page and _require_totp(): with a stolen session
    # cookie this form was otherwise an unthrottled 6-digit guessing loop, and
    # winning it has the same end state as a stolen authenticator.
    if _login_locked():
        flash("Too many incorrect attempts - try again in a few minutes.", "error")
        return redirect(url_for("admin_2fa"))
    code = request.form.get("totp_code", "")
    secret = db.get_setting("admin_totp_secret")
    if secret and twofactor.verify_code(secret, code):
        _register_login_success()
        twofactor.disable()
        _rotate_admin_session_epoch()
        flash("Two-factor authentication disabled.", "success")
    else:
        _register_login_failure()
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
    hours = db.parse_int(raw, DEFAULT_USER_SESSION_TIMEOUT_HOURS)
    if hours <= 0:
        return None
    return min(hours, MAX_SESSION_TIMEOUT_HOURS) * 3600


# ---------------------------------------------------------------------------
# Status history retention (the checks themselves are in health.py)
# ---------------------------------------------------------------------------
# How long individual health-check results are kept (settings-page tunable). The
# uptime figures come from the daily aggregates (status_daily, kept a year), so the
# per-check rows are only detail. This default is what a new install gets; an install
# that existed before the aggregates had db.LEGACY_HISTORY_RETENTION_DAYS written into
# its settings once, by init_db(), unless it had saved a value of its own.
DEFAULT_HISTORY_RETENTION_DAYS = 14

def _history_retention_days():
    days = db.parse_int(db.get_setting("status_history_retention_days", ""))
    # 0 isn't "keep nothing": it falls back to the default, like anything unparseable.
    return days if days else DEFAULT_HISTORY_RETENTION_DAYS


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
    daily_deleted = db.prune_status_daily()
    if daily_deleted:
        _logger.info("Pruned %d status_daily rows older than %d days",
                     daily_deleted, db.STATUS_DAILY_RETENTION_DAYS)
    daily_note = (f" Removed {daily_deleted} daily summary row(s) older than "
                  f"{db.STATUS_DAILY_RETENTION_DAYS} days." if daily_deleted else "")
    if deleted:
        _logger.info("Pruned %d status_history rows older than %d days", deleted, days)
        return f"Removed {deleted} check result(s) older than {days} days.{daily_note}"
    # A normal success, not a TaskSkipped: there was nothing to do because the table
    # is already inside its retention window, which is the job working, not the job
    # being unconfigured.
    return f"Nothing to remove - no check results are older than {days} days.{daily_note}"


scheduler.register(
    "prune_status_history",
    "Prune old check results",
    "Deletes individual health-check results older than the retention window set "
    "under Settings, and daily uptime summaries older than a year. Keeping them "
    "trimmed is what stops the database and its backups growing every week.",
    _prune_status_history_task,
    default_schedule_kind="daily",
    default_daily_at="03:30",
)
health.register_tasks()


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
    is_alive=health._health_thread_alive,
)
scheduler.register_loop(
    "resource_polling",
    "Resource polling",
    "Samples CPU, memory, disks and network into the cache the Resources & VMs page reads, "
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
# Routes split out of this file by domain (see routes/__init__.py). Imported last, so
# every helper above exists by the time they `from app import` it. Under
# `python app.py` this module runs as __main__, so it is aliased as `app` first -
# otherwise those imports would load a second copy of this file with its own Flask
# object, and the routes would land on that one.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    sys.modules.setdefault("app", sys.modules[__name__])
import routes.account
import routes.admin_content
import routes.admin_integrations
import routes.admin_system
import routes.admin_users
import routes.api
import routes.public
import routes.search  # noqa: F401

# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging_setup.init_logging()
    db.init_db()
    # If the previous shutdown was an in-app update restarting into a new version,
    # this is where that gets confirmed (or reported as not having taken effect).
    updater.check_pending_marker()
    health.start_background_checker()
    monitoring.start_background_refresh(config.RESOURCE_REFRESH_SECONDS)
    scheduler.start()
    discord_bot.start()
    # debug must stay False whenever this is reachable outside localhost.
    app.run(host=config.HOST, port=config.PORT, debug=False)
