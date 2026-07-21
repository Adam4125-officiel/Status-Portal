"""
app.py — Portail de statut du serveur perso.
Lance avec : python app.py
Panel admin : /admin (mot de passe défini au premier lancement)
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

import db

app = Flask(__name__)
app.secret_key = os.environ.get("PORTAL_SECRET_KEY", "change-moi-en-prod-" + os.urandom(8).hex())

CHECK_INTERVAL_SECONDS = 120  # fréquence des health checks auto

_URL_RE = re.compile(r"(https?://[^\s<]+)")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


@app.template_filter("richtext")
def richtext_filter(text):
    """Texte libre (annonces, page infos) -> HTML minimal et sûr.
    Supporte **gras**, liens auto-cliquables, sauts de ligne."""
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
# Pages publiques
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    services = db.list_services()
    announcements = db.list_announcements(limit=10)
    incidents = db.list_incidents(limit=8)
    info = db.get_info_page()
    overall = compute_overall_status(services)
    return render_template("index.html", services=services, announcements=announcements,
                            incidents=incidents, info=info, overall=overall)


@app.route("/api/status")
def api_status():
    services = db.list_services()
    announcements = db.list_announcements(limit=10)
    incidents = db.list_incidents(limit=8)
    return jsonify({
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
        password = request.form.get("password", "")
        if first_run:
            confirm = request.form.get("confirm", "")
            if len(password) < 6:
                flash("Le mot de passe doit faire au moins 6 caractères.", "error")
            elif password != confirm:
                flash("Les mots de passe ne correspondent pas.", "error")
            else:
                db.set_setting("admin_password_hash", generate_password_hash(password))
                session["logged_in"] = True
                return redirect(url_for("admin_dashboard"))
        else:
            stored = db.get_setting("admin_password_hash")
            if stored and check_password_hash(stored, password):
                session["logged_in"] = True
                nxt = request.args.get("next") or url_for("admin_dashboard")
                return redirect(nxt)
            flash("Mot de passe incorrect.", "error")
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
        flash("Service ajouté.", "success")
        return redirect(url_for("admin_services"))
    return render_template("admin_service_form.html", service=None, active="services")


@app.route("/admin/services/<int:service_id>/edit", methods=["GET", "POST"])
@login_required
def admin_service_edit(service_id):
    service = db.get_service(service_id)
    if not service:
        flash("Service introuvable.", "error")
        return redirect(url_for("admin_services"))
    if request.method == "POST":
        data = dict(request.form)
        data["manual_override"] = 1 if request.form.get("manual_override") else 0
        data["auto_check"] = 1 if request.form.get("auto_check") else 0
        db.update_service(service_id, data)
        flash("Service mis à jour.", "success")
        return redirect(url_for("admin_services"))
    return render_template("admin_service_form.html", service=service, active="services")


@app.route("/admin/services/<int:service_id>/delete", methods=["POST"])
@login_required
def admin_service_delete(service_id):
    db.delete_service(service_id)
    flash("Service supprimé.", "success")
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
        flash("Annonce publiée.", "success")
        return redirect(url_for("admin_announcements"))
    return render_template("admin_announcement_form.html", announcement=None, active="announcements")


@app.route("/admin/announcements/<int:aid>/edit", methods=["GET", "POST"])
@login_required
def admin_announcement_edit(aid):
    announcement = db.get_announcement(aid)
    if not announcement:
        flash("Annonce introuvable.", "error")
        return redirect(url_for("admin_announcements"))
    if request.method == "POST":
        data = dict(request.form)
        data["pinned"] = 1 if request.form.get("pinned") else 0
        db.update_announcement(aid, data)
        flash("Annonce mise à jour.", "success")
        return redirect(url_for("admin_announcements"))
    return render_template("admin_announcement_form.html", announcement=announcement, active="announcements")


@app.route("/admin/announcements/<int:aid>/delete", methods=["POST"])
@login_required
def admin_announcement_delete(aid):
    db.delete_announcement(aid)
    flash("Annonce supprimée.", "success")
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
        flash("Incident enregistré.", "success")
        return redirect(url_for("admin_incidents"))
    return render_template("admin_incident_form.html", incident=None, services=services, active="incidents")


@app.route("/admin/incidents/<int:iid>/edit", methods=["GET", "POST"])
@login_required
def admin_incident_edit(iid):
    incident = db.get_incident(iid)
    services = db.list_services()
    if not incident:
        flash("Incident introuvable.", "error")
        return redirect(url_for("admin_incidents"))
    if request.method == "POST":
        db.update_incident(iid, request.form)
        flash("Incident mis à jour.", "success")
        return redirect(url_for("admin_incidents"))
    return render_template("admin_incident_form.html", incident=incident, services=services, active="incidents")


@app.route("/admin/incidents/<int:iid>/delete", methods=["POST"])
@login_required
def admin_incident_delete(iid):
    db.delete_incident(iid)
    flash("Incident supprimé.", "success")
    return redirect(url_for("admin_incidents"))


# ---- Info page ----
@app.route("/admin/info", methods=["GET", "POST"])
@login_required
def admin_info():
    if request.method == "POST":
        db.set_info_page(request.form.get("content", ""))
        flash("Page d'infos mise à jour.", "success")
        return redirect(url_for("admin_info"))
    content = db.get_info_page()
    return render_template("admin_info.html", content=content, active="info")


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
            flash("Mot de passe actuel incorrect.", "error")
        elif len(new) < 6:
            flash("Le nouveau mot de passe doit faire au moins 6 caractères.", "error")
        elif new != confirm:
            flash("Les mots de passe ne correspondent pas.", "error")
        else:
            db.set_setting("admin_password_hash", generate_password_hash(new))
            flash("Mot de passe changé.", "success")
    return render_template("admin_settings.html", check_interval=CHECK_INTERVAL_SECONDS, active="settings")


# ---------------------------------------------------------------------------
# Health check en tâche de fond
# ---------------------------------------------------------------------------
def run_health_checks():
    while True:
        try:
            services = db.list_services()
            for s in services:
                if not s["auto_check"] or s["manual_override"] or not s["check_url"]:
                    continue
                start = time.time()
                try:
                    r = requests.get(s["check_url"], timeout=5)
                    elapsed_ms = int((time.time() - start) * 1000)
                    status = "operational" if r.ok else "degraded"
                except requests.RequestException:
                    elapsed_ms = None
                    status = "down"
                db.update_service_status_from_check(s["id"], status, elapsed_ms)
        except Exception as e:
            print(f"[health-check] erreur: {e}")
        time.sleep(CHECK_INTERVAL_SECONDS)


def start_background_checker():
    t = threading.Thread(target=run_health_checks, daemon=True)
    t.start()


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    db.init_db()
    start_background_checker()
    # En prod (IIS/waitress), debug doit rester False.
    app.run(host="0.0.0.0", port=5000, debug=False)
