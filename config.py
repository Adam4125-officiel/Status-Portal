"""
config.py — All configuration in one place, read from environment variables
(or a local .env file via python-dotenv, for people who'd rather edit a text
file than set real env vars). Nothing else in this app should read
os.environ directly - add new settings here instead.
"""
import os

from dotenv import load_dotenv

load_dotenv()

SECRET_KEY = os.environ.get("PORTAL_SECRET_KEY", "change-me-in-prod-" + os.urandom(8).hex())
PORT = int(os.environ.get("PORTAL_PORT", "5000"))

# Backend health-check frequency (server-side polling of each service's check_url).
CHECK_INTERVAL_SECONDS = int(os.environ.get("PORTAL_CHECK_INTERVAL_SECONDS", "120"))

# Public page auto-refresh frequency (browser-side full reload). Independent of the above.
PUBLIC_REFRESH_SECONDS = int(os.environ.get("PORTAL_PUBLIC_REFRESH_SECONDS", "60"))

# Disk path the admin resource monitor reports free/used space for.
MONITOR_DISK_PATH = os.environ.get("PORTAL_MONITOR_DISK_PATH", "/")

# Set to true only if a reverse proxy (nginx, Caddy, Cloudflare Tunnel...) sits in front
# of this app - enables trusting its X-Forwarded-* headers (client IP/scheme). Leave
# false if the app's port is reachable directly, to avoid trusting spoofable headers.
BEHIND_PROXY = os.environ.get("PORTAL_BEHIND_PROXY", "false").lower() == "true"

# Set to true once this is served over HTTPS (directly or via a TLS-terminating
# reverse proxy) to mark the session cookie Secure - browsers then refuse to send it
# over plain HTTP. Leave false for plain-HTTP LAN/Tailscale-only setups.
FORCE_HTTPS_COOKIES = os.environ.get("PORTAL_FORCE_HTTPS_COOKIES", "false").lower() == "true"
