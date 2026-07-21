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


def test_404_renders_custom_error_page(client):
    resp = client.get("/this-route-does-not-exist")
    assert resp.status_code == 404
    assert b"Page not found" in resp.data
