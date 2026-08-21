import sqlite3
from datetime import datetime, timedelta, timezone

import db


def test_init_db_retrofits_columns_on_a_pre_existing_database(tmp_path, monkeypatch):
    """Regression test for a real bug: a database created before a given column existed
    (services.group_name/auto_incident, incidents.auto_created,
    integrations.service_id/show_on_public) never got that column, because CREATE
    TABLE IF NOT EXISTS is a no-op once the table already exists - every save touching
    that column then failed with 'no such column'. init_db() must retrofit it without
    losing existing rows."""
    old_db_path = tmp_path / "old_schema.db"
    conn = sqlite3.connect(old_db_path)
    conn.execute("""
        CREATE TABLE services (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, description TEXT DEFAULT '',
            url TEXT NOT NULL, icon TEXT DEFAULT '⚙', status TEXT NOT NULL DEFAULT 'operational',
            manual_override INTEGER NOT NULL DEFAULT 0, auto_check INTEGER NOT NULL DEFAULT 0,
            check_url TEXT DEFAULT '', last_checked TEXT DEFAULT '', response_ms INTEGER DEFAULT NULL,
            sort_order INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE incidents (
            id INTEGER PRIMARY KEY AUTOINCREMENT, service_id INTEGER, title TEXT NOT NULL,
            description TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'investigating',
            started_at TEXT NOT NULL, resolved_at TEXT DEFAULT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE integrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, kind TEXT NOT NULL,
            base_url TEXT NOT NULL, api_key TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1
        )
    """)
    conn.execute("INSERT INTO services (name, url) VALUES ('Jellyfin', 'http://server:8096')")
    conn.commit()
    conn.close()

    monkeypatch.setattr(db, "DB_PATH", str(old_db_path))
    db.init_db()  # must not raise, and must not wipe the existing row

    service = db.get_service(1)
    assert service["name"] == "Jellyfin"  # pre-existing data survived
    assert service["auto_incident"] == 1  # backfilled with the column's default
    assert service["group_name"] == ""

    # These must no longer raise "no such column"
    db.update_service(1, {"name": "Jellyfin", "url": "http://server:8096", "auto_incident": 0})
    iid = db.create_integration({"name": "Sonarr", "kind": "arr", "base_url": "http://sonarr:8989",
                                  "api_key": "x", "service_id": 1, "show_on_public": 1})
    assert db.list_integrations_for_service(1)[0]["id"] == iid


def test_init_db_seeds_defaults(isolated_db):
    services = db.list_services()
    assert len(services) == 2
    assert {s["name"] for s in services} == {"Jellyfin", "SMB share"}


def test_service_crud(isolated_db):
    db.create_service({"name": "Test", "url": "http://example.com", "group_name": "Media"})
    new = [s for s in db.list_services() if s["name"] == "Test"][0]
    assert new["group_name"] == "Media"

    db.update_service(new["id"], {"name": "Test2", "url": "http://example.com", "group_name": "Other"})
    updated = db.get_service(new["id"])
    assert updated["name"] == "Test2"
    assert updated["group_name"] == "Other"

    db.delete_service(new["id"])
    assert db.get_service(new["id"]) is None


def test_service_ignore_in_overall_status_persists(isolated_db):
    sid = db.create_service({"name": "Test", "url": "http://example.com", "ignore_in_overall_status": 1})
    assert db.get_service(sid)["ignore_in_overall_status"] == 1

    db.update_service(sid, {"name": "Test", "url": "http://example.com", "ignore_in_overall_status": 0})
    assert db.get_service(sid)["ignore_in_overall_status"] == 0


def test_service_show_report_button_defaults_on_and_is_toggleable(isolated_db):
    sid = db.create_service({"name": "Test", "url": "http://example.com"})
    assert db.get_service(sid)["show_report_button"] == 1

    db.update_service(sid, {"name": "Test", "url": "http://example.com", "show_report_button": 0})
    assert db.get_service(sid)["show_report_button"] == 0

    db.update_service(sid, {"name": "Test", "url": "http://example.com", "show_report_button": 1})
    assert db.get_service(sid)["show_report_button"] == 1


def test_service_auto_incident_defaults_on_and_is_toggleable(isolated_db):
    # Default is on (1) when not specified, so existing behavior doesn't change for
    # anyone who doesn't touch the new checkbox.
    sid = db.create_service({"name": "Test", "url": "http://example.com"})
    assert db.get_service(sid)["auto_incident"] == 1

    db.update_service(sid, {"name": "Test", "url": "http://example.com", "auto_incident": 0})
    assert db.get_service(sid)["auto_incident"] == 0

    db.update_service(sid, {"name": "Test", "url": "http://example.com", "auto_incident": 1})
    assert db.get_service(sid)["auto_incident"] == 1


def test_service_links_replace(isolated_db):
    sid = db.list_services()[0]["id"]
    db.replace_service_links(sid, [("Tailscale", "http://100.0.0.1"), ("LAN", "http://192.168.1.1")])
    assert [l["label"] for l in db.list_service_links(sid)] == ["Tailscale", "LAN"]

    db.replace_service_links(sid, [("Only", "http://only.example")])
    links = db.list_service_links(sid)
    assert len(links) == 1 and links[0]["label"] == "Only"


def test_incident_lifecycle_and_updates(isolated_db):
    sid = db.list_services()[0]["id"]
    db.create_incident({"service_id": sid, "title": "Test outage", "status": "investigating"})
    incident = db.list_incidents()[0]
    assert incident["status"] == "investigating"
    assert incident["resolved_at"] is None

    db.create_incident_update(incident["id"], "Looking into it", "identified")
    updates = db.list_incident_updates(incident["id"])
    assert len(updates) == 1
    assert updates[0]["message"] == "Looking into it"

    db.update_incident(incident["id"], {"title": "Test outage", "status": "resolved"})
    resolved = db.get_incident(incident["id"])
    assert resolved["status"] == "resolved"
    assert resolved["resolved_at"] is not None


def test_list_incidents_max_age_days_hides_only_old_resolved(isolated_db):
    sid = db.list_services()[0]["id"]
    old_resolved = db.create_incident({"service_id": sid, "title": "Old resolved", "status": "resolved"})
    still_open = db.create_incident({"service_id": sid, "title": "Still open", "status": "investigating"})
    recent_resolved = db.create_incident({"service_id": sid, "title": "Recent resolved", "status": "resolved"})

    conn = db.get_db()
    conn.execute("UPDATE incidents SET resolved_at='2000-01-01T00:00:00' WHERE id=?", (old_resolved,))
    conn.commit()
    conn.close()

    ids = {i["id"] for i in db.list_incidents(max_age_days=30)}
    assert old_resolved not in ids
    assert still_open in ids
    assert recent_resolved in ids

    # Without a cutoff, everything (including the old resolved one) still shows.
    assert old_resolved in {i["id"] for i in db.list_incidents()}


def test_list_incidents_exclude_ids_returns_the_rest_newest_first(isolated_db):
    sid = db.list_services()[0]["id"]
    ids = [db.create_incident({"service_id": sid, "title": f"Incident {n}", "status": "resolved"}) for n in range(5)]
    first_page = db.list_incidents(limit=2)
    assert [i["id"] for i in first_page] == list(reversed(ids))[:2]

    second_page = db.list_incidents(limit=2, exclude_ids=[i["id"] for i in first_page])
    assert [i["id"] for i in second_page] == list(reversed(ids))[2:4]


def test_list_incidents_exclude_ids_ignores_max_age_days_filter(isolated_db):
    """Regression test for a real bug (2026-08-10): the "load more" endpoint used
    to re-apply the same max_age_days filter as the initial view, so anything
    older than the cutoff was permanently unreachable - the initial page hid it,
    and "load more" hid it again forever, defeating the point of the history
    feature entirely. Paging past the initial view must walk the FULL unfiltered
    timeline."""
    sid = db.list_services()[0]["id"]
    old1 = db.create_incident({"service_id": sid, "title": "Old 1", "status": "resolved"})
    old2 = db.create_incident({"service_id": sid, "title": "Old 2", "status": "resolved"})
    recent = db.create_incident({"service_id": sid, "title": "Recent", "status": "resolved"})
    conn = db.get_db()
    conn.execute("UPDATE incidents SET resolved_at='2000-01-01T00:00:00' WHERE id IN (?, ?)", (old1, old2))
    conn.commit()
    conn.close()

    # Initial (age-filtered) view only shows the recent one.
    initial = db.list_incidents(limit=8, max_age_days=30)
    assert [i["id"] for i in initial] == [recent]

    # "Load more" (as app.py's api_incidents_more() actually calls it: no
    # max_age_days, excluding what's already shown) reveals both older ones.
    more = db.list_incidents(limit=10, exclude_ids=[i["id"] for i in initial])
    assert {i["id"] for i in more} == {old1, old2}


def test_list_incidents_exclude_ids_reaches_items_hidden_in_an_id_space_gap(isolated_db):
    """Regression test for the subtler second version of the same 2026-08-10 bug:
    a still-open incident (never hidden, any age) can sit at a LOWER id than a
    newer incident that got resolved and aged out of the initial view - a gap in
    id-space between what's shown. Any position-based cursor (`id < oldest_shown`)
    skips straight over whatever is hidden inside that gap; excluding the shown
    ids instead cannot, because it never reasons about position at all."""
    sid = db.list_services()[0]["id"]
    old_open = db.create_incident({"service_id": sid, "title": "Old but still open", "status": "investigating"})
    hidden = db.create_incident({"service_id": sid, "title": "Newer but resolved+hidden", "status": "resolved"})
    recent = db.create_incident({"service_id": sid, "title": "Recent", "status": "investigating"})
    conn = db.get_db()
    conn.execute("UPDATE incidents SET resolved_at='2000-01-01T00:00:00' WHERE id=?", (hidden,))
    conn.commit()
    conn.close()

    initial = db.list_incidents(limit=8, max_age_days=30)
    shown_ids = [i["id"] for i in initial]
    assert shown_ids == [recent, old_open]  # `hidden` sits between them in id-space
    assert hidden not in shown_ids

    more = db.list_incidents(limit=10, exclude_ids=shown_ids)
    assert [i["id"] for i in more] == [hidden]


def test_list_incidents_exclude_ids_never_returns_a_visible_item(isolated_db):
    """Regression test for the user-reported symptom that made "Load more" look
    completely broken: with nothing actually hidden, clicking it re-appended the
    entire visible list (an id cursor seeded from the newest shown item returned
    everything below it). Excluding shown ids must return nothing at all here."""
    sid = db.list_services()[0]["id"]
    for n in range(3):
        db.create_incident({"service_id": sid, "title": f"Visible {n}", "status": "resolved"})

    initial = db.list_incidents(limit=8)
    more = db.list_incidents(limit=10, exclude_ids=[i["id"] for i in initial])
    assert more == []


def test_list_ended_maintenance_windows_only_returns_ended(isolated_db):
    sid = db.list_services()[0]["id"]
    ended_id = db.create_maintenance_window({
        "service_id": sid, "title": "Past", "starts_at": "2000-01-01T00:00", "ends_at": "2000-01-02T00:00",
    })
    db.create_maintenance_window({
        "service_id": sid, "title": "Upcoming", "starts_at": "2099-01-01T00:00", "ends_at": "2099-01-02T00:00",
    })
    db.process_maintenance_windows()
    assert db.get_maintenance_window(ended_id)["ended"] == 1

    history = db.list_ended_maintenance_windows()
    assert [w["id"] for w in history] == [ended_id]
    # The still-upcoming window must never appear in history, and the
    # still-relevant public list must never include the ended one.
    assert ended_id not in {w["id"] for w in db.list_public_maintenance_windows()}


def test_list_ended_maintenance_windows_respects_max_age_days(isolated_db):
    sid = db.list_services()[0]["id"]
    mid = db.create_maintenance_window({
        "service_id": sid, "title": "Long ago", "starts_at": "2000-01-01T00:00", "ends_at": "2000-01-02T00:00",
    })
    db.process_maintenance_windows()
    assert db.get_maintenance_window(mid)["ended"] == 1

    assert db.list_ended_maintenance_windows(max_age_days=30) == []
    assert len(db.list_ended_maintenance_windows()) == 1


def test_auto_incident_helpers(isolated_db):
    sid = db.list_services()[0]["id"]
    assert db.get_open_auto_incident_for_service(sid) is None

    iid = db.create_auto_incident(sid, "Service down", "investigating")
    open_incident = db.get_open_auto_incident_for_service(sid)
    assert open_incident is not None
    assert open_incident["id"] == iid
    assert open_incident["auto_created"] == 1

    db.update_incident(iid, {"title": "Service down", "status": "resolved"})
    assert db.get_open_auto_incident_for_service(sid) is None


def test_incident_multi_service_create_and_replace(isolated_db):
    services = db.list_services()
    s1, s2 = services[0]["id"], services[1]["id"]

    iid = db.create_incident({"title": "Storage outage", "status": "investigating"},
                              service_ids=[s1, s2])
    incident = db.get_incident(iid)
    assert {s["id"] for s in incident["services"]} == {s1, s2}
    assert incident["service_id"] == s1  # primary/first stays on the legacy column

    # Editing to drop down to just one service replaces the association entirely.
    db.update_incident(iid, {"title": "Storage outage", "status": "investigating"}, service_ids=[s2])
    incident = db.get_incident(iid)
    assert [s["id"] for s in incident["services"]] == [s2]

    # A status-only update (service_ids omitted) must leave services untouched -
    # this is the path admin_incident_add_update()/the auto-incident lifecycle use.
    db.update_incident(iid, {"title": "Storage outage", "status": "resolved"})
    incident = db.get_incident(iid)
    assert [s["id"] for s in incident["services"]] == [s2]
    assert incident["status"] == "resolved"


def test_incident_with_no_service_stays_general(isolated_db):
    iid = db.create_incident({"title": "General notice", "status": "investigating"})
    incident = db.get_incident(iid)
    assert incident["services"] == []
    assert incident["service_id"] is None


def test_maintenance_window_multi_service_applies_and_restores_independently(isolated_db):
    """Two services in one window must each get their own pre_status snapshot and
    restore independently - a shared single pre_status/pre_manual_override (the old
    single-service schema) would corrupt one of them."""
    services = db.list_services()
    s1, s2 = services[0]["id"], services[1]["id"]
    db.update_service(s1, {**db.get_service(s1), "status": "operational"})
    db.update_service(s2, {**db.get_service(s2), "status": "degraded"})

    mid = db.create_maintenance_window({
        "title": "Network switch upgrade", "starts_at": "2000-01-01T00:00", "ends_at": "2099-01-01T00:00",
    }, service_ids=[s1, s2])
    events = db.process_maintenance_windows()
    assert len(events) == 2
    assert db.get_service(s1)["status"] == "maintenance"
    assert db.get_service(s2)["status"] == "maintenance"

    window = db.get_maintenance_window(mid)
    assert {s["id"] for s in window["services"]} == {s1, s2}

    conn = db.get_db()
    conn.execute("UPDATE maintenance_windows SET ends_at='2000-01-02T00:00' WHERE id=?", (mid,))
    conn.commit()
    conn.close()
    events = db.process_maintenance_windows()
    assert len(events) == 2
    assert db.get_service(s1)["status"] == "operational"  # each restored to its own pre-status
    assert db.get_service(s2)["status"] == "degraded"


def test_maintenance_window_delete_while_active_restores_all_services(isolated_db):
    services = db.list_services()
    s1, s2 = services[0]["id"], services[1]["id"]
    mid = db.create_maintenance_window({
        "title": "Upgrade", "starts_at": "2000-01-01T00:00", "ends_at": "2099-01-01T00:00",
    }, service_ids=[s1, s2])
    db.process_maintenance_windows()
    assert db.get_service(s1)["status"] == "maintenance"
    assert db.get_service(s2)["status"] == "maintenance"

    db.delete_maintenance_window(mid)
    assert db.get_service(s1)["status"] == "operational"
    assert db.get_service(s2)["status"] == "operational"


def test_maintenance_window_edit_updates_fields_and_services(isolated_db):
    services = db.list_services()
    s1, s2 = services[0]["id"], services[1]["id"]
    mid = db.create_maintenance_window({
        "title": "Upgrade", "starts_at": "2099-01-01T00:00", "ends_at": "2099-01-02T00:00",
    }, service_ids=[s1])

    db.update_maintenance_window(mid, {
        "title": "Upgrade (rescheduled)", "starts_at": "2099-02-01T00:00", "ends_at": "2099-02-02T00:00",
    }, service_ids=[s1, s2])
    window = db.get_maintenance_window(mid)
    assert window["title"] == "Upgrade (rescheduled)"
    assert {s["id"] for s in window["services"]} == {s1, s2}

    # service_ids omitted (the "already applied" path in app.py) must leave services alone.
    db.update_maintenance_window(mid, {
        "title": "Upgrade (rescheduled again)", "starts_at": "2099-02-01T00:00", "ends_at": "2099-02-03T00:00",
    })
    window = db.get_maintenance_window(mid)
    assert window["title"] == "Upgrade (rescheduled again)"
    assert {s["id"] for s in window["services"]} == {s1, s2}


def test_backfill_seeds_join_tables_for_pre_existing_single_service_rows(isolated_db):
    """Regression test for the multi-service migration: a maintenance_windows/incidents
    row written before incident_services/maintenance_window_services existed (or by
    code that still only knows the single legacy service_id column) must still read
    back correctly through the new join-table-based helpers after init_db() runs
    again - simulated here by inserting directly via raw SQL, bypassing the ORM-less
    helper functions entirely, then re-running init_db()."""
    sid = db.list_services()[0]["id"]
    conn = db.get_db()
    conn.execute("""
        INSERT INTO incidents (service_id, title, description, status, started_at)
        VALUES (?, 'Legacy incident', '', 'investigating', ?)
    """, (sid, db.now_iso()))
    conn.execute("""
        INSERT INTO maintenance_windows (service_id, title, starts_at, ends_at, created_at)
        VALUES (?, 'Legacy window', '2099-01-01T00:00', '2099-01-02T00:00', ?)
    """, (sid, db.now_iso()))
    conn.commit()
    conn.close()

    db.init_db()  # re-run: must backfill the join tables without raising or duplicating

    incident = db.list_incidents()[0]
    assert incident["service_names"] == db.get_service(sid)["name"]
    window = db.list_maintenance_windows()[0]
    assert window["service_names"] == db.get_service(sid)["name"]

    db.init_db()  # idempotent - running it again must not duplicate the backfilled rows
    assert len(db.get_incident(incident["id"])["services"]) == 1
    assert len(db.get_maintenance_window(window["id"])["services"]) == 1


def test_uptime_percentage(isolated_db):
    sid = db.list_services()[0]["id"]
    assert db.get_uptime_percentage(sid) is None  # no history yet

    db.record_status_history(sid, "operational", 50)
    db.record_status_history(sid, "operational", 45)
    db.record_status_history(sid, "down", None)
    db.record_status_history(sid, "maintenance", None)  # excluded from the ratio

    assert db.get_uptime_percentage(sid) == 66.7  # 2 up out of 3 non-maintenance checks


def test_settings_get_set(isolated_db):
    assert db.get_setting("site_name", "Server") == "Server"
    db.set_setting("site_name", "HomeLab")
    assert db.get_setting("site_name", "Server") == "HomeLab"


def test_low_disk_alert_state_persists(isolated_db):
    assert db.get_low_disk_alert_state("/") is False
    db.set_low_disk_alert_state("/", True)
    assert db.get_low_disk_alert_state("/") is True
    db.set_low_disk_alert_state("/", False)
    assert db.get_low_disk_alert_state("/") is False


def test_integrations_crud(isolated_db):
    db.create_integration({"name": "Sonarr", "kind": "arr", "base_url": "http://sonarr:8989/",
                            "api_key": "abc", "enabled": 1})
    integ = db.list_integrations()[0]
    assert integ["base_url"] == "http://sonarr:8989"  # trailing slash stripped

    db.update_integration(integ["id"], {"name": "Sonarr2", "kind": "arr", "base_url": "http://sonarr:8989",
                                         "api_key": "", "enabled": 1})
    updated = db.get_integration(integ["id"])
    assert updated["name"] == "Sonarr2"
    assert updated["api_key"] == "abc"  # blank api_key on edit keeps the old one

    db.delete_integration(integ["id"])
    assert db.get_integration(integ["id"]) is None


def test_create_service_returns_new_id(isolated_db):
    new_id = db.create_service({"name": "Test", "url": ""})
    assert isinstance(new_id, int)
    assert db.get_service(new_id)["name"] == "Test"


def test_service_run_target_defaults_empty_and_round_trips(isolated_db):
    sid = db.create_service({"name": "Jellyfin", "url": ""})
    assert db.get_service(sid)["run_target"] == ""

    db.update_service(sid, {"name": "Jellyfin", "url": "", "run_target": "vm:VM-Media02"})
    assert db.get_service(sid)["run_target"] == "vm:VM-Media02"

    db.update_service(sid, {"name": "Jellyfin", "url": "", "run_target": "host"})
    assert db.get_service(sid)["run_target"] == "host"


def test_service_dependencies_round_trip_and_replace(isolated_db):
    seerr = db.create_service({"name": "Seerr", "url": ""})
    radarr = db.create_service({"name": "Radarr", "url": ""})
    sonarr = db.create_service({"name": "Sonarr", "url": ""})
    assert db.get_service_dependencies(seerr) == []

    db.set_service_dependencies(seerr, [radarr, sonarr])
    assert sorted(db.get_service_dependencies(seerr)) == sorted([radarr, sonarr])

    # Replaces the full set rather than appending.
    db.set_service_dependencies(seerr, [radarr])
    assert db.get_service_dependencies(seerr) == [radarr]


def test_service_dependencies_filters_out_self_dependency(isolated_db):
    seerr = db.create_service({"name": "Seerr", "url": ""})
    db.set_service_dependencies(seerr, [seerr])
    assert db.get_service_dependencies(seerr) == []


def test_service_dependencies_cascade_delete_either_side(isolated_db):
    seerr = db.create_service({"name": "Seerr", "url": ""})
    radarr = db.create_service({"name": "Radarr", "url": ""})
    db.set_service_dependencies(seerr, [radarr])

    db.delete_service(radarr)
    assert db.get_service_dependencies(seerr) == []

    sonarr = db.create_service({"name": "Sonarr", "url": ""})
    db.set_service_dependencies(seerr, [sonarr])
    db.delete_service(seerr)
    conn = db.get_db()
    remaining = conn.execute("SELECT * FROM service_dependencies").fetchall()
    conn.close()
    assert remaining == []


def test_integration_service_linking(isolated_db):
    sid = db.create_service({"name": "Sonarr", "url": "http://sonarr:8989"})

    # Not linked / not opted into public display -> shouldn't show up
    iid = db.create_integration({"name": "Sonarr", "kind": "arr", "base_url": "http://sonarr:8989",
                                  "api_key": "x", "enabled": 1, "service_id": sid, "show_on_public": 0})
    assert db.list_integrations_for_service(sid) == []

    db.update_integration(iid, {"name": "Sonarr", "kind": "arr", "base_url": "http://sonarr:8989",
                                 "api_key": "", "enabled": 1, "service_id": sid, "show_on_public": 1})
    linked = db.list_integrations_for_service(sid)
    assert len(linked) == 1
    assert linked[0]["id"] == iid

    # Disabling it should hide it again even though show_on_public is still set
    db.update_integration(iid, {"name": "Sonarr", "kind": "arr", "base_url": "http://sonarr:8989",
                                 "api_key": "", "enabled": 0, "service_id": sid, "show_on_public": 1})
    assert db.list_integrations_for_service(sid) == []


def test_maintenance_window_starts_and_ends(isolated_db):
    sid = db.list_services()[0]["id"]
    db.update_service(sid, {"name": "Jellyfin", "url": "http://server:8096", "status": "operational"})

    mid = db.create_maintenance_window({
        "service_id": sid, "title": "Upgrade", "description": "Disk swap",
        "starts_at": "2000-01-01T00:00", "ends_at": "2099-01-01T00:00",
    })
    events = db.process_maintenance_windows()
    assert len(events) == 1
    assert events[0]["event"] == "maintenance_started"
    assert db.get_service(sid)["status"] == "maintenance"
    assert db.get_service(sid)["manual_override"] == 1
    window = db.get_maintenance_window(mid)
    assert window["applied"] == 1
    assert window["pre_status"] == "operational"

    # Not due to end yet - re-running shouldn't change anything.
    db.process_maintenance_windows()
    assert db.get_service(sid)["status"] == "maintenance"

    # Force it into the past and confirm it restores the pre-maintenance state.
    conn = db.get_db()
    conn.execute("UPDATE maintenance_windows SET ends_at='2000-01-02T00:00' WHERE id=?", (mid,))
    conn.commit()
    conn.close()
    events = db.process_maintenance_windows()
    assert events[0]["event"] == "maintenance_ended"
    assert db.get_service(sid)["status"] == "operational"
    assert db.get_service(sid)["manual_override"] == 0
    assert db.get_maintenance_window(mid)["ended"] == 1


def test_maintenance_window_delete_while_active_restores_service(isolated_db):
    sid = db.list_services()[0]["id"]
    mid = db.create_maintenance_window({
        "service_id": sid, "title": "Upgrade", "starts_at": "2000-01-01T00:00", "ends_at": "2099-01-01T00:00",
    })
    db.process_maintenance_windows()
    assert db.get_service(sid)["status"] == "maintenance"

    db.delete_maintenance_window(mid)
    assert db.get_service(sid)["status"] == "operational"
    assert db.get_service(sid)["manual_override"] == 0


def test_maintenance_window_not_yet_due_is_left_alone(isolated_db):
    sid = db.list_services()[0]["id"]
    db.create_maintenance_window({
        "service_id": sid, "title": "Future", "starts_at": "2099-01-01T00:00", "ends_at": "2099-01-02T00:00",
    })
    assert db.process_maintenance_windows() == []
    assert db.get_service(sid)["status"] == "operational"


def test_count_open_reports_by_service_excludes_resolved_and_general(isolated_db):
    services = db.list_services()
    sid, other_sid = services[0]["id"], services[1]["id"]
    db.create_problem_report("Open 1", "", sid)
    db.create_problem_report("Open 2", "", sid)
    reviewed = db.create_problem_report("Reviewed but still open", "", sid)
    db.update_problem_report_status(reviewed, "reviewed")
    resolved = db.create_problem_report("Resolved", "", sid)
    db.update_problem_report_status(resolved, "resolved")
    db.create_problem_report("General, no service", "", None)
    db.create_problem_report("For a different service", "", other_sid)

    counts = db.count_open_reports_by_service()
    assert counts[sid] == 3  # "new" + "new" + "reviewed", not the resolved one
    assert counts[other_sid] == 1
    assert len(counts) == 2  # no entry for the general (service_id=None) report


# ---------------------------------------------------------------------------
# Uptime aggregation, indexes and history retention
# ---------------------------------------------------------------------------
def test_grouped_uptime_matches_the_per_service_version(isolated_db):
    """The grouped query replaced N per-service queries on the public page's hot
    path - it has to answer identically, not just faster."""
    a = db.create_service({"name": "A", "url": "", "check_url": "", "description": ""})
    b = db.create_service({"name": "B", "url": "", "check_url": "", "description": ""})
    c = db.create_service({"name": "C", "url": "", "check_url": "", "description": ""})
    for status in ("operational", "operational", "down", "slow"):
        db.record_status_history(a, status, 10)
    for status in ("down", "down"):
        db.record_status_history(b, status, None)
    # c has no history at all.

    grouped = db.get_uptime_percentages()
    assert grouped == {a: 75.0, b: 0.0}
    assert grouped.get(a) == db.get_uptime_percentage(a)
    assert grouped.get(b) == db.get_uptime_percentage(b)
    assert grouped.get(c) is None and db.get_uptime_percentage(c) is None


def test_uptime_excludes_maintenance_checks_in_both_forms(isolated_db):
    sid = db.create_service({"name": "S", "url": "", "check_url": "", "description": ""})
    db.record_status_history(sid, "operational", 10)
    db.record_status_history(sid, "maintenance", None)
    db.record_status_history(sid, "down", None)
    # 2 counted checks, 1 down -> 50%, with the maintenance row ignored entirely.
    assert db.get_uptime_percentage(sid) == 50.0
    assert db.get_uptime_percentages()[sid] == 50.0


def test_prune_status_history_drops_only_rows_past_the_cutoff(isolated_db):
    sid = db.create_service({"name": "S", "url": "", "check_url": "", "description": ""})
    conn = db.get_db()
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    recent = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    conn.executemany(
        "INSERT INTO status_history (service_id, status, response_ms, checked_at) VALUES (?,?,?,?)",
        [(sid, "operational", 5, old), (sid, "operational", 5, recent)])
    conn.commit()
    conn.close()

    assert db.prune_status_history(30) == 1
    conn = db.get_db()
    remaining = conn.execute("SELECT checked_at FROM status_history").fetchall()
    conn.close()
    assert [r["checked_at"] for r in remaining] == [recent]

    # A missing/zero retention must never be read as "delete everything".
    assert db.prune_status_history(0) == 0
    assert db.prune_status_history(None) == 0


def test_uptime_query_is_answered_from_a_covering_index(isolated_db):
    """Guards the index actually being used - a plain (service_id, checked_at) index
    still 'works' but drops back to a full table scan for this query, which is the
    slow behavior this replaced."""
    conn = db.get_db()
    plan = conn.execute("""
        EXPLAIN QUERY PLAN
        SELECT service_id, COUNT(*), SUM(CASE WHEN status = 'down' THEN 1 ELSE 0 END)
        FROM status_history WHERE checked_at >= ? AND status != 'maintenance'
        GROUP BY service_id
    """, ("2000-01-01",)).fetchall()
    conn.close()
    assert any("COVERING INDEX idx_status_history_service_checked" in row[3] for row in plan), plan


def test_indexes_are_created_on_a_database_that_predates_them(isolated_db):
    """init_db() must be able to add an index to an existing database - the same
    problem _ensure_column() solves for columns. CREATE INDEX IF NOT EXISTS does
    apply to an existing table, unlike CREATE TABLE IF NOT EXISTS."""
    conn = db.get_db()
    conn.execute("DROP INDEX idx_status_history_service_checked")
    conn.commit()
    conn.close()

    db.init_db()

    conn = db.get_db()
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
    conn.close()
    assert "idx_status_history_service_checked" in names


def test_public_integrations_by_service_matches_the_per_service_filter(isolated_db):
    sid = db.create_service({"name": "S", "url": "", "check_url": "", "description": ""})
    other = db.create_service({"name": "O", "url": "", "check_url": "", "description": ""})
    shown = db.create_integration({"name": "shown", "kind": "jellyfin", "base_url": "http://x",
                                    "api_key": "k", "enabled": 1, "service_id": sid,
                                    "show_on_public": 1, "auto_incident": 0})
    db.create_integration({"name": "hidden", "kind": "jellyfin", "base_url": "http://x",
                            "api_key": "k", "enabled": 1, "service_id": sid,
                            "show_on_public": 0, "auto_incident": 0})
    db.create_integration({"name": "disabled", "kind": "jellyfin", "base_url": "http://x",
                            "api_key": "k", "enabled": 0, "service_id": other,
                            "show_on_public": 1, "auto_incident": 0})

    grouped = db.list_public_integrations_by_service()
    assert [i["id"] for i in grouped[sid]] == [shown]
    assert other not in grouped
    assert grouped[sid] == db.list_integrations_for_service(sid)


# ---------------------------------------------------------------------------
# Scheduled task rows
# ---------------------------------------------------------------------------
def test_ensure_task_row_is_idempotent_and_never_overwrites(isolated_db):
    db.ensure_task_row("t", {"enabled": 1, "schedule_kind": "interval",
                              "interval_minutes": 60, "daily_at": "03:00"})
    db.update_task_schedule("t", False, "daily", 15, "07:45")
    # A restart re-registers the task, calling ensure_task_row again with the
    # registry defaults - the admin's saved settings must survive that.
    db.ensure_task_row("t", {"enabled": 1, "schedule_kind": "interval",
                              "interval_minutes": 60, "daily_at": "03:00"})
    row = db.get_task_row("t")
    assert (row["enabled"], row["schedule_kind"], row["interval_minutes"], row["daily_at"]) \
        == (0, "daily", 15, "07:45")


def test_update_task_schedule_rejects_an_unknown_kind(isolated_db):
    db.ensure_task_row("t", {})
    db.update_task_schedule("t", True, "cron", 60, "03:00")
    assert db.get_task_row("t")["schedule_kind"] == "interval"


def test_update_task_schedule_clamps_the_interval_to_at_least_one_minute(isolated_db):
    db.ensure_task_row("t", {})
    db.update_task_schedule("t", True, "interval", 0, "03:00")
    assert db.get_task_row("t")["interval_minutes"] == 1


def test_record_task_run_leaves_the_schedule_alone(isolated_db):
    db.ensure_task_row("t", {})
    db.update_task_schedule("t", True, "daily", 30, "05:00")
    db.record_task_run("t", "failed", "went wrong", 12, "manual")
    row = db.get_task_row("t")
    assert (row["last_status"], row["last_message"], row["last_duration_ms"], row["last_trigger"]) \
        == ("failed", "went wrong", 12, "manual")
    assert (row["schedule_kind"], row["interval_minutes"], row["daily_at"]) == ("daily", 30, "05:00")


# ---------------------------------------------------------------------------
# The cached Jellyfin user list
# ---------------------------------------------------------------------------
def _jf_user(uid, name, admin=False, disabled=False):
    return {"id": uid, "name": name, "is_administrator": admin, "is_disabled": disabled}


def test_replace_jellyfin_users_stores_and_looks_up_case_insensitively(isolated_db):
    db.replace_jellyfin_users([_jf_user("abc", "Adam"), _jf_user("def", "Someone", admin=True)])
    assert db.count_jellyfin_users() == 2
    assert db.get_jellyfin_user_by_name("adam")["id"] == "abc"
    assert db.get_jellyfin_user_by_name("  ADAM  ")["id"] == "abc"
    assert db.get_jellyfin_user("def")["is_administrator"] == 1


def test_replace_jellyfin_users_is_a_full_replace(isolated_db):
    db.replace_jellyfin_users([_jf_user("a", "A"), _jf_user("b", "B")])
    db.replace_jellyfin_users([_jf_user("a", "A")])
    assert [u["id"] for u in db.list_jellyfin_users()] == ["a"]
    assert db.get_jellyfin_user_by_name("B") is None


def test_first_seen_at_is_preserved_across_syncs(isolated_db):
    """It has to keep meaning "when this portal first saw this account" rather than
    quietly becoming "when the last sync ran"."""
    db.replace_jellyfin_users([_jf_user("a", "A")])
    first_seen = db.get_jellyfin_user("a")["first_seen_at"]
    db.replace_jellyfin_users([_jf_user("a", "A renamed")])
    row = db.get_jellyfin_user("a")
    assert row["first_seen_at"] == first_seen
    assert row["name"] == "A renamed"
    assert row["last_synced_at"] >= first_seen


def test_a_rename_is_tracked_by_id_not_by_name(isolated_db):
    """Jellyfin's user GUID is stable across a rename; the username is not - which
    is exactly why the cache is keyed by id."""
    db.replace_jellyfin_users([_jf_user("a", "OldName")])
    db.replace_jellyfin_users([_jf_user("a", "NewName")])
    assert db.count_jellyfin_users() == 1
    assert db.get_jellyfin_user_by_name("oldname") is None
    assert db.get_jellyfin_user_by_name("newname")["id"] == "a"


def test_synced_at_distinguishes_never_synced_from_synced_and_empty(isolated_db):
    """An empty cache must never be readable as "this user does not exist"."""
    assert db.jellyfin_users_synced_at() is None
    db.replace_jellyfin_users([_jf_user("a", "A")])
    assert db.jellyfin_users_synced_at() is not None


def test_problem_reports_record_their_reporter(isolated_db):
    rid = db.create_problem_report("broken", "", None, reporter_user="adam")
    assert db.get_problem_report(rid)["reporter_user"] == "adam"
    anon = db.create_problem_report("also broken")
    assert db.get_problem_report(anon)["reporter_user"] == ""


# ---------------------------------------------------------------------------
# Report replies and per-user report history
# ---------------------------------------------------------------------------
def test_reports_are_scoped_to_their_own_reporter(isolated_db):
    """The one that must never regress: a signed-in user sees their reports and
    nobody else's."""
    db.create_problem_report("mine", "", None, reporter_user="adam", reporter_user_id="u1")
    db.create_problem_report("theirs", "", None, reporter_user="sam", reporter_user_id="u2")
    mine = db.list_reports_for_user("u1")
    assert [r["message"] for r in mine] == ["mine"]


def test_a_blank_user_id_matches_nothing(isolated_db):
    """Every anonymous report has reporter_user_id = '', so a caller passing an empty
    id must get nothing rather than the whole anonymous pile."""
    db.create_problem_report("anonymous one")
    db.create_problem_report("anonymous two")
    assert db.list_reports_for_user("") == []
    assert db.list_reports_for_user(None) == []
    assert db.count_unseen_replies("") == 0
    assert db.mark_replies_seen("") == 0


def test_a_reply_is_stored_and_starts_unseen(isolated_db):
    rid = db.create_problem_report("broken", "", None, reporter_user="adam", reporter_user_id="u1")
    db.set_problem_report_reply(rid, "  Looking into it now.  ")
    row = db.get_problem_report(rid)
    assert row["admin_reply"] == "Looking into it now."
    assert row["replied_at"] is not None
    assert row["reply_seen"] == 0
    assert db.count_unseen_replies("u1") == 1


def test_marking_replies_seen_clears_the_count(isolated_db):
    rid = db.create_problem_report("broken", "", None, reporter_user="adam", reporter_user_id="u1")
    db.set_problem_report_reply(rid, "Fixed.")
    assert db.mark_replies_seen("u1") == 1
    assert db.count_unseen_replies("u1") == 0
    # Nothing left to mark, so a second visit writes nothing at all.
    assert db.mark_replies_seen("u1") == 0


def test_editing_a_reply_makes_it_unread_again(isolated_db):
    """The text they read is no longer the text that's there, so it has to resurface."""
    rid = db.create_problem_report("broken", "", None, reporter_user="adam", reporter_user_id="u1")
    db.set_problem_report_reply(rid, "Fixed.")
    db.mark_replies_seen("u1")
    db.set_problem_report_reply(rid, "Actually, not fixed.")
    assert db.count_unseen_replies("u1") == 1


def test_an_empty_reply_clears_it(isolated_db):
    rid = db.create_problem_report("broken", "", None, reporter_user="adam", reporter_user_id="u1")
    db.set_problem_report_reply(rid, "Oops, wrong report.")
    db.set_problem_report_reply(rid, "")
    row = db.get_problem_report(rid)
    assert row["admin_reply"] == ""
    assert row["replied_at"] is None
    assert db.count_unseen_replies("u1") == 0


def test_a_linked_incident_is_reported_with_its_current_status(isolated_db):
    rid = db.create_problem_report("down", "", None, reporter_user="adam", reporter_user_id="u1")
    iid = db.create_incident({"title": "Jellyfin unreachable", "description": "",
                               "status": "investigating"})
    db.set_problem_report_incident(rid, iid)
    report = db.list_reports_for_user("u1")[0]
    assert report["incident_title"] == "Jellyfin unreachable"
    assert report["incident_status"] == "investigating"

    db.update_incident(iid, {"title": "Jellyfin unreachable", "description": "",
                              "status": "resolved", "service_id": None})
    assert db.list_reports_for_user("u1")[0]["incident_status"] == "resolved"


def test_a_deleted_incident_leaves_the_report_listable(isolated_db):
    """There's no FK to null the column out, so the LEFT JOIN has to cope."""
    rid = db.create_problem_report("down", "", None, reporter_user="adam", reporter_user_id="u1")
    iid = db.create_incident({"title": "Gone", "description": "", "status": "investigating"})
    db.set_problem_report_incident(rid, iid)
    db.delete_incident(iid)
    report = db.list_reports_for_user("u1")[0]
    assert report["incident_id"] == iid
    assert report["incident_title"] is None


def test_reports_follow_a_rename_because_they_key_off_the_user_id(isolated_db):
    """reporter_user is for display and freezes at submission time; reporter_user_id
    is what "my reports" looks up by."""
    db.create_problem_report("old", "", None, reporter_user="adam", reporter_user_id="u1")
    db.create_problem_report("new", "", None, reporter_user="adam-renamed", reporter_user_id="u1")
    assert len(db.list_reports_for_user("u1")) == 2


# ---------------------------------------------------------------------------
# User preferences
# ---------------------------------------------------------------------------
def test_preferences_default_without_a_stored_row(isolated_db):
    assert db.get_user_preferences("u1") == {"theme": "auto", "contact": ""}
    assert db.get_user_preferences("") == {"theme": "auto", "contact": ""}


def test_preferences_round_trip(isolated_db):
    db.set_user_preferences("u1", theme="light", contact="adam@example.invalid")
    assert db.get_user_preferences("u1") == {"theme": "light", "contact": "adam@example.invalid"}


def test_setting_only_the_theme_leaves_the_contact_alone(isolated_db):
    """The toggle button's endpoint only knows about the theme and must not be able
    to blank out anything else."""
    db.set_user_preferences("u1", theme="dark", contact="keep me")
    db.set_user_preferences("u1", theme="light")
    assert db.get_user_preferences("u1") == {"theme": "light", "contact": "keep me"}


def test_an_unknown_theme_is_ignored_rather_than_stored(isolated_db):
    db.set_user_preferences("u1", theme="rainbow")
    assert db.get_user_preferences("u1")["theme"] == "auto"
    db.set_user_preferences("u1", theme="dark")
    db.set_user_preferences("u1", theme="nonsense")
    assert db.get_user_preferences("u1")["theme"] == "dark"


def test_preferences_are_untouched_by_a_user_sync(isolated_db):
    """The reason preferences live in their own table: replace_jellyfin_users() is a
    full delete-and-reinsert, and anything stored there has to be explicitly carried
    across or it's silently wiped."""
    db.replace_jellyfin_users([{"id": "u1", "name": "adam"}])
    db.set_user_preferences("u1", theme="light", contact="me@example.invalid")
    db.replace_jellyfin_users([{"id": "u1", "name": "adam"}, {"id": "u2", "name": "sam"}])
    assert db.get_user_preferences("u1") == {"theme": "light", "contact": "me@example.invalid"}
