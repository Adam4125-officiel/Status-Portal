"""The display-device API: GET /api/device/summary, and the shaping behind it.

What these pin is everything the server decides - whether the endpoint exists at all,
who may read it, what each section carries, and that the answer stays small enough for a
microcontroller with ~35 KB of free RAM whatever is in the database. The firmware side
is the sibling Status-ESP repository.
"""
import json
import sqlite3

import pytest

import app as app_module
import db
import device_api
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
    assert resp.get_json()["valid"] == list(device_api.SECTIONS)


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
