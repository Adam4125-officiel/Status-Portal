"""Admin settings pages: Settings (general, monitoring, password, logo, database
backup), Security, Notifications (admin channels, per-user defaults and overrides,
Seerr alerts, the delegation API key), the display-device page and two-factor
authentication. See routes/__init__.py for how this module attaches to the app.

The logo's upload directory (portal.LOGO_UPLOAD_DIR) is read through the app module
at call time, because the tests replace it there."""
import io
import os
import platform
import secrets
import tempfile
import zipfile
from datetime import datetime, timezone

from flask import (
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

import app as portal
import config
import db
import device_api
import discord_bot
import integrations
import monitoring
import notifications
import scheduler
import seerr_alerts
import twofactor
import user_notify
from app import (
    _PUBLIC_MEDIA_KEYS,
    _PUBLIC_RESOURCE_KEYS,
    DEFAULT_HISTORY_RETENTION_DAYS,
    DEFAULT_SESSION_TIMEOUT_HOURS,
    DEVICE_API_ENABLED_SETTING,
    DEVICE_API_KEY_SETTING,
    KIOSK_DEFAULT_ROTATION_SECONDS,
    KIOSK_MAX_ROTATION_SECONDS,
    KIOSK_MIN_ROTATION_SECONDS,
    KIOSK_VIEW_LABELS,
    LOGO_ALLOWED_EXTENSIONS,
    MAX_SESSION_TIMEOUT_HOURS,
    NOTIFY_API_KEY_SETTING,
    PUBLIC_SECTIONS,
    SERVICE_DEFAULT_FIELDS,
    _kiosk_rotation_seconds,
    _kiosk_view_choices,
    _login_locked,
    _media_requires_login,
    _public_media_visibility,
    _public_resource_visibility,
    _public_section_order,
    _register_login_failure,
    _register_login_success,
    _require_totp,
    _rotate_admin_session_epoch,
    _security_checks,
    _service_defaults,
    _setting_digits,
    _show_public_version,
    app,
    kiosk_enabled,
    login_required,
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
    os.makedirs(portal.LOGO_UPLOAD_DIR, exist_ok=True)
    filename = secure_filename(f"logo.{ext}")
    # A re-upload in a different format (e.g. .png replacing an old .svg) would
    # otherwise leave the old file orphaned on disk alongside the new one, since
    # the filename itself (and therefore the on-disk path) changes with it.
    old = db.get_setting("site_logo_filename", "")
    if old and old != filename:
        try:
            os.remove(os.path.join(portal.LOGO_UPLOAD_DIR, old))
        except OSError:
            pass
    file.save(os.path.join(portal.LOGO_UPLOAD_DIR, filename))
    db.set_setting("site_logo_filename", filename)
    flash("Logo updated.", "success")
    return redirect(url_for("admin_settings"))


@app.route("/admin/settings/logo/remove", methods=["POST"])
@login_required
def admin_settings_logo_remove():
    filename = db.get_setting("site_logo_filename", "")
    if filename:
        try:
            os.remove(os.path.join(portal.LOGO_UPLOAD_DIR, filename))
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
