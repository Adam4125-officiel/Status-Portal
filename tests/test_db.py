import sqlite3

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


def test_list_incidents_before_id_continues_from_cursor(isolated_db):
    sid = db.list_services()[0]["id"]
    ids = [db.create_incident({"service_id": sid, "title": f"Incident {n}", "status": "resolved"}) for n in range(5)]
    first_page = db.list_incidents(limit=2)
    second_page = db.list_incidents(limit=2, before_id=first_page[-1]["id"])
    assert [i["id"] for i in first_page] == list(reversed(ids))[:2]
    assert [i["id"] for i in second_page] == list(reversed(ids))[2:4]


def test_list_incidents_before_id_ignores_max_age_days_filter(isolated_db):
    """Regression test for a real bug (2026-08-10): the "load more" endpoint used
    to re-apply the same max_age_days filter as the initial view, so anything
    older than the cutoff was permanently unreachable - the initial page hid it,
    and "load more" hid it again forever, defeating the point of the history
    feature entirely. before_id must page through the FULL unfiltered timeline
    regardless of max_age_days (max_age_days is only ever passed for the
    *initial* view, never alongside before_id in real app.py callers - this test
    confirms before_id-based calls surface old items even if the caller mistakenly
    passed a filter too, that path shouldn't behave the old broken way anyway)."""
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
    # max_age_days) must reveal both older incidents, not return empty.
    more = db.list_incidents(limit=10, before_id=initial[-1]["id"])
    assert {i["id"] for i in more} == {old1, old2}


def test_list_incidents_before_id_reaches_items_hidden_in_an_id_space_gap(isolated_db):
    """Regression test for a second, subtler version of the same 2026-08-10 bug: a
    still-open incident (never hidden, any age) can sit at a LOWER id than a
    newer incident that got resolved and aged out of the initial view - a gap in
    id-space between what's shown. Cursoring "load more" from the smallest shown
    id (the first fix attempt) skips straight over anything filtered out inside
    that gap, same missing-data bug one page later. app.py's template now seeds
    the cursor from the largest (newest) shown id instead, which this test
    exercises directly at the db layer."""
    sid = db.list_services()[0]["id"]
    old_open = db.create_incident({"service_id": sid, "title": "Old but still open", "status": "investigating"})
    hidden = db.create_incident({"service_id": sid, "title": "Newer but resolved+hidden", "status": "resolved"})
    recent = db.create_incident({"service_id": sid, "title": "Recent", "status": "investigating"})
    conn = db.get_db()
    conn.execute("UPDATE incidents SET resolved_at='2000-01-01T00:00:00' WHERE id=?", (hidden,))
    conn.commit()
    conn.close()

    # Initial (age-filtered) view: hidden's id sits *between* old_open and recent,
    # but only old_open and recent actually show - a gap.
    initial = db.list_incidents(limit=8, max_age_days=30)
    shown_ids = [i["id"] for i in initial]
    assert shown_ids == [recent, old_open]
    assert hidden not in shown_ids

    # Cursoring from the newest shown id (recent) must still reach the item
    # filtered out inside the gap.
    more = db.list_incidents(limit=10, before_id=initial[0]["id"])
    assert hidden in {i["id"] for i in more}


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
