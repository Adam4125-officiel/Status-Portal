"""
app.py — Personal server status portal.
Run with: python app.py
Admin panel: /admin (password is set on first launch)
"""
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import format_datetime
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash, Response
from werkzeug.security import generate_password_hash, check_password_hash
from markupsafe import Markup, escape
import requests

import config
import db
import integrations
import monitoring
import notifications

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


ADMIN_RESOURCE_VISIBLE = {"cpu": True, "memory": True, "disks": True, "disk_io": True,
                          "network": True, "gpu": True}

_PUBLIC_RESOURCE_KEYS = ["show_public_cpu", "show_public_memory", "show_public_disks",
                         "show_public_disk_io", "show_public_network", "show_public_gpu",
                         "show_public_vms"]


def _public_resource_visibility():
    return {key[len("show_public_"):]: db.get_setting(key, "0") == "1" for key in _PUBLIC_RESOURCE_KEYS}


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
            _handle_integration_incident_lifecycle(integ, previous["status"]["reachable"], status["reachable"])


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
    incidents = _enrich_incidents(db.list_incidents(limit=8))
    maintenance_windows = db.list_public_maintenance_windows()
    info = db.get_info_page()
    overall = compute_overall_status(services)
    site_name = db.get_setting("site_name", "Server")
    visible = _public_resource_visibility()
    show_any_resource = any(visible[k] for k in ("cpu", "memory", "disks", "disk_io", "network", "gpu"))
    snapshot = monitoring.get_resource_snapshot() if show_any_resource else None
    vms = monitoring.get_vm_snapshot() if visible["vms"] else []
    return render_template("index.html", services=services, groups=groups, announcements=announcements,
                            incidents=incidents, maintenance_windows=maintenance_windows, info=info, overall=overall,
                            refresh_seconds=config.PUBLIC_REFRESH_SECONDS,
                            resource_refresh_seconds=config.RESOURCE_REFRESH_SECONDS,
                            site_name=site_name, visible=visible, show_any_resource=show_any_resource,
                            snapshot=snapshot, vms=vms)


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
        "maintenance_windows": db.list_public_maintenance_windows(),
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


STATUS_BADGE_LABEL = {"operational": "operational", "degraded": "degraded",
                       "maintenance": "maintenance", "down": "down"}
STATUS_BADGE_COLOR = {"operational": "#3ddc97", "degraded": "#ffb545",
                       "maintenance": "#a08bff", "down": "#ff5470"}


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
        service = db.get_service(i["service_id"]) if i["service_id"] else None
        title = f"{service['name']}: " if service else ""
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
        data["auto_incident"] = 1 if request.form.get("auto_incident") else 0
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
        data["auto_incident"] = 1 if request.form.get("auto_incident") else 0
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
        service = db.get_service(request.form.get("service_id")) if request.form.get("service_id") else None
        prefix = f"{service['name']}: " if service else ""
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
        db.create_maintenance_window(request.form)
        # Applies immediately rather than waiting for the next health-check cycle (up
        # to PORTAL_CHECK_INTERVAL_SECONDS later) - matters most for a window whose
        # start time is already in the past (e.g. "this has actually been going on
        # since two days ago, I forgot to log it"), which should flip the service to
        # maintenance right now, not minutes from now.
        _process_maintenance_and_notify()
        flash("Maintenance window scheduled.", "success")
        return redirect(url_for("admin_maintenance"))
    return render_template("admin_maintenance_form.html", services=services, active="maintenance")


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
    vms = monitoring.get_vm_snapshot()
    return render_template("admin_resources.html", snapshot=snapshot, vms=vms, visible=ADMIN_RESOURCE_VISIBLE,
                            refresh_seconds=config.RESOURCE_REFRESH_SECONDS, active="resources")


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
        url = request.form.get("url", "").strip()
        service_id = db.create_service({
            "name": request.form.get("name", ""),
            "icon": request.form.get("icon", "") or "⚙",
            "description": request.form.get("description", ""),
            "url": url,
            "group_name": request.form.get("group_name", ""),
        })
        db.create_integration({
            "name": request.form.get("name", ""),
            "kind": request.form.get("kind", "arr"),
            "base_url": url,
            "api_key": request.form.get("api_key", ""),
            "enabled": 1,
            "service_id": service_id,
            "show_on_public": 1 if request.form.get("show_on_public") else 0,
        })
        flash("Service and status check created.", "success")
        return redirect(url_for("admin_services"))
    return render_template("admin_new_combined.html", active="services")


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
                            show_public=_public_resource_visibility(),
                            discord_configured=bool(config.DISCORD_WEBHOOK_URL),
                            ntfy_configured=bool(config.NTFY_URL),
                            active="settings")


@app.route("/admin/settings/general", methods=["POST"])
@login_required
def admin_settings_general():
    db.set_setting("site_name", request.form.get("site_name", "").strip() or "Server")
    for key in _PUBLIC_RESOURCE_KEYS:
        db.set_setting(key, "1" if request.form.get(key) else "0")
    flash("Settings updated.", "success")
    return redirect(url_for("admin_settings"))


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


def run_health_checks():
    while True:
        try:
            # Runs before the per-service checks below, so a window that just started
            # (setting manual_override=1) is already in effect for this same cycle's
            # loop - otherwise a service being intentionally taken down for maintenance
            # could get a spurious auto-incident opened in the same instant its window begins.
            _process_maintenance_and_notify()
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
                if s["auto_incident"]:
                    _handle_incident_lifecycle(s, previous_status, status)
            _refresh_integration_cache()
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
    health check already opened one, this won't open a second."""
    service_id = integration["service_id"]
    if new_reachable is False and previous_reachable is not False:
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
    db.init_db()
    start_background_checker()
    # debug must stay False whenever this is reachable outside localhost.
    app.run(host="0.0.0.0", port=5000, debug=False)
