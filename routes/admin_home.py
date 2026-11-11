"""The admin panel's front door: /admin/login (password, then the 2FA code when it is
on), /admin/logout and the /admin overview. The session itself is started and ended
by app.py's _start_admin_session() and _end_admin_session(); the first is still the
only place that sets `logged_in`. See routes/__init__.py for how this module attaches
to the app."""
import logging
import time
from datetime import datetime, timezone

from flask import flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import config
import db
import health
import scheduler
import seerr_alerts
import twofactor
import updater
import version_checks
from app import (
    _check_and_process_password_reset_flag,
    _end_admin_session,
    _first_run_setup_allowed,
    _login_locked,
    _register_login_failure,
    _register_login_success,
    _safe_next_url,
    _security_checks,
    _start_admin_session,
    app,
    is_first_run,
    login_required,
)

# Logs under app.py's logger name, as these views did before they moved here.
_logger = logging.getLogger("app")




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
