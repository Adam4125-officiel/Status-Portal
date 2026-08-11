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
import requests

import config
import db
import monitoring

TIMEOUT = 5


def fetch_integration_status(integration):
    fetchers = {
        "arr": fetch_arr_status,
        "jellyfin": fetch_jellyfin_status,
        "jellyseerr": fetch_jellyseerr_status,
        "bazarr": fetch_bazarr_status,
        "tdarr": fetch_tdarr_status,
        "byparr": fetch_byparr_status,
    }
    fn = fetchers.get(integration["kind"])
    if not fn:
        return {"reachable": False, "version": None, "issues": [],
                "error": f"Unknown integration kind: {integration['kind']}"}
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
