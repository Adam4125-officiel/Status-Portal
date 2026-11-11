"""Admin pages about the server and the portal itself: Resources & VMs (with host and
VM control), the log viewer, the System page (caches, restart), About (update check,
self-update, database restore) and Scheduled tasks. See routes/__init__.py for how
this module attaches to the app.

The restart, the database-backup helpers and their settings stay in app.py: the
tests replace them there, so these views call _restart_process through the app
module at call time."""
import logging
import os
import platform
import sys
import tempfile
from datetime import datetime, timezone

from flask import (
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    stream_with_context,
    url_for,
)

import admin_search
import app as portal
import config
import db
import discord_bot
import health
import integrations
import logging_setup
import monitoring
import scheduler
import twofactor
import updater
import user_notify
from app import (
    ADMIN_RESOURCE_VISIBLE,
    ADMIN_SESSION_EPOCH_SETTING,
    SCHEDULED_BACKUP_PREFIX,
    UPTIME_CACHE_TTL_SECONDS,
    _admin_session_epoch,
    _bump_asset_cache_salt,
    _db_safety_snapshot,
    _prune_db_safety_backups,
    _remove_sqlite_sidecars,
    _require_totp,
    _uptime_cache,
    _uptime_strip_cache,
    _write_uploaded_database,
    app,
    list_db_safety_backups,
    login_required,
)

# Logs under app.py's logger name, as these views did before they moved here.
_logger = logging.getLogger("app")


# ---- Resources ----
@app.route("/admin/resources")
@login_required
def admin_resources():
    snapshot = monitoring.get_resource_snapshot()
    vms = monitoring.get_cached_vm_snapshot()
    return render_template("admin_resources.html", snapshot=snapshot, vms=vms, visible=ADMIN_RESOURCE_VISIBLE,
                            refresh_seconds=config.RESOURCE_REFRESH_SECONDS,
                            totp_enabled=twofactor.is_enabled(), active="resources")


@app.route("/admin/resources/host-control", methods=["POST"])
@login_required
def admin_host_control():
    """Restarts/shuts down the host machine itself - see monitoring.control_host()
    for why this is safe from an injection standpoint (zero user-controlled input:
    exactly two fixed actions). The blast radius comes entirely from what the
    action *does*, not from anything an attacker could smuggle into it - login +
    CSRF (already required on every admin POST) plus the confirmation panel in
    admin_resources.html (a click on Confirm, and the 2FA code when it is on) are the
    mitigations for that.

    Step-up authentication: if 2FA is enabled, a fresh code is required here even
    though the session is already logged in - a stolen/replayed session cookie
    alone must not be enough to trigger this specific action, given what it does."""
    action = request.form.get("action", "")
    if action not in ("restart", "shutdown"):
        flash("Unknown host action.", "error")
        return redirect(url_for("admin_resources"))
    blocked = _require_totp("Incorrect or missing 2FA code - host action cancelled.",
                            "admin_resources")
    if blocked:
        return blocked
    success, message = monitoring.control_host(action)
    flash(message, "success" if success else "error")
    return redirect(url_for("admin_resources"))


@app.route("/admin/resources/vm-control", methods=["POST"])
@login_required
def admin_vm_control():
    """Same step-up reasoning as admin_host_control above: a stolen/replayed
    session cookie alone must not be enough to stop or restart a VM."""
    name = request.form.get("name", "")
    action = request.form.get("action", "")
    if action not in ("start", "stop", "restart"):
        flash("Unknown VM action.", "error")
        return redirect(url_for("admin_resources"))
    blocked = _require_totp("Incorrect or missing 2FA code - VM action cancelled.",
                            "admin_resources")
    if blocked:
        return blocked
    success, message = monitoring.control_vm(name, action)
    flash(message, "success" if success else "error")
    return redirect(url_for("admin_resources"))


# ---- Logs (logging_setup.py owns reading them; this is just the page) ----
LOG_PAGE_SIZES = (100, 200, 500, 1000)
LOG_LEVEL_CHOICES = (("", "Everything"), ("INFO", "Info and above"),
                     ("WARNING", "Warnings and errors"), ("ERROR", "Errors only"))


@app.route("/admin/logs")
@login_required
def admin_logs():
    """The last N entries of the current log file, plus what's downloadable.

    Reads the file on every load and deliberately does not cache: a log page whose
    whole job is answering "what just happened" must not show a snapshot from a
    minute ago. That's affordable because every read here is bounded - see
    logging_setup's reader section for why this isn't the slow-I/O rule's business.

    Nothing here writes, so it isn't behind _require_totp() - but it is behind
    login_required, and the log can quote paths, user names and error text from
    every integration, so it is admin-only like the rest of /admin."""
    try:
        limit = int(request.args.get("limit", 200))
    except ValueError:
        limit = 200
    if limit not in LOG_PAGE_SIZES:
        limit = 200
    min_level = request.args.get("level", "")
    if min_level not in [value for value, _ in LOG_LEVEL_CHOICES]:
        min_level = ""

    files = logging_setup.log_files()
    # Read the offset *before* the entries: anything written between the two is then
    # picked up by the first live poll, where taking it after would silently skip it.
    offset = logging_setup.log_size()
    entries = logging_setup.tail_entries(limit=limit, min_level=min_level or None)
    return render_template(
        "admin_logs.html", entries=entries, files=files, limit=limit, offset=offset,
        min_level=min_level, page_sizes=LOG_PAGE_SIZES, level_choices=LOG_LEVEL_CHOICES,
        total_bytes=sum(f["size_bytes"] for f in files),
        log_dir=logging_setup.LOG_DIR, retention_days=config.LOG_RETENTION_DAYS,
        active="logs")


@app.route("/admin/logs/tail")
@login_required
def admin_logs_tail():
    """Whatever has been written to the log since the caller's byte offset, as a
    server-rendered HTML fragment - the same partial the full page renders, so a
    live-appended entry can't look different from one that was there on load. Same
    convention as /api/incidents/more; this app has one JSON API and no client-side
    templating.

    Polled rather than streamed. An SSE or WebSocket endpoint would hold a request
    thread per open page for as long as it stays open, and waitress runs a fixed pool
    of them (config.WAITRESS_THREADS) - a couple of forgotten admin tabs would eat
    the pool that serves the actual portal. A poll that usually costs one getsize()
    and reads nothing is the cheaper answer by a wide margin.

    The response carries the new offset, and whether the caller should replace its
    contents rather than append (the file rotated, was truncated, or so much arrived
    that honouring the cursor would mean an unbounded read)."""
    try:
        since = int(request.args.get("since", ""))
    except ValueError:
        since = None
    try:
        limit = int(request.args.get("limit", 200))
    except ValueError:
        limit = 200
    if limit not in LOG_PAGE_SIZES:
        limit = 200
    min_level = request.args.get("level", "")
    if min_level not in [value for value, _ in LOG_LEVEL_CHOICES]:
        min_level = ""

    text, offset, reset = logging_setup.read_since(since)
    entries = logging_setup.parse_entries(text)
    if min_level:
        threshold = logging_setup.LEVELS.index(min_level)
        entries = [e for e in entries if e["level"] in logging_setup.LEVELS
                    and logging_setup.LEVELS.index(e["level"]) >= threshold]
    entries = entries[-limit:]
    fragment = render_template("sections/_log_entries.html", entries=entries)
    return Response(
        f'<div data-log-fragment data-log-offset="{offset}" '
        f'data-log-reset="{1 if reset else 0}">{fragment}</div>',
        mimetype="text/html", headers={"Cache-Control": "no-store"})


@app.route("/admin/logs/download")
@login_required
def admin_logs_download():
    """Downloads one log file, or (with no name) every one of them concatenated
    oldest-first - which is what someone means by "the full log" once rotation has
    split it into four.

    GET rather than POST: it has no side effects. `name` is resolved through
    logging_setup.log_file_path(), which only ever returns a path for a name in its
    own generated list, so this cannot be pointed at another file. max_age=0 for the
    same reason the database backup uses it - a point-in-time file is not a
    versioned asset, and SEND_FILE_MAX_AGE_DEFAULT is 30 days."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    name = request.args.get("name")
    if name:
        path = logging_setup.log_file_path(name)
        if not path:
            abort(404)
        return send_file(path, mimetype="text/plain", as_attachment=True,
                          download_name=f"status-portal-{name}-{stamp}.log", max_age=0)
    if not logging_setup.log_files():
        abort(404)
    return Response(
        stream_with_context(logging_setup.iter_all_log_bytes()),
        mimetype="text/plain",
        headers={"Content-Disposition":
                  f'attachment; filename="status-portal-{stamp}.log"',
                  "Cache-Control": "no-store"})


@app.route("/admin/system")
@login_required
def admin_system():
    caches, connections = _cache_inventory()
    return render_template("admin_system.html", discord_status=discord_bot.get_status(),
                            discord_configured=bool(config.DISCORD_BOT_TOKEN),
                            totp_enabled=twofactor.is_enabled(), caches=caches,
                            connections=connections, cache_clear_note=CACHE_CLEAR_NOTE,
                            active="system")


CACHE_CLEAR_NOTE = ("Everything here is derived data that rebuilds itself - the "
                    "background checker refills it within one check cycle, and "
                    "nothing stored in the database is touched.")


def _cache_inventory():
    """What's currently held in memory, for the System page's overview. Read-only,
    and cheap: every entry is a len() over a dict this process already has.

    The integration rows double as the "is it actually reachable" view - that
    reachability *is* the cached value, so there's nothing extra to query for it."""
    integration_names = {i["id"]: i["name"] for i in db.list_integrations()}
    entries = [
        {"name": "Uptime percentages",
         "entries": len(_uptime_cache["value"]),
         "detail": f"services (recomputed every {UPTIME_CACHE_TTL_SECONDS}s)",
         "updated_at": None},
        {"name": "Integration status checks",
         "entries": len(health._integration_status_cache),
         "detail": "integrations polled", "updated_at": None},
        {"name": "Update check (GitHub releases)",
         "entries": 1 if updater._update_cache["result"] else 0,
         "detail": "result cached", "updated_at": None},
        {"name": "Jellyfin activity",
         "entries": len(integrations.get_cached_jellyfin_activity()["running_tasks"]),
         "detail": "running tasks", "updated_at": None},
        {"name": "Seerr account lookups",
         "entries": len(user_notify._seerr_account_cache),
         "detail": f"users (recomputed every {user_notify.SEERR_ACCOUNT_CACHE_TTL_SECONDS}s)",
         "updated_at": None},
    ] + monitoring.cache_summary()
    # Reachability per integration, straight out of the cache above.
    connections = [
        {"name": integration_names.get(iid, f"integration {iid}"),
         "reachable": entry["status"]["reachable"],
         "checked_at": entry["checked_at"]}
        for iid, entry in sorted(health._integration_status_cache.items())
    ]
    return entries, connections


def _clear_all_caches():
    """Drops every in-memory cache in the app and bumps the static-asset cache-buster
    so browsers re-fetch CSS/JS too.

    Each module clears its own globals (monitoring/integrations/updater) rather than
    this function reaching into them - a cache added over there should not need an
    edit over here to be covered by this button."""
    _uptime_cache["value"] = {}
    _uptime_cache["fetched_at"] = 0.0
    _uptime_strip_cache["value"] = {}
    _uptime_strip_cache["fetched_at"] = 0.0
    health.clear_caches()
    integrations.clear_caches()
    monitoring.clear_caches()
    updater.clear_update_cache()
    user_notify.clear_caches()
    admin_search.clear_caches()
    _bump_asset_cache_salt()


@app.route("/admin/system/clear-caches", methods=["POST"])
@login_required
def admin_system_clear_caches():
    """Deliberately not behind _require_totp(), unlike the restart buttons next to it:
    nothing is destroyed and nothing goes offline - the worst case is one slightly
    emptier page until the background loop's next tick refills things."""
    _clear_all_caches()
    flash("Cached data cleared. Fresh values are fetched on the next background "
          "check, and browsers will re-download the page's CSS/JS.", "success")
    return redirect(url_for("admin_system"))


@app.route("/admin/system/restart", methods=["POST"])
@login_required
def admin_system_restart():
    """Restarts either the whole app process or just the Discord bot connection -
    see _restart_process()/discord_bot.restart() for what each actually does.
    Same step-up 2FA reasoning as admin_host_control(): a stolen/replayed session
    cookie alone must not be enough to trigger this, given a full-app restart
    briefly takes the whole portal offline and a bot restart interrupts anyone
    mid-conversation with it."""
    component = request.form.get("component", "")
    if component not in ("app", "discord-bot"):
        flash("Unknown restart target.", "error")
        return redirect(url_for("admin_system"))
    blocked = _require_totp("Incorrect or missing 2FA code - restart cancelled.",
                            "admin_system")
    if blocked:
        return blocked
    if component == "app":
        portal._restart_process()
        flash("Restarting the app now - this page will be briefly unreachable.", "success")
    else:
        if not config.DISCORD_BOT_TOKEN:
            flash("Discord bot isn't configured (PORTAL_DISCORD_BOT_TOKEN not set).", "error")
            return redirect(url_for("admin_system"))
        if discord_bot.restart():
            flash("Discord bot restarted.", "success")
        else:
            # restart() reports False when the old connection had to be abandoned
            # rather than closed - a fresh one is starting either way, but that is
            # a symptom worth telling the admin about rather than papering over.
            flash("The bot's previous connection wouldn't shut down, so it was abandoned and a "
                  "fresh one started. If this keeps happening, check instance/logs/app.log.",
                  "error")
    return redirect(url_for("admin_system"))


# ---- About / self-update (see updater.py for everything that actually happens) ----
def _update_check_task_view():
    """The `update_check` task's row as the admin page sees it, or None if updater.py
    somehow didn't register it (a build with the task removed shouldn't 500 here)."""
    task = scheduler.get_task(updater.TASK_NAME)
    return scheduler.task_view(task) if task else None


def _update_check_schedule_label():
    """How often the check runs, in words, for the About page - which shouldn't have
    to know that a schedule can be either an interval or a daily time."""
    view = _update_check_task_view()
    if not view:
        return "on a schedule"
    if view["schedule_kind"] == "daily":
        return f"daily at {view['daily_at']} UTC"
    minutes = view["interval_minutes"] or 0
    if minutes >= 120 and minutes % 60 == 0:
        return f"every {minutes // 60}h"
    return f"every {minutes} min"


def _set_update_check_enabled(enabled):
    """Flips the scheduled task on or off without touching its schedule."""
    view = _update_check_task_view()
    if not view:
        return
    scheduler.save_schedule(updater.TASK_NAME, enabled=enabled,
                             schedule_kind=view["schedule_kind"],
                             interval_minutes=view["interval_minutes"],
                             daily_at=view["daily_at"])


@app.route("/admin/about")
@login_required
def admin_about():
    """Reads the update-check cache only - never checks GitHub inline. The cache is
    refreshed by the `update_check` scheduled task (see /admin/tasks), same
    read-the-cache pattern as health._integration_status_cache. A cache miss (nothing checked
    yet this process, or checking disabled) renders as "not checked yet" rather than
    blocking the page on an outbound call."""
    return render_template(
        "admin_about.html",
        version=config.VERSION,
        version_display=config.VERSION_DISPLAY,
        is_git_checkout=config.IS_GIT_CHECKOUT,
        update_status=updater.get_cached_update_status(),
        channel=updater.get_channel(),
        channels=updater.CHANNELS,
        check_enabled=updater.update_check_enabled(),
        check_schedule=_update_check_schedule_label(),
        inapp_update_enabled=config.ENABLE_INAPP_UPDATE,
        repo_url=updater.REPO_URL,
        releases_url=updater.RELEASES_PAGE_URL,
        backups=list(reversed(updater.list_backups()))[:5],
        db_backups=list_db_safety_backups(),
        scheduled_db_backups=list_db_safety_backups(SCHEDULED_BACKUP_PREFIX),
        db_backup_task=scheduler.task_view(scheduler.get_task("db_backup")),
        python_version=sys.version.split()[0],
        platform_name=f"{platform.system()} {platform.release()}".strip(),
        db_path=db.DB_PATH,
        log_path=os.path.join(config.APP_ROOT, "instance", "logs", "app.log"),
        app_root=config.APP_ROOT,
        totp_enabled=twofactor.is_enabled(),
        active="about",
    )


@app.route("/admin/about/check", methods=["POST"])
@login_required
def admin_about_check():
    """The sanctioned exception to the no-slow-I/O-in-a-request-handler rule: an
    explicit one-shot admin action the admin knows will take a moment, exactly like
    the integrations "Check now" button. Never fires automatically."""
    result = updater.refresh_update_cache_if_stale(force=True)
    if result and result["ok"]:
        if result["update_available"]:
            flash(f"Update available: {result['current']} → {result['latest']}.", "success")
        elif result["ahead"]:
            flash(f"You're running {result['current']}, newer than the latest "
                  f"{result['channel']} release ({result['latest']}).", "success")
        else:
            flash(f"Up to date ({result['current']}).", "success")
    else:
        flash(f"Couldn't check for updates: {result['error'] if result else 'unknown error'}", "error")
    return redirect(url_for("admin_about"))


@app.route("/admin/about/settings", methods=["POST"])
@login_required
def admin_about_settings():
    channel = request.form.get("update_channel", "")
    if channel not in updater.CHANNELS:
        flash("Unknown update channel.", "error")
        return redirect(url_for("admin_about"))
    db.set_setting("update_channel", channel)
    # Writes the scheduled task's own enabled flag rather than a second setting beside
    # it: /admin/about and /admin/tasks are two views of one row, so they cannot drift
    # into disagreeing about whether the portal checks for updates. The rest of the
    # schedule is preserved - this checkbox is an on/off switch, not a reschedule.
    _set_update_check_enabled(bool(request.form.get("update_check_enabled")))
    # The cached result belongs to the old channel - drop it so the page doesn't show
    # a stale "latest stable" reading next to a freshly-selected unstable channel.
    updater.clear_update_cache()
    flash("Update preferences saved.", "success")
    return redirect(url_for("admin_about"))


@app.route("/admin/about/update", methods=["POST"])
@login_required
def admin_update():
    """Installs the latest release and restarts the app into it.

    This is the most powerful button in the app - it installs and then executes new
    code - so it is gated at least as heavily as the host restart/shutdown control:
    login + CSRF (both automatic for any POST under /admin/), a confirmation panel
    client-side, and step-up 2FA requiring a fresh code even on an already
    authenticated session. It additionally honours config.ENABLE_INAPP_UPDATE, which
    lives in an env var rather than a DB setting precisely so an attacker who owns
    the admin panel can't just switch it back on.

    Runs updater.perform_update() synchronously: a deliberate, documented instance
    of the "explicit one-shot admin action the user knows will be slow" exception,
    same as the integration Check-now button, not an automatic background path.
    Restarting reuses _restart_process() rather than inventing a second mechanism."""
    if not config.ENABLE_INAPP_UPDATE:
        flash("In-app updates are disabled (PORTAL_ENABLE_INAPP_UPDATE=false). "
              "Use the update.py script over SSH instead.", "error")
        return redirect(url_for("admin_about"))
    if config.IS_GIT_CHECKOUT:
        flash("This is a git checkout, not an installed release - updating would overwrite "
              "tracked files. Use `git pull` instead.", "error")
        return redirect(url_for("admin_about"))
    blocked = _require_totp("Incorrect or missing 2FA code - update cancelled.",
                            "admin_about")
    if blocked:
        return blocked

    lines = []

    def progress(message):
        lines.append(message)
        _logger.info("[update] %s", message)

    try:
        result = updater.perform_update(progress=progress)
    except updater.UpdateError as e:
        _logger.error("In-app update failed: %s", e)
        flash(f"Update failed: {e}", "error")
        return redirect(url_for("admin_about"))
    except Exception as e:
        _logger.exception("In-app update crashed")
        flash(f"Update failed unexpectedly: {e} (see instance/logs/app.log)", "error")
        return redirect(url_for("admin_about"))

    if not result["applied"]:
        flash(f"Nothing to update - {result['reason']}.", "success")
        return redirect(url_for("admin_about"))

    # Written before the restart so the next successful start can confirm it came up
    # on the new version - and so `python update.py rollback` knows which backup to
    # use if it doesn't. See updater.write_pending_marker() for what this can and
    # cannot detect.
    updater.write_pending_marker(result["backup"], result["latest"])
    flash(f"Updated {result['current']} → {result['latest']}. Restarting now - this page will be "
          f"briefly unreachable. If it doesn't come back, run "
          f"`python update.py rollback` on the server.", "success")
    portal._restart_process()
    return redirect(url_for("admin_about"))


@app.route("/admin/about/restore-db", methods=["POST"])
@login_required
def admin_restore_db():
    """Replaces the live database with an uploaded backup, then restarts.

    Behind _require_totp() like the other actions where a stolen session cookie alone
    must not be enough (host restart/shutdown, app restart, self-update) - this one
    replaces every piece of data the portal holds, so it belongs in that set rather than
    beside the harmless cache buttons.

    The order below is the safety machinery and is not rearrangeable:
      1. write the upload to a temp file - the live database is untouched;
      2. validate it is a well-formed SQLite database *and* one of ours;
      3. snapshot the current database, so a regretted restore is still recoverable;
      4. atomically replace, dealing with the WAL sidecars (see db.restore_from_file);
      5. restart, because every existing connection still points at the old file.
    """
    blocked = _require_totp("Enter your 2FA code to restore the database.", "admin_about")
    if blocked:
        return blocked

    upload = request.files.get("backup")
    if not upload or not upload.filename:
        flash("Choose a backup file to restore.", "error")
        return redirect(url_for("admin_about"))
    # Carried over into the restored database below. The backup holds whatever epoch
    # it was taken with: keeping that would sign out the admin doing the restore, and
    # restoring an older backup would bring back cookies revoked since it was taken.
    session_epoch = _admin_session_epoch()

    # Staged next to the database rather than in the system temp directory, so the final
    # os.replace() is a rename within one filesystem (atomic) rather than a cross-device
    # copy - and so a large upload can't fill a small /tmp.
    os.makedirs(os.path.dirname(db.DB_PATH), exist_ok=True)
    fd, staged = tempfile.mkstemp(prefix="restore-", suffix=".db",
                                   dir=os.path.dirname(db.DB_PATH))
    os.close(fd)
    snapshot = None
    installed = None
    try:
        error = _write_uploaded_database(upload, staged)
        if error is None:
            error = db.validate_backup_file(staged)
        if error:
            flash(f"Restore refused: {error} Your database has not been touched.", "error")
            return redirect(url_for("admin_about"))

        try:
            snapshot = _db_safety_snapshot()
        except Exception as e:
            _logger.exception("Could not snapshot the database before restoring")
            flash(f"Restore aborted: couldn't back up your current database first ({e}). "
                  "Nothing has been changed.", "error")
            return redirect(url_for("admin_about"))

        try:
            db.restore_from_file(staged)
        except Exception as e:
            _logger.exception("Database restore failed")
            flash(f"Restore failed: {e}. Your previous database was saved to "
                  f"{snapshot} before the attempt.", "error")
            return redirect(url_for("admin_about"))
        # The staged file has been renamed onto the live path, so it must not be
        # deleted below - but its sidecars still need clearing, hence the separate flag
        # rather than just dropping the name.
        installed = staged
        staged = None
        _prune_db_safety_backups()
    finally:
        if staged and os.path.exists(staged):
            try:
                os.remove(staged)
            except OSError:
                pass
        # Validating the upload opens it with SQLite, and opening a WAL-mode database -
        # which every backup of this app is - creates -wal/-shm sidecars beside it. The
        # rename above moves only the main file, so without this every restore left two
        # orphaned files in instance/ forever. Found by looking at the directory after a
        # release re-test, not by any test.
        _remove_sqlite_sidecars(installed or staged)

    try:
        db.set_setting(ADMIN_SESSION_EPOCH_SETTING, session_epoch)
    except Exception:
        # The restore itself has succeeded; the cost of this failing is at most
        # signing in again, so it must not turn a good restore into an error.
        _logger.exception("Could not carry the admin session epoch into the restored database")
    _logger.warning("Database restored from an uploaded backup; previous database saved to %s",
                     os.path.basename(snapshot))
    flash(f"Database restored. Your previous database was saved as "
          f"{os.path.basename(snapshot)} in instance/db_backups/. Restarting now - this "
          f"page will be briefly unreachable.", "success")
    portal._restart_process()
    return redirect(url_for("admin_about"))


# ---- Scheduled tasks (see scheduler.py - the framework; the tasks themselves are
# registered by whichever module owns them) ----
@app.route("/admin/tasks")
@login_required
def admin_tasks():
    return render_template("admin_tasks.html", tasks=scheduler.list_task_views(),
                            loops=scheduler.list_loop_views(),
                            last_health_cycle_at=db.get_setting(health.LAST_HEALTH_CYCLE_SETTING),
                            heartbeat_enabled=bool(config.HEARTBEAT_URL),
                            tick_seconds=config.SCHEDULER_TICK_SECONDS, active="tasks")


@app.route("/admin/tasks/<name>/save", methods=["POST"])
@login_required
def admin_task_save(name):
    if scheduler.get_task(name) is None:
        flash("No such scheduled task.", "error")
        return redirect(url_for("admin_tasks"))
    kind = request.form.get("schedule_kind", "interval")
    interval = request.form.get("interval_minutes", type=int) or 60
    daily_at = request.form.get("daily_at", "03:00").strip() or "03:00"
    scheduler.save_schedule(name, enabled=bool(request.form.get("enabled")),
                             schedule_kind=kind, interval_minutes=interval, daily_at=daily_at)
    flash("Schedule saved.", "success")
    return redirect(url_for("admin_tasks"))


@app.route("/admin/tasks/<name>/run", methods=["POST"])
@login_required
def admin_task_run(name):
    """Runs the task synchronously, inside the request. That is the same sanctioned
    exception to the no-slow-I/O-in-a-request-handler rule as the integrations "Check
    now" button and perform_update(): an explicit one-shot action the admin knowingly
    triggered, where the whole point is getting the result back in the response.
    Moving it to a background thread would just mean staring at a page that says
    nothing happened yet."""
    if scheduler.get_task(name) is None:
        flash("No such scheduled task.", "error")
        return redirect(url_for("admin_tasks"))
    status, message = scheduler.run_task(name, trigger="manual")
    category = {"success": "success", "skipped": "error", "busy": "error"}.get(status, "error")
    flash(f"{scheduler.get_task(name).label}: {status}{' - ' + message if message else ''}", category)
    return redirect(url_for("admin_tasks"))
