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
"""
import json
import math
import re
from datetime import datetime, timezone

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

# The request's `sections` values, in the order they appear in the response.
SECTIONS = ("services", "incidents", "maintenance", "resources", "announcements")

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

_ANNOUNCEMENT_TYPES = ("info", "warning", "critical", "success")


def parse_sections(raw):
    """The sections a request asked for, in canonical order.

    Absent or blank (or nothing but commas and spaces) means all of them. Names the portal doesn't know are ignored, so a
    firmware newer than the portal keeps working; but a request naming *only* unknown
    sections returns None, which the route turns into a 400 - an answer that is just a
    header, for a typo, would look like a quiet portal."""
    if raw is None or not raw.strip():
        return list(SECTIONS)
    asked = {part.strip().lower() for part in raw.split(",") if part.strip()}
    if not asked:
        return list(SECTIONS)
    chosen = [name for name in SECTIONS if name in asked]
    return chosen or None


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


def services_section(services, include_ok=False):
    """Counts for every status, plus the worst few services that aren't operational
    (or, with ``include_ok``, every service, worst first, up to ALL_SERVICE_ITEMS).

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
    cap = ALL_SERVICE_ITEMS if include_ok else SERVICE_ITEMS
    section["items"] = [{"name": text(s.get("name"), SERVICE_NAME_BYTES), "status": s["status"]}
                        for s in listed[:cap]]
    return section


def incidents_section(open_count, incidents):
    return {
        "open": open_count,
        "items": [{
            "title": text(i.get("title"), INCIDENT_TITLE_BYTES),
            "status": i.get("status") or "investigating",
            "since": iso_utc(i.get("started_at")),
            "services": text(i.get("service_names"), INCIDENT_SERVICES_BYTES),
        } for i in incidents[:INCIDENT_ITEMS]],
    }


def maintenance_section(windows):
    """Windows in progress first, then the soonest upcoming. "In progress" is the same
    decision the public page makes (``applied``, set by the health-check loop once a
    window has started), so the two can't disagree about what is happening."""
    active = [w for w in windows if w.get("applied")]
    upcoming = [w for w in windows if not w.get("applied")]
    return {
        "active": len(active),
        "upcoming": len(upcoming),
        "items": [{
            "title": text(w.get("title"), MAINTENANCE_TITLE_BYTES),
            "state": "active" if w.get("applied") else "upcoming",
            "services": text(w.get("service_names"), MAINTENANCE_SERVICES_BYTES),
            "starts": iso_utc(w.get("starts_at")),
            "ends": iso_utc(w.get("ends_at")),
        } for w in (active + upcoming)[:MAINTENANCE_ITEMS]],
    }


def announcements_section(announcements):
    """Only ever fed ``db.list_active_announcements()``: a scheduled or expired
    announcement is unpublished content, and this endpoint is no place to leak one."""
    return {
        "count": len(announcements),
        "items": [{
            "title": text(a.get("title"), ANNOUNCEMENT_TITLE_BYTES),
            # The portal's own markup (**bold**) means nothing on a device.
            "text": text((a.get("message") or "").replace("**", ""), ANNOUNCEMENT_TEXT_BYTES),
            "type": a.get("type") if a.get("type") in _ANNOUNCEMENT_TYPES else "info",
            "pinned": bool(a.get("pinned")),
        } for a in announcements[:ANNOUNCEMENT_ITEMS]],
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


def build_summary(sections, *, now, site, overall, services=(), open_incident_count=0,
                  incidents=(), maintenance=(), announcements=(), resources=None,
                  all_services=False, all_resources=False):
    """The response body as a dict. Only the requested sections are present; the
    header (version, server time, site name, overall status) always is - it costs
    nothing and a device needs the server's clock to age the timestamps it is given
    without trusting its own."""
    summary = {
        "v": SCHEMA_VERSION,
        "now": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "site": text(site, SITE_BYTES),
        "overall": overall,
    }
    if "services" in sections:
        summary["services"] = services_section(services, include_ok=all_services)
    if "incidents" in sections:
        summary["incidents"] = incidents_section(open_incident_count, incidents)
    if "maintenance" in sections:
        summary["maintenance"] = maintenance_section(maintenance)
    if "resources" in sections:
        summary["resources"] = resources_section(resources, include_all=all_resources)
    if "announcements" in sections:
        summary["announcements"] = announcements_section(announcements)
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


def example_json():
    """(pretty text for a human, size in bytes of what is actually sent) - the admin page
    shows the first and quotes the second."""
    example = example_summary()
    return (json.dumps(example, ensure_ascii=False, indent=2),
            len(dumps(example).encode("utf-8")))
