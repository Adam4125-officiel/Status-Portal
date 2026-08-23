"""
integrations.py — Read-only status/log checks against other self-hosted apps
(Jellyfin, Jellyseerr/Overseerr, Servarr-family apps: Sonarr, Radarr,
Prowlarr, Lidarr, Readarr, Bazarr, and the media-processing/anti-bot tools
Tdarr and Byparr). Nothing here writes to or controls those services - it
only reads health/logs to surface warnings in the admin section. Every
fetcher returns the same shape so the template doesn't need to care which
kind of service it's showing:

    {"reachable": bool, "version": str|None, "issues": [{"level", "message"}], "error": str|None}
"""
import logging
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode

import requests

import config
import db
import monitoring
import scheduler

_logger = logging.getLogger(__name__)

TIMEOUT = 5


def fetch_integration_status(integration):
    fetchers = {
        "arr": fetch_arr_status,
        "jellyfin": fetch_jellyfin_status,
        "jellyseerr": fetch_jellyseerr_status,
        "bazarr": fetch_bazarr_status,
        "tdarr": fetch_tdarr_status,
        "byparr": fetch_byparr_status,
        "qbittorrent": fetch_qbittorrent_status,
    }
    fn = fetchers.get(integration["kind"])
    if not fn:
        return {"reachable": False, "version": None, "issues": [],
                "error": f"Unknown integration kind: {integration['kind']}"}
    if integration["kind"] == "qbittorrent":
        # The one kind that authenticates with a username/password login rather than an
        # API key, so it takes different arguments. Handled here rather than by giving
        # every fetcher a uniform six-parameter signature it doesn't need.
        return fn(integration["base_url"], integration["username"], integration["password"])
    return fn(integration["base_url"], integration["api_key"])


def fetch_arr_status(base_url, api_key):
    """Sonarr/Radarr/Prowlarr/Readarr use API v3; Lidarr is still on v1 as of this
    writing - v3 is tried first and this falls back to v1 on a 404 rather than making
    the admin guess a version number per app."""
    base_url = base_url.rstrip("/")
    headers = {"X-Api-Key": api_key}
    last_error = "Unreachable"
    for version in ("v3", "v1"):
        try:
            r = requests.get(f"{base_url}/api/{version}/health", headers=headers, timeout=TIMEOUT)
            if r.status_code == 404:
                last_error = "API endpoint not found (unsupported API version?)"
                continue
            r.raise_for_status()
            issues = [
                {"level": "error" if (item.get("type") or "").lower() == "error" else "warning",
                 "message": item.get("message", "")}
                for item in r.json()
            ]
            return {"reachable": True, "version": None, "issues": issues, "error": None}
        except requests.RequestException as e:
            last_error = str(e)
        except ValueError:
            last_error = "Unexpected (non-JSON) response"
    return {"reachable": False, "version": None, "issues": [], "error": last_error}


def fetch_bazarr_status(base_url, api_key):
    """Bazarr, unlike the Servarr apps above, expects its API key as a query
    param (?apikey=...) rather than an X-Api-Key header - confirmed against
    Bazarr's own source (bazarr/api/system/status.py) and community docs, not
    against a real running instance (unverified, same caveat as every other
    integration here). The health endpoint's exact issue-list shape is a
    best-effort guess modeled on the Servarr apps' /health shape (a list of
    {"type", "text"/"message"} objects) - if a real instance's response
    differs, this degrades to an empty issues list rather than raising,
    since parsing errors are swallowed the same way as the other fetchers'
    bonus/secondary calls."""
    base_url = base_url.rstrip("/")
    params = {"apikey": api_key}
    try:
        status = requests.get(f"{base_url}/api/system/status", params=params, timeout=TIMEOUT)
        status.raise_for_status()
        version = (status.json().get("data") or {}).get("bazarr_version")
    except requests.RequestException as e:
        return {"reachable": False, "version": None, "issues": [], "error": str(e)}
    except ValueError:
        return {"reachable": False, "version": None, "issues": [], "error": "Unexpected (non-JSON) response"}

    issues = []
    try:
        health = requests.get(f"{base_url}/api/system/health", params=params, timeout=TIMEOUT)
        if health.ok:
            payload = health.json()
            items = payload.get("data", payload) if isinstance(payload, dict) else payload
            for item in items if isinstance(items, list) else []:
                level = "error" if (item.get("type") or "").lower() == "error" else "warning"
                issues.append({"level": level, "message": item.get("text") or item.get("issue") or item.get("message", "")})
    except (requests.RequestException, ValueError):
        pass  # the health check is a bonus - failing to fetch it shouldn't fail the whole check

    return {"reachable": True, "version": version, "issues": issues, "error": None}


def fetch_tdarr_status(base_url, api_key):
    """Tdarr has no built-in API key concept (api_key is accepted on the
    integration form for consistency with every other kind, but unused here -
    left for a reverse-proxy-enforced auth layer, if any, to handle
    transparently). /api/v2/status returns {"status", "isProduction", "os",
    "version", "uptime"} - confirmed shape via Tdarr's own docs, not against a
    real instance."""
    base_url = base_url.rstrip("/")
    try:
        r = requests.get(f"{base_url}/api/v2/status", timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        return {"reachable": False, "version": None, "issues": [], "error": str(e)}
    except ValueError:
        return {"reachable": False, "version": None, "issues": [], "error": "Unexpected (non-JSON) response"}

    issues = []
    status = (data.get("status") or "").lower()
    if status and status != "good":
        issues.append({"level": "warning", "message": f"Server status: {data.get('status')}"})
    return {"reachable": True, "version": data.get("version"), "issues": issues, "error": None}


def fetch_byparr_status(base_url, api_key):
    """Byparr (a FlareSolverr-compatible Cloudflare-challenge-solving proxy) has
    no API key of its own - GET /health makes it actually solve a real
    challenge against google.com internally and returns 200 on success, 500
    on failure, with no version field exposed anywhere. Confirmed against
    Byparr's own source (src/endpoints.py), not against a real instance.
    /health is the only health endpoint Byparr documents - there's no lighter
    alternative to switch to. It's genuinely just slow (real challenge-solving,
    not a simple ping), which is why this gets its own, much longer timeout
    (config.BYPARR_TIMEOUT_SECONDS) instead of the shared TIMEOUT every other
    fetcher uses - a real instance was confirmed timing out against the
    generic 5s value. See CLAUDE.md."""
    base_url = base_url.rstrip("/")
    try:
        r = requests.get(f"{base_url}/health", timeout=config.BYPARR_TIMEOUT_SECONDS)
    except requests.RequestException as e:
        return {"reachable": False, "version": None, "issues": [], "error": str(e)}
    if r.status_code >= 500:
        return {"reachable": False, "version": None, "issues": [], "error": "Health check failed (couldn't solve a test challenge)"}
    if not r.ok:
        return {"reachable": False, "version": None, "issues": [], "error": f"Unexpected status {r.status_code}"}
    return {"reachable": True, "version": None, "issues": [], "error": None}


def fetch_jellyfin_status(base_url, api_key):
    base_url = base_url.rstrip("/")
    headers = {"X-Emby-Token": api_key}
    try:
        info = requests.get(f"{base_url}/System/Info", headers=headers, timeout=TIMEOUT)
        info.raise_for_status()
        version = info.json().get("Version")
    except requests.RequestException as e:
        return {"reachable": False, "version": None, "issues": [], "error": str(e)}
    except ValueError:
        return {"reachable": False, "version": None, "issues": [], "error": "Unexpected (non-JSON) response"}

    issues = []
    try:
        log = requests.get(f"{base_url}/System/ActivityLog/Entries",
                            headers=headers, params={"limit": 20}, timeout=TIMEOUT)
        if log.ok:
            for entry in log.json().get("Items", []):
                severity = (entry.get("Severity") or "").lower()
                if severity in ("warn", "warning", "error", "critical"):
                    issues.append({
                        "level": "error" if severity in ("error", "critical") else "warning",
                        "message": entry.get("Name") or entry.get("ShortOverview") or "",
                    })
    except (requests.RequestException, ValueError):
        pass  # the activity log is a bonus - failing to fetch it shouldn't fail the whole check

    return {"reachable": True, "version": version, "issues": issues, "error": None}


def fetch_jellyseerr_status(base_url, api_key):
    base_url = base_url.rstrip("/")
    headers = {"X-Api-Key": api_key}
    try:
        status = requests.get(f"{base_url}/api/v1/status", headers=headers, timeout=TIMEOUT)
        status.raise_for_status()
        version = status.json().get("version")
    except requests.RequestException as e:
        return {"reachable": False, "version": None, "issues": [], "error": str(e)}
    except ValueError:
        return {"reachable": False, "version": None, "issues": [], "error": "Unexpected (non-JSON) response"}

    issues = []
    try:
        log = requests.get(f"{base_url}/api/v1/log", headers=headers,
                            params={"take": 20, "filter": "warn"}, timeout=TIMEOUT)
        if log.ok:
            for entry in log.json().get("results", []):
                level = (entry.get("level") or "warn").lower()
                issues.append({
                    "level": "error" if level == "error" else "warning",
                    "message": entry.get("message", ""),
                })
    except (requests.RequestException, ValueError):
        pass

    return {"reachable": True, "version": version, "issues": issues, "error": None}


# ---------------------------------------------------------------------------
# qBittorrent - the one integration that logs in rather than presenting a key
# ---------------------------------------------------------------------------
# qBittorrent's WebUI API has no API-key concept at all: you POST a username and
# password to /api/v2/auth/login and it hands back an SID cookie which every later
# request needs. That is why the integration form grows two fields rather than reusing
# the shared api_key one - and why these functions take (base_url, username, password)
# instead of (base_url, api_key).
#
# Each call logs in fresh and throws the session away. Caching the SID across calls
# would be a small optimisation and a real complication (expiry, invalidation on a
# password change, a stale cookie looking exactly like wrong credentials), for a
# request this app makes once every few minutes from a background task.
def _qbittorrent_session(base_url, username, password):
    """A requests.Session holding a valid SID cookie, or None if login failed.

    qBittorrent answers 403 to a *rate-limited* login as well as a wrong one, and its
    body is the plain text "Fails." for bad credentials, so both are reported the same
    way here - there is nothing in the response that reliably distinguishes them."""
    session = requests.Session()
    try:
        r = session.post(f"{base_url.rstrip('/')}/api/v2/auth/login",
                          data={"username": username, "password": password},
                          # Referer is required: qBittorrent rejects the login outright
                          # without one, as a CSRF defence for its own WebUI.
                          headers={"Referer": base_url.rstrip("/")},
                          timeout=TIMEOUT)
    except requests.RequestException:
        return None
    if not r.ok or "Ok." not in (r.text or ""):
        return None
    return session


def fetch_qbittorrent_status(base_url, username, password):
    """Health check, same shape as every other fetcher here.

    A local qBittorrent with "Bypass authentication for clients on localhost" turned on
    accepts requests with no login at all, so an empty username is not treated as
    misconfiguration - the version call below is what decides whether it worked."""
    base_url = base_url.rstrip("/")
    session = _qbittorrent_session(base_url, username, password) if username else requests.Session()
    if session is None:
        return {"reachable": False, "version": None, "issues": [],
                "error": "Login refused (wrong username/password, or too many recent attempts)"}
    try:
        r = session.get(f"{base_url}/api/v2/app/version", timeout=TIMEOUT)
        r.raise_for_status()
        version = (r.text or "").strip()
    except requests.RequestException as e:
        return {"reachable": False, "version": None, "issues": [], "error": str(e)}
    return {"reachable": True, "version": version or None, "issues": [], "error": None}


def fetch_qbittorrent_downloads(base_url, username, password, limit=20):
    """What is actively transferring right now, newest-progress-first.

    `filter=downloading` is qBittorrent's own definition, which excludes paused,
    queued, seeding and completed torrents - i.e. exactly "what's moving". progress
    arrives as 0..1 and is converted to a percentage here so no template has to know
    that."""
    base_url = base_url.rstrip("/")
    session = _qbittorrent_session(base_url, username, password) if username else requests.Session()
    if session is None:
        raise RuntimeError("qBittorrent login refused")
    r = session.get(f"{base_url}/api/v2/torrents/info",
                     params={"filter": "downloading"}, timeout=TIMEOUT)
    r.raise_for_status()
    torrents = r.json()
    if not isinstance(torrents, list):
        raise ValueError("Unexpected response from /api/v2/torrents/info")
    items = []
    for t in torrents[:limit]:
        items.append({
            "name": t.get("name") or "(unnamed)",
            "progress": round(float(t.get("progress") or 0) * 100, 1),
            "state": t.get("state") or "",
            "size_gb": round((t.get("size") or 0) / (1024 ** 3), 2),
            "dl_speed_mbs": round((t.get("dlspeed") or 0) / (1024 ** 2), 2),
            # qBittorrent uses 8640000 (100 days) to mean "unknown", which would render
            # as a nonsense countdown rather than as the unknown it actually is.
            "eta_seconds": (t.get("eta") if (t.get("eta") or 0) < 8640000 else None),
        })
    return sorted(items, key=lambda i: i["progress"], reverse=True)


# ---------------------------------------------------------------------------
# Search (Jellyfin + Seerr) - the one place an outbound call happens live
# ---------------------------------------------------------------------------
# Everything else in this module is polled into a cache by a background task, because a
# request handler must never wait on another server. Search genuinely cannot work that
# way: the query isn't known until somebody types it. So these two use their own, much
# shorter timeout (config.SEARCH_TIMEOUT_SECONDS) and the caller degrades to "search is
# unavailable right now" rather than holding a request thread open.
def search_jellyfin(base_url, api_key, query, jellyfin_user_id=None, limit=12):
    """What's already in the library, as far as this user is concerned.

    `userId` matters: Jellyfin filters results by what that account may actually see, so
    passing it means the portal can't reveal a library the person has no access to.
    Without one, Jellyfin answers as the API key's owner - which is why it's passed
    whenever it's known."""
    base_url = base_url.rstrip("/")
    params = {"searchTerm": query, "Recursive": "true", "Limit": limit,
              "IncludeItemTypes": "Movie,Series",
              "Fields": "ProductionYear", "EnableTotalRecordCount": "false"}
    if jellyfin_user_id:
        params["userId"] = jellyfin_user_id
    r = requests.get(f"{base_url}/Items", headers={"X-Emby-Token": api_key},
                      params=params, timeout=config.SEARCH_TIMEOUT_SECONDS)
    r.raise_for_status()
    payload = r.json()
    items = payload.get("Items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise ValueError("Unexpected response from /Items")
    return [{
        "source": "jellyfin",
        "title": item.get("Name") or "(untitled)",
        "year": item.get("ProductionYear"),
        "media_type": "tv" if item.get("Type") == "Series" else "movie",
        "jellyfin_id": item.get("Id"),
        "in_library": True,
    } for item in items if item.get("Id")]


def _http_error_detail(exc):
    """The body of a 4xx/5xx, trimmed, or "".

    Seerr answers a rejected request with JSON explaining what it objected to, and
    throwing that away was what made an HTTP 400 unactionable - "answered HTTP 400" is
    true and tells nobody what to change. requests' HTTPError carries the response, so
    the explanation is right there."""
    response = getattr(exc, "response", None)
    if response is None:
        return ""
    try:
        payload = response.json()
    except Exception:
        # Anything at all here - a non-JSON body, or a response object that doesn't
        # implement json() - must degrade to "no detail", never to a second exception
        # raised while explaining the first.
        return (getattr(response, "text", "") or "").strip()[:300]
    if isinstance(payload, dict):
        for key in ("message", "error", "detail", "errors"):
            if payload.get(key):
                return str(payload[key])[:300]
    return str(payload)[:300]


def describe_request_error(exc):
    """A short, human phrase for why an outbound call failed.

    Exists because "Seerr couldn't be reached" is the same sentence whether the server
    refused the connection, took too long, or answered with a 500 - and those have
    completely different fixes. The full exception still goes to the log; this is what a
    person is shown."""
    if isinstance(exc, requests.Timeout):
        return "timed out"
    if isinstance(exc, requests.HTTPError):
        status = exc.response.status_code if exc.response is not None else 0
        if status in (401, 403):
            return f"refused the API key (HTTP {status})"
        detail = _http_error_detail(exc)
        return f"answered HTTP {status}" + (f" - {detail}" if detail else "")
    if isinstance(exc, requests.ConnectionError):
        return "couldn't be connected to"
    if isinstance(exc, ValueError):
        return "answered with something that isn't JSON"
    return "failed"


def diagnose_seerr(base_url, api_key, query="test"):
    """Runs the health check and the search call back to back against the same Seerr,
    timing both, and reports exactly what each did.

    This exists because of a real report: the search page said Seerr couldn't be reached
    while the Integrations page showed it reachable with a version. Those two facts come
    from genuinely different calls, and guessing which difference mattered would have
    meant widening a timeout and hoping. The two candidate explanations this
    distinguishes:

    * **/api/v1/status is served locally; /api/v1/search proxies to TMDB.** If the Seerr
      host's outbound internet is slow or blocked, or TMDB is having a bad day, search
      fails while status answers instantly. Nothing about the portal's configuration is
      wrong in that case.
    * **The Integrations page reads a cache** refreshed on the health-check interval, so
      what it shows can be up to CHECK_INTERVAL_SECONDS old. "At the same time" isn't
      literally simultaneous, and Seerr may simply have gone away in between.

    Note the timeouts are *not* a candidate: search already gets the longer of the two
    (config.SEARCH_TIMEOUT_SECONDS vs the shared TIMEOUT)."""
    base_url = base_url.rstrip("/")
    headers = {"X-Api-Key": api_key}
    report = {"base_url": base_url, "query": query, "checks": []}

    # Each entry is (name, full url, timeout, note). The search URL is built exactly the
    # way search_seerr() builds it - percent-encoded - so this diagnoses the request the
    # portal actually makes rather than a lookalike.
    for name, url, timeout, note in (
            ("Health check (/api/v1/status)", f"{base_url}/api/v1/status", TIMEOUT,
             "Served entirely by Seerr itself - no internet access needed."),
            ("Search (/api/v1/search)", _seerr_search_url(base_url, query),
             config.SEARCH_TIMEOUT_SECONDS,
             "Seerr proxies this to TMDB, so it needs working outbound internet on the "
             "Seerr host - which the health check above does not."),
    ):
        entry = {"name": name, "timeout": timeout, "note": note,
                 "ok": False, "status_code": None, "elapsed_ms": None, "error": None,
                 "url": None, "body": None}
        started = time.monotonic()
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
            entry["status_code"] = r.status_code
            # The exact URL requests built, so a query that Seerr rejects can be seen
            # verbatim rather than guessed at.
            entry["url"] = r.url
            r.raise_for_status()
            r.json()
            entry["ok"] = True
        except requests.HTTPError as e:
            entry["error"] = describe_request_error(e)
            entry["body"] = _http_error_detail(e)
        except (requests.RequestException, ValueError) as e:
            entry["error"] = f"{describe_request_error(e)} - {e}"
        entry["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        report["checks"].append(entry)

    healthy, searchable = (c["ok"] for c in report["checks"])
    if healthy and not searchable:
        report["verdict"] = (
            "Seerr itself is fine, but its search isn't answering. Search is the only one "
            "of these two that Seerr has to reach TMDB for, so this points at the Seerr "
            "host's outbound internet (or TMDB), not at this portal's settings.")
    elif not healthy and not searchable:
        report["verdict"] = "Seerr isn't answering at all right now."
    elif searchable and not healthy:
        report["verdict"] = "Search works but the health endpoint doesn't - unusual; check the URL."
    else:
        report["verdict"] = ("Both calls succeeded just now. If the search page reported a "
                             "problem earlier, it was intermittent - the Integrations page "
                             "shows a cached result up to one check interval old, so the two "
                             "were never looking at the same moment.")
    return report


def _seerr_search_url(base_url, query, page=1):
    """The search URL, with the query **percent**-encoded.

    Seerr proxies search to TMDB, and TMDB rejects a `+` in the query with
    HTTP 400 "Parameter 'query' must be url encoded. Its value may not contain reserved
    characters." requests' default param encoding uses `+` for a space (the
    form-encoding convention), so every multi-word search failed while single words
    worked - which is exactly why the health check and a "test" query both passed while
    "Harry Potter" did not.

    urlencode(quote_via=quote) percent-encodes spaces as %20, and reserved characters
    (&, /, ') along with them. Confirmed against a real instance, not deduced."""
    return f"{base_url.rstrip('/')}/api/v1/search?" + urlencode(
        {"query": query, "page": page}, quote_via=quote)


def search_seerr(base_url, api_key, query, limit=12):
    """What exists at all, whether or not it's in the library.

    Seerr searches TMDB, so this is where "we don't have it, do you want it?" comes
    from. `mediaInfo.status == 5` means Seerr already considers it available, which is
    how a result gets recognised as in-library even when Jellyfin's own search didn't
    match the title."""
    # Pre-built rather than passed as params=, so the query is percent-encoded - see
    # _seerr_search_url() for why that is not a stylistic choice.
    r = requests.get(_seerr_search_url(base_url, query),
                      headers={"X-Api-Key": api_key},
                      timeout=config.SEARCH_TIMEOUT_SECONDS)
    r.raise_for_status()
    payload = r.json()
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        raise ValueError("Unexpected response from /api/v1/search")

    items = []
    for entry in results[:limit]:
        media_type = entry.get("mediaType")
        if media_type not in ("movie", "tv"):
            continue  # person results and anything else Seerr returns aren't requestable
        info = entry.get("mediaInfo") or {}
        date = entry.get("releaseDate") or entry.get("firstAirDate") or ""
        items.append({
            "source": "seerr",
            "title": entry.get("title") or entry.get("name") or "(untitled)",
            "year": int(date[:4]) if date[:4].isdigit() else None,
            "media_type": media_type,
            "tmdb_id": entry.get("id"),
            "overview": (entry.get("overview") or "")[:300],
            "poster_path": entry.get("posterPath") or "",
            "in_library": info.get("status") == 5,
            "requested": info.get("status") in (2, 3),
        })
    return items


def request_via_seerr(base_url, api_key, media_type, tmdb_id, seerr_user_id=None):
    """Asks Seerr for something, on behalf of a specific Seerr user where one is known.

    `userId` is what makes the request show up in Seerr's own approval queue attributed
    to the person who actually asked, rather than to whoever owns the API key. It is
    only ever passed when a *real* Jellyfin-to-Seerr link exists - see media_search.py
    for why that link is never guessed."""
    base_url = base_url.rstrip("/")
    payload = {"mediaType": media_type, "mediaId": int(tmdb_id)}
    if seerr_user_id:
        payload["userId"] = int(seerr_user_id)
    if media_type == "tv":
        # Seerr requires a season selection for series; "all" is the only sane default
        # for a portal that isn't going to render a season picker.
        payload["seasons"] = "all"
    r = requests.post(f"{base_url}/api/v1/request",
                       headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
                       json=payload, timeout=config.SEARCH_TIMEOUT_SECONDS)
    r.raise_for_status()
    return r.json() if r.content else {}


# ---------------------------------------------------------------------------
# Media activity: what's coming, what was asked for, what's downloading
# ---------------------------------------------------------------------------
# Four read-only views assembled into one cache, refreshed by the `media_refresh`
# scheduled task and read by the public section and the admin page. Same rule as
# everything else here: a request handler only ever reads the cache.
# How far ahead the "Coming soon" list looks, and the bounds an admin may set it to.
# A window rather than an open-ended pull: a Sonarr with a long-running series tracked
# would otherwise return everything it knows about, indefinitely.
DEFAULT_CALENDAR_DAYS = 14
MIN_CALENDAR_DAYS = 1
MAX_CALENDAR_DAYS = 90


def calendar_days():
    raw = db.get_setting("media_calendar_days", str(DEFAULT_CALENDAR_DAYS))
    value = int(raw) if raw.isdigit() else DEFAULT_CALENDAR_DAYS
    return max(MIN_CALENDAR_DAYS, min(MAX_CALENDAR_DAYS, value))


def fetch_arr_calendar(base_url, api_key, days_ahead=None, limit=20):
    """Upcoming releases from a Radarr or Sonarr calendar.

    One endpoint serves both and the response shapes differ, so the app is identified
    from the *item* rather than by asking /system/status first: a Sonarr entry carries a
    `series` object, a Radarr entry doesn't. That keeps this to a single request, and
    means it can't get the answer wrong the way guessing from the integration's name
    could."""
    base_url = base_url.rstrip("/")
    days_ahead = calendar_days() if days_ahead is None else days_ahead
    now = datetime.now(timezone.utc)
    r = requests.get(f"{base_url}/api/v3/calendar",
                      headers={"X-Api-Key": api_key},
                      params={"start": now.date().isoformat(),
                              "end": (now + timedelta(days=days_ahead)).date().isoformat(),
                              "unmonitored": "false"},
                      timeout=TIMEOUT)
    r.raise_for_status()
    entries = r.json()
    if not isinstance(entries, list):
        raise ValueError("Unexpected response from /api/v3/calendar")

    items = []
    for entry in entries:
        if entry.get("series"):
            series = entry["series"] or {}
            season, number = entry.get("seasonNumber"), entry.get("episodeNumber")
            items.append({
                "kind": "episode",
                "title": series.get("title") or "(unknown series)",
                "detail": (f"S{season:02d}E{number:02d}" if isinstance(season, int) and isinstance(number, int) else "")
                          + (f" — {entry['title']}" if entry.get("title") else ""),
                "date": entry.get("airDateUtc") or "",
                "have": bool(entry.get("hasFile")),
            })
        else:
            items.append({
                "kind": "movie",
                "title": entry.get("title") or "(unknown film)",
                "detail": str(entry.get("year") or ""),
                # Whichever release this actually is, soonest first. A film with only a
                # cinema date isn't "coming to the server", so that one is ignored.
                "date": entry.get("digitalRelease") or entry.get("physicalRelease") or "",
                "have": bool(entry.get("hasFile")),
            })
    items = [i for i in items if i["date"]]
    return sorted(items, key=lambda i: i["date"])[:limit]


# Overseerr/Jellyseerr status codes. Numbers in the API, words for humans.
SEERR_REQUEST_STATUS = {1: "Pending approval", 2: "Approved", 3: "Declined", 4: "Failed"}
SEERR_MEDIA_STATUS = {1: "Unknown", 2: "Pending", 3: "Processing",
                      4: "Partially available", 5: "Available"}

# Stable keys the template styles on, so colour never depends on matching the English
# label above. Every status in both maps needs an entry here, plus "unknown" for a code
# Seerr adds later that this app doesn't know yet.
SEERR_REQUEST_STATUS_KEY = {1: "pending", 2: "approved", 3: "declined", 4: "failed"}
SEERR_MEDIA_STATUS_KEY = {1: "unknown", 2: "pending", 3: "processing",
                          4: "partial", 5: "available"}


def fetch_seerr_requests(base_url, api_key, limit=20):
    """Recent requests and what became of them."""
    base_url = base_url.rstrip("/")
    r = requests.get(f"{base_url}/api/v1/request",
                      headers={"X-Api-Key": api_key},
                      params={"take": limit, "sort": "added", "filter": "all"},
                      timeout=TIMEOUT)
    r.raise_for_status()
    payload = r.json()
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        raise ValueError("Unexpected response from /api/v1/request")

    items = []
    for entry in results:
        media = entry.get("media") or {}
        requested_by = entry.get("requestedBy") or {}
        media_type = media.get("mediaType") or entry.get("type") or ""
        title = _seerr_title(media)
        year = None
        # The request payload embeds media by id and frequently carries no title, which
        # is why this list used to read "TMDB #438631". Ask Seerr what that id actually
        # is - the same question the "Coming soon" list gets answered for free by the
        # *Arr calendar, which returns real titles.
        if title.startswith("TMDB #"):
            detail = fetch_seerr_detail(base_url, api_key, media_type, media.get("tmdbId"))
            if detail and detail["title"]:
                title, year = detail["title"], detail["year"]
        items.append({
            "id": entry.get("id"),
            "title": title,
            "year": year,
            "media_type": media.get("mediaType") or entry.get("type") or "",
            "request_status": SEERR_REQUEST_STATUS.get(entry.get("status"), "Unknown"),
            "request_status_key": SEERR_REQUEST_STATUS_KEY.get(entry.get("status"), "unknown"),
            "media_status_label": SEERR_MEDIA_STATUS.get(media.get("status"), "Unknown"),
            "media_status_key": SEERR_MEDIA_STATUS_KEY.get(media.get("status"), "unknown"),
            "requested_by": requested_by.get("displayName") or requested_by.get("username") or "",
            # The Seerr user id, kept so a request can be traced back to the Jellyfin
            # account that made it - via Seerr's own jellyfinUserId link, never a guess.
            "requested_by_id": str(requested_by.get("id") or ""),
            "requested_at": entry.get("createdAt") or "",
            "pending": entry.get("status") == 1,
            "media_status": media.get("status"),
        })
    return items


# tmdb key -> {"title", "year", "poster_path"}. Overseerr's request payload is built
# around ids and often carries no title at all, so each one has to be looked up - and
# looked up once, not on every refresh, since a request's title never changes.
_seerr_detail_cache = {}


def fetch_seerr_detail(base_url, api_key, media_type, tmdb_id):
    """Title (and year) for one requested item, from Seerr's own detail endpoint.

    Cached forever in-process: this is immutable data keyed by a TMDB id. Without the
    cache, a queue of twenty requests would mean twenty extra HTTP calls every time the
    media task ran, for answers that cannot change."""
    if media_type not in ("movie", "tv") or not tmdb_id:
        return None
    key = f"{media_type}:{tmdb_id}"
    if key in _seerr_detail_cache:
        return _seerr_detail_cache[key]
    try:
        r = requests.get(f"{base_url.rstrip('/')}/api/v1/{media_type}/{tmdb_id}",
                          headers={"X-Api-Key": api_key}, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        # Not cached: a transient failure must not pin "unknown" in place forever.
        _logger.info("Could not resolve Seerr %s %s: %s", media_type, tmdb_id, e)
        return None
    date = data.get("releaseDate") or data.get("firstAirDate") or ""
    detail = {
        "title": data.get("title") or data.get("name") or "",
        "year": int(date[:4]) if date[:4].isdigit() else None,
        "poster_path": data.get("posterPath") or "",
    }
    if detail["title"]:
        _seerr_detail_cache[key] = detail
    return detail


def _seerr_title(media):
    """A displayable name for a requested item.

    Overseerr's request payload is built around ids, and which title fields come back
    with the embedded media object varies by version - so this tries the ones that have
    been observed, and falls back to naming the id rather than rendering a blank row.
    Unverified against a real instance; see docs/HISTORY.md."""
    for key in ("title", "name", "originalTitle", "originalName"):
        if media.get(key):
            return media[key]
    if media.get("tmdbId"):
        return f"TMDB #{media['tmdbId']}"
    return "(unknown title)"


def fetch_seerr_pending(base_url, api_key, limit=50):
    """Requests awaiting an approval decision, and how many there are in total.

    Uses filter=pending, which is Overseerr's own definition, rather than fetching
    everything and filtering here - the count has to be the real total, not the total
    within whatever page size this asked for. `pageInfo.results` is that total; the
    items are capped separately because they only exist to be named in an alert."""
    base_url = base_url.rstrip("/")
    r = requests.get(f"{base_url}/api/v1/request",
                      headers={"X-Api-Key": api_key},
                      params={"take": limit, "filter": "pending", "sort": "added"},
                      timeout=TIMEOUT)
    r.raise_for_status()
    payload = r.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise ValueError("Unexpected response from /api/v1/request")
    items = [{
        "id": entry.get("id"),
        "title": _seerr_title(entry.get("media") or {}),
        "media_type": (entry.get("media") or {}).get("mediaType") or entry.get("type") or "",
        "requested_by": ((entry.get("requestedBy") or {}).get("displayName")
                         or (entry.get("requestedBy") or {}).get("username") or ""),
        "requested_at": entry.get("createdAt") or "",
    } for entry in payload["results"]]
    total = (payload.get("pageInfo") or {}).get("results")
    return items, (total if isinstance(total, int) else len(items))


def fetch_seerr_notification_settings(base_url, api_key, seerr_user_id):
    """One user's notification settings, where Seerr actually keeps their Discord ID.

    Seerr splits a person across two structures: the `User` record (email, account
    info) and a per-user `UserSettings` sub-resource (notification preferences). The
    user list only carries the first, which is why email synced correctly and Discord
    ID never did - it was structurally invisible to a caller reading only the base
    record, rather than a flaky field.

    Note `discordIds` is a **list** in current Seerr, not a single `discordId` string.
    Verified against seerr-team/seerr's server/routes/user/usersettings.ts."""
    r = requests.get(f"{base_url.rstrip('/')}/api/v1/user/{seerr_user_id}/settings/notifications",
                      headers={"X-Api-Key": api_key}, timeout=TIMEOUT)
    r.raise_for_status()
    payload = r.json()
    return payload if isinstance(payload, dict) else {}


def first_discord_id(settings):
    """Seerr allows several Discord IDs per user; this portal sends to one. First
    non-empty entry, tolerating the older singular `discordId` spelling."""
    ids = settings.get("discordIds")
    if isinstance(ids, list):
        return next((str(i).strip() for i in ids if str(i).strip()), "")
    return str(settings.get("discordId") or "").strip()


def fetch_seerr_users(base_url, api_key, limit=200, with_notification_settings=False):
    """Every Seerr user, normalised - including `jellyfin_user_id`, which is the whole
    point of this call.

    Seerr can import Jellyfin accounts, and when it has, each Seerr user carries the
    Jellyfin id it was imported from. That is a *real* link and the only one this app
    will follow. Matching on email or username instead would eventually send one
    person's notifications to another, which is the failure mode worth refusing
    outright rather than mitigating.

    Field naming varies across Overseerr/Jellyseerr versions (jellyfinUserId is the
    documented one; some builds expose it as jellyfinId), so both are checked. The
    Discord id lives under the user's own notification settings."""
    base_url = base_url.rstrip("/")
    r = requests.get(f"{base_url}/api/v1/user",
                      headers={"X-Api-Key": api_key},
                      params={"take": limit}, timeout=TIMEOUT)
    r.raise_for_status()
    payload = r.json()
    results = payload.get("results") if isinstance(payload, dict) else payload
    if not isinstance(results, list):
        raise ValueError("Unexpected response from /api/v1/user")

    users = []
    for entry in results:
        # The base record's embedded settings, if this Seerr version exposes any. Kept
        # as a best-effort source; the sub-resource below is the authoritative one.
        embedded = entry.get("settings") or {}
        users.append({
            "id": str(entry.get("id")) if entry.get("id") is not None else "",
            "display_name": entry.get("displayName") or entry.get("username") or "",
            "email": entry.get("email") or "",
            "discord_id": first_discord_id(embedded),
            "jellyfin_user_id": str(entry.get("jellyfinUserId") or entry.get("jellyfinId") or ""),
        })
    users = [u for u in users if u["id"]]

    if with_notification_settings:
        # One extra request per *linked* user. That's an N+1, and it's the accepted
        # cost of the data living somewhere else: it runs hourly in a background task,
        # only for users who actually map to a Jellyfin account, and a failure on one
        # user leaves the rest (and their email) intact.
        for user in users:
            if not user["jellyfin_user_id"]:
                continue
            try:
                settings = fetch_seerr_notification_settings(base_url, api_key, user["id"])
            except (requests.RequestException, ValueError) as e:
                _logger.info("Could not read Seerr notification settings for user %s: %s",
                              user["id"], e)
                continue
            user["discord_id"] = first_discord_id(settings) or user["discord_id"]
    return users


def push_seerr_contact(base_url, api_key, seerr_user_id, email=None, discord_id=None):
    """Writes contact details back to a Seerr user's own settings.

    **The only call in this entire application that modifies another service.** So it is
    deliberately narrow: exactly one user, exactly the two contact fields, and only ever
    from an explicit button press with the change shown first. It must never be
    reachable from a sync or a background task.

    Three things about Seerr's API make this less obvious than it looks, all verified
    against seerr-team/seerr's server/routes/user/usersettings.ts:

    * **Email and Discord live in different places.** Email is on the user's general
      settings (`/settings/main`); Discord is on their notification settings
      (`/settings/notifications`). Posting an email to the notifications endpoint - as
      an earlier version of this did - is silently ignored.
    * **Discord is `discordIds`, a list**, not a `discordId` string.
    * **Both POSTs overwrite every field they read from the body.** Sending only the one
      field being changed therefore *erases* the rest - the user's PGP key, Telegram
      chat, Pushover tokens and quotas. So each write is read-modify-write: fetch the
      current settings, change the one value, send the whole object back.
    """
    base_url = base_url.rstrip("/")
    headers = {"X-Api-Key": api_key, "Content-Type": "application/json"}
    changed = False

    if email is not None:
        current = requests.get(f"{base_url}/api/v1/user/{seerr_user_id}/settings/main",
                                headers=headers, timeout=TIMEOUT)
        current.raise_for_status()
        body = current.json() if current.content else {}
        body = dict(body) if isinstance(body, dict) else {}
        body["email"] = email
        r = requests.post(f"{base_url}/api/v1/user/{seerr_user_id}/settings/main",
                           headers=headers, json=body, timeout=TIMEOUT)
        r.raise_for_status()
        changed = True

    if discord_id is not None:
        current = requests.get(
            f"{base_url}/api/v1/user/{seerr_user_id}/settings/notifications",
            headers=headers, timeout=TIMEOUT)
        current.raise_for_status()
        body = current.json() if current.content else {}
        body = dict(body) if isinstance(body, dict) else {}
        # Replaces the list rather than appending: this portal manages one ID, and
        # accumulating stale ones would mean messaging people who'd been removed.
        body["discordIds"] = [discord_id] if discord_id else []
        r = requests.post(
            f"{base_url}/api/v1/user/{seerr_user_id}/settings/notifications",
            headers=headers, json=body, timeout=TIMEOUT)
        r.raise_for_status()
        changed = True

    return changed


def fetch_prowlarr_indexers(base_url, api_key):
    """Per-indexer health, which is the failure that actually happens in practice:
    "Prowlarr is up" hides one or two indexers going stale or getting rate-limited
    while Prowlarr itself runs perfectly.

    Prowlarr's API is v1, not v3. /indexerstatus lists only indexers currently in
    trouble, so an indexer's absence from it is the good case."""
    base_url = base_url.rstrip("/")
    headers = {"X-Api-Key": api_key}
    r = requests.get(f"{base_url}/api/v1/indexer", headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    indexers = r.json()
    if not isinstance(indexers, list):
        raise ValueError("Unexpected response from /api/v1/indexer")

    failing = {}
    try:
        status = requests.get(f"{base_url}/api/v1/indexerstatus", headers=headers, timeout=TIMEOUT)
        if status.ok:
            for row in status.json() or []:
                failing[row.get("indexerId")] = row
    except (requests.RequestException, ValueError):
        # A bonus call, exactly like the *Arr health list: failing to fetch it must not
        # fail the whole indexer list, it just means no failure detail.
        pass

    items = []
    for indexer in indexers:
        trouble = failing.get(indexer.get("id"))
        items.append({
            "name": indexer.get("name") or "(unnamed)",
            "enabled": bool(indexer.get("enable", True)),
            "failing": trouble is not None,
            "disabled_till": (trouble or {}).get("disabledTill") or "",
            "last_error": (trouble or {}).get("mostRecentFailure") or "",
        })
    return sorted(items, key=lambda i: (not i["failing"], i["name"].lower()))


# What the media_refresh task publishes and the pages read. Empty lists are exactly
# right before the first refresh, and after one that failed.
_media_cache = {"calendar": [], "requests": [], "downloads": [], "indexers": [],
                "refreshed_at": None, "errors": {}}


def get_cached_media():
    """A shallow copy, so a template iterating one of these lists can't be tripped by
    the refresh task replacing it mid-render."""
    return {key: (list(value) if isinstance(value, list) else value)
            for key, value in _media_cache.items()}


SEERR_INTEGRATION_SETTING = "seerr_integration_id"


def seerr_integration():
    """The Jellyseerr/Overseerr integration everything Seerr-shaped should use.

    One function rather than the three near-identical "first enabled one" pickers that
    media_search, user_notify and seerr_alerts each had. Those could silently disagree
    the moment a second Seerr existed - and worse, so could the *diagnostic*, which
    tests whichever integration the admin clicked while search quietly used a different
    one. That is a very plausible reading of "the diagnostic says fine but searching
    returns HTTP 400".

    Same shape as jellyfin_auth.auth_integration(): an explicit choice wins, nothing
    chosen falls back to the first enabled one (right for the common case of having
    exactly one), and a stored id pointing at something deleted or disabled resolves to
    None rather than silently using some *other* Seerr."""
    raw = db.get_setting(SEERR_INTEGRATION_SETTING, "")
    candidates = [i for i in db.list_integrations()
                  if i["kind"] == "jellyseerr" and i["enabled"]]
    if raw.isdigit():
        return next((i for i in candidates if i["id"] == int(raw)), None)
    return candidates[0] if candidates else None


def _first_enabled(kind):
    return next((i for i in db.list_integrations()
                 if i["kind"] == kind and i["enabled"]), None)


def refresh_media_cache():
    """Body of the `media_refresh` scheduled task.

    Every source is independent: one unreachable app leaves the other three showing
    real data, with its own error recorded against it rather than blanking the section.
    Which is also why this returns a summary instead of raising on the first failure."""
    integrations_by_kind = {
        "arr": [i for i in db.list_integrations() if i["kind"] == "arr" and i["enabled"]],
        "jellyseerr": seerr_integration(),
        "qbittorrent": _first_enabled("qbittorrent"),
    }
    if not any(integrations_by_kind.values()):
        raise scheduler.TaskSkipped(
            "No Radarr/Sonarr, Jellyseerr or qBittorrent integration is enabled.")

    errors = {}
    calendar, indexers = [], []
    # Every *Arr integration is asked for both a calendar and an indexer list, because
    # which app it is isn't known here - and asking is one request cheaper than finding
    # out first. Each app answers one of them and 404s the other: Radarr and Sonarr have
    # a calendar and no indexers, Prowlarr the reverse.
    #
    # A 404 therefore means "this app doesn't have that", which is not a failure and
    # must not be recorded as one. Anything else is a real error and is reported, so a
    # genuinely broken Prowlarr doesn't quietly show an empty indexer list.
    for integration in integrations_by_kind["arr"]:
        for label, fetch, sink in (
                ("calendar", fetch_arr_calendar, calendar),
                ("indexers", fetch_prowlarr_indexers, indexers)):
            try:
                sink.extend(fetch(integration["base_url"], integration["api_key"]))
            except requests.HTTPError as e:
                if e.response is None or e.response.status_code != 404:
                    errors[f"{label}:{integration['name']}"] = str(e)
            except (requests.RequestException, ValueError) as e:
                errors[f"{label}:{integration['name']}"] = str(e)
    _media_cache["calendar"] = sorted(calendar, key=lambda i: i["date"])[:20]
    _media_cache["indexers"] = indexers

    seerr = integrations_by_kind["jellyseerr"]
    if seerr:
        try:
            _media_cache["requests"] = fetch_seerr_requests(seerr["base_url"], seerr["api_key"])
        except (requests.RequestException, ValueError) as e:
            errors["requests"] = str(e)
    else:
        _media_cache["requests"] = []

    qbit = integrations_by_kind["qbittorrent"]
    if qbit:
        try:
            _media_cache["downloads"] = fetch_qbittorrent_downloads(
                qbit["base_url"], qbit["username"], qbit["password"])
        except (requests.RequestException, ValueError, RuntimeError) as e:
            errors["downloads"] = str(e)
    else:
        _media_cache["downloads"] = []

    _media_cache["errors"] = errors
    _media_cache["refreshed_at"] = db.now_iso()
    summary = (f"{len(_media_cache['calendar'])} upcoming, "
               f"{len(_media_cache['requests'])} request(s), "
               f"{len(_media_cache['downloads'])} downloading, "
               f"{len(_media_cache['indexers'])} indexer(s)")
    if errors:
        return f"{summary}; {len(errors)} source(s) failed: " + "; ".join(sorted(errors))
    return summary


scheduler.register(
    "media_refresh",
    "Media activity refresh",
    "Reads the Radarr/Sonarr calendar, recent Jellyseerr requests, what qBittorrent is "
    "downloading and Prowlarr's per-indexer health into the cache the Media section "
    "reads. Read-only - it never changes anything on those apps.",
    refresh_media_cache,
    default_interval_minutes=5,
)


# ---------------------------------------------------------------------------
# High server load indicator
# ---------------------------------------------------------------------------
# Jellyfin-derived load signals (active transcodes, running scheduled tasks such as
# trickplay image extraction) - refreshed from app.py's existing background
# health-check loop, never fetched live from a request handler, same rule as every
# other outbound HTTP call in this app. Defaults represent "nothing configured/no
# data yet", which is exactly correct until the first refresh happens.
_jellyfin_activity_cache = {"transcoding": 0, "running_tasks": []}


def fetch_jellyfin_sessions(base_url, api_key):
    """Count of active playback sessions currently transcoding
    (PlayState.PlayMethod == "Transcode") - one signal used to detect high server
    load. Best-effort: any failure returns 0 rather than raising, same
    degrade-gracefully pattern as every fetcher above."""
    base_url = base_url.rstrip("/")
    headers = {"X-Emby-Token": api_key}
    try:
        r = requests.get(f"{base_url}/Sessions", headers=headers, timeout=TIMEOUT)
        r.raise_for_status()
        sessions = r.json()
    except (requests.RequestException, ValueError):
        return 0
    return sum(1 for s in sessions if (s.get("PlayState") or {}).get("PlayMethod") == "Transcode")


def fetch_jellyfin_running_tasks(base_url, api_key):
    """Names of currently-running Jellyfin scheduled tasks. Trickplay image
    extraction and library scans don't show up as playback sessions - a running
    scheduled task is the only way the Jellyfin API surfaces them. Best-effort:
    any failure returns []."""
    base_url = base_url.rstrip("/")
    headers = {"X-Emby-Token": api_key}
    try:
        r = requests.get(f"{base_url}/ScheduledTasks", headers=headers, timeout=TIMEOUT)
        r.raise_for_status()
        tasks = r.json()
    except (requests.RequestException, ValueError):
        return []
    return [t.get("Name", "task") for t in tasks if t.get("State") == "Running"]


def clear_caches():
    """Drops the Jellyfin activity cache (transcode count, running tasks). The
    background loop refills it on its next tick; until then the public page simply
    shows no Jellyfin activity, same as before the first refresh ever ran."""
    _jellyfin_activity_cache["transcoding"] = 0
    _jellyfin_activity_cache["running_tasks"] = []
    _media_cache["calendar"] = []
    _media_cache["requests"] = []
    _media_cache["downloads"] = []
    _media_cache["indexers"] = []
    _media_cache["refreshed_at"] = None
    _media_cache["errors"] = {}
    _seerr_detail_cache.clear()


def refresh_jellyfin_activity_cache(base_url, api_key):
    """Called from app.py's background health-check loop (never from a request
    handler) for the first enabled Jellyfin-kind integration, if any."""
    _jellyfin_activity_cache["transcoding"] = fetch_jellyfin_sessions(base_url, api_key)
    _jellyfin_activity_cache["running_tasks"] = fetch_jellyfin_running_tasks(base_url, api_key)


def get_cached_jellyfin_activity():
    return dict(_jellyfin_activity_cache)


HIGHLOAD_DEFAULTS = {"cpu_percent": "90", "disk_io_mbs": "150", "network_mbs": "80"}


def high_load_thresholds():
    """Admin-configurable thresholds (DB settings, editable at /admin/settings) -
    read here rather than duplicated in both app.py and discord_bot.py, since both
    need the exact same setting keys/defaults to stay in sync with each other."""
    return {key: int(db.get_setting(f"highload_{key}", default))
            for key, default in HIGHLOAD_DEFAULTS.items()}


def evaluate_high_load(snapshot):
    """The one place both the public page (app.py) and the Discord bot compute the
    "server under high load" indicator, so the two can't drift out of sync -
    combines monitoring.evaluate_high_load() (system-metric thresholds: CPU, disk
    I/O, network) with the Jellyfin-derived signals cached above."""
    result = monitoring.evaluate_high_load(snapshot, high_load_thresholds())
    activity = get_cached_jellyfin_activity()
    if activity["transcoding"] > 0:
        result["reasons"].append(f"{activity['transcoding']} active transcode(s)")
        result["active"] = True
    if activity["running_tasks"]:
        result["reasons"].append("Running: " + ", ".join(activity["running_tasks"]))
        result["active"] = True
    return result
