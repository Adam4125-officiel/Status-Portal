"""Admin pages for the integrations (Jellyfin, Seerr, the *Arr apps) and the add-new
wizard that creates a service, an integration, or both. See routes/__init__.py for
how this module attaches to the app."""
import platform

from flask import flash, redirect, render_template, request, session, url_for

import config
import db
import health
import integrations
import monitoring
import scheduler
import version_checks
from app import _service_defaults, _setting_digits, app, login_required


# ---- Integrations (read-only Jellyfin/Jellyseerr/*Arr status) ----
@app.route("/admin/integrations")
@login_required
def admin_integrations():
    configured = db.list_integrations()
    statuses = {i["id"]: health._integration_status_cache.get(i["id"]) for i in configured if i["enabled"]}
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
    health._integration_status_cache[iid] = {"status": status, "checked_at": db.now_iso()}
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
    db.set_setting(integrations.SEERR_INTEGRATION_SETTING,
                    _setting_digits(request.form.get("seerr_integration_id"), ""))
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
