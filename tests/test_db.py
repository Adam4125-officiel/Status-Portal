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
