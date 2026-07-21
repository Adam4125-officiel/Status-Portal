"""
app.py — Personal server status portal.
Run with: python app.py
Admin panel: /admin (password is set on first launch)
"""
import os
import re
import threading
import time
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
from werkzeug.security import generate_password_hash, check_password_hash
from markupsafe import Markup, escape
import requests

import config
import db
import integrations
import monitoring

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


@app.errorhandler(500)
def handle_server_error(e):
    return render_template("error.html", code=500, message="Something went wrong."), 500


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


# ---------------------------------------------------------------------------
# Public pages
# ---------------------------------------------------------------------------
def _enrich_services(services):
    for s in services:
        s["links"] = db.list_service_links(s["id"])
        s["uptime"] = db.get_uptime_percentage(s["id"])
    return services


def _enrich_incidents(incidents):
    for i in incidents:
        i["updates"] = db.list_incident_updates(i["id"])
    return incidents


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
    services = _enrich_services(db.list_services())
    groups = _group_services(services)
    announcements = db.list_announcements(limit=10)
    incidents = _enrich_incidents(db.list_incidents(limit=8))
    info = db.get_info_page()
    overall = compute_overall_status(services)
    site_name = db.get_setting("site_name", "Server")
    show_public_resources = db.get_setting("show_public_resources", "0") == "1"
    snapshot = monitoring.get_resource_snapshot() if show_public_resources else None
    return render_template("index.html", services=services, groups=groups, announcements=announcements,
                            incidents=incidents, info=info, overall=overall,
                            refresh_seconds=config.PUBLIC_REFRESH_SECONDS,
                            resource_refresh_seconds=config.RESOURCE_REFRESH_SECONDS,
                            site_name=site_name, show_public_resources=show_public_resources,
                            snapshot=snapshot)


@app.route("/api/status")
def api_status():
    services = _enrich_services(db.list_services())
    incidents = _enrich_incidents(db.list_incidents(limit=8))
    announcements = db.list_announcements(limit=10)
    return jsonify({
        "site_name": db.get_setting("site_name", "Server"),
        "overall": compute_overall_status(services),
        "services": services,
        "announcements": announcements,
        "incidents": incidents,
    })


def compute_overall_status(services):
    if not services:
        return "operational"
    statuses = [s["status"] for s in services]
    if "down" in statuses:
        return "down"
    if "degraded" in statuses:
        return "degraded"
    if "maintenance" in statuses:
        return "maintenance"
    return "operational"


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    first_run = is_first_run()
    if request.method == "POST":
        if not first_run and _login_locked():
            flash("Too many failed attempts. Try again in a few minutes.", "error")
            return render_template("login.html", first_run=first_run)
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
                _register_login_success()
                session["logged_in"] = True
                nxt = request.args.get("next") or url_for("admin_dashboard")
                return redirect(nxt)
            _register_login_failure()
            flash("Incorrect password.", "error")
    return render_template("login.html", first_run=first_run)


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
    return render_template("admin_services.html", services=services, active="services")


@app.route("/admin/services/new", methods=["GET", "POST"])
@login_required
def admin_service_new():
    if request.method == "POST":
        data = dict(request.form)
        data["manual_override"] = 1 if request.form.get("manual_override") else 0
        data["auto_check"] = 1 if request.form.get("auto_check") else 0
        db.create_service(data)
        flash("Service added.", "success")
        return redirect(url_for("admin_services"))
    return render_template("admin_service_form.html", service=None, active="services")


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
        db.update_service(service_id, data)
        labels = request.form.getlist("link_label")
        urls = request.form.getlist("link_url")
        links = [(label.strip(), url.strip()) for label, url in zip(labels, urls) if label.strip() and url.strip()]
        db.replace_service_links(service_id, links)
        flash("Service updated.", "success")
        return redirect(url_for("admin_services"))
    links = db.list_service_links(service_id)
    return render_template("admin_service_form.html", service=service, links=links, active="services")


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
        db.create_incident(request.form)
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
        db.update_incident(iid, request.form)
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
        flash("Update posted.", "success")
    return redirect(url_for("admin_incident_edit", iid=iid))


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
    vms = monitoring.get_vm_snapshot()
    return render_template("admin_resources.html", snapshot=snapshot, vms=vms,
                            refresh_seconds=config.RESOURCE_REFRESH_SECONDS, active="resources")


# ---- Integrations (read-only Jellyfin/Jellyseerr/*Arr status) ----
@app.route("/admin/integrations")
@login_required
def admin_integrations():
    configured = db.list_integrations()
    statuses = {i["id"]: integrations.fetch_integration_status(i) for i in configured if i["enabled"]}
    return render_template("admin_integrations.html", integrations=configured, statuses=statuses,
                            active="integrations")


@app.route("/admin/integrations/new", methods=["GET", "POST"])
@login_required
def admin_integration_new():
    if request.method == "POST":
        data = dict(request.form)
        data["enabled"] = 1 if request.form.get("enabled") else 0
        db.create_integration(data)
        flash("Integration added.", "success")
        return redirect(url_for("admin_integrations"))
    return render_template("admin_integration_form.html", integration=None, active="integrations")


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
        db.update_integration(iid, data)
        flash("Integration updated.", "success")
        return redirect(url_for("admin_integrations"))
    return render_template("admin_integration_form.html", integration=integration, active="integrations")


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
    return render_template("admin_settings.html", check_interval=config.CHECK_INTERVAL_SECONDS,
                            refresh_seconds=config.PUBLIC_REFRESH_SECONDS,
                            site_name=db.get_setting("site_name", "Server"),
                            show_public_resources=db.get_setting("show_public_resources", "0") == "1",
                            active="settings")


@app.route("/admin/settings/general", methods=["POST"])
@login_required
def admin_settings_general():
    db.set_setting("site_name", request.form.get("site_name", "").strip() or "Server")
    db.set_setting("show_public_resources", "1" if request.form.get("show_public_resources") else "0")
    flash("Settings updated.", "success")
    return redirect(url_for("admin_settings"))


# ---------------------------------------------------------------------------
# Background health check
# ---------------------------------------------------------------------------
def run_health_checks():
    while True:
        try:
            services = db.list_services()
            for s in services:
                if not s["auto_check"] or s["manual_override"] or not s["check_url"]:
                    continue
                previous_status = s["status"]
                start = time.time()
                try:
                    r = requests.get(s["check_url"], timeout=5)
                    elapsed_ms = int((time.time() - start) * 1000)
                    status = "operational" if r.ok else "degraded"
                except requests.RequestException:
                    elapsed_ms = None
                    status = "down"
                db.update_service_status_from_check(s["id"], status, elapsed_ms)
                db.record_status_history(s["id"], status, elapsed_ms)
                _handle_incident_lifecycle(s, previous_status, status)
        except Exception as e:
            print(f"[health-check] error: {e}")
        time.sleep(config.CHECK_INTERVAL_SECONDS)


def _handle_incident_lifecycle(service, previous_status, new_status):
    """Auto-opens an incident the moment a service goes down, and auto-resolves it the
    moment it recovers. Only hard 'down' transitions are automated - 'degraded' is left
    for a human to judge, since a single slow response isn't necessarily an incident."""
    if new_status == "down" and previous_status != "down":
        if not db.get_open_auto_incident_for_service(service["id"]):
            incident_id = db.create_auto_incident(
                service["id"], f"{service['name']} is unreachable", "investigating")
            db.create_incident_update(
                incident_id, "Automatic health check could not reach this service.", "investigating")
    elif new_status != "down" and previous_status == "down":
        incident = db.get_open_auto_incident_for_service(service["id"])
        if incident:
            db.update_incident(incident["id"], {
                "service_id": service["id"],
                "title": incident["title"],
                "description": incident["description"],
                "status": "resolved",
            })
            db.create_incident_update(
                incident["id"], "Automatic health check confirmed this service has recovered.", "resolved")


def start_background_checker():
    t = threading.Thread(target=run_health_checks, daemon=True)
    t.start()


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    db.init_db()
    start_background_checker()
    # debug must stay False whenever this is reachable outside localhost.
    app.run(host="0.0.0.0", port=5000, debug=False)
