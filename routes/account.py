"""The signed-in visitor's side: Jellyfin-backed sign-in and sign-out, the account
page and its contact prompt, report replies, the Seerr contact import/push and the
theme toggle. Entirely separate from the admin session (see app.py's "Visitor
sessions" section). See routes/__init__.py for how this module attaches to the app."""
import logging

import requests
from flask import abort, flash, redirect, render_template, request, session, url_for

import app as portal
import db
import integrations
import jellyfin_auth
import user_notify
from app import (
    REPORT_STATUS_LABELS,
    _contact_prompt_url_if_needed,
    _end_user_session,
    _register_user_login_failure,
    _register_user_login_success,
    _safe_next_url,
    _save_account_prefs,
    _start_user_session,
    _user_login_locked,
    app,
    current_user,
    user_login_required,
)

# Logs under app.py's logger name, as these views did before they moved here.
_logger = logging.getLogger("app")


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

        slots = portal._user_login_slots
        if not slots.acquire(blocking=False):
            # Busy, not a wrong guess: never counted towards the lockout.
            flash("Sign-in is busy right now - please try again in a moment.", "error")
            return render_template("user_login.html", next_url=next_url)
        try:
            result = jellyfin_auth.authenticate(username, password)
        finally:
            slots.release()
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
        errors = _save_account_prefs(user_id, request.form)
        for message in errors:
            flash(message, "error")
        flash("Your settings have been saved." if not errors
              else "The rest of your settings have been saved.", "success")
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
                            my_requests=integrations.requests_for_jellyfin_user(user_id),
                            requests_refreshed=integrations.get_cached_media()["refreshed_at"],
                            requests_configured=integrations.seerr_integration() is not None,
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
                            user=user)


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
    # Live, not the hourly mirror: this writes to a Seerr account, so which one is
    # decided by what Seerr says now.
    account = user_notify.find_seerr_account(user["id"], live=True)
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
        flash(f"Couldn't update Seerr: {integrations.describe_request_error(e)}", "error")
        return redirect(url_for("user_account"))
    db.set_user_preferences(user["id"], seerr_user_id=account["id"])
    user_notify._invalidate_seerr_account_cache(user["id"])
    flash("Your Seerr account has been updated with these details.", "success")
    return redirect(url_for("user_account"))


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


@app.route("/logout", methods=["POST"])
def user_logout():
    """Ends only the visitor session - see _end_user_session() for why this must not
    be session.clear()."""
    _end_user_session()
    flash("You have been signed out.", "success")
    return redirect(url_for("index"))
