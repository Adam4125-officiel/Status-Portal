"""
config.py — All configuration in one place, read from environment variables
(or a local .env file via python-dotenv, for people who'd rather edit a text
file than set real env vars). Nothing else in this app should read
os.environ directly - add new settings here instead.
"""
import os

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

SECRET_KEY = os.environ.get("PORTAL_SECRET_KEY", "change-me-in-prod-" + os.urandom(8).hex())
PORT = int(os.environ.get("PORTAL_PORT", "5000"))

# Backend health-check frequency (server-side polling of each service's check_url).
CHECK_INTERVAL_SECONDS = int(os.environ.get("PORTAL_CHECK_INTERVAL_SECONDS", "120"))

# Public page auto-refresh frequency (browser-side full reload). Independent of the above.
PUBLIC_REFRESH_SECONDS = int(os.environ.get("PORTAL_PUBLIC_REFRESH_SECONDS", "60"))

# How often the resources page (admin, and public if enabled) auto-refreshes itself.
RESOURCE_REFRESH_SECONDS = int(os.environ.get("PORTAL_RESOURCE_REFRESH_SECONDS", "10"))

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
# Self-update (see updater.py / update.py)
# ---------------------------------------------------------------------------
# How often the background thread re-asks GitHub what the latest release is. This
# is a check interval, so per this project's config split it's an env var, not a
# DB setting - unlike the *channel* (stable/unstable), which is a routine toggle an
# admin flips from the browser and therefore lives in the settings table.
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
