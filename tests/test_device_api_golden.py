"""The default device answer, pinned byte for byte.

Status-ESP's firmware sizes its buffers and its parser around exactly what 1.11.1 sends, so
anything added to the endpoint later must leave a request that doesn't opt in with the *same
bytes*, not merely "the same keys". `tests/golden/device_api_default.json` holds the compact
answer for a rich, hostile set of rows (long and non-ASCII names, quotes, every list past its
cap, every optional modifier), generated from the unmodified 1.11.1 `device_api.py` (commit
39566d9) before `detail=full` was written. These tests build the same answers today and
compare them to it.

If one fails, the default answer changed. That is never a refactoring side effect: it is a
decision about every installed display, so it belongs in a release note and a new golden,
not in a quiet edit of this file.

To regenerate on purpose:
    python -c "import json, sys; sys.path[:0] = ['.', 'tests']; import test_device_api_golden as g; \
open(g.GOLDEN, 'w').write(json.dumps(g.build_cases(), ensure_ascii=False, indent=1) + '\\n')"
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import device_api

GOLDEN = Path(__file__).parent / "golden" / "device_api_default.json"

NOW = datetime(2026, 10, 6, 12, 0, 0, tzinfo=timezone.utc)
_NASTY = '"\\' * 30 + "\U0001F4A5" * 4 + "\x00\x07  café \t tabs\nand newlines"


def _rows():
    statuses = ("down", "degraded", "maintenance", "slow", "operational")
    services = [{"name": f"Service {i:02d} " + ("long name " * (i % 5)), "status": statuses[i % 5],
                 "response_ms": (i * 37) % 900 if i % 3 else None,
                 "ignore_in_overall_status": 1 if i % 11 == 0 else 0}
                for i in range(55)]
    services.append({"name": _NASTY, "status": "down", "response_ms": 12})
    services.append({"name": "Café serveur éè", "status": "operational", "response_ms": 5.5})
    incidents = [{"title": f"Incident {i}: " + ("something is wrong " * (i + 1)),
                  "status": ("investigating", "identified", "monitoring", None)[i % 4],
                  "started_at": ("2026-10-06T11:40:12.345678+00:00", "2026-10-05T08:00:00+02:00",
                                 "2026-10-04T23:59:59Z", "")[i % 4],
                  "service_names": ", ".join(f"Service {j:02d}" for j in range(i + 1))}
                 for i in range(6)]
    incidents.append({"title": _NASTY, "service_names": _NASTY})
    maintenance = [{"title": f"Window {i} " + ("details " * i), "applied": i % 2,
                    "service_names": ", ".join(f"Service {j:02d}" for j in range(i + 2)),
                    "starts_at": f"2026-10-0{i + 1}T22:00", "ends_at": f"2026-10-0{i + 1}T23:30"}
                   for i in range(6)]
    maintenance.append({"title": _NASTY, "applied": 1, "service_names": _NASTY,
                        "starts_at": "garbage", "ends_at": ""})
    announcements = [{"title": f"Announcement {i} " + ("title " * i),
                      "message": "Friday at **8pm**, " + ("bring snacks and a long explanation. " * (i + 1)),
                      "type": ("info", "warning", "critical", "success", "odd")[i % 5], "pinned": i % 2}
                     for i in range(5)]
    announcements.append({"title": _NASTY, "message": _NASTY, "type": "critical", "pinned": 1})
    resources = {
        "cpu_percent": 91.5, "cpu_severity": "crit", "cpu_temp_c": None,
        "mem_percent": 40.04, "mem_severity": "ok", "mem_used_gb": 6.4, "mem_total_gb": 16.0,
        "network": {"up_mb_s": 0.123, "down_mb_s": 1.5},
        "disks": [{"path": f"/mnt/d{i}", "label": "" if i % 2 else f"Disk {i} with a long label",
                   "percent": 100 - i * 7.3, "severity": ("crit", "warn", "ok")[i % 3], "free_gb": 10.5 * i}
                  for i in range(10)],
        "gpus": [{"name": "NVIDIA GeForce RTX 3080 Ti", "util_percent": 12.0 + i, "severity": "ok",
                  "mem_used_gb": 3.2, "mem_total_gb": 12.0, "temp_c": 61.0 + i} for i in range(6)],
    }
    vms = [{"name": f"VM-{i}", "state": ("Running", "Off", "Paused")[i % 3], "uptime": f"{i}d 4h"}
           for i in range(13)]
    jellyfin = {"transcoding": 3, "running_tasks": ["Scan media library", "Generate trickplay images", "A", "B"]}
    return dict(services=services, open_incident_count=9, incidents=incidents, maintenance=maintenance,
                announcements=announcements, resources=resources, vms=vms, jellyfin=jellyfin)


def build_cases():
    rows = _rows()

    def answer(sections, **options):
        return device_api.dumps(device_api.build_summary(
            sections, now=NOW, site="Home Server " + _NASTY, overall="down", **rows, **options))

    return {
        "example_summary": device_api.dumps(device_api.example_summary()),
        "example_vms_summary": device_api.dumps(device_api.example_vms_summary()),
        "default_sections": answer(list(device_api.SECTIONS)),
        "only_incidents": answer(["incidents"]),
        "services_all": answer(list(device_api.SECTIONS), all_services=True),
        "resources_all": answer(list(device_api.SECTIONS), all_resources=True),
        "jellyfin_header": answer(list(device_api.SECTIONS), with_jellyfin=True),
        "everything": answer(list(device_api.ALL_SECTIONS), all_services=True, all_resources=True,
                             with_jellyfin=True),
    }


@pytest.fixture(scope="module")
def golden():
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_the_golden_covers_every_case():
    assert set(json.loads(GOLDEN.read_text(encoding="utf-8"))) == set(build_cases())


@pytest.mark.parametrize("name", sorted(build_cases()))
def test_the_default_answer_is_byte_for_byte_what_1_11_1_sent(name, golden):
    assert build_cases()[name] == golden[name]
