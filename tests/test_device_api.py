"""The display-device API: GET /api/device/summary, and the shaping behind it.

What these pin is everything the server decides - whether the endpoint exists at all,
who may read it, what each section carries, and that the answer stays small enough for a
microcontroller with ~35 KB of free RAM whatever is in the database. The firmware side
is the sibling Status-ESP repository.
"""
import json
import re
import sqlite3
from datetime import datetime, timezone

import pytest
from markupsafe import escape

import admin_search
import app as app_module
import db
import device_api
import integrations
import monitoring

KEY = "d" * 48
HEADERS = {"X-Api-Key": KEY}
URL = "/api/device/summary"


def _enable():
    db.set_setting(app_module.DEVICE_API_ENABLED_SETTING, "1")
    db.set_setting(app_module.DEVICE_API_KEY_SETTING, KEY)


def _clean_slate():
    """The seeded example services would otherwise be part of every count."""
    for service in db.list_services():
        db.delete_service(service["id"])


def _get(client, query="", headers=HEADERS):
    resp = client.get(URL + query, headers=headers)
    return resp


def _summary(client, query=""):
    resp = _get(client, query)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return json.loads(resp.get_data(as_text=True))


@pytest.fixture
def enabled(client):
    _enable()
    _clean_slate()
    return client


# ---------------------------------------------------------------------------
# Whether it exists, and who may read it
# ---------------------------------------------------------------------------
def test_the_endpoint_is_off_by_default(client):
    """Off until an admin switches it on: it publishes data the public pages hide, so
    it has to be a deliberate choice. 404 - the answer an install that never had the
    feature gives - even for a request that carries a header."""
    assert client.get(URL).status_code == 404
    assert client.get(URL, headers=HEADERS).status_code == 404


def test_enabled_with_no_key_still_404s(client):
    """A switch with no key behind it would answer 401 to everybody, which still tells a
    stranger the feature is there."""
    db.set_setting(app_module.DEVICE_API_ENABLED_SETTING, "1")
    assert client.get(URL, headers=HEADERS).status_code == 404
    assert app_module.device_api_enabled() is False


def test_a_key_alone_does_not_turn_it_on(client):
    """Disabling keeps the key (so re-enabling reconnects the same device) but must
    close the endpoint."""
    db.set_setting(app_module.DEVICE_API_KEY_SETTING, KEY)
    db.set_setting(app_module.DEVICE_API_ENABLED_SETTING, "0")
    assert client.get(URL, headers=HEADERS).status_code == 404


@pytest.mark.parametrize("headers", [
    {},
    {"X-Api-Key": ""},
    {"X-Api-Key": "wrong"},
    {"X-Api-Key": KEY[:-1]},
    {"X-Api-Key": KEY + "x"},
    {"Authorization": "Bearer " + KEY},
])
def test_a_missing_or_wrong_key_is_refused_with_no_data(enabled, headers):
    resp = enabled.get(URL, headers=headers)
    assert resp.status_code == 401
    assert resp.get_json() == {"error": "Missing or invalid API key"}


def test_the_key_must_be_in_the_header_not_the_query_string(enabled):
    """A key in a URL ends up in access logs, proxy logs and browser history."""
    assert enabled.get(f"{URL}?key={KEY}").status_code == 401
    assert enabled.get(f"{URL}?api_key={KEY}").status_code == 401


def test_the_notification_key_does_not_open_the_display_api(enabled):
    """Two secrets, two capabilities: the one that makes the portal post to Discord and
    send email must not also read the resource data, and the reverse."""
    db.set_setting(app_module.NOTIFY_API_KEY_SETTING, "n" * 48)
    assert enabled.get(URL, headers={"X-Api-Key": "n" * 48}).status_code == 401
    resp = enabled.post("/api/notify/admin", json={"subject": "s", "body": "b"}, headers=HEADERS)
    assert resp.status_code == 401


def test_only_get_is_accepted(enabled):
    assert enabled.post(URL, headers=HEADERS).status_code == 405


def test_hidden_resources_need_the_key_and_come_with_it(enabled, monkeypatch):
    """The owner's rule: resource data the admin hid on the public pages must only be
    reachable with the key. Every show_public_* switch is off here (the default), the
    public resources page 404s, and the keyed endpoint still returns the numbers."""
    for key in app_module._PUBLIC_RESOURCE_KEYS:
        db.set_setting(key, "0")
    assert enabled.get("/resources").status_code == 404
    assert enabled.get(URL + "?sections=resources").status_code == 401
    assert "cpu" not in enabled.get(URL + "?sections=resources").get_data(as_text=True)

    body = _summary(enabled, "?sections=resources")
    assert body["resources"]["cpu"] is not None
    assert body["resources"]["mem"] is not None


def test_the_response_is_not_cacheable_and_is_declared_utf8(enabled):
    resp = _get(enabled)
    assert resp.headers["Cache-Control"] == "no-store"
    assert resp.headers["Content-Type"] == "application/json; charset=utf-8"
    assert int(resp.headers["Content-Length"]) == len(resp.data)


def test_nothing_that_maps_the_private_network_is_in_a_response(enabled):
    """check_url is the LAN address the portal probes. /api/status already strips it;
    this endpoint never carries a service row at all, only a name and a status."""
    db.create_service({"name": "Secret box", "url": "http://10.9.8.7", "check_url": "http://10.9.8.7:8096",
                       "run_target": "vm:hidden-vm", "status": "down"})
    body = _get(enabled).get_data(as_text=True)
    assert "Secret box" in body
    assert "10.9.8.7" not in body and "hidden-vm" not in body


# ---------------------------------------------------------------------------
# The header, and the sections filter
# ---------------------------------------------------------------------------
def test_the_header_is_always_present(enabled):
    db.set_setting("site_name", "My Rack")
    body = _summary(enabled, "?sections=services")
    assert body["v"] == device_api.SCHEMA_VERSION == 1
    assert body["site"] == "My Rack"
    assert body["overall"] == "operational"
    assert body["now"].endswith("Z") and len(body["now"]) == 20


def test_by_default_every_section_is_returned(enabled):
    assert [k for k in _summary(enabled) if k not in ("v", "now", "site", "overall")] == \
        list(device_api.SECTIONS)


@pytest.mark.parametrize("query", ["", "?sections=", "?sections=%20,%20"])
def test_a_blank_sections_parameter_means_all(enabled, query):
    assert set(_summary(enabled, query)) >= set(device_api.SECTIONS)


def test_sections_limits_the_response_to_what_was_asked(enabled):
    body = _summary(enabled, "?sections=announcements,services")
    assert set(body) == {"v", "now", "site", "overall", "services", "announcements"}


def test_sections_is_case_and_space_tolerant_and_unknown_names_are_ignored(enabled):
    body = _summary(enabled, "?sections=Services,%20bogus,INCIDENTS")
    assert set(body) == {"v", "now", "site", "overall", "services", "incidents"}


def test_a_request_naming_only_unknown_sections_is_a_400(enabled):
    """An answer that is just a header, for a typo, would look like a quiet portal."""
    resp = _get(enabled, "?sections=nope,nothing")
    assert resp.status_code == 400
    assert resp.get_json()["valid"] == list(device_api.ALL_SECTIONS)


def test_a_section_nobody_asked_for_costs_nothing(enabled, monkeypatch):
    """The resource snapshot is the most expensive thing a public page does; a display
    that only wants the status must not pay for it."""
    def boom():
        raise AssertionError("the resource snapshot was taken for a request that did not ask for it")
    monkeypatch.setattr(monitoring, "get_resource_snapshot", boom)
    assert _get(enabled, "?sections=services,incidents,maintenance,announcements").status_code == 200


def test_a_failing_resource_snapshot_degrades_to_null_not_a_500(enabled, monkeypatch):
    def boom():
        raise OSError("sleeping drive")
    monkeypatch.setattr(monitoring, "get_resource_snapshot", boom)
    body = _summary(enabled)
    assert body["resources"] is None
    assert body["services"]["total"] == 0  # the other sections are unaffected


# ---------------------------------------------------------------------------
# services
# ---------------------------------------------------------------------------
def test_services_counts_every_status_including_slow(enabled):
    for i, status in enumerate(["operational", "operational", "slow", "degraded",
                                "maintenance", "down", "down"]):
        db.create_service({"name": f"S{i}", "url": "", "status": status})
    section = _summary(enabled, "?sections=services")["services"]
    assert {k: section[k] for k in ("total", "operational", "slow", "degraded", "maintenance", "down")} == \
        {"total": 7, "operational": 2, "slow": 1, "degraded": 1, "maintenance": 1, "down": 2}


def test_services_lists_only_the_unhealthy_worst_first(enabled):
    for name, status in [("Fine", "operational"), ("Sluggish", "slow"), ("Hurting", "degraded"),
                         ("Dead", "down"), ("Serviced", "maintenance"), ("Also dead", "down")]:
        db.create_service({"name": name, "url": "", "status": status})
    items = _summary(enabled, "?sections=services")["services"]["items"]
    assert [(i["name"], i["status"]) for i in items] == [
        ("Dead", "down"), ("Also dead", "down"), ("Hurting", "degraded"),
        ("Serviced", "maintenance"), ("Sluggish", "slow")]


def test_the_overall_status_is_the_portals_own_precedence(enabled):
    """Reused from compute_overall_status(), not re-derived: slow ranks below
    maintenance and above operational."""
    db.create_service({"name": "A", "url": "", "status": "slow"})
    assert _summary(enabled)["overall"] == "slow"
    db.create_service({"name": "B", "url": "", "status": "maintenance"})
    assert _summary(enabled)["overall"] == "maintenance"
    db.create_service({"name": "C", "url": "", "status": "degraded"})
    assert _summary(enabled)["overall"] == "degraded"
    db.create_service({"name": "D", "url": "", "status": "down"})
    assert _summary(enabled)["overall"] == "down"


def test_a_service_ignored_in_the_overall_status_is_ignored_here_too(enabled):
    """A device that said "all up" above a list naming a service the admin deliberately
    muted would contradict itself."""
    db.create_service({"name": "Flaky test box", "url": "", "status": "down",
                       "ignore_in_overall_status": 1})
    db.create_service({"name": "Real", "url": "", "status": "operational"})
    body = _summary(enabled)
    assert body["overall"] == "operational"
    assert body["services"]["total"] == 1 and body["services"]["down"] == 0
    assert body["services"]["items"] == []


def test_the_service_list_is_capped_and_the_counts_still_tell_the_truth(enabled):
    for i in range(30):
        db.create_service({"name": f"Down {i}", "url": "", "status": "down"})
    section = _summary(enabled, "?sections=services")["services"]
    assert len(section["items"]) == device_api.SERVICE_ITEMS
    assert section["down"] == 30 and section["total"] == 30


# ---------------------------------------------------------------------------
# incidents and maintenance
# ---------------------------------------------------------------------------
def test_incidents_are_the_open_ones_only(enabled):
    sid = db.create_service({"name": "Jellyfin", "url": ""})
    open_id = db.create_incident({"title": "Still broken", "status": "identified"}, service_ids=[sid])
    done = db.create_incident({"title": "Fixed already"})
    db.update_incident(done, {"title": "Fixed already", "status": "resolved"})
    section = _summary(enabled, "?sections=incidents")["incidents"]
    assert section["open"] == 1
    assert [i["title"] for i in section["items"]] == ["Still broken"]
    item = section["items"][0]
    assert item["status"] == "identified" and item["services"] == "Jellyfin"
    assert item["since"].endswith("Z") and len(item["since"]) == 20
    assert open_id  # the row exists


def test_an_old_open_incident_is_not_pushed_out_by_newer_resolved_ones(enabled):
    """Taking the newest N and filtering afterwards would drop exactly the long-running
    incident a status display most needs to show."""
    old_open = db.create_incident({"title": "Open since forever"})
    for i in range(8):
        rid = db.create_incident({"title": f"Resolved {i}"})
        db.update_incident(rid, {"title": f"Resolved {i}", "status": "resolved"})
    section = _summary(enabled, "?sections=incidents")["incidents"]
    assert section["open"] == 1
    assert [i["title"] for i in section["items"]] == ["Open since forever"]
    assert old_open


def test_incident_list_is_capped_newest_first_and_the_count_is_the_total(enabled):
    for i in range(10):
        db.create_incident({"title": f"Incident {i}"})
    section = _summary(enabled, "?sections=incidents")["incidents"]
    assert section["open"] == 10
    assert [i["title"] for i in section["items"]] == ["Incident 9", "Incident 8", "Incident 7"]


def test_maintenance_lists_windows_in_progress_before_upcoming_ones(enabled):
    a = db.create_service({"name": "A", "url": ""})
    b = db.create_service({"name": "B", "url": ""})
    db.create_maintenance_window({"title": "Later", "starts_at": "2099-01-01T00:00",
                                  "ends_at": "2099-01-02T00:00"}, service_ids=[a, b])
    db.create_maintenance_window({"title": "Now", "starts_at": "2000-01-01T00:00",
                                  "ends_at": "2099-06-01T00:00"}, service_ids=[b])
    db.process_maintenance_windows()  # the health-check loop does this each cycle
    section = _summary(enabled, "?sections=maintenance")["maintenance"]
    assert (section["active"], section["upcoming"]) == (1, 1)
    first, second = section["items"]
    assert (first["title"], first["state"], first["services"]) == ("Now", "active", "B")
    assert (second["title"], second["state"], second["services"]) == ("Later", "upcoming", "A, B")


def test_maintenance_times_are_normalised_to_utc_iso(enabled):
    """The form stores naive datetime-local values (documented as UTC); the device gets
    the same unambiguous Z form as every other timestamp."""
    sid = db.create_service({"name": "A", "url": ""})
    db.create_maintenance_window({"title": "W", "starts_at": "2099-01-01T22:30",
                                  "ends_at": "2099-01-02T01:00"}, service_ids=[sid])
    item = _summary(enabled, "?sections=maintenance")["maintenance"]["items"][0]
    assert (item["starts"], item["ends"]) == ("2099-01-01T22:30:00Z", "2099-01-02T01:00:00Z")


def test_maintenance_is_capped_and_counts_everything(enabled):
    sid = db.create_service({"name": "A", "url": ""})
    for i in range(9):
        db.create_maintenance_window({"title": f"W{i}", "starts_at": f"2099-01-0{i + 1}T00:00",
                                      "ends_at": f"2099-02-0{i + 1}T00:00"}, service_ids=[sid])
    section = _summary(enabled, "?sections=maintenance")["maintenance"]
    assert section["upcoming"] == 9 and len(section["items"]) == device_api.MAINTENANCE_ITEMS
    assert [i["title"] for i in section["items"]] == ["W0", "W1", "W2"]  # soonest first


# ---------------------------------------------------------------------------
# announcements
# ---------------------------------------------------------------------------
def test_only_announcements_showing_right_now_are_returned(enabled):
    """The scheduled one is unpublished content; leaking it to a device would be a leak
    like any other. Same rule as the five public consumers in CLAUDE.md."""
    db.create_announcement({"title": "Live", "message": "visible"})
    db.create_announcement({"title": "Later", "message": "x", "starts_at": "2099-01-01T00:00"})
    db.create_announcement({"title": "Gone", "message": "x", "ends_at": "2000-01-01T00:00"})
    section = _summary(enabled, "?sections=announcements")["announcements"]
    assert section["count"] == 1
    assert [a["title"] for a in section["items"]] == ["Live"]


def test_announcement_text_is_one_plain_line(enabled):
    db.create_announcement({"title": "Notice", "type": "warning", "pinned": 1,
                            "message": "Server **restarts**\n\ntonight   at 23:00"})
    item = _summary(enabled, "?sections=announcements")["announcements"]["items"][0]
    assert item["text"] == "Server restarts tonight at 23:00"
    assert item["type"] == "warning" and item["pinned"] is True


def test_announcements_are_capped_pinned_first(enabled):
    for i in range(6):
        db.create_announcement({"title": f"N{i}", "message": "m", "pinned": 1 if i == 0 else 0})
    section = _summary(enabled, "?sections=announcements")["announcements"]
    assert section["count"] == 6 and len(section["items"]) == device_api.ANNOUNCEMENT_ITEMS
    assert section["items"][0]["title"] == "N0"


# ---------------------------------------------------------------------------
# resources
# ---------------------------------------------------------------------------
def _snapshot(**overrides):
    snapshot = {
        "cpu_percent": 91.5, "cpu_severity": "crit", "cpu_temp_c": None,
        "mem_percent": 40.0, "mem_severity": "ok", "mem_used_gb": 6.4, "mem_total_gb": 16.0,
        "network": {"up_mb_s": 0.5, "down_mb_s": 2.25},
        "disks": [
            {"path": "/", "label": "", "percent": 20.0, "severity": "ok", "free_gb": 400.0},
            {"path": "D:\\", "label": "Media library with a long name", "percent": 93.1,
             "severity": "crit", "free_gb": 12.0},
        ],
        "gpus": [],
    }
    snapshot.update(overrides)
    return snapshot


def test_resources_carry_the_portals_own_severity_and_the_fullest_disks_first(enabled, monkeypatch):
    monkeypatch.setattr(monitoring, "get_resource_snapshot", lambda: _snapshot())
    res = _summary(enabled, "?sections=resources")["resources"]
    assert (res["cpu"], res["cpu_sev"], res["cpu_temp_c"]) == (91.5, "crit", None)
    assert (res["mem"], res["mem_sev"], res["mem_used_gb"], res["mem_total_gb"]) == (40.0, "ok", 6.4, 16.0)
    assert (res["net_up_mb_s"], res["net_down_mb_s"]) == (0.5, 2.25)
    assert res["disk_count"] == 2
    assert [(d["pct"], d["sev"]) for d in res["disks"]] == [(93.1, "crit"), (20.0, "ok")]
    # A label wins over the path, and is cut to what a 240px screen can draw.
    assert res["disks"][0]["name"].endswith("...") and len(res["disks"][0]["name"].encode()) <= 16
    assert res["disks"][1]["name"] == "/"


def test_resources_with_no_network_reading_yet_say_null(enabled, monkeypatch):
    """The first reading after start has no previous one to take a rate against."""
    monkeypatch.setattr(monitoring, "get_resource_snapshot", lambda: _snapshot(network=None))
    res = _summary(enabled, "?sections=resources")["resources"]
    assert res["net_up_mb_s"] is None and res["net_down_mb_s"] is None


def test_resource_disks_are_capped(enabled, monkeypatch):
    disks = [{"path": f"/m{i}", "label": "", "percent": float(i), "severity": "ok", "free_gb": 1.0}
             for i in range(12)]
    monkeypatch.setattr(monitoring, "get_resource_snapshot", lambda: _snapshot(disks=disks))
    res = _summary(enabled, "?sections=resources")["resources"]
    assert res["disk_count"] == 12 and len(res["disks"]) == device_api.DISK_ITEMS
    assert res["disks"][0]["pct"] == 11.0


def test_a_nan_reading_becomes_null_rather_than_invalid_json(enabled, monkeypatch):
    monkeypatch.setattr(monitoring, "get_resource_snapshot",
                        lambda: _snapshot(cpu_percent=float("nan"), mem_percent=float("inf")))
    res = _summary(enabled, "?sections=resources")["resources"]
    assert res["cpu"] is None and res["mem"] is None


# ---------------------------------------------------------------------------
# Size: bounded by construction, proven against the worst case
# ---------------------------------------------------------------------------
def _worst_case_text(length=200):
    """Characters that cost the most once serialised: a double quote and a backslash
    each become two bytes of JSON, a four-byte emoji stays four, and raw control
    characters have to be dropped rather than escaped to six bytes each."""
    return ('"\\' * (length // 2)) + "\U0001F4A5" * 10 + "\x00\x07\u2028"


def test_the_worst_possible_response_is_under_the_hard_cap(enabled, monkeypatch):
    nasty = _worst_case_text()
    for i in range(40):
        db.create_service({"name": nasty, "url": "", "status": "down"})
    sid = db.create_service({"name": nasty, "url": "", "status": "down"})
    for i in range(10):
        db.create_incident({"title": nasty}, service_ids=[sid])
        db.create_announcement({"title": nasty, "message": nasty, "type": "critical", "pinned": 1})
        db.create_maintenance_window({"title": nasty, "starts_at": f"2099-01-{i + 10}T00:00",
                                      "ends_at": f"2099-02-{i + 10}T00:00"}, service_ids=[sid])
    db.set_setting("site_name", nasty)
    disks = [{"path": nasty, "label": nasty, "percent": 99.9, "severity": "crit",
              "free_gb": 99999.9} for _ in range(10)]
    monkeypatch.setattr(monitoring, "get_resource_snapshot",
                        lambda: _snapshot(disks=disks, cpu_temp_c=99.9, mem_used_gb=9999.9,
                                          mem_total_gb=9999.9,
                                          network={"up_mb_s": 99999.99, "down_mb_s": 99999.99}))
    raw = _get(enabled).data
    assert len(raw) < device_api.MAX_BYTES, f"{len(raw)} bytes"
    body = json.loads(raw)  # still valid JSON after all of that
    for section in ("services", "incidents", "maintenance", "announcements"):
        assert body[section]["items"]


def test_a_typical_response_is_well_under_three_kilobytes(enabled, monkeypatch):
    for i in range(12):
        db.create_service({"name": f"Service number {i}", "url": "",
                           "status": ("down", "degraded", "slow", "operational")[i % 4]})
    sid = db.list_services()[0]["id"]
    for i in range(3):
        db.create_incident({"title": f"Something is wrong with thing {i}"}, service_ids=[sid])
        db.create_announcement({"title": f"Announcement {i}", "message": "A sentence of ordinary length."})
        db.create_maintenance_window({"title": f"Window {i}", "starts_at": f"2099-01-0{i + 1}T00:00",
                                      "ends_at": f"2099-01-0{i + 1}T04:00"}, service_ids=[sid])
    monkeypatch.setattr(monitoring, "get_resource_snapshot", lambda: _snapshot())
    assert len(_get(enabled).data) < 3000


def test_the_documented_example_is_a_real_response_under_the_cap():
    """The admin page and the API contract show example_summary(); it is built through
    build_summary() so it cannot drift, and it has to respect the same bound."""
    example = device_api.example_summary()
    assert set(example) == {"v", "now", "site", "overall", *device_api.SECTIONS}
    assert len(device_api.dumps(example).encode()) < 3000


def test_non_ascii_is_sent_as_utf8_not_escapes(enabled):
    """The device folds UTF-8 to ASCII itself; \\uXXXX escapes would triple the size of
    every accented letter."""
    db.create_service({"name": "Caf\u00e9 serveur", "url": "", "status": "down"})
    raw = _get(enabled, "?sections=services").data
    assert "Caf\u00e9 serveur".encode("utf-8") in raw
    assert b"\\u" not in raw


def test_the_query_count_does_not_grow_with_the_data(enabled, monkeypatch):
    """Every per-item lookup is a grouped query. A new query inside a loop over services,
    incidents or windows shows up here as a difference."""
    def add(n, start):
        ids = [db.create_service({"name": f"S{start + i}", "url": "", "status": "down"}) for i in range(n)]
        for i, sid in enumerate(ids):
            db.create_incident({"title": f"I{start + i}"}, service_ids=[sid])
            db.create_maintenance_window({"title": f"W{start + i}", "starts_at": "2099-01-01T00:00",
                                          "ends_at": "2099-01-02T00:00"}, service_ids=[sid])
            db.create_announcement({"title": f"A{start + i}", "message": "m"})

    def statements():
        real_connect = sqlite3.connect
        seen = []

        def tracing(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            conn.set_trace_callback(seen.append)
            return conn

        with monkeypatch.context() as m:
            m.setattr(sqlite3, "connect", tracing)
            assert enabled.get(URL + "?sections=services,incidents,maintenance,announcements",
                               headers=HEADERS).status_code == 200
        return len(seen)

    add(3, 0)
    few = statements()
    add(12, 100)
    assert statements() == few


# ---------------------------------------------------------------------------
# The pure helpers
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw, expected", [
    (None, list(device_api.SECTIONS)),
    ("", list(device_api.SECTIONS)),
    ("services", ["services"]),
    ("resources,services", ["services", "resources"]),   # canonical order, not request order
    ("services,services", ["services"]),
    ("services,bogus", ["services"]),
    ("bogus", None),
])
def test_parse_sections(raw, expected):
    assert device_api.parse_sections(raw) == expected


def test_text_collapses_whitespace_and_drops_control_characters():
    assert device_api.text("  a\n\tb \x00 c\u00a0d ", 40) == "a b c d"
    assert device_api.text(None, 10) == ""


def test_text_cuts_on_bytes_never_inside_a_character():
    cut = device_api.text("\u00e9" * 30, 10)             # two bytes each
    assert cut.endswith("...") and len(cut.encode("utf-8")) <= 10
    cut.encode("utf-8").decode("utf-8")                    # still valid
    emoji = device_api.text("\U0001F4A5" * 10, 10)         # four bytes each
    assert len(emoji.encode("utf-8")) <= 10 and emoji.endswith("...")
    assert device_api.text("short", 10) == "short"
    assert device_api.text("exactly10!", 10) == "exactly10!"


@pytest.mark.parametrize("raw, expected", [
    ("2026-10-06T11:40:12.345678+00:00", "2026-10-06T11:40:12Z"),
    ("2026-10-06T14:30", "2026-10-06T14:30:00Z"),            # naive: the forms say UTC
    ("2026-10-06T14:30:00+02:00", "2026-10-06T12:30:00Z"),
    ("2026-10-06T14:30:00Z", "2026-10-06T14:30:00Z"),
    ("", ""), (None, ""), ("not a date", ""),
])
def test_iso_utc(raw, expected):
    assert device_api.iso_utc(raw) == expected


def test_the_count_order_and_the_rank_map_cover_the_same_statuses():
    assert set(device_api.COUNT_ORDER) == set(device_api.STATUS_RANK)


# ---------------------------------------------------------------------------
# The admin page: /admin/device
# ---------------------------------------------------------------------------
def _login(client):
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})


def test_the_admin_page_needs_a_login(client):
    assert client.get("/admin/device").status_code == 302
    assert client.post("/admin/device", data={"device_api_enabled": "on"}).status_code == 302
    assert client.post("/admin/device/key/regenerate").status_code == 302
    assert db.get_setting(app_module.DEVICE_API_KEY_SETTING, "") == ""


def test_the_admin_page_renders_with_matching_title_heading_and_nav_label(client):
    """CLAUDE.md: an admin page's <h1>, its title and its nav label must all agree."""
    _login(client)
    body = client.get("/admin/device").get_data(as_text=True)
    assert "<title>Display device" in body
    assert "<h1>Display device</h1>" in body
    nav = body[body.index('<nav class="admin-nav">'):body.index("</nav>")]
    assert ">Display device</a>" in nav and "class=\"active\"" in nav.split("Display device")[0][-120:]


def test_the_page_says_how_to_configure_the_display_and_shows_a_real_example(client):
    _login(client)
    body = client.get("/admin/device").get_data(as_text=True)
    assert f"http://192.168.1.10:{app_module.config.PORT}" in body
    assert "X-Api-Key" in body and "/api/device/summary" in body
    assert "even where the public pages hide them" in body
    # The example is the real builder's output, not typed text.
    pretty, size = device_api.example_json()
    assert str(escape(pretty.splitlines()[1].strip())) in body
    assert f"{size} bytes" in body


def test_before_anything_is_set_up_the_page_offers_a_key_and_the_api_is_off(client):
    _login(client)
    body = client.get("/admin/device").get_data(as_text=True)
    assert "No key generated yet" in body and "Generate key" in body
    assert client.get(URL, headers=HEADERS).status_code == 404


def test_enabling_for_the_first_time_generates_a_key_and_the_endpoint_goes_live(client):
    _login(client)
    resp = client.post("/admin/device", data={"device_api_enabled": "on"})
    assert resp.status_code == 302
    key = db.get_setting(app_module.DEVICE_API_KEY_SETTING, "")
    assert len(key) == 48
    assert key in client.get("/admin/device").get_data(as_text=True)
    assert client.get(URL, headers={"X-Api-Key": key}).status_code == 200


def test_disabling_closes_the_endpoint_but_keeps_the_key(client):
    _login(client)
    client.post("/admin/device", data={"device_api_enabled": "on"})
    key = db.get_setting(app_module.DEVICE_API_KEY_SETTING, "")
    client.post("/admin/device", data={})                  # an unticked box sends nothing
    assert client.get(URL, headers={"X-Api-Key": key}).status_code == 404
    assert db.get_setting(app_module.DEVICE_API_KEY_SETTING, "") == key
    client.post("/admin/device", data={"device_api_enabled": "on"})
    assert db.get_setting(app_module.DEVICE_API_KEY_SETTING, "") == key   # same device reconnects
    assert client.get(URL, headers={"X-Api-Key": key}).status_code == 200


def test_regenerating_the_key_kills_the_old_one_and_leaves_the_notification_key_alone(client):
    _login(client)
    client.post("/admin/device", data={"device_api_enabled": "on"})
    first = db.get_setting(app_module.DEVICE_API_KEY_SETTING, "")
    db.set_setting(app_module.NOTIFY_API_KEY_SETTING, "n" * 48)

    client.post("/admin/device/key/regenerate")
    second = db.get_setting(app_module.DEVICE_API_KEY_SETTING, "")
    assert second and second != first
    assert client.get(URL, headers={"X-Api-Key": first}).status_code == 401
    assert client.get(URL, headers={"X-Api-Key": second}).status_code == 200
    assert db.get_setting(app_module.NOTIFY_API_KEY_SETTING, "") == "n" * 48


def test_the_two_keys_are_generated_independently(client):
    _login(client)
    client.post("/admin/notifications/api-key/regenerate")
    client.post("/admin/device", data={"device_api_enabled": "on"})
    assert (db.get_setting(app_module.NOTIFY_API_KEY_SETTING, "")
            != db.get_setting(app_module.DEVICE_API_KEY_SETTING, ""))


def test_the_regenerate_confirm_is_a_constant_not_an_interpolated_value(client):
    """The inline-handler convention: nothing from outside goes inside an on*= attribute.
    The confirm text here is fixed, and only present once there is a key to lose."""
    _login(client)
    assert "onsubmit" not in client.get("/admin/device").get_data(as_text=True)
    client.post("/admin/device", data={"device_api_enabled": "on"})
    assert "onsubmit=\"return confirm(" in client.get("/admin/device").get_data(as_text=True)


def test_the_admin_search_finds_the_page_and_its_controls():
    assert any(r["endpoint"] == "admin_device" for r in admin_search.search("display device"))
    assert any(r["endpoint"] == "admin_device" and r["jump"] == "device_api_enabled"
               for r in admin_search.search("enable display device api"))


def test_services_all_lists_every_service_worst_first(enabled):
    for name, status in [("Fine", "operational"), ("Dead", "down"), ("Sluggish", "slow"),
                         ("Also fine", "operational")]:
        db.create_service({"name": name, "url": "", "status": status})
    items = _summary(enabled, "?sections=services&services=all")["services"]["items"]
    assert [(i["name"], i["status"]) for i in items] == [
        ("Dead", "down"), ("Sluggish", "slow"), ("Fine", "operational"), ("Also fine", "operational")]
    # Without it the list is still the unhealthy ones only.
    assert len(_summary(enabled, "?sections=services")["services"]["items"]) == 2


def test_services_all_is_capped_and_the_worst_case_fits_its_ceiling(enabled, monkeypatch):
    nasty = _worst_case_text()
    for i in range(60):
        db.create_service({"name": nasty, "url": "", "status": "operational"})
    sid = db.create_service({"name": nasty, "url": "", "status": "down"})
    for i in range(10):
        db.create_incident({"title": nasty}, service_ids=[sid])
        db.create_announcement({"title": nasty, "message": nasty, "type": "critical", "pinned": 1})
        db.create_maintenance_window({"title": nasty, "starts_at": f"2099-01-{i + 10}T00:00",
                                      "ends_at": f"2099-02-{i + 10}T00:00"}, service_ids=[sid])
    db.set_setting("site_name", nasty)
    disks = [{"path": nasty, "label": nasty, "percent": 99.9, "severity": "crit",
              "free_gb": 99999.9} for _ in range(10)]
    monkeypatch.setattr(monitoring, "get_resource_snapshot",
                        lambda: _snapshot(disks=disks, cpu_temp_c=99.9, mem_used_gb=9999.9,
                                          mem_total_gb=9999.9,
                                          network={"up_mb_s": 99999.99, "down_mb_s": 99999.99}))
    raw = _get(enabled, "?services=all").data
    assert len(raw) < device_api.MAX_BYTES_ALL_SERVICES, f"{len(raw)} bytes"
    body = json.loads(raw)
    assert len(body["services"]["items"]) == device_api.ALL_SERVICE_ITEMS
    assert body["services"]["items"][0]["status"] == "down"


# ---------------------------------------------------------------------------
# resources=all: every disk and the GPUs, for a device that pages through them
# ---------------------------------------------------------------------------
def _gpu(i=0, **overrides):
    gpu = {"name": f"NVIDIA GeForce RTX {3000 + i}", "util_percent": 37, "severity": "ok",
           "mem_used_gb": 4.2, "mem_total_gb": 10.0, "temp_c": 61}
    gpu.update(overrides)
    return gpu


def test_resources_without_all_are_what_1_10_sent(enabled, monkeypatch):
    """No `resources=all`, no GPUs and no extra disks: a firmware that never asked is not
    handed a bigger body than it sized its buffer for."""
    disks = [{"path": f"/m{i}", "label": "", "percent": float(i), "severity": "ok", "free_gb": 1.0}
             for i in range(12)]
    monkeypatch.setattr(monitoring, "get_resource_snapshot",
                        lambda: _snapshot(disks=disks, gpus=[_gpu()]))
    res = _summary(enabled, "?sections=resources")["resources"]
    assert "gpus" not in res and "gpu_count" not in res
    assert res["disk_count"] == 12 and len(res["disks"]) == device_api.DISK_ITEMS


def test_resources_all_lists_more_disks_and_the_gpus(enabled, monkeypatch):
    disks = [{"path": f"/m{i}", "label": "", "percent": float(i), "severity": "ok", "free_gb": 1.0}
             for i in range(12)]
    monkeypatch.setattr(monitoring, "get_resource_snapshot",
                        lambda: _snapshot(disks=disks, gpus=[_gpu(0, severity="crit", util_percent=91),
                                                             _gpu(1, temp_c=None)]))
    res = _summary(enabled, "?sections=resources&resources=all")["resources"]
    assert res["disk_count"] == 12 and len(res["disks"]) == device_api.ALL_DISK_ITEMS
    assert res["disks"][0]["pct"] == 11.0   # still the fullest first
    assert res["gpu_count"] == 2
    assert res["gpus"][0]["name"] == "RTX 3000"   # the vendor words are dropped to fit a 240px screen
    assert (res["gpus"][0]["mem_used_gb"], res["gpus"][0]["mem_total_gb"], res["gpus"][0]["temp_c"]) == (4.2, 10.0, 61)
    assert (res["gpus"][0]["pct"], res["gpus"][0]["sev"]) == (91, "crit")
    assert res["gpus"][1]["temp_c"] is None   # a card that exposes no temperature


def test_resources_all_with_no_gpu_says_so(enabled, monkeypatch):
    monkeypatch.setattr(monitoring, "get_resource_snapshot", lambda: _snapshot(gpus=[]))
    res = _summary(enabled, "?sections=resources&resources=all")["resources"]
    assert res["gpu_count"] == 0 and res["gpus"] == []


def test_resources_all_is_capped_in_gpus(enabled, monkeypatch):
    monkeypatch.setattr(monitoring, "get_resource_snapshot",
                        lambda: _snapshot(gpus=[_gpu(i) for i in range(9)]))
    res = _summary(enabled, "?sections=resources&resources=all")["resources"]
    assert res["gpu_count"] == 9 and len(res["gpus"]) == device_api.GPU_ITEMS


def test_an_unknown_resources_value_is_ignored(enabled, monkeypatch):
    monkeypatch.setattr(monitoring, "get_resource_snapshot", lambda: _snapshot(gpus=[_gpu()]))
    assert "gpus" not in _summary(enabled, "?sections=resources&resources=everything")["resources"]


def test_a_gpu_without_a_severity_reads_null_rather_than_failing(enabled, monkeypatch):
    """A GPU snapshot cached by an older portal build carries no `severity`."""
    gpu = _gpu()
    del gpu["severity"]
    monkeypatch.setattr(monitoring, "get_resource_snapshot", lambda: _snapshot(gpus=[gpu]))
    assert _summary(enabled, "?sections=resources&resources=all")["resources"]["gpus"][0]["sev"] is None


def test_the_largest_possible_answer_fits_its_ceiling(enabled, monkeypatch):
    """Every list full, `services=all` and `resources=all` together, and every string made
    of what costs most once serialised: the answer a device with the biggest request can
    receive, and the number it sizes its buffer by."""
    nasty = _worst_case_text()
    for i in range(60):
        db.create_service({"name": nasty, "url": "", "status": "operational"})
    sid = db.create_service({"name": nasty, "url": "", "status": "down"})
    for i in range(10):
        db.create_incident({"title": nasty}, service_ids=[sid])
        db.create_announcement({"title": nasty, "message": nasty, "type": "critical", "pinned": 1})
        db.create_maintenance_window({"title": nasty, "starts_at": f"2099-01-{i + 10}T00:00",
                                      "ends_at": f"2099-02-{i + 10}T00:00"}, service_ids=[sid])
    db.set_setting("site_name", nasty)
    disks = [{"path": nasty, "label": nasty, "percent": 99.9, "severity": "crit",
              "free_gb": 99999.9} for _ in range(20)]
    gpus = [_gpu(i, name=nasty, util_percent=99.9, mem_used_gb=9999.9, mem_total_gb=9999.9,
                 temp_c=99.9) for i in range(10)]
    monkeypatch.setattr(monitoring, "get_resource_snapshot",
                        lambda: _snapshot(disks=disks, gpus=gpus, cpu_temp_c=99.9, mem_used_gb=9999.9,
                                          mem_total_gb=9999.9,
                                          network={"up_mb_s": 99999.99, "down_mb_s": 99999.99}))
    for service in db.list_services():
        if service["status"] == "operational":
            db.update_service_status_from_check(service["id"], "operational", 99999)
    monkeypatch.setattr(integrations, "get_cached_jellyfin_activity",
                        lambda: {"transcoding": 99, "running_tasks": [nasty] * 10})
    raw = _get(enabled, "?services=all&resources=all&jellyfin=1").data
    print("LARGEST", len(raw))
    assert len(raw) < device_api.MAX_BYTES_ALL, f"{len(raw)} bytes"
    body = json.loads(raw)
    assert len(body["resources"]["disks"]) == device_api.ALL_DISK_ITEMS
    assert len(body["resources"]["gpus"]) == device_api.GPU_ITEMS
    assert len(body["jellyfin"]["tasks"]) == device_api.JELLYFIN_TASK_ITEMS
    assert len(body["services"]["items"]) == device_api.ALL_SERVICE_ITEMS
    assert body["services"]["items"][-1]["ms"] == 99999
    # Asking for only one of the two stays under the smaller ceiling it had before.
    assert len(_get(enabled, "?services=all").data) < device_api.MAX_BYTES_ALL_SERVICES
    assert len(_get(enabled).data) < device_api.MAX_BYTES


@pytest.mark.parametrize("raw, expected", [
    ("NVIDIA GeForce RTX 3080 Ti", "RTX 3080 Ti"),
    ("NVIDIA RTX A4000", "RTX A4000"),
    ("Tesla T4", "Tesla T4"),
    ("NVIDIA GeForce", "GeForce"),   # only a whole leading word is dropped
    ("NVIDIA GeForce RTX 4090 Laptop GPU Founders", "RTX 4090 Laptop G..."),
    ("", ""),
])
def test_gpu_names_drop_the_vendor_words_and_fit(raw, expected):
    assert device_api.gpu_name(raw) == expected
    assert len(device_api.gpu_name(raw).encode()) <= device_api.GPU_NAME_BYTES


# ---------------------------------------------------------------------------
# Latency beside the services, and Jellyfin's activity (both only with =all)
# ---------------------------------------------------------------------------
def test_services_all_carry_the_last_latency_of_the_healthy_ones(enabled):
    fine = db.create_service({"name": "Fine", "url": "", "status": "operational"})
    slow = db.create_service({"name": "Sluggish", "url": "", "status": "slow"})
    down = db.create_service({"name": "Dead", "url": "", "status": "down"})
    unmeasured = db.create_service({"name": "Manual", "url": "", "status": "operational"})
    db.update_service_status_from_check(fine, "operational", 45)
    db.update_service_status_from_check(slow, "slow", 1850)
    db.update_service_status_from_check(down, "down", 30000)
    items = {i["name"]: i for i in _summary(enabled, "?sections=services&services=all")["services"]["items"]}
    assert items["Fine"]["ms"] == 45 and items["Sluggish"]["ms"] == 1850
    assert "ms" not in items["Dead"]         # a timeout is not a latency worth a line
    assert "ms" not in items["Manual"]       # nothing was ever measured: no 0 that reads as instant
    # Without services=all the items are exactly what 1.10.0 sent.
    plain = _summary(enabled, "?sections=services")["services"]["items"]
    assert all(set(i) == {"name", "status"} for i in plain)


def test_jellyfin_activity_is_in_the_header_and_only_when_asked_for(enabled, monkeypatch):
    monkeypatch.setattr(integrations, "get_cached_jellyfin_activity",
                        lambda: {"transcoding": 2, "running_tasks": ["Generate Trickplay Images", "Scan Media Library"]})
    # With any sections, even ones that have nothing to do with resources.
    body = _summary(enabled, "?sections=services&jellyfin=1")
    assert body["jellyfin"] == {"transcodes": 2, "tasks": ["Generate Trickplay Images", "Scan Media Library"]}
    assert "resources" not in body
    # Not asked for: the response is what 1.10.0 sent.
    assert "jellyfin" not in _summary(enabled, "?sections=services")
    assert "jellyfin" not in _summary(enabled, "?sections=resources&resources=all&services=all")
    assert list(body)[:5] == ["v", "now", "site", "overall", "jellyfin"]


def test_idle_jellyfin_says_so_and_a_portal_without_one_does_not_fail(enabled, monkeypatch):
    monkeypatch.setattr(integrations, "get_cached_jellyfin_activity",
                        lambda: {"transcoding": 0, "running_tasks": []})
    assert _summary(enabled, "?jellyfin=1")["jellyfin"] == {"transcodes": 0, "tasks": []}
    monkeypatch.setattr(integrations, "get_cached_jellyfin_activity", lambda: {})
    assert _summary(enabled, "?jellyfin=1")["jellyfin"] == {"transcodes": 0, "tasks": []}


def test_jellyfin_tasks_are_capped_and_cut(enabled, monkeypatch):
    monkeypatch.setattr(integrations, "get_cached_jellyfin_activity",
                        lambda: {"transcoding": 0, "running_tasks": [f"Task {i} " + "x" * 60 for i in range(9)]})
    tasks = _summary(enabled, "?jellyfin=1")["jellyfin"]["tasks"]
    assert len(tasks) == device_api.JELLYFIN_TASK_ITEMS
    assert all(len(t.encode()) <= device_api.JELLYFIN_TASK_BYTES and t.endswith("...") for t in tasks)


# ---------------------------------------------------------------------------
# sections=vms: the Hyper-V virtual machines, only for a request that names them
# ---------------------------------------------------------------------------
def _vm(name="Docker-Host", state="Running", uptime="3d 4h"):
    return {"name": name, "state": state, "uptime": uptime}


def _fake_vms(monkeypatch, vms):
    monkeypatch.setattr(monitoring, "get_cached_vm_snapshot", lambda: vms)


def test_vms_are_opt_in_the_default_answer_never_carries_them(enabled, monkeypatch):
    """The rule for every extension of this endpoint: a firmware that does not ask is handed
    exactly what it was before, so an old Status-ESP never receives a body bigger than the
    buffer it was sized for."""
    _fake_vms(monkeypatch, [_vm()])
    assert "vms" not in _summary(enabled)
    for query in ("", "?sections=", "?sections=services,incidents,maintenance,resources,announcements",
                  "?services=all&resources=all&jellyfin=1"):
        assert "vms" not in _summary(enabled, query), query
    assert device_api.parse_sections(None) == list(device_api.SECTIONS)
    assert "vms" not in device_api.SECTIONS and "vms" in device_api.OPT_IN_SECTIONS


def test_a_section_nobody_asked_for_never_reads_the_vm_list(enabled, monkeypatch):
    def boom():
        raise AssertionError("the VM list was read for a request that did not ask for it")
    monkeypatch.setattr(monitoring, "get_cached_vm_snapshot", boom)
    assert _get(enabled).status_code == 200
    assert _get(enabled, "?sections=services,resources").status_code == 200


def test_sections_vms_carries_the_count_the_running_ones_and_each_vm(enabled, monkeypatch):
    _fake_vms(monkeypatch, [_vm("Game-Server", "Running", "3h 20m"), _vm("Test-VM", "Off", "0m"),
                            _vm("Docker-Host", "Running", "12d 4h"), _vm("Old-VM", "Saved", "0m")])
    body = _summary(enabled, "?sections=vms")
    assert set(body) == {"v", "now", "site", "overall", "vms"}
    assert body["vms"]["total"] == 4 and body["vms"]["running"] == 2
    assert body["vms"]["items"] == [   # by name, whatever order Hyper-V listed them in
        {"name": "Docker-Host", "state": "Running", "up": "12d 4h"},
        {"name": "Game-Server", "state": "Running", "up": "3h 20m"},
        {"name": "Old-VM", "state": "Saved", "up": "0m"},
        {"name": "Test-VM", "state": "Off", "up": "0m"},
    ]


def test_vms_can_be_asked_for_together_with_other_sections(enabled, monkeypatch):
    _fake_vms(monkeypatch, [_vm()])
    body = _summary(enabled, "?sections=services,vms")
    assert list(body)[-2:] == ["services", "vms"]   # canonical order, opt-in sections last


def test_no_vms_is_an_empty_list_not_a_null(enabled, monkeypatch):
    """A portal that is not on a Hyper-V host answers fine and it was "none"; the device says
    so, rather than treating it as a failure to read."""
    _fake_vms(monkeypatch, [])
    assert _summary(enabled, "?sections=vms")["vms"] == {"total": 0, "running": 0, "items": []}


def test_a_failing_vm_read_degrades_to_null_not_a_500(enabled, monkeypatch):
    def boom():
        raise OSError("cache gone")
    monkeypatch.setattr(monitoring, "get_cached_vm_snapshot", boom)
    body = _summary(enabled, "?sections=services,vms")
    assert body["vms"] is None and "services" in body


def test_vms_are_returned_even_where_the_public_page_hides_them(enabled, monkeypatch):
    """Same rule as the resources: the public switch decides what visitors see, the key decides
    what the admin's own display sees."""
    db.set_setting("show_public_vms", "0")
    _fake_vms(monkeypatch, [_vm()])
    assert _summary(enabled, "?sections=vms")["vms"]["total"] == 1
    assert _get(enabled, "?sections=vms", headers={}).status_code == 401


def test_the_vm_list_is_capped_and_the_total_still_tells_the_truth(enabled, monkeypatch):
    _fake_vms(monkeypatch, [_vm(f"VM-{i:02d}") for i in range(30)])
    vms = _summary(enabled, "?sections=vms")["vms"]
    assert vms["total"] == 30 and vms["running"] == 30
    assert len(vms["items"]) == device_api.VM_ITEMS
    assert vms["items"][0]["name"] == "VM-00"


def test_a_vm_with_odd_fields_is_still_one_clean_line_each(enabled, monkeypatch):
    _fake_vms(monkeypatch, [{"name": "  A\nVM \t with   space ", "state": None, "uptime": "—"},
                            {"name": "x" * 200, "state": "Snapshotting-and-more-than-12", "uptime": 5}])
    items = _summary(enabled, "?sections=vms")["vms"]["items"]
    assert items[0] == {"name": "A VM with space", "state": "", "up": "—"}
    assert len(items[1]["name"].encode()) <= device_api.VM_NAME_BYTES
    assert len(items[1]["state"].encode()) <= device_api.VM_STATE_BYTES
    assert items[1]["up"] == "5"


def test_the_largest_possible_answer_with_vms_fits_its_ceiling(enabled, monkeypatch):
    """The largest request the display can make - every section, all three modifiers, and the
    VMs - against the same adversarial data as the test above, plus VMs whose every string is
    made of what costs most once serialised. This is the number the device sizes its buffer by."""
    nasty = _worst_case_text()
    for i in range(60):
        db.create_service({"name": nasty, "url": "", "status": "operational"})
    sid = db.create_service({"name": nasty, "url": "", "status": "down"})
    for i in range(10):
        db.create_incident({"title": nasty}, service_ids=[sid])
        db.create_announcement({"title": nasty, "message": nasty, "type": "critical", "pinned": 1})
        db.create_maintenance_window({"title": nasty, "starts_at": f"2099-01-{i + 10}T00:00",
                                      "ends_at": f"2099-02-{i + 10}T00:00"}, service_ids=[sid])
    db.set_setting("site_name", nasty)
    disks = [{"path": nasty, "label": nasty, "percent": 99.9, "severity": "crit",
              "free_gb": 99999.9} for _ in range(20)]
    gpus = [_gpu(i, name=nasty, util_percent=99.9, mem_used_gb=9999.9, mem_total_gb=9999.9,
                 temp_c=99.9) for i in range(10)]
    monkeypatch.setattr(monitoring, "get_resource_snapshot",
                        lambda: _snapshot(disks=disks, gpus=gpus, cpu_temp_c=99.9, mem_used_gb=9999.9,
                                          mem_total_gb=9999.9,
                                          network={"up_mb_s": 99999.99, "down_mb_s": 99999.99}))
    for service in db.list_services():
        if service["status"] == "operational":
            db.update_service_status_from_check(service["id"], "operational", 99999)
    monkeypatch.setattr(integrations, "get_cached_jellyfin_activity",
                        lambda: {"transcoding": 99, "running_tasks": [nasty] * 10})
    _fake_vms(monkeypatch, [{"name": '"' * 40 + str(i), "state": '"' * 40, "uptime": '"' * 40}
                            for i in range(40)])
    query = ("?sections=" + ",".join(device_api.ALL_SECTIONS)
             + "&services=all&resources=all&jellyfin=1")
    raw = _get(enabled, query).data
    print("LARGEST WITH VMS", len(raw))
    assert len(raw) < device_api.MAX_BYTES_ALL_WITH_VMS, f"{len(raw)} bytes"
    body = json.loads(raw)
    assert len(body["vms"]["items"]) == device_api.VM_ITEMS and body["vms"]["total"] == 40
    # The ceilings every older request keeps are untouched by the new section.
    assert len(_get(enabled, "?services=all&resources=all&jellyfin=1").data) < device_api.MAX_BYTES_ALL
    assert len(_get(enabled).data) < device_api.MAX_BYTES


def test_a_typical_vm_answer_is_small(enabled, monkeypatch):
    _fake_vms(monkeypatch, [_vm(f"Virtual-Machine-{i}", "Running" if i % 2 else "Off", "12d 23h")
                            for i in range(10)])
    assert len(_get(enabled, "?sections=vms").data) < 1100


def test_the_vm_example_is_a_real_answer(enabled):
    example = device_api.example_vms_summary()
    assert set(example) == {"v", "now", "site", "overall", "vms"}
    assert example["vms"]["running"] == 2 and example["vms"]["total"] == 3


def test_the_admin_page_documents_the_opt_in_vms_section_with_a_real_example(client):
    _login(client)
    html = client.get("/admin/device").data.decode()
    assert "<code>vms</code>" in html and "Only sent when you name it" in html
    assert "sections=vms" in html
    assert "Docker-Host" in html   # the example comes from example_vms_summary(), not hand-typed


# ---------------------------------------------------------------------------
# detail=full: the same answer with room to breathe, for a server (the Jelly Portal plugin)
# ---------------------------------------------------------------------------
# The query the plugin makes, spelled out here rather than imported: this is the contract.
PLUGIN_QUERY = "?sections=services,incidents,maintenance,announcements&services=all&detail=full"
PLUGIN_SECTIONS = ["services", "incidents", "maintenance", "announcements"]
# Keys only a detail=full answer may carry.
FULL_ONLY_KEYS = (b'"detail":', b'"service_names":', b'"description":')


def _without_now(raw):
    """An answer with the server clock cut out, so two requests a second apart compare."""
    return re.sub(rb'"now":"[^"]*",', b"", raw)


def _typical_snapshot(monkeypatch):
    monkeypatch.setattr(monitoring, "get_resource_snapshot", lambda: _snapshot())


def _seed_scenario():
    """A little of everything, with names long enough to tell the compact answer from the full one."""
    long_name = "Nextcloud family photos and documents archive"   # 45 bytes: cut at 24 when compact
    ids = {
        "jelly": db.create_service({"name": "Jellyfin", "url": "", "status": "operational"}),
        "cloud": db.create_service({"name": long_name, "url": "", "status": "down"}),
        "sonarr": db.create_service({"name": "Sonarr", "url": "", "status": "degraded"}),
        "muted": db.create_service({"name": "Muted box", "url": "", "status": "down",
                                    "ignore_in_overall_status": 1}),
    }
    db.update_service_status_from_check(ids["jelly"], "operational", 41)
    db.create_incident({"title": "Nextcloud is unreachable after the overnight update"},
                       service_ids=[ids["cloud"], ids["sonarr"]])
    db.create_incident({"title": "Wi-Fi is slow in the living room"})
    db.create_maintenance_window({"title": "Disk replacement", "description": "Streaming stops\nfor about three hours.",
                                  "starts_at": "2099-01-01T22:00", "ends_at": "2099-01-02T01:00"},
                                 service_ids=[ids["jelly"], ids["cloud"], ids["sonarr"]])
    db.create_maintenance_window({"title": "Router firmware", "description": "",
                                  "starts_at": "2000-01-01T00:00", "ends_at": "2099-06-01T00:00"},
                                 service_ids=[ids["sonarr"]])
    db.process_maintenance_windows()
    db.create_announcement({"title": "Movie night", "message": "Friday at **8pm**"})
    db.create_announcement({"title": "Scheduled", "message": "not yet", "starts_at": "2099-01-01T00:00"})
    return ids


# --- parse_detail, and the limits ------------------------------------------------------------
@pytest.mark.parametrize("raw, expected", [
    ("full", True), ("FULL", True), ("Full", True), ("  full ", True), ("\tfull\n", True),
    (None, False), ("", False), ("   ", False), ("ful", False), ("fullx", False), ("full full", False),
    ("full,compact", False), ("compact", False), ("1", False), ("true", False), ("all", False),
])
def test_parse_detail(raw, expected):
    assert device_api.parse_detail(raw) is expected


def test_the_compact_limits_are_the_original_constants():
    """COMPACT is what every request got before detail=full existed; if one of its numbers is
    ever not the constant it is named after, the default answer has changed."""
    c = device_api.COMPACT
    assert (c.site_bytes, c.service_items, c.all_service_items, c.service_name_bytes) == (
        device_api.SITE_BYTES, device_api.SERVICE_ITEMS, device_api.ALL_SERVICE_ITEMS,
        device_api.SERVICE_NAME_BYTES)
    assert (c.incident_items, c.incident_title_bytes, c.incident_services_bytes) == (
        device_api.INCIDENT_ITEMS, device_api.INCIDENT_TITLE_BYTES, device_api.INCIDENT_SERVICES_BYTES)
    assert (c.maintenance_items, c.maintenance_title_bytes, c.maintenance_services_bytes) == (
        device_api.MAINTENANCE_ITEMS, device_api.MAINTENANCE_TITLE_BYTES,
        device_api.MAINTENANCE_SERVICES_BYTES)
    assert (c.announcement_items, c.announcement_title_bytes, c.announcement_text_bytes) == (
        device_api.ANNOUNCEMENT_ITEMS, device_api.ANNOUNCEMENT_TITLE_BYTES,
        device_api.ANNOUNCEMENT_TEXT_BYTES)
    assert c.full is False and device_api.limits_for(False) is c
    assert device_api.incident_limit() == device_api.INCIDENT_ITEMS == 3


def test_the_full_limits_are_the_documented_contract():
    """The numbers a consumer of detail=full relies on. Changing one is a decision (and means
    re-running the worst-case size tests below), so it has to be a decision in this test too."""
    f = device_api.FULL
    assert f.full is True and device_api.limits_for(True) is f
    assert (f.site_bytes, f.service_items, f.all_service_items, f.service_name_bytes) == (64, 100, 100, 64)
    assert (f.incident_items, f.incident_title_bytes, f.incident_services_bytes) == (10, 120, 120)
    assert (f.maintenance_items, f.maintenance_title_bytes, f.maintenance_services_bytes,
            f.maintenance_description_bytes) == (20, 120, 120, 400)
    assert (f.announcement_items, f.announcement_title_bytes, f.announcement_text_bytes) == (10, 100, 500)
    assert f.service_list_items == 16
    assert device_api.incident_limit(True) == 10
    assert device_api.DETAIL_FULL == "full"
    assert device_api.FULL_EXAMPLE_QUERY == PLUGIN_QUERY.lstrip("?")


def test_the_two_full_ceilings_are_whole_multiples_of_four_kilobytes():
    for ceiling in (device_api.MAX_BYTES_FULL_SECTIONS, device_api.MAX_BYTES_FULL):
        assert ceiling > 0 and ceiling % 4096 == 0
    assert device_api.MAX_BYTES_FULL_SECTIONS <= device_api.MAX_BYTES_FULL
    assert device_api.MAX_BYTES_FULL > device_api.MAX_BYTES_ALL_WITH_VMS


# --- the service name list ---------------------------------------------------------------------
def test_service_name_list_cuts_like_the_services_section_and_skips_empty_names():
    long_name = "n" * 100
    rows = [{"id": 1, "name": "Plain"}, {"id": 2, "name": ""}, {"id": 3, "name": None},
            {"id": 4, "name": "  \t "}, {"id": 5, "name": long_name}, {"id": 6, "name": "a, b"}]
    names = device_api.service_name_list(rows, device_api.FULL)
    assert names == ["Plain", device_api.text(long_name, device_api.FULL_SERVICE_NAME_BYTES), "a, b"]
    assert names[1] == "n" * 61 + "..." and len(names[1].encode()) == 64


def test_service_name_list_is_capped_and_tolerates_missing_or_odd_rows():
    rows = [{"id": i, "name": f"S{i}"} for i in range(40)]
    assert device_api.service_name_list(rows, device_api.FULL) == [f"S{i}" for i in range(16)]
    assert device_api.service_name_list(None, device_api.FULL) == []
    assert device_api.service_name_list([], device_api.FULL) == []
    assert device_api.service_name_list(["not a dict", 5, {"id": 1}], device_api.FULL) == []


def test_empty_names_do_not_use_up_the_list_cap():
    rows = [{"name": ""}] * 5 + [{"name": f"S{i}"} for i in range(20)]
    assert device_api.service_name_list(rows, device_api.FULL) == [f"S{i}" for i in range(16)]


# --- the default answer is not touched ----------------------------------------------------------
def test_without_detail_full_the_new_keys_never_appear_even_for_rows_that_carry_the_data():
    """The rows the route passes in always carry `services` and `description`; only the mode decides
    whether they are used. (The golden file proves the bytes for rows that don't have them.)"""
    now = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
    rows = {
        "incidents": [{"title": "T", "service_names": "A, B",
                       "services": [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]}],
        "maintenance": [{"title": "W", "service_names": "A", "services": [{"id": 1, "name": "A"}],
                         "description": "details", "starts_at": "2099-01-01T00:00",
                         "ends_at": "2099-01-02T00:00"}],
    }
    plain = device_api.dumps(device_api.build_summary(PLUGIN_SECTIONS, now=now, site="s", overall="operational", **rows))
    for key in FULL_ONLY_KEYS:
        assert key not in plain.encode()
    full = device_api.dumps(device_api.build_summary(PLUGIN_SECTIONS, now=now, site="s", overall="operational",
                                                    detail_full=True, **rows))
    assert '"service_names":["A","B"]' in full and '"description":"details"' in full
    assert '"detail":"full"' in full


def test_the_marker_sits_right_after_overall_and_before_jellyfin():
    now = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
    full = device_api.build_summary(["services"], now=now, site="s", overall="operational",
                                    detail_full=True, with_jellyfin=True, jellyfin={})
    assert list(full)[:6] == ["v", "now", "site", "overall", "detail", "jellyfin"]
    plain = device_api.build_summary(["services"], now=now, site="s", overall="operational",
                                     with_jellyfin=True, jellyfin={})
    assert list(plain)[:5] == ["v", "now", "site", "overall", "jellyfin"]
    assert list(full)[:4] == list(plain)[:4]


@pytest.mark.parametrize("base", [
    "",
    "?sections=services,incidents,maintenance,announcements&services=all",
    "?sections=services&services=all&resources=all&jellyfin=1",
    "?sections=" + ",".join(device_api.ALL_SECTIONS) + "&services=all&resources=all&jellyfin=1",
])
@pytest.mark.parametrize("detail", ["", "bogus", "ful", "fullx", "compact", "1", "true", "all",
                                    "full%2Ccompact", "%20", "none"])
def test_any_detail_value_but_full_is_byte_for_byte_the_default_answer(enabled, monkeypatch, base, detail):
    """Through the route, on a seeded database: `detail=` with anything else is no `detail=` at all."""
    _seed_scenario()
    _typical_snapshot(monkeypatch)
    _fake_vms(monkeypatch, [_vm()])
    default = _get(enabled, base).data
    other = _get(enabled, f"{base}{'&' if base else '?'}detail={detail}").data
    assert _without_now(other) == _without_now(default)
    for key in FULL_ONLY_KEYS:
        assert key not in default


# --- what detail=full changes ---------------------------------------------------------------------
@pytest.mark.parametrize("value", ["full", "FULL", "Full", "%20full%20", "+Full+", "full%09"])
def test_detail_full_is_case_and_space_tolerant(enabled, value):
    assert _summary(enabled, f"?sections=services&detail={value}")["detail"] == "full"


def test_detail_full_adds_the_marker_the_name_arrays_and_the_description(enabled):
    ids = _seed_scenario()
    body = _summary(enabled, PLUGIN_QUERY)
    assert list(body)[:5] == ["v", "now", "site", "overall", "detail"]
    assert body["detail"] == "full"
    assert list(body)[5:] == PLUGIN_SECTIONS
    assert body["overall"] == "down"

    for item in body["services"]["items"]:
        assert set(item) <= {"name", "status", "ms"}
    cloud = next(i for i in body["services"]["items"] if i["status"] == "down")
    assert cloud["name"] == "Nextcloud family photos and documents archive"   # whole: 45 bytes

    newest, older = body["incidents"]["items"]
    assert list(newest) == ["title", "status", "since", "services", "service_names"]
    assert newest["title"] == "Wi-Fi is slow in the living room" and newest["service_names"] == []
    assert older["title"] == "Nextcloud is unreachable after the overnight update"   # 51 bytes: whole
    assert older["service_names"] == ["Nextcloud family photos and documents archive", "Sonarr"]
    # The existing string key keeps its meaning (the names joined), just with the larger cap.
    assert older["services"] == "Nextcloud family photos and documents archive, Sonarr"

    active, upcoming = body["maintenance"]["items"]
    assert list(active) == ["title", "state", "services", "starts", "ends", "service_names", "description"]
    assert (active["state"], active["service_names"], active["description"]) == ("active", ["Sonarr"], "")
    assert upcoming["service_names"] == ["Jellyfin", "Nextcloud family photos and documents archive", "Sonarr"]
    assert upcoming["description"] == "Streaming stops for about three hours."   # one line, like every string

    (announcement,) = body["announcements"]["items"]
    assert list(announcement) == ["title", "text", "type", "pinned"]
    assert announcement["text"] == "Friday at 8pm"
    assert ids  # seeded


def test_the_compact_answer_for_the_same_data_cuts_what_the_full_one_keeps(enabled):
    _seed_scenario()
    body = _summary(enabled, "?" + PLUGIN_QUERY.split("&detail=")[0].lstrip("?"))
    assert "detail" not in body
    cloud = next(i for i in body["services"]["items"] if i["status"] == "down")
    assert cloud["name"] == "Nextcloud family phot..."
    assert all("service_names" not in i for i in body["incidents"]["items"])
    assert all("service_names" not in i and "description" not in i for i in body["maintenance"]["items"])


# --- the caps, one section at a time -------------------------------------------------------------
def test_full_cuts_a_service_name_at_64_bytes_and_the_site_name_at_64(enabled):
    db.create_service({"name": "x" * 100, "url": "", "status": "down"})
    db.set_setting("site_name", "s" * 100)
    body = _summary(enabled, "?sections=services&detail=full")
    assert body["services"]["items"][0]["name"] == "x" * 61 + "..."
    assert body["site"] == "s" * 61 + "..."
    compact = _summary(enabled, "?sections=services")
    assert compact["services"]["items"][0]["name"] == "x" * 21 + "..."
    assert compact["site"] == "s" * 21 + "..."


@pytest.mark.parametrize("query", ["?sections=services&detail=full", "?sections=services&services=all&detail=full"])
def test_full_lists_up_to_100_services_with_and_without_services_all(enabled, query):
    for i in range(105):
        db.create_service({"name": f"Down {i:03d}", "url": "", "status": "down"})
    db.create_service({"name": "Fine", "url": "", "status": "operational"})
    section = _summary(enabled, query)["services"]
    assert len(section["items"]) == device_api.FULL_SERVICE_ITEMS == 100
    assert section["total"] == 106 and section["down"] == 105 and section["operational"] == 1
    assert section["items"][0]["name"] == "Down 000"   # still the admin's order within a status


def test_full_without_services_all_still_lists_only_the_unhealthy(enabled):
    db.create_service({"name": "Fine", "url": "", "status": "operational"})
    db.create_service({"name": "Dead", "url": "", "status": "down"})
    items = _summary(enabled, "?sections=services&detail=full")["services"]["items"]
    assert [i["name"] for i in items] == ["Dead"]
    items = _summary(enabled, "?sections=services&services=all&detail=full")["services"]["items"]
    assert [i["name"] for i in items] == ["Dead", "Fine"]


def test_full_services_keep_ms_only_for_the_measured_healthy_ones_with_services_all(enabled):
    fine = db.create_service({"name": "Fine", "url": "", "status": "operational"})
    down = db.create_service({"name": "Dead", "url": "", "status": "down"})
    db.create_service({"name": "Manual", "url": "", "status": "operational"})
    db.update_service_status_from_check(fine, "operational", 45)
    db.update_service_status_from_check(down, "down", 30000)
    items = {i["name"]: i for i in _summary(enabled, "?sections=services&services=all&detail=full")["services"]["items"]}
    assert items["Fine"] == {"name": "Fine", "status": "operational", "ms": 45}
    assert items["Dead"] == {"name": "Dead", "status": "down"}
    assert items["Manual"] == {"name": "Manual", "status": "operational"}
    plain = _summary(enabled, "?sections=services&detail=full")["services"]["items"]
    assert all("ms" not in i for i in plain)


def test_full_lists_10_incidents_and_open_is_still_the_total(enabled):
    for i in range(12):
        db.create_incident({"title": f"Incident {i}"})
    long = db.create_incident({"title": "t" * 130})
    section = _summary(enabled, "?sections=incidents&detail=full")["incidents"]
    assert section["open"] == 13
    assert len(section["items"]) == device_api.FULL_INCIDENT_ITEMS == 10
    assert section["items"][0]["title"] == "t" * 117 + "..."          # 120 bytes
    assert [i["title"] for i in section["items"][1:4]] == ["Incident 11", "Incident 10", "Incident 9"]
    compact = _summary(enabled, "?sections=incidents")["incidents"]
    assert len(compact["items"]) == 3 and compact["items"][0]["title"] == "t" * 37 + "..."
    assert long


def test_full_cuts_the_incident_service_string_at_120_bytes(enabled):
    ids = [db.create_service({"name": f"{i}" * 60, "url": "", "status": "operational"}) for i in range(3)]
    db.create_incident({"title": "I"}, service_ids=ids)
    item = _summary(enabled, "?sections=incidents&detail=full")["incidents"]["items"][0]
    assert len(item["services"].encode()) == 120 and item["services"].endswith("...")
    assert item["service_names"] == ["0" * 60, "1" * 60, "2" * 60]   # the array is not cut by it
    assert len(_summary(enabled, "?sections=incidents")["incidents"]["items"][0]["services"].encode()) == 32


def test_full_lists_20_windows_and_the_counts_are_still_the_totals(enabled):
    sid = db.create_service({"name": "A", "url": ""})
    for i in range(3):
        db.create_maintenance_window({"title": f"Now {i}", "starts_at": "2000-01-01T00:00",
                                      "ends_at": "2099-06-01T00:00"}, service_ids=[sid])
    db.process_maintenance_windows()
    for i in range(22):
        db.create_maintenance_window({"title": f"Later {i:02d}", "description": "d" * 500,
                                      "starts_at": f"2099-01-{i + 1:02d}T00:00",
                                      "ends_at": f"2099-02-{i + 1:02d}T00:00"}, service_ids=[sid])
    section = _summary(enabled, "?sections=maintenance&detail=full")["maintenance"]
    assert (section["active"], section["upcoming"]) == (3, 22)
    assert len(section["items"]) == device_api.FULL_MAINTENANCE_ITEMS == 20
    assert [i["state"] for i in section["items"]] == ["active"] * 3 + ["upcoming"] * 17   # active first
    assert section["items"][-1]["title"] == "Later 16"                                      # soonest first
    assert section["items"][3]["description"] == "d" * 397 + "..."                          # 400 bytes
    assert len(_summary(enabled, "?sections=maintenance")["maintenance"]["items"]) == 3


def test_full_cuts_a_window_title_and_service_string_at_120_bytes(enabled):
    ids = [db.create_service({"name": f"{i}" * 60, "url": ""}) for i in range(3)]
    db.create_maintenance_window({"title": "T" * 130, "starts_at": "2099-01-01T00:00",
                                  "ends_at": "2099-01-02T00:00"}, service_ids=ids)
    item = _summary(enabled, "?sections=maintenance&detail=full")["maintenance"]["items"][0]
    assert item["title"] == "T" * 117 + "..."
    assert len(item["services"].encode()) == 120 and item["services"].endswith("...")
    assert item["service_names"] == ["0" * 60, "1" * 60, "2" * 60]


def test_full_lists_10_announcements_with_longer_title_and_text(enabled):
    for i in range(12):
        db.create_announcement({"title": f"N{i}", "message": "m", "pinned": 1 if i == 0 else 0})
    db.create_announcement({"title": "q" * 150, "message": "w" * 600, "pinned": 1})
    section = _summary(enabled, "?sections=announcements&detail=full")["announcements"]
    assert section["count"] == 13 and len(section["items"]) == device_api.FULL_ANNOUNCEMENT_ITEMS == 10
    long = next(a for a in section["items"] if a["title"].startswith("q"))
    assert long["title"] == "q" * 97 + "..." and long["text"] == "w" * 497 + "..."
    compact = _summary(enabled, "?sections=announcements")["announcements"]
    assert len(compact["items"]) == 3


def test_full_announcement_text_is_still_one_plain_line(enabled):
    db.create_announcement({"title": "Notice", "message": "Server **restarts**\n\ntonight   at 23:00\n\n- and again **later**"})
    item = _summary(enabled, "?sections=announcements&detail=full")["announcements"]["items"][0]
    assert item["text"] == "Server restarts tonight at 23:00 - and again later"


# --- the name arrays ----------------------------------------------------------------------------
def test_a_window_lists_the_exact_names_of_its_services_even_when_the_compact_string_cuts_them(enabled):
    plain = db.create_service({"name": "Plain", "url": ""})
    comma = db.create_service({"name": "Photos, documents and backups", "url": ""})
    wordy = db.create_service({"name": "W" * 50, "url": ""})
    db.create_maintenance_window({"title": "W", "starts_at": "2099-01-01T00:00",
                                  "ends_at": "2099-01-02T00:00"}, service_ids=[plain, comma, wordy])
    item = _summary(enabled, "?sections=maintenance&detail=full")["maintenance"]["items"][0]
    assert item["service_names"] == ["Plain", "Photos, documents and backups", "W" * 50]
    # The compact string cannot do this: it is cut, and a comma inside a name is ambiguous.
    compact = _summary(enabled, "?sections=maintenance")["maintenance"]["items"][0]
    assert compact["services"] == "Plain, Photos, documents and..."
    assert "service_names" not in compact


def test_an_incident_without_services_has_an_empty_name_array(enabled):
    db.create_incident({"title": "Manual, no service attached"})
    item = _summary(enabled, "?sections=incidents&detail=full")["incidents"]["items"][0]
    assert item["service_names"] == [] and item["services"] == ""


def test_a_name_in_an_array_equals_the_same_services_name_in_the_services_section_even_past_64_bytes(enabled):
    sid = db.create_service({"name": "L" * 100, "url": "", "status": "down"})
    db.create_incident({"title": "I"}, service_ids=[sid])
    db.create_maintenance_window({"title": "W", "starts_at": "2099-01-01T00:00",
                                  "ends_at": "2099-01-02T00:00"}, service_ids=[sid])
    body = _summary(enabled, PLUGIN_QUERY)
    listed = body["services"]["items"][0]["name"]
    assert listed == "L" * 61 + "..."
    assert body["incidents"]["items"][0]["service_names"] == [listed]
    assert body["maintenance"]["items"][0]["service_names"] == [listed]


def test_a_name_array_holds_at_most_16_names_and_the_string_still_names_them_all_it_can(enabled):
    ids = [db.create_service({"name": f"Svc{i:02d}", "url": ""}) for i in range(20)]
    db.create_incident({"title": "I"}, service_ids=ids)
    db.create_maintenance_window({"title": "W", "starts_at": "2099-01-01T00:00",
                                  "ends_at": "2099-01-02T00:00"}, service_ids=ids)
    body = _summary(enabled, "?sections=incidents,maintenance&detail=full")
    expected = [f"Svc{i:02d}" for i in range(16)]
    assert body["incidents"]["items"][0]["service_names"] == expected
    assert body["maintenance"]["items"][0]["service_names"] == expected
    assert device_api.FULL_SERVICE_LIST_ITEMS == 16


# --- what is and is not filtered, unchanged ---------------------------------------------------
def test_a_muted_service_stays_out_of_the_services_section_in_full_mode_but_incidents_and_windows_keep_it(enabled):
    muted = db.create_service({"name": "Flaky box", "url": "", "status": "down", "ignore_in_overall_status": 1})
    real = db.create_service({"name": "Real", "url": "", "status": "operational"})
    db.create_incident({"title": "On the muted one"}, service_ids=[muted])
    db.create_maintenance_window({"title": "Both", "starts_at": "2099-01-01T00:00",
                                  "ends_at": "2099-01-02T00:00"}, service_ids=[muted, real])
    body = _summary(enabled, PLUGIN_QUERY)
    assert body["overall"] == "operational"
    assert body["services"]["total"] == 1 and body["services"]["down"] == 0
    assert [i["name"] for i in body["services"]["items"]] == ["Real"]
    assert body["incidents"]["items"][0]["service_names"] == ["Flaky box"]       # not filtered, as before
    assert body["maintenance"]["items"][0]["service_names"] == ["Flaky box", "Real"]


def test_a_scheduled_or_expired_announcement_never_appears_in_full_mode(enabled):
    db.create_announcement({"title": "Live", "message": "visible"})
    db.create_announcement({"title": "Later", "message": "secret", "starts_at": "2099-01-01T00:00"})
    db.create_announcement({"title": "Gone", "message": "old", "ends_at": "2000-01-01T00:00"})
    raw = _get(enabled, PLUGIN_QUERY).data
    section = json.loads(raw)["announcements"]
    assert section["count"] == 1 and [a["title"] for a in section["items"]] == ["Live"]
    assert b"secret" not in raw and b"Later" not in raw and b"Gone" not in raw


def test_an_old_open_incident_is_still_found_behind_newer_resolved_ones_in_full_mode(enabled):
    old_open = db.create_incident({"title": "Open since forever"})
    for i in range(12):
        rid = db.create_incident({"title": f"Resolved {i}"})
        db.update_incident(rid, {"title": f"Resolved {i}", "status": "resolved"})
    section = _summary(enabled, "?sections=incidents&detail=full")["incidents"]
    assert section["open"] == 1 and [i["title"] for i in section["items"]] == ["Open since forever"]
    assert old_open


# --- resources, vms and jellyfin are not touched by detail=full -----------------------------------
def test_detail_full_leaves_resources_vms_and_jellyfin_exactly_as_they_were(enabled, monkeypatch):
    """A plugin must never ask for these, and if it does it must not get a different answer from a
    display: the mode only changes the site name and the four sections."""
    disks = [{"path": f"/m{i}", "label": f"Disk {i} with a long label", "percent": float(i), "severity": "ok",
              "free_gb": 1.0} for i in range(12)]
    monkeypatch.setattr(monitoring, "get_resource_snapshot",
                        lambda: _snapshot(disks=disks, gpus=[_gpu(0), _gpu(1, name="N" * 60)]))
    _fake_vms(monkeypatch, [_vm(f"Virtual-Machine-with-a-long-name-{i}") for i in range(14)])
    monkeypatch.setattr(integrations, "get_cached_jellyfin_activity",
                        lambda: {"transcoding": 2, "running_tasks": [f"Task {i} " + "x" * 60 for i in range(5)]})
    for query in ("?sections=resources", "?sections=resources&resources=all", "?sections=vms",
                  "?sections=vms,resources&resources=all&jellyfin=1", "?sections=services&jellyfin=1"):
        default = _summary(enabled, query)
        full = _summary(enabled, query + "&detail=full")
        assert full.pop("detail") == "full"
        assert list(full) == list(default) and full["overall"] == default["overall"]
        for section in ("resources", "vms", "jellyfin"):
            assert full.get(section) == default.get(section), (query, section)


# --- the route's rules, with the parameter ------------------------------------------------------
def test_a_wrong_or_missing_key_with_detail_full_gets_the_401_and_no_data(enabled):
    _seed_scenario()
    for headers in ({}, {"X-Api-Key": "wrong"}):
        resp = enabled.get(URL + PLUGIN_QUERY, headers=headers)
        assert resp.status_code == 401
        assert resp.get_json() == {"error": "Missing or invalid API key"}
        assert b"Jellyfin" not in resp.data and b"detail" not in resp.data
    assert enabled.get(f"{URL}{PLUGIN_QUERY}&key={KEY}").status_code == 401   # header only, never the URL


def test_a_disabled_api_is_a_404_with_detail_full_too(client):
    assert client.get(URL + PLUGIN_QUERY, headers=HEADERS).status_code == 404
    db.set_setting(app_module.DEVICE_API_KEY_SETTING, KEY)
    db.set_setting(app_module.DEVICE_API_ENABLED_SETTING, "0")
    assert client.get(URL + PLUGIN_QUERY, headers=HEADERS).status_code == 404


def test_detail_full_is_utf8_no_store_and_get_only(enabled):
    db.create_service({"name": "Café serveur", "url": "", "status": "down"})
    resp = _get(enabled, PLUGIN_QUERY)
    assert resp.headers["Cache-Control"] == "no-store"
    assert resp.headers["Content-Type"] == "application/json; charset=utf-8"
    assert "Café serveur".encode() in resp.data and b"\\u" not in resp.data
    assert enabled.post(URL + PLUGIN_QUERY, headers=HEADERS).status_code == 405


def test_detail_full_does_not_make_an_unknown_sections_request_valid(enabled):
    assert _get(enabled, "?sections=nope&detail=full").status_code == 400


def test_detail_full_alone_still_means_every_default_section(enabled):
    body = _summary(enabled, "?detail=full")
    assert [k for k in body if k not in ("v", "now", "site", "overall", "detail")] == list(device_api.SECTIONS)


# --- no new SQL --------------------------------------------------------------------------------
def _statements(client, monkeypatch, query):
    real_connect = sqlite3.connect
    seen = []

    def tracing(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(seen.append)
        return conn

    with monkeypatch.context() as m:
        m.setattr(sqlite3, "connect", tracing)
        assert client.get(URL + query, headers=HEADERS).status_code == 200
    return seen


def _shape(statement):
    """A statement with its numbers (ids, LIMIT) folded away: the shape the database was asked."""
    return re.sub(r"\d+(?:\s*,\s*\d+)*", "N", " ".join(statement.split()))


def test_detail_full_runs_the_same_statements_as_the_default_and_none_of_them_writes(enabled, monkeypatch):
    """The name arrays come from the `services` lists db.py already attaches with its grouped
    queries; a new lookup would show here as a statement the default request does not make."""
    _seed_scenario()
    default = _statements(enabled, monkeypatch, PLUGIN_QUERY.replace("&detail=full", ""))
    full = _statements(enabled, monkeypatch, PLUGIN_QUERY)
    assert [_shape(s) for s in full] == [_shape(s) for s in default]
    assert not [s for s in full if s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "REPLACE"))]


def test_the_query_count_does_not_grow_with_the_data_in_full_mode(enabled, monkeypatch):
    def add(n, start):
        ids = [db.create_service({"name": f"S{start + i}", "url": "", "status": "down"}) for i in range(n)]
        for i in range(n):
            db.create_incident({"title": f"I{start + i}"}, service_ids=ids[:5])
            db.create_maintenance_window({"title": f"W{start + i}", "description": "d",
                                          "starts_at": "2099-01-01T00:00", "ends_at": "2099-01-02T00:00"},
                                         service_ids=ids[:5])
            db.create_announcement({"title": f"A{start + i}", "message": "m"})

    add(5, 0)
    few = len(_statements(enabled, monkeypatch, PLUGIN_QUERY))
    add(12, 100)
    assert len(_statements(enabled, monkeypatch, PLUGIN_QUERY)) == few


# --- size: bounded by construction, proven against the worst case -----------------------------------
def _seed_worst_case_full():
    """The adversarial data for the plugin's request: every list past its cap, every string made of
    the characters that cost most once serialised, and every incident and window covering more
    services than a name array holds, all of them with such names."""
    nasty = _worst_case_text()
    ids = [db.create_service({"name": nasty, "url": "", "status": "operational"}) for _ in range(110)]
    for sid in ids:
        db.update_service_status_from_check(sid, "operational", 99999)
    covered = ids[:device_api.FULL_SERVICE_LIST_ITEMS + 4]
    for i in range(12):
        db.create_incident({"title": nasty}, service_ids=covered)
    for i in range(25):
        db.create_maintenance_window({"title": nasty, "description": nasty,
                                      "starts_at": f"2099-01-{i + 1:02d}T00:00",
                                      "ends_at": f"2099-02-{i + 1:02d}T00:00"}, service_ids=covered)
    for i in range(12):
        db.create_announcement({"title": nasty, "message": nasty, "type": "critical", "pinned": 1})
    db.set_setting("site_name", nasty)


def test_the_plugins_worst_possible_request_fits_its_ceiling(enabled):
    _seed_worst_case_full()
    raw = _get(enabled, PLUGIN_QUERY).data
    print("PLUGIN WORST CASE", len(raw))
    assert len(raw) < device_api.MAX_BYTES_FULL_SECTIONS, f"{len(raw)} bytes"
    body = json.loads(raw)   # still valid JSON after all of that
    assert len(body["services"]["items"]) == device_api.FULL_SERVICE_ITEMS
    assert body["services"]["items"][-1]["ms"] == 99999
    assert len(body["incidents"]["items"]) == device_api.FULL_INCIDENT_ITEMS and body["incidents"]["open"] == 12
    assert len(body["maintenance"]["items"]) == device_api.FULL_MAINTENANCE_ITEMS
    assert body["maintenance"]["upcoming"] == 25
    assert len(body["announcements"]["items"]) == device_api.FULL_ANNOUNCEMENT_ITEMS
    for item in body["incidents"]["items"] + body["maintenance"]["items"]:
        assert len(item["service_names"]) == device_api.FULL_SERVICE_LIST_ITEMS   # the arrays are full
    # The same data, asked the way a display asks, stays under the ceilings it always had.
    assert len(_get(enabled, "").data) < device_api.MAX_BYTES
    assert len(_get(enabled, "?services=all").data) < device_api.MAX_BYTES_ALL_SERVICES


def test_the_largest_possible_full_answer_fits_its_ceiling(enabled, monkeypatch):
    """Everything at once: every section including resources and vms, every modifier, and
    detail=full, against adversarial data in every field. The biggest answer the endpoint can give."""
    _seed_worst_case_full()
    nasty = _worst_case_text()
    disks = [{"path": nasty, "label": nasty, "percent": 99.9, "severity": "crit", "free_gb": 99999.9}
             for _ in range(20)]
    gpus = [_gpu(i, name=nasty, util_percent=99.9, mem_used_gb=9999.9, mem_total_gb=9999.9, temp_c=99.9)
            for i in range(10)]
    monkeypatch.setattr(monitoring, "get_resource_snapshot",
                        lambda: _snapshot(disks=disks, gpus=gpus, cpu_temp_c=99.9, mem_used_gb=9999.9,
                                          mem_total_gb=9999.9,
                                          network={"up_mb_s": 99999.99, "down_mb_s": 99999.99}))
    monkeypatch.setattr(integrations, "get_cached_jellyfin_activity",
                        lambda: {"transcoding": 99, "running_tasks": [nasty] * 10})
    _fake_vms(monkeypatch, [{"name": '"' * 40 + str(i), "state": '"' * 40, "uptime": '"' * 40}
                            for i in range(40)])
    query = ("?sections=" + ",".join(device_api.ALL_SECTIONS)
             + "&services=all&resources=all&jellyfin=1&detail=full")
    raw = _get(enabled, query).data
    print("LARGEST FULL", len(raw))
    assert len(raw) < device_api.MAX_BYTES_FULL, f"{len(raw)} bytes"
    body = json.loads(raw)
    assert len(body["resources"]["disks"]) == device_api.ALL_DISK_ITEMS
    assert len(body["resources"]["gpus"]) == device_api.GPU_ITEMS
    assert len(body["vms"]["items"]) == device_api.VM_ITEMS
    assert len(body["jellyfin"]["tasks"]) == device_api.JELLYFIN_TASK_ITEMS
    assert len(body["services"]["items"]) == device_api.FULL_SERVICE_ITEMS
    # Without the parameter the same request keeps the ceiling it had before.
    assert len(_get(enabled, query.replace("&detail=full", "")).data) < device_api.MAX_BYTES_ALL_WITH_VMS


def test_a_typical_full_answer_is_a_few_kilobytes(enabled):
    """About 30 services, 3 incidents, 5 windows, 3 announcements: what a real install sends."""
    ids = [db.create_service({"name": f"Service number {i}", "url": "",
                              "status": ("down", "degraded", "slow", "operational", "operational")[i % 5]})
           for i in range(30)]
    for sid in ids:
        db.update_service_status_from_check(sid, db.get_service(sid)["status"], 80)
    for i in range(3):
        db.create_incident({"title": f"Something is wrong with thing {i}"}, service_ids=ids[i:i + 2])
        db.create_announcement({"title": f"Announcement {i}", "message": "A sentence of ordinary length."})
    for i in range(5):
        db.create_maintenance_window({"title": f"Window {i}", "description": "A sentence about what happens.",
                                      "starts_at": f"2099-01-0{i + 1}T00:00", "ends_at": f"2099-01-0{i + 1}T04:00"},
                                     service_ids=ids[i:i + 3])
    size = len(_get(enabled, PLUGIN_QUERY).data)
    print("TYPICAL FULL", size)
    assert size < 6144


# --- the example and the admin page ---------------------------------------------------------------
def test_the_full_example_is_a_real_answer_built_through_the_same_code():
    example = device_api.example_full_summary()
    assert list(example)[:5] == ["v", "now", "site", "overall", "detail"] and example["detail"] == "full"
    assert list(example)[5:] == PLUGIN_SECTIONS
    incidents = example["incidents"]["items"]
    assert incidents[0]["service_names"] and incidents[1]["service_names"] == []   # one without services
    assert all("service_names" in w and "description" in w for w in example["maintenance"]["items"])
    # The string and the array of one row cannot disagree: the array is what the string is made from.
    assert incidents[0]["services"] == ", ".join(incidents[0]["service_names"])
    # A name the compact answer would have cut is whole here, and the example shows latency.
    assert any(len(i["name"].encode()) > device_api.SERVICE_NAME_BYTES for i in example["services"]["items"])
    assert any("ms" in i for i in example["services"]["items"])
    pretty, size = device_api.example_full_json()
    assert json.loads(pretty) == example
    assert size == len(device_api.dumps(example).encode("utf-8")) < 3000
