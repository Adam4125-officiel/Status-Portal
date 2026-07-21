import db


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
