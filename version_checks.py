"""
version_checks.py — "Is Radarr/Sonarr/Prowlarr behind?", read-only.

Each Servarr-family app reports its own running version over its API, and publishes
its releases on GitHub, so the same question this portal already answers about itself
can be answered about them - without anyone opening three separate web UIs to find
out. Strictly informational: nothing here updates, restarts or configures anything on
those apps, and there is no code path that could.

Design notes worth keeping
--------------------------
- **Which app an integration actually is comes from the app itself**, never from the
  name an admin typed. Every Servarr app answers /api/v3/system/status with an
  `appName` field, so the mapping to a GitHub repository is made from the app's own
  answer. Guessing "the one called 'radarr-4k' is probably Radarr" would eventually
  check the wrong project's releases and confidently report the wrong answer.
- **The repository list is a module constant and must stay one**, for the same reason
  updater.py's is: a configurable "where do I look up versions" is a request-forgery
  primitive handed to anyone who can write a setting. This one only *reads* an API
  and never downloads or executes anything, so the blast radius is much smaller than
  the self-updater's - but the argument for a constant is free, so it stays a constant.
- **Results are persisted, not just cached in memory.** This app is meant to survive
  its own restart cleanly, and a daily task plus an in-process cache would mean the
  admin page saying "not checked yet" for up to a day after every restart.
- **Everything degrades to a recorded error, never an exception.** One unreachable app
  must not stop the other two being checked, and none of them may take the task down.
"""
import json
import logging

import requests

import db
import integrations
import scheduler
import updater

_logger = logging.getLogger(__name__)

TASK_NAME = "arr_version_check"
RESULT_SETTING = "arr_version_check_result"

# GitHub API timeout is shared with the self-updater's: same host, same shape of call.
GITHUB_TIMEOUT_SECONDS = updater.API_TIMEOUT_SECONDS

# appName as the app reports it (lowercased) -> the GitHub repo that releases it.
# Deliberately only the apps whose release tags are known to be plain version numbers.
# Adding one is a line here plus a check that its tags actually look like versions -
# a repo that tags releases "2024.10-hotfix" would compare as 0.0.0 and silently
# report "up to date" forever.
KNOWN_APPS = {
    "radarr": "Radarr/Radarr",
    "sonarr": "Sonarr/Sonarr",
    "prowlarr": "Prowlarr/Prowlarr",
}


def parse_version(text):
    """'5.14.0.9383' -> (5, 14, 0, 9383). Compares as a plain tuple.

    Deliberately *not* updater.parse_version(), and this is the one thing here worth
    reading twice. Servarr versions carry four numeric components and the fourth (the
    build number) is the one that actually moves between releases; updater.parse_version
    keeps three and spends its fourth slot on a prerelease rank, so it would parse
    5.14.0.9383 and 5.14.0.9420 as the identical tuple and report a months-old Radarr
    as up to date. The portal's own tags are semver with -rc.N suffixes and genuinely
    need that function; these are not the same format and must not share a parser.

    Anything unparseable sorts at the bottom, same defensive choice as updater's: a
    malformed tag can then never be mistaken for "newer than what you're running".
    """
    if not text:
        return (0,)
    raw = str(text).strip().lstrip("vV")
    # Drop any trailing non-numeric qualifier ('1.2.3-develop' -> '1.2.3') rather than
    # trying to rank it: these apps use branches, not prereleases, and a develop build
    # is not something this check is trying to order precisely.
    raw = raw.split("-")[0].split("+")[0]
    parts = []
    for chunk in raw.split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def _compare(installed, latest):
    """-1 / 0 / 1, padding the shorter tuple with zeros so (5, 14) and (5, 14, 0, 0)
    compare equal rather than by length."""
    a, b = parse_version(installed), parse_version(latest)
    width = max(len(a), len(b))
    a = a + (0,) * (width - len(a))
    b = b + (0,) * (width - len(b))
    return (a > b) - (a < b)


def _fetch_app_identity(base_url, api_key):
    """(appName, version) straight from the app's own status endpoint.

    v3 first, falling back to v1 for Lidarr-era APIs - the same two-version dance
    integrations.fetch_arr_status() already does, for the same reason: making the
    admin pick an API version per app would be a worse form."""
    base_url = base_url.rstrip("/")
    headers = {"X-Api-Key": api_key}
    last_error = "Unreachable"
    for version in ("v3", "v1"):
        try:
            r = requests.get(f"{base_url}/api/{version}/system/status",
                              headers=headers, timeout=integrations.TIMEOUT)
            if r.status_code == 404:
                last_error = "API endpoint not found (unsupported API version?)"
                continue
            r.raise_for_status()
            data = r.json()
            return (data.get("appName") or "").strip(), (data.get("version") or "").strip(), None
        except requests.RequestException as e:
            last_error = str(e)
        except ValueError:
            last_error = "Unexpected (non-JSON) response"
    return None, None, last_error


def _fetch_latest_release(repo):
    """The newest published release of `repo`, as (version, html_url).

    Uses /releases/latest, which GitHub already defines as "the most recent
    non-prerelease, non-draft release" - exactly what someone running a stable Radarr
    wants compared against, and one request rather than a list to sort."""
    r = requests.get(f"https://api.github.com/repos/{repo}/releases/latest",
                      headers={"Accept": "application/vnd.github+json",
                               "User-Agent": f"status-portal/{updater.current_version()}"},
                      timeout=GITHUB_TIMEOUT_SECONDS,
                      verify=True)  # never disable, same rule as updater.py
    r.raise_for_status()
    data = r.json()
    tag = (data.get("tag_name") or "").strip()
    if not tag:
        raise ValueError("release has no tag")
    return tag.lstrip("vV"), data.get("html_url") or f"https://github.com/{repo}/releases"


def check_one(integration):
    """One integration -> one result row. Never raises."""
    result = {
        "integration_id": integration["id"],
        "name": integration["name"],
        "app": None,
        "repo": None,
        "installed": None,
        "latest": None,
        "latest_url": None,
        "update_available": False,
        "error": None,
    }
    app_name, installed, error = _fetch_app_identity(integration["base_url"], integration["api_key"])
    if error:
        result["error"] = error
        return result
    result["app"] = app_name
    result["installed"] = installed
    repo = KNOWN_APPS.get((app_name or "").lower())
    if not repo:
        # Not a failure: Bazarr, Tdarr and anything else simply isn't covered here.
        # Reported so the admin can see *why* an app they expected is missing.
        result["error"] = f"No release feed configured for '{app_name or 'unknown app'}'."
        return result
    result["repo"] = repo
    try:
        latest, url = _fetch_latest_release(repo)
    except (requests.RequestException, ValueError) as e:
        result["error"] = f"Could not read {repo} releases: {e}"
        return result
    result["latest"] = latest
    result["latest_url"] = url
    result["update_available"] = _compare(installed, latest) < 0
    return result


def _arr_integrations():
    return [i for i in db.list_integrations() if i["kind"] == "arr" and i["enabled"]]


def run_check_task():
    """Body of the `arr_version_check` scheduled task."""
    targets = _arr_integrations()
    if not targets:
        raise scheduler.TaskSkipped(
            "No enabled Servarr integrations to check - add one under Integrations.")
    results = [check_one(i) for i in targets]
    # Stored before the failure check below, so a partly-successful run still publishes
    # what it did learn - and so a fully-failed run still records the *reason* against
    # each app rather than only a one-line task message.
    store_results(results)
    behind = [r for r in results if r["update_available"]]
    failed = [r for r in results if r["error"]]

    if len(failed) == len(results):
        # Every app failed, so this run learned nothing. Recording it as a success
        # would show green in the task list for a check that answered no question at
        # all - and the most likely cause (GitHub's 60-requests-per-hour limit for
        # unauthenticated callers, shared across everything on this IP) is exactly the
        # sort of thing an admin should see rather than have smoothed over.
        raise RuntimeError("Nothing could be checked - " + failed[0]["error"])

    parts = []
    if behind:
        parts.append("update available for " + ", ".join(
            f"{r['app'] or r['name']} ({r['installed']} -> {r['latest']})" for r in behind))
    if failed:
        parts.append(f"{len(failed)} couldn't be checked")
    if not parts:
        parts.append(f"all {len(results)} up to date")
    return "Checked " + str(len(results)) + " app(s): " + "; ".join(parts) + "."


def store_results(results):
    db.set_setting(RESULT_SETTING, json.dumps({"checked_at": db.now_iso(), "results": results}))


def get_results():
    """What the last run found, or None if it has never run. Persisted rather than
    cached in memory so a restart doesn't blank the admin page until tomorrow."""
    raw = db.get_setting(RESULT_SETTING, "")
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        _logger.warning("Stored Servarr version-check result is not valid JSON; ignoring it")
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        return None
    return payload


def updates_available():
    """How many apps are behind, for a badge. 0 when nothing has been checked."""
    payload = get_results()
    if not payload:
        return 0
    return sum(1 for r in payload["results"] if r.get("update_available"))


scheduler.register(
    TASK_NAME,
    "Servarr version check",
    "Asks each enabled Radarr/Sonarr/Prowlarr integration what version it's running "
    "and compares it against that project's newest GitHub release, so you can see "
    "from here whether any of them is behind. Read-only - this never updates them.",
    run_check_task,
    default_schedule_kind="daily",
    default_daily_at="04:00",
)
