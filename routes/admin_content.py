"""Admin pages for what the public status page shows: services, announcements,
incidents, maintenance windows, the info page, problem reports, and the admin
search box. See routes/__init__.py for how this module attaches to the app."""
import platform
import threading

from flask import flash, redirect, render_template, request, url_for

import admin_search
import db
import discord_bot
import health
import monitoring
import notifications
import user_notify
from app import _service_defaults, app, login_required


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


# ---- Services ----
@app.route("/admin/services")
@login_required
def admin_services():
    services = db.list_services()
    tls_expiry = {s["id"]: db.get_tls_expiry(s["id"]) for s in services}
    return render_template("admin_services.html", services=services, tls_expiry=tls_expiry, hostname=platform.node(), active="services")


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
        # Anything that isn't a plain id is skipped, as before - through db.parse_int(),
        # since str.isdigit() let "²" through to an int() that raised.
        depends_on_ids = [n for n in (db.parse_int(i) for i in request.form.getlist("depends_on"))
                          if n is not None]
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
@app.route("/admin/search")
@login_required
def admin_search_results():
    """Returns a server-rendered HTML fragment, not JSON - the same convention as
    /api/incidents/more, /admin/logs/tail and /search/live. This app has exactly one
    JSON API (/api/status, for external consumers) and no client-side templating
    anywhere, and a search box is not the place to introduce some.

    Cheap enough to run per keystroke: it matches against an in-memory index built from
    the templates once (see admin_search.build_index), so a query is string work over a
    few hundred entries with no database or disk access at all."""
    query = request.args.get("q", "")
    return render_template("_admin_search_results.html",
                            query=query.strip(), results=admin_search.search(query))


@app.route("/admin/announcements")
@login_required
def admin_announcements():
    # Every announcement, whatever its window - this is the only page that can edit a
    # scheduled or expired one, so filtering here would strand them. Each row carries
    # its own state so the list can say which is which.
    announcements = db.list_announcements()
    for a in announcements:
        a["window_state"] = db.announcement_window_state(a)
    return render_template("admin_announcements.html", announcements=announcements,
                            sends=db.list_announcement_sends(), active="announcements",
                            **_announcement_channel_context())


def _announcement_window_error(form):
    """The one validation an announcement window needs: an end before its start is a
    window that can never be open, and silently accepting it would leave the admin
    looking at an announcement that simply never appears. Returns a message, or None.

    Everything else is legal on purpose - a start in the past means "already showing",
    an end in the past means "expired, keep it for the record", and both blank means
    exactly what an announcement meant before windows existed."""
    starts_at = (form.get("starts_at") or "").strip()
    ends_at = (form.get("ends_at") or "").strip()
    if starts_at and ends_at and ends_at <= starts_at:
        return "The end of the window has to be after its start."
    return None


def _announcement_send_block(aid):
    """Why this announcement can't be sent as a notification right now, or None.

    Sending "here is some news" for something nobody can see on the site is
    incoherent in both directions: a scheduled one hasn't been published yet, and an
    expired one is telling people to go and look at something that isn't there. The
    admin's fix is obvious and stated - adjust the window, or clear it."""
    announcement = db.get_announcement(aid)
    if not announcement:
        return None
    state = db.announcement_window_state(announcement)
    if state == "scheduled":
        return ("This announcement isn't showing yet, so it wasn't sent — a notification "
                "would point people at something they can't see. Adjust when it starts, "
                "or clear the window, then send it.")
    if state == "expired":
        return ("This announcement has expired, so it wasn't sent — a notification would "
                "point people at something that's no longer on the page. Extend or clear "
                "the window, then send it.")
    return None


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
        window_error = _announcement_window_error(request.form)
        if window_error:
            flash(window_error, "error")
            return render_template("admin_announcement_form.html", announcement=request.form,
                                    is_edit=False, active="announcements",
                                    **_announcement_channel_context())
        data = dict(request.form)
        data["pinned"] = 1 if request.form.get("pinned") else 0
        aid = db.create_announcement(data)
        channels = [c for c in request.form.getlist("channels") if c in ("email", "discord")]
        blocked = _announcement_send_block(aid) if channels else None
        if blocked:
            flash("Announcement published. " + blocked, "error")
        elif channels:
            notes = _dispatch_announcement_send(aid, data["title"], data["message"], channels)
            flash("Announcement published. " + " ".join(notes), "success")
        else:
            flash("Announcement published.", "success")
        return redirect(url_for("admin_announcements"))
    return render_template("admin_announcement_form.html", announcement=None, is_edit=False,
                            active="announcements", **_announcement_channel_context())


@app.route("/admin/announcements/<int:aid>/edit", methods=["GET", "POST"])
@login_required
def admin_announcement_edit(aid):
    announcement = db.get_announcement(aid)
    if not announcement:
        flash("Announcement not found.", "error")
        return redirect(url_for("admin_announcements"))
    if request.method == "POST":
        window_error = _announcement_window_error(request.form)
        if window_error:
            flash(window_error, "error")
            return render_template("admin_announcement_form.html", announcement=request.form,
                                    is_edit=True, active="announcements",
                                    **_announcement_channel_context())
        data = dict(request.form)
        data["pinned"] = 1 if request.form.get("pinned") else 0
        db.update_announcement(aid, data)
        channels = [c for c in request.form.getlist("channels") if c in ("email", "discord")]
        # Checked *after* the update, deliberately: the admin may be fixing the window
        # and sending in the same submit, and the window they just saved is the one that
        # should decide.
        blocked = _announcement_send_block(aid) if channels else None
        if blocked:
            flash("Announcement updated. " + blocked, "error")
        elif channels:
            notes = _dispatch_announcement_send(aid, data["title"], data["message"], channels)
            flash("Announcement updated. " + " ".join(notes), "success")
        else:
            flash("Announcement updated.", "success")
        return redirect(url_for("admin_announcements"))
    return render_template("admin_announcement_form.html", announcement=announcement, is_edit=True,
                            active="announcements", **_announcement_channel_context())


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

    blocked = _announcement_send_block(aid)
    if blocked:
        flash(blocked, "error")
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
        notifications.notify_async("Incident opened", f"{prefix}{request.form.get('title', '')}")
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
        notifications.notify_async(f"Incident update — {status}", f"{incident['title']}: {message}")
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
        health._process_maintenance_and_notify()
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
        health._process_maintenance_and_notify()
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
