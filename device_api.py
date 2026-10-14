"""The compact JSON summary served to small display devices (Status-ESP).

Pure shaping, no Flask and no database: ``app.api_device_summary()`` gathers the rows
from the existing caches/queries and hands them in, and this module decides what a
screen the size of a postage stamp gets to see. That split is what keeps it testable
with plain dicts, and what lets the admin page render a *real* example (``example()``)
instead of a hand-written one that would drift.

The contract is written for a firmware with roughly 35 KB of free RAM, so everything
here is bounded by construction rather than by hope:

- every list has a fixed cap, and every string a fixed cap in UTF-8 **bytes** (not
  characters - four bytes per emoji would otherwise blow any character-based budget);
- the worst case, with every list full and every string made of characters that JSON
  has to escape, stays under ``MAX_BYTES`` (tests/test_device_api.py builds exactly
  that and asserts it). A typical response is about half the cap.

Strings are left as UTF-8: the device folds them to ASCII itself, and ``\\uXXXX``
escapes would triple the size of every accented letter.

A second kind of consumer, a server rather than a microcontroller (the Jelly Portal Jellyfin
plugin), wants the same data with room to breathe. It asks for ``detail=full``: the same
header and the same sections, shaped with the ``FULL`` limits below (longer strings, longer
lists) plus a few extra keys. Without the parameter nothing changes by a single byte
(tests/test_device_api_golden.py pins that), and the full answer is bounded just as hard,
against its own, much larger ceilings.
"""
import json
import math
import re
from datetime import datetime, timezone
from typing import NamedTuple

SCHEMA_VERSION = 1

# Hard ceiling on a response, asserted in the tests against adversarial data. The
# device's own budget is "under 3 KB, never over 4 KB".
MAX_BYTES = 4096

# With ``services=all`` the list also names every operational service, so the ceiling for
# that request is higher; the device asks for it only when it pages through the list.
MAX_BYTES_ALL_SERVICES = 7168

# With ``resources=all`` the section also carries the GPUs and more disks. Asked for together
# with ``services=all`` (which is how Status-ESP asks) this is the largest answer the endpoint
# can give, and the ceiling a device sizes its buffer for.
MAX_BYTES_ALL = 8192

# With the opt-in ``vms`` section as well - every section, ``services=all``, ``resources=all``,
# ``jellyfin=1`` and ten VMs - the answer grows by the VM list: the adversarial worst case
# measures about 8.9 KB (tests/test_device_api.py asserts it), a realistic one 3-4 KB. A device
# that asks for ``vms`` sizes its buffer by this; one that does not never receives more than
# MAX_BYTES_ALL.
MAX_BYTES_ALL_WITH_VMS = 9216

# ``detail=full`` is for a server, so its ceilings are in tens of kilobytes, but they are ceilings
# all the same: tests/test_device_api.py builds the adversarial worst case for each and asserts it.
# MAX_BYTES_FULL_SECTIONS is for the request the Jelly Portal plugin makes (``sections=services,
# incidents,maintenance,announcements&services=all&detail=full``); MAX_BYTES_FULL is the largest
# answer the endpoint can ever give: every section, every modifier, ``vms`` and ``detail=full``.
# A realistic full answer is a few kilobytes (about 4 KB for 30 services, 3 incidents, 5 windows and
# 3 announcements). Most of the worst case is the maintenance list: 20 windows, each with a title, a
# service string, a 400-byte description and a name array of 16 names, all made of characters that
# JSON has to escape. Raising any FULL_* cap means re-running the worst-case tests in
# tests/test_device_api.py and moving these two numbers, not trusting arithmetic.
MAX_BYTES_FULL_SECTIONS = 114688   # 112 KB; the adversarial worst case measures 112,785 bytes
MAX_BYTES_FULL = 118784            # 116 KB; the adversarial worst case measures 115,576 bytes

# The request's `sections` values, in the order they appear in the response. An absent or
# blank `sections` means exactly these.
SECTIONS = ("services", "incidents", "maintenance", "resources", "announcements")

# Sections a request only gets by naming them. Leaving `sections` out still means the five above,
# so a device that has never heard of one is never handed a body bigger than it sized for: the same
# opt-in rule as `services=all`, `resources=all` and `jellyfin=1`.
OPT_IN_SECTIONS = ("vms",)

# Every name `sections` accepts, in response order.
ALL_SECTIONS = SECTIONS + OPT_IN_SECTIONS

# Worst first. Every status enumeration in this app needs the fifth, cosmetic `slow`
# tier (CLAUDE.md), and tests/test_conventions.py checks this map for it.
STATUS_RANK = {"down": 0, "degraded": 1, "maintenance": 2, "slow": 3, "operational": 4}

# The order the per-status counts are written in: healthy first, the way a status page
# reads. Must hold exactly the keys of STATUS_RANK (a test checks).
COUNT_ORDER = ("operational", "slow", "degraded", "maintenance", "down")

# List caps. The counts next to each list say how many exist in total, so a device can
# render "+N more" without the portal sending N rows it has no room to draw.
SERVICE_ITEMS = 6
ALL_SERVICE_ITEMS = 40
INCIDENT_ITEMS = 3
MAINTENANCE_ITEMS = 3
ANNOUNCEMENT_ITEMS = 3
DISK_ITEMS = 4
ALL_DISK_ITEMS = 8
GPU_ITEMS = 4
JELLYFIN_TASK_ITEMS = 3
VM_ITEMS = 10

# String caps, in UTF-8 bytes.
SITE_BYTES = 24
SERVICE_NAME_BYTES = 24
INCIDENT_TITLE_BYTES = 40
INCIDENT_SERVICES_BYTES = 32
MAINTENANCE_TITLE_BYTES = 32
MAINTENANCE_SERVICES_BYTES = 32
ANNOUNCEMENT_TITLE_BYTES = 32
ANNOUNCEMENT_TEXT_BYTES = 80
DISK_NAME_BYTES = 16
GPU_NAME_BYTES = 20
JELLYFIN_TASK_BYTES = 28
VM_NAME_BYTES = 24
VM_STATE_BYTES = 12     # Hyper-V's longest state name is "Snapshotting"
VM_UPTIME_BYTES = 10    # "123d 23h", "23h 59m", or the em dash the snapshot uses for "unknown"

# ``detail=full``: the caps for a consumer that is a server. Only the site name and the services,
# incidents, maintenance and announcements sections are shaped by them; resources, vms and jellyfin
# are the same under either mode (they carry server internals, and a plugin must not ask for them).
DETAIL_FULL = "full"

FULL_SITE_BYTES = 64
FULL_SERVICE_ITEMS = 100          # with and without services=all
FULL_SERVICE_NAME_BYTES = 64
FULL_INCIDENT_ITEMS = 10
FULL_INCIDENT_TITLE_BYTES = 120
FULL_INCIDENT_SERVICES_BYTES = 120
FULL_MAINTENANCE_ITEMS = 20
FULL_MAINTENANCE_TITLE_BYTES = 120
FULL_MAINTENANCE_SERVICES_BYTES = 120
FULL_MAINTENANCE_DESCRIPTION_BYTES = 400
FULL_ANNOUNCEMENT_ITEMS = 10
FULL_ANNOUNCEMENT_TITLE_BYTES = 100
FULL_ANNOUNCEMENT_TEXT_BYTES = 500
FULL_SERVICE_LIST_ITEMS = 16      # names in one incident's / window's ``service_names`` array

_ANNOUNCEMENT_TYPES = ("info", "warning", "critical", "success")


class Limits(NamedTuple):
    """The caps one request is shaped with, so the shaping functions take one argument instead of a
    dozen. ``COMPACT`` is built from the constants above and is what every request got before
    ``detail=full`` existed - the shaping calls ``text()`` with exactly the values it always did,
    which is what keeps the default answer byte for byte the same. ``full`` is what switches on the
    keys only the full answer carries."""
    full: bool
    site_bytes: int
    service_items: int              # services listed without services=all
    all_service_items: int          # services listed with services=all
    service_name_bytes: int
    incident_items: int
    incident_title_bytes: int
    incident_services_bytes: int
    maintenance_items: int
    maintenance_title_bytes: int
    maintenance_services_bytes: int
    maintenance_description_bytes: int   # full only
    announcement_items: int
    announcement_title_bytes: int
    announcement_text_bytes: int
    service_list_items: int              # full only


COMPACT = Limits(
    full=False, site_bytes=SITE_BYTES, service_items=SERVICE_ITEMS, all_service_items=ALL_SERVICE_ITEMS,
    service_name_bytes=SERVICE_NAME_BYTES, incident_items=INCIDENT_ITEMS,
    incident_title_bytes=INCIDENT_TITLE_BYTES, incident_services_bytes=INCIDENT_SERVICES_BYTES,
    maintenance_items=MAINTENANCE_ITEMS, maintenance_title_bytes=MAINTENANCE_TITLE_BYTES,
    maintenance_services_bytes=MAINTENANCE_SERVICES_BYTES, maintenance_description_bytes=0,
    announcement_items=ANNOUNCEMENT_ITEMS, announcement_title_bytes=ANNOUNCEMENT_TITLE_BYTES,
    announcement_text_bytes=ANNOUNCEMENT_TEXT_BYTES, service_list_items=0)

FULL = Limits(
    full=True, site_bytes=FULL_SITE_BYTES, service_items=FULL_SERVICE_ITEMS,
    all_service_items=FULL_SERVICE_ITEMS, service_name_bytes=FULL_SERVICE_NAME_BYTES,
    incident_items=FULL_INCIDENT_ITEMS, incident_title_bytes=FULL_INCIDENT_TITLE_BYTES,
    incident_services_bytes=FULL_INCIDENT_SERVICES_BYTES, maintenance_items=FULL_MAINTENANCE_ITEMS,
    maintenance_title_bytes=FULL_MAINTENANCE_TITLE_BYTES,
    maintenance_services_bytes=FULL_MAINTENANCE_SERVICES_BYTES,
    maintenance_description_bytes=FULL_MAINTENANCE_DESCRIPTION_BYTES,
    announcement_items=FULL_ANNOUNCEMENT_ITEMS, announcement_title_bytes=FULL_ANNOUNCEMENT_TITLE_BYTES,
    announcement_text_bytes=FULL_ANNOUNCEMENT_TEXT_BYTES, service_list_items=FULL_SERVICE_LIST_ITEMS)


def limits_for(full):
    return FULL if full else COMPACT


def incident_limit(full=False):
    """How many open incidents the route asks the database for: the same number the section will
    list, so a full answer is not capped by a query that fetched only the compact three."""
    return limits_for(full).incident_items


def parse_sections(raw):
    """The sections a request asked for, in canonical order.

    Absent or blank (or nothing but commas and spaces) means the default sections, which
    leave out the opt-in ones (``vms``): those come only when named. Names the portal doesn't
    know are ignored, so a firmware newer than the portal keeps working; but a request naming
    *only* unknown sections returns None, which the route turns into a 400 - an answer that is
    just a header, for a typo, would look like a quiet portal."""
    if raw is None or not raw.strip():
        return list(SECTIONS)
    asked = {part.strip().lower() for part in raw.split(",") if part.strip()}
    if not asked:
        return list(SECTIONS)
    chosen = [name for name in ALL_SECTIONS if name in asked]
    return chosen or None


def parse_detail(raw):
    """True when the request asked for ``detail=full``, False for anything else.

    Only the exact value ``full`` (case and surrounding spaces ignored) opts in; a typo, an empty
    value or a value from a future portal is the default answer, like ``services=`` and
    ``resources=`` treat anything but ``all``. An older portal ignores the parameter entirely,
    which is why the full answer says ``"detail":"full"`` in its header: the caller can tell."""
    return raw is not None and raw.strip().lower() == DETAIL_FULL


def text(value, max_bytes):
    """One line of at most ``max_bytes`` UTF-8 bytes.

    Whitespace (newlines included) collapses to single spaces and anything
    non-printable is dropped - a display has one line to spend, and a control
    character would cost six bytes as an escape. A cut ends in an ASCII "..." so the
    device can tell it was truncated, and never lands inside a multi-byte character."""
    # Any whitespace (newlines, tabs, no-break spaces) becomes a space, any other
    # non-printable character is dropped, and only then are the spaces collapsed.
    kept = "".join(" " if ch.isspace() else ch for ch in str(value if value is not None else "")
                   if ch.isspace() or ch.isprintable())
    cleaned = " ".join(kept.split())
    raw = cleaned.encode("utf-8")
    if len(raw) <= max_bytes:
        return cleaned
    cut = raw[:max_bytes - 3].decode("utf-8", errors="ignore").rstrip()
    return cut + "..."


_GPU_VENDOR_PREFIX = re.compile(r"^(?:NVIDIA\s+)?(?:GeForce\s+)?", re.IGNORECASE)


def gpu_name(value):
    """A GPU's name as a small screen wants it: the vendor words every NVIDIA card starts
    with ("NVIDIA GeForce RTX 3080" -> "RTX 3080") are what 20 bytes cannot afford."""
    name = text(value, 80)
    short = _GPU_VENDOR_PREFIX.sub("", name)
    return text(short or name, GPU_NAME_BYTES)


def iso_utc(value):
    """A stored timestamp as ``YYYY-MM-DDTHH:MM:SSZ``, or "" when it can't be read.

    The database holds two shapes: full ISO with an offset (incidents, from
    ``db.now_iso()``) and ``datetime-local`` values with no zone (maintenance windows,
    whose form says UTC). A naive value is therefore taken as UTC - the same reading
    the portal's own scheduler gives it when it compares them."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _number(value):
    """A finite number, or None. A NaN or infinity is not valid JSON and the device
    could not parse it anyway."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def services_section(services, include_ok=False, limits=COMPACT):
    """Counts for every status, plus the worst few services that aren't operational
    (or, with ``include_ok``, every service, worst first, up to ALL_SERVICE_ITEMS). ``limits``
    (``FULL`` for ``detail=full``) sets those caps and the length of a name.

    A service flagged ``ignore_in_overall_status`` is left out of both, exactly as
    ``compute_overall_status()`` leaves it out of the headline: this is the headline's
    little sibling, and a device that said "all up" above a list naming a service the
    admin deliberately muted would contradict itself."""
    counted = [s for s in services if not s.get("ignore_in_overall_status")]
    counts = {status: 0 for status in COUNT_ORDER}
    for s in counted:
        if s.get("status") in counts:
            counts[s["status"]] += 1
    listed = sorted((s for s in counted if s.get("status") in STATUS_RANK
                     and (include_ok or s["status"] != "operational")),
                    key=lambda s: STATUS_RANK[s["status"]])  # stable: keeps sort_order
    section = {"total": len(counted)}
    section.update(counts)
    cap = limits.all_service_items if include_ok else limits.service_items
    items = []
    for s in listed[:cap]:
        item = {"name": text(s.get("name"), limits.service_name_bytes), "status": s["status"]}
        # With include_ok, how long the last check took to answer, for the services whose tag a
        # device writes a latency beside. Left out when nothing was measured (a manual service, a
        # check that has not run yet) rather than sent as a 0 that reads as "instant".
        ms = _number(s.get("response_ms"))
        if include_ok and ms is not None and ms >= 0 and s["status"] in ("operational", "slow"):
            item["ms"] = int(ms)
        items.append(item)
    section["items"] = items
    return section


def service_name_list(rows, limits):
    """The full names of the services a row covers, as a list, for ``detail=full``.

    ``rows`` is the ``services`` list of ``{"id", "name"}`` dicts that ``db.py`` already attaches to
    every incident and maintenance window (one grouped query for all of them), so this adds no
    query. Each name is cut with the same ``text()`` and the same cap as a name in the services
    section, so a name here compares equal to the same service's name there. Empty names are
    skipped, and the list stops at ``limits.service_list_items``."""
    names = []
    for row in rows or ():
        name = text(row.get("name") if isinstance(row, dict) else None, limits.service_name_bytes)
        if name:
            names.append(name)
            if len(names) >= limits.service_list_items:
                break
    return names


def incidents_section(open_count, incidents, limits=COMPACT):
    items = []
    for i in incidents[:limits.incident_items]:
        item = {
            "title": text(i.get("title"), limits.incident_title_bytes),
            "status": i.get("status") or "investigating",
            "since": iso_utc(i.get("started_at")),
            "services": text(i.get("service_names"), limits.incident_services_bytes),
        }
        if limits.full:
            item["service_names"] = service_name_list(i.get("services"), limits)
        items.append(item)
    return {"open": open_count, "items": items}


def maintenance_section(windows, limits=COMPACT):
    """Windows in progress first, then the soonest upcoming. "In progress" is the same
    decision the public page makes (``applied``, set by the health-check loop once a
    window has started), so the two can't disagree about what is happening."""
    active = [w for w in windows if w.get("applied")]
    upcoming = [w for w in windows if not w.get("applied")]
    items = []
    for w in (active + upcoming)[:limits.maintenance_items]:
        item = {
            "title": text(w.get("title"), limits.maintenance_title_bytes),
            "state": "active" if w.get("applied") else "upcoming",
            "services": text(w.get("service_names"), limits.maintenance_services_bytes),
            "starts": iso_utc(w.get("starts_at")),
            "ends": iso_utc(w.get("ends_at")),
        }
        if limits.full:
            item["service_names"] = service_name_list(w.get("services"), limits)
            item["description"] = text(w.get("description"), limits.maintenance_description_bytes)
        items.append(item)
    return {"active": len(active), "upcoming": len(upcoming), "items": items}


def announcements_section(announcements, limits=COMPACT):
    """Only ever fed ``db.list_active_announcements()``: a scheduled or expired
    announcement is unpublished content, and this endpoint is no place to leak one."""
    return {
        "count": len(announcements),
        "items": [{
            "title": text(a.get("title"), limits.announcement_title_bytes),
            # The portal's own markup (**bold**) means nothing on a device.
            "text": text((a.get("message") or "").replace("**", ""), limits.announcement_text_bytes),
            "type": a.get("type") if a.get("type") in _ANNOUNCEMENT_TYPES else "info",
            "pinned": bool(a.get("pinned")),
        } for a in announcements[:limits.announcement_items]],
    }


def resources_section(snapshot, include_all=False):
    """The fields of ``monitoring.get_resource_snapshot()`` a small screen can use.

    ``*_sev`` is the portal's own ok/warn/crit judgement (``monitoring._severity``),
    passed through so the device colours its bars exactly as the web page does instead
    of carrying a second copy of the thresholds. Disks are the fullest first: when only
    four fit, those are the four worth seeing. Returns None when there is no snapshot.

    With ``include_all`` (``resources=all``) the list holds up to ALL_DISK_ITEMS disks and
    the section gains ``gpu_count`` and ``gpus``, so a device that pages through its
    resources can show them all. Without it the section is exactly what 1.10.0 sent: a
    firmware that never asked for more is not handed a body bigger than it sized for."""
    if not snapshot:
        return None
    network = snapshot.get("network") or {}
    disks = sorted(snapshot.get("disks") or [], key=lambda d: -(_number(d.get("percent")) or 0))
    section = {
        "cpu": _number(snapshot.get("cpu_percent")),
        "cpu_sev": snapshot.get("cpu_severity"),
        "cpu_temp_c": _number(snapshot.get("cpu_temp_c")),
        "mem": _number(snapshot.get("mem_percent")),
        "mem_sev": snapshot.get("mem_severity"),
        "mem_used_gb": _number(snapshot.get("mem_used_gb")),
        "mem_total_gb": _number(snapshot.get("mem_total_gb")),
        "net_up_mb_s": _number(network.get("up_mb_s")),
        "net_down_mb_s": _number(network.get("down_mb_s")),
        "disk_count": len(disks),
        "disks": [{
            "name": text(d.get("label") or d.get("path"), DISK_NAME_BYTES),
            "pct": _number(d.get("percent")),
            "sev": d.get("severity"),
            "free_gb": _number(d.get("free_gb")),
        } for d in disks[:ALL_DISK_ITEMS if include_all else DISK_ITEMS]],
    }
    if include_all:
        gpus = snapshot.get("gpus") or []
        section["gpu_count"] = len(gpus)
        section["gpus"] = [{
            "name": gpu_name(g.get("name")),
            "pct": _number(g.get("util_percent")),
            "sev": g.get("severity"),
            "mem_used_gb": _number(g.get("mem_used_gb")),
            "mem_total_gb": _number(g.get("mem_total_gb")),
            "temp_c": _number(g.get("temp_c")),
        } for g in gpus[:GPU_ITEMS]]
    return section


def vms_section(vms):
    """The Hyper-V virtual machines the portal has detected: how many, how many are running, and
    the first few (by name) with their state and uptime, as the public VM page lists them. It
    reads the background-refreshed cache, never PowerShell. ``state`` is Hyper-V's own word
    (Running, Off, Paused, Saved, Starting...) so a device can colour by it; ``up`` is the portal's
    short uptime text ("3d 4h"), and means nothing for a VM that is not running.

    No VMs at all (not a Windows host, no Hyper-V) is an empty list, not a null: the portal read
    its answer fine and it was "none". Only a list that could not be read is None."""
    if vms is None:
        return None
    ordered = sorted(vms, key=lambda vm: str(vm.get("name") or "").casefold())
    return {
        "total": len(ordered),
        "running": sum(1 for vm in ordered if vm.get("state") == "Running"),
        "items": [{
            "name": text(vm.get("name"), VM_NAME_BYTES),
            "state": text(vm.get("state"), VM_STATE_BYTES),
            "up": text(vm.get("uptime"), VM_UPTIME_BYTES),
        } for vm in ordered[:VM_ITEMS]],
    }


def jellyfin_section(activity):
    """What Jellyfin is doing right now, from the cache the public page reads: how many
    transcodes are running and the names of the scheduled tasks (trickplay generation, a
    library scan). Names only: the cache holds no progress, so a device cannot draw a bar."""
    activity = activity or {}
    return {
        "transcodes": int(_number(activity.get("transcoding")) or 0),
        "tasks": [text(t, JELLYFIN_TASK_BYTES)
                  for t in (activity.get("running_tasks") or [])[:JELLYFIN_TASK_ITEMS]],
    }


def build_summary(sections, *, now, site, overall, services=(), open_incident_count=0,
                  incidents=(), maintenance=(), announcements=(), resources=None,
                  all_services=False, all_resources=False, jellyfin=None,
                  with_jellyfin=False, vms=None, detail_full=False):
    """The response body as a dict. Only the requested sections are present; the
    header (version, server time, site name, overall status) always is - it costs
    nothing and a device needs the server's clock to age the timestamps it is given
    without trusting its own.

    ``detail_full`` (``detail=full``) shapes the site name and the services, incidents,
    maintenance and announcements sections with the ``FULL`` limits and adds ``service_names`` and
    ``description``; resources, vms and jellyfin are untouched by it. It also puts
    ``"detail":"full"`` in the header, right after ``overall``, so a caller can tell a portal that
    honoured the parameter from an older one that ignored it."""
    limits = limits_for(detail_full)
    summary = {
        "v": SCHEMA_VERSION,
        "now": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "site": text(site, limits.site_bytes),
        "overall": overall,
    }
    if detail_full:
        summary["detail"] = DETAIL_FULL
    if with_jellyfin:
        # In the header, not in a section: a display that shows it above everything must get it
        # whichever sections it switched on.
        summary["jellyfin"] = jellyfin_section(jellyfin)
    if "services" in sections:
        summary["services"] = services_section(services, include_ok=all_services, limits=limits)
    if "incidents" in sections:
        summary["incidents"] = incidents_section(open_incident_count, incidents, limits)
    if "maintenance" in sections:
        summary["maintenance"] = maintenance_section(maintenance, limits)
    if "resources" in sections:
        summary["resources"] = resources_section(resources, include_all=all_resources)
    if "announcements" in sections:
        summary["announcements"] = announcements_section(announcements, limits)
    if "vms" in sections:
        summary["vms"] = vms_section(vms)
    return summary


def dumps(summary):
    """Compact JSON, UTF-8 left as UTF-8 (see the module docstring)."""
    return json.dumps(summary, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def example_summary():
    """A realistic full response built through ``build_summary()`` from canned rows, for
    the admin page and the API contract. Built rather than typed so it cannot drift from
    what the portal actually sends."""
    now = datetime(2026, 10, 6, 12, 0, 0, tzinfo=timezone.utc)
    return build_summary(
        list(SECTIONS), now=now, site="Home Server", overall="down",
        services=[
            {"name": "Jellyfin", "status": "operational"},
            {"name": "Radarr", "status": "slow"},
            {"name": "Sonarr", "status": "degraded"},
            {"name": "Nextcloud", "status": "down"},
            {"name": "Seerr", "status": "operational"},
        ],
        open_incident_count=1,
        incidents=[{"title": "Nextcloud is unreachable", "status": "investigating",
                    "started_at": "2026-10-06T11:40:12.345678+00:00",
                    "service_names": "Nextcloud"}],
        maintenance=[
            {"title": "Disk replacement", "applied": 0, "service_names": "Jellyfin, Radarr",
             "starts_at": "2026-10-08T22:00", "ends_at": "2026-10-09T01:00"},
            {"title": "Router firmware", "applied": 1, "service_names": "Seerr",
             "starts_at": "2026-10-06T11:30", "ends_at": "2026-10-06T12:30"},
        ],
        announcements=[{"title": "Movie night", "message": "Friday at **8pm**, bring snacks.",
                        "type": "info", "pinned": 1}],
        resources={
            "cpu_percent": 23.4, "cpu_severity": "ok", "cpu_temp_c": 54.0,
            "mem_percent": 61.2, "mem_severity": "warn", "mem_used_gb": 9.8, "mem_total_gb": 16.0,
            "network": {"up_mb_s": 0.12, "down_mb_s": 1.4},
            "disks": [
                {"path": "/", "label": "", "percent": 41.0, "severity": "ok", "free_gb": 280.4},
                {"path": "/mnt/media", "label": "Media", "percent": 88.5, "severity": "crit",
                 "free_gb": 460.2},
            ],
        },
    )


def example_vms_summary():
    """What ``sections=vms`` answers, built through ``build_summary()`` like the main example."""
    now = datetime(2026, 10, 6, 12, 0, 0, tzinfo=timezone.utc)
    return build_summary(
        ["vms"], now=now, site="Home Server", overall="operational",
        vms=[
            {"name": "Docker-Host", "state": "Running", "uptime": "12d 4h"},
            {"name": "Game-Server", "state": "Running", "uptime": "3h 20m"},
            {"name": "Test-VM", "state": "Off", "uptime": "0m"},
        ])


# The query the Jelly Portal plugin makes, and the one example_full_summary() answers.
FULL_EXAMPLE_QUERY = "sections=services,incidents,maintenance,announcements&services=all&detail=full"


def example_full_summary():
    """What the plugin's request (``FULL_EXAMPLE_QUERY``) answers, built through ``build_summary()``
    from canned rows like the other examples, never typed. The rows carry the ``services`` lists
    ``db.py`` attaches to incidents and windows; ``service_names`` is derived from them here the
    way ``db.py`` derives it, so the string and the array in the example cannot disagree."""
    now = datetime(2026, 10, 6, 12, 0, 0, tzinfo=timezone.utc)

    def covering(row, *names):
        row["services"] = [{"id": n, "name": name} for n, name in enumerate(names, 1)]
        row["service_names"] = ", ".join(names)
        return row

    return build_summary(
        ["services", "incidents", "maintenance", "announcements"], now=now, site="Home Server",
        overall="down", all_services=True, detail_full=True,
        services=[
            {"name": "Jellyfin", "status": "operational", "response_ms": 41},
            {"name": "Radarr", "status": "slow", "response_ms": 1850},
            {"name": "Sonarr", "status": "degraded"},
            {"name": "Nextcloud (family photos and documents)", "status": "down"},
            {"name": "Seerr", "status": "operational", "response_ms": 87},
        ],
        open_incident_count=2,
        incidents=[
            covering({"title": "Nextcloud and Seerr are unreachable after the overnight update",
                      "status": "investigating", "started_at": "2026-10-06T11:40:12.345678+00:00"},
                     "Nextcloud (family photos and documents)", "Seerr"),
            covering({"title": "Slow Wi-Fi in the living room", "status": "monitoring",
                      "started_at": "2026-10-05T18:02:44.000000+00:00"}),
        ],
        maintenance=[
            covering({"title": "Disk replacement", "applied": 0,
                      "description": "The media disk is swapped for a larger one. Streaming stops "
                                     "for about three hours.",
                      "starts_at": "2026-10-08T22:00", "ends_at": "2026-10-09T01:00"},
                     "Jellyfin", "Radarr", "Sonarr"),
            covering({"title": "Router firmware", "applied": 1,
                      "description": "Updating to the latest firmware; the network may drop for a "
                                     "few minutes.",
                      "starts_at": "2026-10-06T11:30", "ends_at": "2026-10-06T12:30"}, "Seerr"),
        ],
        announcements=[{"title": "Movie night",
                        "message": "Friday at **8pm**, bring snacks. We are watching the film "
                                   "that won the vote.", "type": "info", "pinned": 1}],
    )


def example_json():
    """(pretty text for a human, size in bytes of what is actually sent) - the admin page
    shows the first and quotes the second."""
    example = example_summary()
    return (json.dumps(example, ensure_ascii=False, indent=2),
            len(dumps(example).encode("utf-8")))


def example_vms_json():
    """The ``sections=vms`` example as pretty text, for the admin page."""
    return json.dumps(example_vms_summary(), ensure_ascii=False, indent=2)


def example_full_json():
    """(pretty text, size in bytes of what is actually sent) for the ``detail=full`` example."""
    example = example_full_summary()
    return (json.dumps(example, ensure_ascii=False, indent=2),
            len(dumps(example).encode("utf-8")))
