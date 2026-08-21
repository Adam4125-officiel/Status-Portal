"""
config.py — All configuration in one place, read from environment variables
(or a local .env file via python-dotenv, for people who'd rather edit a text
file than set real env vars). Nothing else in this app should read
os.environ directly - add new settings here instead.
"""
import os
import secrets

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------
# Releases are built with `git archive`, which strips .git entirely - so a shipped
# zip has no git metadata to derive a version from, and `git describe` is not an
# option at runtime. The version therefore has to be a plain tracked file that
# travels inside the archive. Bumping VERSION is a required step of cutting a
# release (see CLAUDE.md -> Release process); nothing derives it automatically.
#
# A VERSION file is used rather than a literal in this module so that updater.py
# can read the *incoming* release's version straight out of an extracted zip
# without importing the new (not yet installed, possibly incompatible) code.
APP_ROOT = os.path.dirname(os.path.abspath(__file__))


def _read_version():
    try:
        with open(os.path.join(APP_ROOT, "VERSION"), "r", encoding="utf-8") as f:
            return f.read().strip() or "0.0.0"
    except OSError:
        # A missing VERSION file means someone deleted a tracked file or is running
        # from a broken extraction - degrade to a sentinel that sorts below every
        # real release rather than crashing the whole app on import.
        return "0.0.0"


VERSION = _read_version()

# True when running from a git working tree rather than an extracted release zip
# (a release zip never contains .git). Two things depend on this: the About page
# labels the version as a dev build, and the updater refuses to overwrite a working
# tree by default - an update over a checkout would silently clobber uncommitted
# work and leave git reporting a tree full of unexplained modifications.
# One os.path.isdir() at import, no git subprocess.
IS_GIT_CHECKOUT = os.path.isdir(os.path.join(APP_ROOT, ".git"))

# What gets displayed. "1.5.0+dev" reads as "the 1.5.0 tree, plus whatever is
# uncommitted/unreleased on top" - deliberately not a version anything compares
# against (VERSION is what update checks use), just an honest label.
VERSION_DISPLAY = VERSION + ("+dev" if IS_GIT_CHECKOUT else "")

# ---------------------------------------------------------------------------
# Session signing key
# ---------------------------------------------------------------------------
# Flask signs the session cookie with this. If it changes, every existing session
# cookie stops validating and every logged-in admin is silently signed out on their
# next request - which is exactly what used to happen on every restart, because the
# fallback here was a fresh os.urandom() per process. Restarts are routine on this
# app (the in-app updater re-execs itself, /admin/system has a restart button, and a
# systemd/Task Scheduler unit restarts on failure), so "a random key per process"
# meant "logged out at random, mid-session".
#
# PORTAL_SECRET_KEY still wins when set. Without it, a key is generated once and
# persisted to instance/secret_key (0600) so it survives restarts on its own -
# instance/ is gitignored and is already the docker-compose volume mount, so this
# also survives a container recreate. If the file can't be written (read-only fs),
# it degrades to a process-lifetime key: same behavior as before, no crash.
SECRET_KEY_FILE = os.path.join(APP_ROOT, "instance", "secret_key")


def _load_or_create_secret_key():
    env_key = os.environ.get("PORTAL_SECRET_KEY", "").strip()
    if env_key:
        return env_key
    try:
        with open(SECRET_KEY_FILE, "r", encoding="utf-8") as f:
            stored = f.read().strip()
        if stored:
            return stored
    except OSError:
        pass
    key = secrets.token_hex(32)
    try:
        os.makedirs(os.path.dirname(SECRET_KEY_FILE), exist_ok=True)
        # 0600 via os.open's mode rather than a chmod after the fact - the key must
        # never exist world-readable, not even for the instant between the two calls.
        fd = os.open(SECRET_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(key)
    except OSError:
        # Nothing is logged here: config.py is imported before logging_setup runs.
        # A per-process key still works, it just logs everyone out on restart.
        pass
    return key


SECRET_KEY = _load_or_create_secret_key()

# How long an idle admin session survives before it's expired server-side, in hours.
# This is the *upper bound* the DB setting (admin_session_timeout_hours, editable at
# /admin/settings) is clamped to, and also the session cookie's own Max-Age - the
# cookie lifetime is deliberately fixed at import rather than tracking the DB
# setting, because Flask reads app.permanent_session_lifetime at cookie-set time and
# mutating that per-request would be a cross-thread global write under waitress.
# The server-side idle check in app.py is the authoritative one; this is the backstop.
SESSION_COOKIE_MAX_AGE_DAYS = 30
PORT = int(os.environ.get("PORTAL_PORT", "5000"))

# Backend health-check frequency (server-side polling of each service's check_url).
CHECK_INTERVAL_SECONDS = int(os.environ.get("PORTAL_CHECK_INTERVAL_SECONDS", "120"))

# Public page auto-refresh frequency (browser-side full reload). Independent of the above.
PUBLIC_REFRESH_SECONDS = int(os.environ.get("PORTAL_PUBLIC_REFRESH_SECONDS", "60"))

# How often the resources page (admin, and public if enabled) auto-refreshes itself.
RESOURCE_REFRESH_SECONDS = int(os.environ.get("PORTAL_RESOURCE_REFRESH_SECONDS", "10"))

# How many request-handling threads waitress (the production server, see
# serve_waitress.py) runs. Waitress's own default is 4, which is low for a page that
# every open browser tab reloads on a timer: four simultaneously slow requests and
# the portal stops answering anything at all until one finishes. Static deployment
# config, so an env var - and changing it needs a restart by definition, since it's
# passed to serve() at startup.
WAITRESS_THREADS = int(os.environ.get("PORTAL_WAITRESS_THREADS", "12"))

# Set to true only if a reverse proxy (nginx, Caddy, Cloudflare Tunnel...) sits in front
# of this app - enables trusting its X-Forwarded-* headers (client IP/scheme). Leave
# false if the app's port is reachable directly, to avoid trusting spoofable headers.
BEHIND_PROXY = os.environ.get("PORTAL_BEHIND_PROXY", "false").lower() == "true"

# Set to true once this is served over HTTPS (directly or via a TLS-terminating
# reverse proxy) to mark the session cookie Secure - browsers then refuse to send it
# over plain HTTP. Leave false for plain-HTTP LAN/Tailscale-only setups.
FORCE_HTTPS_COOKIES = os.environ.get("PORTAL_FORCE_HTTPS_COOKIES", "false").lower() == "true"

# Optional outbound notifications (see notifications.py) - both blank by default
# (disabled). Set either or both to get pinged on incident open/resolve/update and
# maintenance window start/end, instead of only finding out by looking at the page.
DISCORD_WEBHOOK_URL = os.environ.get("PORTAL_DISCORD_WEBHOOK_URL", "").strip()
NTFY_URL = os.environ.get("PORTAL_NTFY_URL", "").strip()

# ---------------------------------------------------------------------------
# Email notifications (the third channel - see notifications.py)
# ---------------------------------------------------------------------------
# Meaningfully more setup surface than the two URL-only channels above, which is the
# tradeoff: it's worth it for anyone using neither Discord nor ntfy, and it's the only
# channel that can reach a person who hasn't installed anything. Email is considered
# configured only when host, from-address and at least one recipient are all present -
# a half-filled block is treated as "not set up" rather than failing at send time.
#
# No new dependency: Python's standard smtplib and email packages do all of this.
SMTP_HOST = os.environ.get("PORTAL_SMTP_HOST", "").strip()
SMTP_PORT = int(os.environ.get("PORTAL_SMTP_PORT", "587"))
SMTP_USERNAME = os.environ.get("PORTAL_SMTP_USERNAME", "").strip()
# Deliberately not .strip()ed: a password is whatever the provider issued, and
# trimming it would silently break a legitimate one that ends in a space.
SMTP_PASSWORD = os.environ.get("PORTAL_SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("PORTAL_SMTP_FROM", "").strip()
# Comma-separated; where incident/maintenance alerts go. Per-user notifications (a
# separate feature) address their own recipients and don't read this.
SMTP_TO = os.environ.get("PORTAL_SMTP_TO", "").strip()
# starttls (587, the usual), ssl (465, implicit TLS), or none (25, unencrypted - only
# sane for an SMTP server on the same machine or LAN).
SMTP_SECURITY = os.environ.get("PORTAL_SMTP_SECURITY", "starttls").strip().lower()
# Its own timeout rather than the 5s the HTTP channels share: an SMTP conversation is
# several round trips plus a TLS handshake, and a relay that queues rather than
# delivering immediately can still take a few seconds to accept. Same reasoning as
# BYPARR_TIMEOUT_SECONDS and JELLYFIN_AUTH_TIMEOUT_SECONDS.
SMTP_TIMEOUT_SECONDS = int(os.environ.get("PORTAL_SMTP_TIMEOUT_SECONDS", "10"))

# Optional Discord bot (separate feature from the webhook above - see discord_bot.py).
# Blank = disabled entirely, no bot thread started. A bot token is a full login
# credential (not just a one-way webhook URL), so like the webhook URLs it's env-only,
# never a DB/admin-UI field. What the bot actually does (respond to a command,
# update its presence, what to include) is admin-configurable at /admin/discord-bot.
DISCORD_BOT_TOKEN = os.environ.get("PORTAL_DISCORD_BOT_TOKEN", "").strip()
DISCORD_BOT_REFRESH_SECONDS = int(os.environ.get("PORTAL_DISCORD_BOT_REFRESH_SECONDS", "300"))
# Optional - if this bot only lives in one server, setting its ID here registers the
# slash command instantly (guild-scoped sync). Left blank, the command syncs
# globally instead, which works everywhere but can take up to an hour to first appear.
DISCORD_BOT_GUILD_ID = os.environ.get("PORTAL_DISCORD_BOT_GUILD_ID", "").strip()

# Byparr's own /health check doesn't just ping the process - it makes Byparr
# actually navigate to a real page and solve a live Cloudflare challenge before
# responding, which routinely takes far longer than the 5s timeout every other
# integration fetcher uses for a plain REST call. A check interval/timeout, so
# per this project's config split it's an env var like the ones below, not a DB
# setting.
BYPARR_TIMEOUT_SECONDS = int(os.environ.get("PORTAL_BYPARR_TIMEOUT_SECONDS", "30"))

# ---------------------------------------------------------------------------
# Scheduled tasks (see scheduler.py) and Jellyfin-backed user accounts
# ---------------------------------------------------------------------------
# How often the scheduler wakes up to look for due tasks. This is the *granularity*
# of every schedule, not a schedule itself: a task set to "every 5 minutes" can only
# be as punctual as this value allows. A check interval, so per this project's config
# split it's an env var - what each task's own schedule is, and whether it's enabled
# at all, are routine admin toggles and live in the database instead.
SCHEDULER_TICK_SECONDS = int(os.environ.get("PORTAL_SCHEDULER_TICK_SECONDS", "30"))

# HTTP timeout for the two Jellyfin calls that back user accounts (fetching the user
# list, and validating a username/password at sign-in). Deliberately its own value
# rather than the 5s integrations.TIMEOUT every read-only health check shares - same
# reasoning as BYPARR_TIMEOUT_SECONDS above, applied to a different pressure: a
# person is sitting there waiting for this one, and a Jellyfin busy transcoding can
# take noticeably longer to answer an authentication request than it does to answer
# /System/Info. Too low and a valid password looks like an outage.
JELLYFIN_AUTH_TIMEOUT_SECONDS = int(os.environ.get("PORTAL_JELLYFIN_AUTH_TIMEOUT_SECONDS", "10"))

# ---------------------------------------------------------------------------
# Self-update (see updater.py / update.py)
# ---------------------------------------------------------------------------
# How often the portal re-asks GitHub what the latest release is. The check is a
# scheduled task now (see updater.py's registration), so this is the *default* for
# that task's schedule rather than a hard interval: per-task scheduling is a DB row an
# admin can change from /admin/tasks, and this decides what it starts as. The
# *channel* (stable/unstable) is a routine toggle and likewise lives in the database.
# 6h by default: GitHub's unauthenticated API allows 60 requests/hour per IP and
# nothing here benefits from knowing about a new release sooner than that.
UPDATE_CHECK_INTERVAL_SECONDS = int(os.environ.get("PORTAL_UPDATE_CHECK_INTERVAL_SECONDS", "21600"))

# Kill-switch for the in-app "Update now" button (the standalone update.py script is
# unaffected and always works). Deliberately an env var rather than a DB setting:
# the risk it mitigates is "someone got into the admin panel", and a toggle that same
# attacker can flip from that same admin panel mitigates nothing. Changing this needs
# filesystem access to the host plus a restart - the same, stronger trust boundary
# that twofactor.py's RESET_2FA flag file relies on. Defaults to enabled; set it to
# false to require SSH access for every update.
ENABLE_INAPP_UPDATE = os.environ.get("PORTAL_ENABLE_INAPP_UPDATE", "true").lower() != "false"
