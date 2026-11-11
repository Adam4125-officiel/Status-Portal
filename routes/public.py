"""The public pages a visitor browses: the status page, the /server, /media and /info
sub-pages (and the old addresses that redirect into /server), kiosk mode and the
"report a problem" form. See routes/__init__.py for how this module attaches to the
app; the helpers these views share with the rest of the portal stay in app.py."""
import time

from flask import abort, flash, redirect, render_template, request, session, url_for

import app as portal
import config
import db
import notifications
from app import (
    REPORT_MIN_SECONDS_TO_FILL,
    _activity_available,
    _attach_integration_status,
    _enrich_incidents,
    _enrich_services,
    _group_services,
    _high_load,
    _kiosk_rendered_views,
    _kiosk_rotation_seconds,
    _public_history_days,
    _public_section_order,
    _register_report_submission,
    _render_public_page,
    _report_rate_limited,
    _resources_available,
    _vms_available,
    app,
    compute_overall_status,
    current_user,
    kiosk_enabled,
)


@app.route("/")
def index():
    services = _attach_integration_status(_enrich_services(db.list_services()))
    groups = _group_services(services)
    announcements = db.list_active_announcements(limit=10)
    incidents = _enrich_incidents(db.list_incidents(limit=8, max_age_days=_public_history_days()))
    # Distinguishes "no incidents exist" from "incidents exist but are all older
    # than public_history_days" - the unfiltered existence check only runs when
    # the filtered list is already empty, so this adds no query in the common case.
    incidents_hidden = not incidents and bool(db.list_incidents(limit=1))
    maintenance_windows = db.list_public_maintenance_windows()
    overall = compute_overall_status(services)
    # What the banner lists when the overall status isn't "operational": same filter as
    # compute_overall_status(), so the list can never name a service the headline ignores.
    affected_services = [] if overall == "operational" else [
        s for s in services
        if s["status"] != "operational" and not s.get("ignore_in_overall_status")]
    site_name = db.get_setting("site_name", "Server")
    return render_template("index.html", services=services, groups=groups, announcements=announcements,
                            incidents=incidents, incidents_hidden=incidents_hidden,
                            maintenance_windows=maintenance_windows, overall=overall,
                            affected_services=affected_services,
                            site_name=site_name,
                            high_load=_high_load(),
                            active_page="status",
                            section_order=_public_section_order())




@app.route("/server")
def public_server():
    return _render_public_page("server")


# The three pages that became sections of /server. Same rule as before: a section that
# is switched off 404s rather than redirecting to a page that doesn't show it.
_SERVER_SECTIONS = {"resources": _resources_available, "vms": _vms_available,
                    "activity": _activity_available}


def _redirect_to_server_section(key):
    if not _SERVER_SECTIONS[key]():
        abort(404)
    return redirect(url_for("public_server") + "#" + key)


@app.route("/resources")
def public_resources():
    return _redirect_to_server_section("resources")


@app.route("/vms")
def public_vms():
    return _redirect_to_server_section("vms")


@app.route("/activity")
def public_activity():
    return _redirect_to_server_section("activity")


@app.route("/media")
def public_media():
    return _render_public_page("media")


@app.route("/info")
def public_info():
    return _render_public_page("info")


@app.route("/kiosk")
def kiosk():
    """The full-screen display itself. 404s while kiosk mode is off, rather than
    rendering an empty or redirected page - the same answer a switched-off public
    sub-page gives, and the same reasoning: an empty page confirms the feature is
    there and merely hidden."""
    if not kiosk_enabled():
        abort(404)
    return render_template("kiosk.html", kiosk_mode=True,
                            views=_kiosk_rendered_views(),
                            rotation_seconds=_kiosk_rotation_seconds(),
                            refresh_seconds=config.PUBLIC_REFRESH_SECONDS,
                            site_name=db.get_setting("site_name", "Server"))


@app.route("/kiosk/views")
def kiosk_views_fragment():
    """The rotating views, re-rendered, as a server-rendered HTML fragment - polled by
    static/js/kiosk.js so the display's data stays current without a page reload
    resetting the rotation. Same convention as /api/incidents/more and
    /admin/logs/tail; this app has one JSON API (/api/status) and no client-side
    templating.

    Nothing here does live outbound I/O: the VM and Jellyfin data come from their
    background-refreshed caches and the resource snapshot reads monitoring's CPU
    cache, exactly as the equivalent public pages do."""
    if not kiosk_enabled():
        abort(404)
    return render_template("kiosk/_views.html", views=_kiosk_rendered_views(),
                            rotation_seconds=_kiosk_rotation_seconds())


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
    (see _report_rate_limited in app.py).

    Optionally gated behind a signed-in Jellyfin user (see _report_login_required).
    When it is, the CSRF exemption above no longer applies either - the form is then
    exercising an authenticated identity, which is exactly the thing that exemption
    was justified by the absence of."""
    if portal._report_login_required() and not current_user():
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
        # Absent means the form was never opened (a script posting directly): refuse.
        rendered_at = session.get("report_form_rendered_at")
        if rendered_at is None or time.time() - rendered_at < REPORT_MIN_SECONDS_TO_FILL:
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
        service_id = request.form.get("service_id", type=int)
        service = db.get_service(service_id) if service_id else None
        user = current_user()
        # A signed-in reporter has no free-text contact box: the report is attributed,
        # the admin replies in the thread, and notifications have their own channels.
        # A contact saved before that box went away is still attached to the report.
        contact = (db.get_user_preferences(user["id"])["contact"] if user
                   else request.form.get("contact", "").strip()[:200])
        db.create_problem_report(message[:2000], contact, service["id"] if service else None,
                                  reporter_user=(user or {}).get("name", "")[:100],
                                  reporter_user_id=(user or {}).get("id", ""))
        _register_report_submission()
        session.pop("report_form_rendered_at", None)  # one GET authorises one POST
        prefix = f"{service['name']}: " if service else ""
        who = f" (from {user['name']})" if user else ""
        # Anonymous visitor text going into the admin's Discord channel: no pings.
        notifications.notify_async("Problem reported", f"{prefix}{message[:200]}{who}", allow_mentions=False)
        flash("Thanks — your report has been submitted.", "success")
        return redirect(url_for("report_problem"))
    session["report_form_rendered_at"] = time.time()
    user = current_user()
    return render_template("report.html", services=services, preselect_service_id=preselect_service_id,
                            site_name=site_name, prefill_contact="")
