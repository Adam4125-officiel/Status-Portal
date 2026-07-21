"""
integrations.py — Read-only status/log checks against other self-hosted apps
(Jellyfin, Jellyseerr/Overseerr, and Servarr-family apps: Sonarr, Radarr,
Prowlarr, Lidarr, Readarr). Nothing here writes to or controls those
services - it only reads health/logs to surface warnings in the admin
section. Every fetcher returns the same shape so the template doesn't need
to care which kind of service it's showing:

    {"reachable": bool, "version": str|None, "issues": [{"level", "message"}], "error": str|None}
"""
import requests

TIMEOUT = 5


def fetch_integration_status(integration):
    fetchers = {
        "arr": fetch_arr_status,
        "jellyfin": fetch_jellyfin_status,
        "jellyseerr": fetch_jellyseerr_status,
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
