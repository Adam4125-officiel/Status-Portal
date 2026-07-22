import app as app_module
import db


def test_compute_overall_status():
    assert app_module.compute_overall_status([]) == "operational"
    assert app_module.compute_overall_status([{"status": "operational"}, {"status": "degraded"}]) == "degraded"
    assert app_module.compute_overall_status([{"status": "down"}, {"status": "maintenance"}]) == "down"
    assert app_module.compute_overall_status([{"status": "maintenance"}]) == "maintenance"


def test_group_services():
    services = [
        {"group_name": "Media", "name": "Jellyfin"},
        {"group_name": "", "name": "Router"},
        {"group_name": "Media", "name": "Sonarr"},
    ]
    groups = app_module._group_services(services)
    assert [g["name"] for g in groups] == ["Media", ""]
    assert [s["name"] for s in groups[0]["services"]] == ["Jellyfin", "Sonarr"]
    assert [s["name"] for s in groups[1]["services"]] == ["Router"]


def test_richtext_filter():
    out = str(app_module.richtext_filter("**bold** http://example.com <script>alert(1)</script>"))
    assert "<strong>bold</strong>" in out
    assert '<a href="http://example.com"' in out
    assert "<script>" not in out  # escaped, never executed


def test_first_run_password_creation(client):
    resp = client.post("/admin/login", data={"password": "short"}, follow_redirects=True)
    assert b"at least 6 characters" in resp.data
    assert db.get_setting("admin_password_hash") is None

    resp = client.post("/admin/login", data={"password": "goodpass1", "confirm": "goodpass1"})
    assert resp.status_code == 302
    assert db.get_setting("admin_password_hash") is not None


def test_login_lockout(client):
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    client.get("/admin/logout")

    for _ in range(5):
        client.post("/admin/login", data={"password": "wrong"})

    resp = client.post("/admin/login", data={"password": "testpass123"}, follow_redirects=True)
    assert b"Too many failed attempts" in resp.data


def test_admin_requires_login(client):
    resp = client.get("/admin/services")
    assert resp.status_code == 302
    assert "/admin/login" in resp.headers["Location"]


def test_auto_incident_lifecycle_opens_and_resolves(isolated_db):
    service = db.list_services()[0]

    app_module._handle_incident_lifecycle(service, previous_status="operational", new_status="down")
    incident = db.get_open_auto_incident_for_service(service["id"])
    assert incident is not None
    assert incident["status"] == "investigating"

    app_module._handle_incident_lifecycle(service, previous_status="down", new_status="operational")
    assert db.get_incident(incident["id"])["status"] == "resolved"


def test_auto_incident_no_duplicate_while_still_down(isolated_db):
    service = db.list_services()[0]
    app_module._handle_incident_lifecycle(service, previous_status="operational", new_status="down")
    app_module._handle_incident_lifecycle(service, previous_status="operational", new_status="down")
    assert len(db.list_incidents()) == 1


def test_admin_service_edit_persists_auto_incident_toggle(client):
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    sid = db.list_services()[0]["id"]

    # Unchecking the box in the form (i.e. omitting it from POST data) should turn it off.
    client.post(f"/admin/services/{sid}/edit", data={
        "name": "Jellyfin", "url": "http://server:8096", "status": "operational",
    })
    assert db.get_service(sid)["auto_incident"] == 0

    client.post(f"/admin/services/{sid}/edit", data={
        "name": "Jellyfin", "url": "http://server:8096", "status": "operational", "auto_incident": "on",
    })
    assert db.get_service(sid)["auto_incident"] == 1


def test_404_renders_custom_error_page(client):
    resp = client.get("/this-route-does-not-exist")
    assert resp.status_code == 404
    assert b"Page not found" in resp.data


def test_wizard_combined_creates_service_and_linked_integration(client):
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    resp = client.post("/admin/new/combined", data={
        "name": "Sonarr", "icon": "📺", "description": "TV automation",
        "url": "http://localhost:1", "group_name": "Media",
        "kind": "arr", "api_key": "testkey", "show_on_public": "on",
    })
    assert resp.status_code == 302

    services = [s for s in db.list_services() if s["name"] == "Sonarr"]
    assert len(services) == 1
    integs = db.list_integrations_for_service(services[0]["id"])
    assert len(integs) == 1
    assert integs[0]["kind"] == "arr"
    assert integs[0]["base_url"] == "http://localhost:1"


def test_public_page_never_fetches_integrations_live(client, monkeypatch):
    """Regression test for a real bug: the public page used to call
    integrations.fetch_integration_status() directly on every request, so a slow or
    unreachable integration (confirmed: ~10s for the *Arr v3->v1 fallback alone) could
    block every single page load, including every auto-refresh cycle. Status must only
    ever come from the background-refreshed cache (app._integration_status_cache)."""
    import integrations as integrations_module

    def _boom(*args, **kwargs):
        raise AssertionError("index() must not call fetch_integration_status directly")

    monkeypatch.setattr(integrations_module, "fetch_integration_status", _boom)

    sid = db.create_service({"name": "Sonarr", "url": "http://unreachable.example"})
    db.create_integration({"name": "Sonarr", "kind": "arr", "base_url": "http://unreachable.example",
                            "api_key": "x", "enabled": 1, "service_id": sid, "show_on_public": 1})

    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Sonarr" in resp.data


def test_integration_status_cache_populates_public_display(isolated_db):
    sid = db.create_service({"name": "Sonarr", "url": "http://sonarr.example"})
    iid = db.create_integration({"name": "Sonarr", "kind": "arr", "base_url": "http://sonarr.example",
                                  "api_key": "x", "enabled": 1, "service_id": sid, "show_on_public": 1})

    services = app_module._attach_integration_status(db.list_services())
    service = [s for s in services if s["id"] == sid][0]
    assert service["integration_status"] is None  # nothing cached yet

    app_module._integration_status_cache[iid] = {
        "status": {"reachable": True, "version": "3.0", "issues": [], "error": None},
        "checked_at": "2026-01-01T00:00:00",
    }
    services = app_module._attach_integration_status(db.list_services())
    service = [s for s in services if s["id"] == sid][0]
    assert service["integration_status"]["reachable"] is True
    assert service["integration_severity"] == "ok"


def test_integration_auto_incident_opens_and_resolves(isolated_db):
    sid = db.create_service({"name": "Sonarr", "url": "http://sonarr.example"})
    integ = db.get_integration(db.create_integration({
        "name": "Sonarr", "kind": "arr", "base_url": "http://sonarr.example",
        "api_key": "x", "enabled": 1, "service_id": sid, "auto_incident": 1,
    }))

    app_module._handle_integration_incident_lifecycle(integ, previous_reachable=True, new_reachable=False)
    incident = db.get_open_auto_incident_for_service(sid)
    assert incident is not None

    app_module._handle_integration_incident_lifecycle(integ, previous_reachable=False, new_reachable=True)
    assert db.get_incident(incident["id"])["status"] == "resolved"


def test_integration_auto_incident_disabled_by_default(isolated_db):
    """auto_incident defaults to 0 - a failing integration shouldn't silently start
    opening incidents unless explicitly opted into, to avoid noise from flaky checks."""
    sid = db.create_service({"name": "Sonarr", "url": "http://sonarr.example"})
    iid = db.create_integration({"name": "Sonarr", "kind": "arr", "base_url": "http://sonarr.example",
                                  "api_key": "x", "enabled": 1, "service_id": sid})
    assert db.get_integration(iid)["auto_incident"] == 0


def test_integration_auto_incident_no_transition_on_first_check(isolated_db):
    """_refresh_integration_cache must not fire the lifecycle on an integration's very
    first check (no previous cache entry) - there's no real transition to react to."""
    sid = db.create_service({"name": "Sonarr", "url": "http://sonarr.example"})
    db.create_integration({"name": "Sonarr", "kind": "arr", "base_url": "http://sonarr.example",
                            "api_key": "x", "enabled": 1, "service_id": sid, "auto_incident": 1})

    import integrations as integrations_module
    import pytest as _pytest
    with _pytest.MonkeyPatch.context() as mp:
        mp.setattr(integrations_module, "fetch_integration_status",
                   lambda integ: {"reachable": False, "version": None, "issues": [], "error": "down"})
        app_module._refresh_integration_cache()
    assert db.get_open_auto_incident_for_service(sid) is None


def test_admin_maintenance_window_crud(client):
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    sid = db.list_services()[0]["id"]

    resp = client.post("/admin/maintenance/new", data={
        "service_id": sid, "title": "Upgrade", "description": "Disk swap",
        "starts_at": "2099-01-01T00:00", "ends_at": "2099-01-02T00:00",
    })
    assert resp.status_code == 302
    windows = db.list_maintenance_windows()
    assert len(windows) == 1
    assert windows[0]["service_name"] == db.get_service(sid)["name"]

    resp = client.post(f"/admin/maintenance/{windows[0]['id']}/delete")
    assert resp.status_code == 302
    assert db.list_maintenance_windows() == []


def test_admin_maintenance_window_with_past_start_applies_immediately(client):
    """Regression test: scheduling a window with a start time already in the past
    (e.g. logging maintenance that started two days ago and is still ongoing) must
    flip the service to 'maintenance' right away, not wait for the next background
    health-check cycle (which could be minutes away)."""
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    sid = db.list_services()[0]["id"]

    resp = client.post("/admin/maintenance/new", data={
        "service_id": sid, "title": "Ongoing disk swap",
        "starts_at": "2000-01-01T00:00", "ends_at": "2099-01-01T00:00",
    })
    assert resp.status_code == 302
    assert db.get_service(sid)["status"] == "maintenance"
    assert db.get_service(sid)["manual_override"] == 1


def test_auto_incident_lifecycle_fires_notifications(isolated_db, monkeypatch):
    calls = []
    monkeypatch.setattr(app_module.notifications, "notify", lambda title, msg: calls.append((title, msg)))
    service = db.list_services()[0]

    app_module._handle_incident_lifecycle(service, previous_status="operational", new_status="down")
    app_module._handle_incident_lifecycle(service, previous_status="down", new_status="operational")

    assert calls[0][0] == "Incident opened"
    assert calls[1][0] == "Incident resolved"


def test_incident_update_fires_notification(client, monkeypatch):
    calls = []
    monkeypatch.setattr(app_module.notifications, "notify", lambda title, msg: calls.append((title, msg)))
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    db.create_incident({"title": "Test outage", "status": "investigating"})
    iid = db.list_incidents()[0]["id"]

    client.post(f"/admin/incidents/{iid}/updates", data={"message": "Looking into it", "status": "identified"})

    assert len(calls) == 1
    assert "identified" in calls[0][0]
    assert "Looking into it" in calls[0][1]


def test_maintenance_events_fire_notifications(isolated_db, monkeypatch):
    calls = []
    monkeypatch.setattr(app_module.notifications, "notify", lambda title, msg: calls.append((title, msg)))
    sid = db.list_services()[0]["id"]
    db.create_maintenance_window({
        "service_id": sid, "title": "Upgrade", "starts_at": "2000-01-01T00:00", "ends_at": "2000-01-02T00:00",
    })
    app_module._process_maintenance_and_notify()

    # Both start and end are due at once here (window fully in the past) - both fire.
    titles = [c[0] for c in calls]
    assert "Maintenance started" in titles
    assert "Maintenance ended" in titles


def test_overall_badge_renders_svg(client):
    resp = client.get("/badge.svg")
    assert resp.status_code == 200
    assert resp.mimetype == "image/svg+xml"
    assert b"operational" in resp.data


def test_service_badge_renders_svg(client):
    sid = db.list_services()[0]["id"]
    resp = client.get(f"/badge/{sid}.svg")
    assert resp.status_code == 200
    assert b"Jellyfin" in resp.data


def test_service_badge_404s_for_unknown_service(client):
    resp = client.get("/badge/99999.svg")
    assert resp.status_code == 404


def test_badge_escapes_service_name(client):
    """A service name containing XML-special characters must not break the SVG or
    allow markup injection - this is admin-controlled input, but still worth guarding."""
    sid = db.create_service({"name": "<script>&\"'", "url": "http://example.com"})
    resp = client.get(f"/badge/{sid}.svg")
    assert resp.status_code == 200
    assert b"<script>" not in resp.data
    assert b"&lt;script&gt;" in resp.data


def test_feed_renders_valid_rss(client):
    db.create_announcement({"title": "Heads up", "message": "Doing some work tonight."})
    db.create_incident({"title": "Test outage", "status": "resolved"})

    resp = client.get("/feed.xml")
    assert resp.status_code == 200
    assert resp.mimetype == "application/rss+xml"

    import xml.etree.ElementTree as ET
    root = ET.fromstring(resp.data)
    items = root.findall("./channel/item")
    titles = [item.findtext("title") for item in items]
    assert any("Heads up" in t for t in titles)
    assert any("Test outage" in t for t in titles)


def test_feed_escapes_special_characters(client):
    db.create_announcement({"title": "<script>alert(1)</script>", "message": "A & B"})
    resp = client.get("/feed.xml")
    assert resp.status_code == 200
    assert b"<script>alert" not in resp.data
    import xml.etree.ElementTree as ET
    ET.fromstring(resp.data)  # must still parse as valid XML


def test_public_page_shows_active_maintenance_window(client):
    sid = db.list_services()[0]["id"]
    db.create_maintenance_window({
        "service_id": sid, "title": "Upgrade", "starts_at": "2000-01-01T00:00", "ends_at": "2099-01-01T00:00",
    })
    db.process_maintenance_windows()

    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Upgrade" in resp.data
    assert b"Scheduled maintenance" in resp.data


def test_service_without_url_hides_open_button(client):
    db.create_service({"name": "NoLinkService", "url": ""})
    resp = client.get("/")
    assert b"NoLinkService" in resp.data
    # crude but sufficient: no service card should render an Open link with an empty href
    assert b'href="" target="_blank" rel="noopener">Open' not in resp.data
