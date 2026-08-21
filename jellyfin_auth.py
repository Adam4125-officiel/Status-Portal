"""
jellyfin_auth.py — Jellyfin as an identity source: the cached user list, the
scheduled job that refreshes it, and the credential check behind the public
sign-in form.

Why this is its own module rather than more of integrations.py
--------------------------------------------------------------
`integrations.py` is deliberately read-only ("Nothing here writes to or controls
those services") and never handles a credential beyond the admin's own API key.
Everything in *this* file touches identity: it reads the user list, and it posts
somebody's password to Jellyfin. Keeping that in one small, separately auditable
file is worth more than the tidiness of having every Jellyfin call in one place.
Both still share the same configuration - the Jellyfin server URL and API key come
from an ordinary Jellyfin integration row, so there is exactly one place in the
admin panel where "where is your Jellyfin" is answered.

What "fall back to the cached list" can and cannot mean
------------------------------------------------------
It cannot mean offline password checking. Jellyfin does not expose password hashes
over its API and must not, so there is no material here to verify a password
against. Handing a session to anyone who types a username that appears in the cache
would not be degraded authentication, it would be none. So:

- **Signing in always requires a live answer from Jellyfin.** If Jellyfin cannot be
  reached, a *new* sign-in is refused, with a message that says so rather than
  implying the password was wrong.
- **The cache keeps existing sessions alive.** Requests are re-validated against the
  cached list, never against Jellyfin, so an outage never signs anybody out. That is
  the "reduced functionality" mode: nobody new gets in, everybody already in stays
  in.
- **An empty cache means "no information", not "no users".** A session is only ever
  revoked when the cache is populated *and* the user is absent, disabled in Jellyfin,
  or blocked here by the admin (a separate, admin-owned flag - see
  portal_access_allowed).
  A failed sync leaves the previous list completely intact (see
  db.replace_jellyfin_users), so one unreachable poll can never lock out the world.

The access token is deliberately thrown away
--------------------------------------------
Authenticating returns a Jellyfin access token. It is never put in the session:
Flask's cookie is signed but *not* encrypted, so anything in it is readable by
whoever holds the cookie, and a Jellyfin token there would be a far worse thing to
leak than "you are signed in as adam". The token is used once - to immediately
revoke itself - so the portal also doesn't accumulate one dead device session per
sign-in in Jellyfin's own device list.
"""
import logging

import requests

import config
import db
import scheduler

_logger = logging.getLogger(__name__)

# Identifies this app to Jellyfin, shown in its Dashboard -> Devices list. A fixed
# device id rather than a per-login random one: Jellyfin treats each distinct
# DeviceId as a separate device, so a random one per sign-in would fill that list
# with hundreds of dead entries. (Each token is revoked immediately anyway - this is
# belt and braces.)
CLIENT_NAME = "Status Portal"
DEVICE_NAME = "Status Portal"
DEVICE_ID = "status-portal"

TASK_NAME = "jellyfin_user_sync"


def _auth_header():
    """Jellyfin's client-identification header. Sent as both `Authorization` and
    `X-Emby-Authorization`: modern Jellyfin reads the former, older versions (and
    Emby-derived builds) read the latter, and sending both costs nothing while
    removing an entire class of "works on my server" bug report."""
    value = (f'MediaBrowser Client="{CLIENT_NAME}", Device="{DEVICE_NAME}", '
             f'DeviceId="{DEVICE_ID}", Version="{config.VERSION}"')
    return {"Authorization": value, "X-Emby-Authorization": value}


# ---------------------------------------------------------------------------
# Configuration - which Jellyfin, and is this feature on
# ---------------------------------------------------------------------------
def auth_integration():
    """The Jellyfin integration user accounts are backed by, or None.

    Reuses an ordinary integration row rather than adding a second copy of the
    server URL and API key: two sources of truth for "where is your Jellyfin" is a
    bug factory, and the integration form is already the established place for it.

    The admin picks one explicitly (settings key jellyfin_auth_integration_id) when
    there are several; with nothing picked this falls back to the first enabled
    Jellyfin integration, which is the right answer for the overwhelmingly common
    case of having exactly one. A stored id pointing at a since-deleted or
    since-disabled integration resolves to None rather than raising - the feature
    then reports itself as not configured, which is what it now is."""
    raw = db.get_setting("jellyfin_auth_integration_id", "")
    candidates = [i for i in db.list_integrations() if i["kind"] == "jellyfin" and i["enabled"]]
    if raw.isdigit():
        chosen = next((i for i in candidates if i["id"] == int(raw)), None)
        if chosen:
            return chosen
        return None
    return candidates[0] if candidates else None


def is_enabled():
    """True when Jellyfin-backed sign-in is both switched on and actually usable.

    Off by default: this adds a public login form to a portal that previously had
    none, which has to be a deliberate choice rather than something that appears
    because an integration happens to exist."""
    return db.get_setting("jellyfin_auth_enabled", "0") == "1" and auth_integration() is not None


def status_summary():
    """Everything the admin pages need to explain the current state in one read -
    including *why* it's off, which is the part an admin actually needs when the
    sign-in form isn't appearing."""
    integration = auth_integration()
    return {
        "enabled": is_enabled(),
        "toggle_on": db.get_setting("jellyfin_auth_enabled", "0") == "1",
        "integration": integration,
        "integration_name": integration["name"] if integration else None,
        "cached_users": db.count_jellyfin_users(),
        "synced_at": db.jellyfin_users_synced_at(),
    }


# ---------------------------------------------------------------------------
# The user list, and the scheduled task that refreshes it
# ---------------------------------------------------------------------------
def fetch_users(base_url, api_key):
    """The current Jellyfin user list, normalised to this app's own shape. Raises on
    any failure - the caller (the scheduled task) turns that into a recorded failure
    with the message intact, which is more useful than a swallowed error."""
    r = requests.get(f"{base_url.rstrip('/')}/Users",
                      headers={"X-Emby-Token": api_key, **_auth_header()},
                      timeout=config.JELLYFIN_AUTH_TIMEOUT_SECONDS)
    r.raise_for_status()
    payload = r.json()
    if not isinstance(payload, list):
        raise ValueError("Unexpected response from /Users (not a list)")
    return [_normalise_user(u) for u in payload if u.get("Id")]


def _normalise_user(raw):
    policy = raw.get("Policy") or {}
    return {
        "id": raw["Id"],
        "name": raw.get("Name") or "",
        "is_administrator": bool(policy.get("IsAdministrator")),
        "is_disabled": bool(policy.get("IsDisabled")),
    }


def sync_users():
    """The body of the jellyfin_user_sync scheduled task.

    Refuses to touch the stored list unless the fetch fully succeeded: the cached
    list is what keeps already-signed-in users valid while Jellyfin is unreachable,
    so wiping it on a failed poll would turn a Jellyfin blip into "everyone is
    signed out". A failure here is raised, recorded against the task by the
    scheduler, and retried on the next scheduled run with the previous list still in
    place."""
    integration = auth_integration()
    if integration is None:
        raise scheduler.TaskSkipped(
            "No enabled Jellyfin integration to sync from - add one under Integrations.")
    users = fetch_users(integration["base_url"], integration["api_key"])
    if not users:
        # A Jellyfin with genuinely zero users is not a thing that happens; an empty
        # list is far more likely to be a proxy returning something odd with a 200.
        # Refusing it keeps the previous list, which is the safe direction.
        raise ValueError("Jellyfin returned an empty user list - keeping the previous one")
    db.replace_jellyfin_users(users)
    disabled = sum(1 for u in users if u["is_disabled"])
    suffix = f" ({disabled} disabled)" if disabled else ""
    return f"Synced {len(users)} Jellyfin user(s){suffix}."


scheduler.register(
    TASK_NAME,
    "Jellyfin user sync",
    "Fetches the Jellyfin user list and caches it locally, so people already signed "
    "in stay signed in while Jellyfin is unreachable. Sign-in itself always checks "
    "the password against Jellyfin live - this cache is never used to authenticate.",
    sync_users,
    default_interval_minutes=60,
)


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
def authenticate(username, password):
    """Checks a username/password against Jellyfin itself, live.

    Returns a dict with a "reason" that the caller turns into a message, because the
    difference between them matters to whoever is typing:

        {"ok": True, "user": {...}}
        {"ok": False, "reason": "not_configured"}
        {"ok": False, "reason": "invalid"}       - Jellyfin said no. Definitive.
        {"ok": False, "reason": "disabled"}      - correct password, disabled account
        {"ok": False, "reason": "unreachable", "error": "..."}

    "unreachable" must never be reported as "wrong password": telling someone their
    password is wrong when the server is simply down sends them off to reset a
    password that was fine.

    The password is used for exactly one outbound request and is never stored,
    logged or put in the session."""
    integration = auth_integration()
    if integration is None:
        return {"ok": False, "reason": "not_configured"}

    base_url = integration["base_url"].rstrip("/")
    try:
        r = requests.post(f"{base_url}/Users/AuthenticateByName",
                           json={"Username": username, "Pw": password},
                           headers={"Content-Type": "application/json", **_auth_header()},
                           timeout=config.JELLYFIN_AUTH_TIMEOUT_SECONDS)
    except requests.RequestException as e:
        _logger.warning("Jellyfin sign-in attempt could not reach the server: %s", e)
        return {"ok": False, "reason": "unreachable", "error": str(e)}

    if r.status_code in (400, 401, 403):
        # Jellyfin answered and rejected the credentials. Deliberately not logging
        # the username at warning level on every typo - the rate limiter in app.py
        # is what surfaces a real attack.
        return {"ok": False, "reason": "invalid"}
    if r.status_code >= 500 or not r.ok:
        _logger.warning("Jellyfin sign-in attempt got HTTP %s", r.status_code)
        return {"ok": False, "reason": "unreachable", "error": f"Jellyfin returned HTTP {r.status_code}"}

    try:
        payload = r.json()
        raw_user = payload.get("User") or {}
        token = payload.get("AccessToken")
    except ValueError:
        return {"ok": False, "reason": "unreachable", "error": "Unexpected (non-JSON) response"}

    if not raw_user.get("Id"):
        return {"ok": False, "reason": "unreachable", "error": "Jellyfin returned no user"}

    # Hand the token straight back rather than keeping it. See the module docstring:
    # a Jellyfin access token has no business living in a signed-but-readable cookie,
    # and leaving it valid would add a dead device entry per sign-in.
    _revoke_token(base_url, token)

    user = _normalise_user(raw_user)
    if user["is_disabled"]:
        return {"ok": False, "reason": "disabled"}
    if not portal_access_allowed(user["id"]):
        # Jellyfin was happy; this portal is not. Reported separately from "disabled"
        # so the admin reading the log (and the person reading the screen) can tell
        # "your Jellyfin account is off" apart from "your access here was revoked" -
        # they're fixed in completely different places.
        _logger.info("Refused sign-in for Jellyfin user '%s' - access to this portal is blocked",
                      user["name"])
        return {"ok": False, "reason": "not_allowed"}
    return {"ok": True, "user": user}


def _revoke_token(base_url, token):
    """Best-effort. A failure here means one stale device session in Jellyfin's list,
    which is untidy but harmless - it must never turn a successful sign-in into a
    failed one."""
    if not token:
        return
    try:
        requests.post(f"{base_url}/Sessions/Logout",
                       headers={"X-Emby-Token": token, **_auth_header()},
                       timeout=config.JELLYFIN_AUTH_TIMEOUT_SECONDS)
    except requests.RequestException as e:
        _logger.info("Could not revoke the short-lived Jellyfin token after sign-in: %s", e)


def portal_access_allowed(user_id):
    """Whether this portal lets the user in, independently of what Jellyfin thinks.

    Unknown users are allowed. A user can legitimately authenticate before ever
    appearing in the cache (they were created in Jellyfin since the last sync), and
    the default for this flag is "on" anyway - so absence of a row is absence of a
    decision, not a refusal."""
    row = db.get_jellyfin_user(user_id)
    return True if row is None else bool(row["portal_allowed"])


def session_user_still_valid(user_id):
    """Whether an already-signed-in user should keep their session, checked against
    the local cache only - never against Jellyfin, so an outage never signs anyone
    out.

    Returns True when the cache has never been populated: an empty cache is missing
    information, not evidence that the account is gone. Once the cache *is*
    populated, a user who has been removed or disabled in Jellyfin loses their
    session on their next request, which is what makes the sync task an actual
    revocation mechanism rather than just a list."""
    if db.jellyfin_users_synced_at() is None:
        return True
    row = db.get_jellyfin_user(user_id)
    if row is None:
        return False
    # Blocking a user takes effect on their very next request, not whenever their
    # session happens to expire - "disabled" that leaves someone signed in for
    # another week isn't disabled.
    return not row["is_disabled"] and bool(row["portal_allowed"])
