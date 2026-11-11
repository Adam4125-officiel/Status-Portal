"""Admin pages for the Jellyfin visitor accounts and the Discord bot: the user list
and its settings, per-user access, contact and account pages, test notifications and
direct messages, and the bot's own settings and servers. See routes/__init__.py for
how this module attaches to the app."""
import logging
from datetime import datetime, timezone

from flask import flash, redirect, render_template, request, url_for

import config
import db
import discord_bot
import jellyfin_auth
import user_notify
from app import (
    DEFAULT_USER_SESSION_TIMEOUT_HOURS,
    MAX_SESSION_TIMEOUT_HOURS,
    REPORT_STATUS_LABELS,
    _safe_next_url,
    _save_account_prefs,
    _setting_digits,
    app,
    login_required,
)

# Logs under app.py's logger name, as these views did before they moved here.
_logger = logging.getLogger("app")


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
    db.set_setting("jellyfin_auth_integration_id",
                    _setting_digits(request.form.get("jellyfin_auth_integration_id"), ""))
    db.set_setting("report_requires_login", "1" if request.form.get("report_requires_login") else "0")
    db.set_setting("user_session_timeout_hours", _setting_digits(
        request.form.get("user_session_timeout_hours"), str(DEFAULT_USER_SESSION_TIMEOUT_HOURS)))
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
        errors = _save_account_prefs(user_id, request.form)
        for message in errors:
            flash(message, "error")
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
