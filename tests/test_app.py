import re

import app as app_module
import db


def _extract_csrf_token(html):
    match = re.search(r'name="csrf-token" content="([^"]+)"', html.decode())
    assert match, "csrf-token meta tag not found in response"
    return match.group(1)


def test_csrf_protection_rejects_missing_or_wrong_token(isolated_db, monkeypatch):
    """Dedicated test of the actual CSRF mechanism using a client that does NOT set
    TESTING=True (unlike the shared `client` fixture, which deliberately bypasses
    this check - see _check_csrf()'s docstring in app.py) - this is the one test
    that exercises the real protection rather than relying on it being disabled."""
    monkeypatch.setitem(app_module.app.config, "TESTING", False)
    with app_module.app.test_client() as c:
        get_resp = c.get("/admin/login")
        token = _extract_csrf_token(get_resp.data)

        # No token at all.
        resp = c.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
        assert resp.status_code == 400

        # Wrong token.
        resp = c.post("/admin/login", data={
            "password": "testpass123", "confirm": "testpass123", "csrf_token": "wrong"})
        assert resp.status_code == 400

        # Correct token - request succeeds normally.
        resp = c.post("/admin/login", data={
            "password": "testpass123", "confirm": "testpass123", "csrf_token": token})
        assert resp.status_code == 302


def test_compute_overall_status():
    assert app_module.compute_overall_status([]) == "operational"
    assert app_module.compute_overall_status([{"status": "operational"}, {"status": "degraded"}]) == "degraded"
    assert app_module.compute_overall_status([{"status": "down"}, {"status": "maintenance"}]) == "down"
    assert app_module.compute_overall_status([{"status": "maintenance"}]) == "maintenance"
    assert app_module.compute_overall_status([{"status": "operational"}, {"status": "slow"}]) == "slow"


def test_compute_overall_status_ignores_flagged_services():
    services = [
        {"status": "down", "ignore_in_overall_status": 1},
        {"status": "operational", "ignore_in_overall_status": 0},
    ]
    assert app_module.compute_overall_status(services) == "operational"

    # A down, non-ignored service among the rest still reports down as normal.
    services[1]["status"] = "down"
    services[1]["ignore_in_overall_status"] = 0
    assert app_module.compute_overall_status(services) == "down"

    # Every service ignored -> nothing left to aggregate over -> operational.
    assert app_module.compute_overall_status(
        [{"status": "down", "ignore_in_overall_status": 1}]) == "operational"
    assert app_module.compute_overall_status([{"status": "slow"}, {"status": "degraded"}]) == "degraded"


class _FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


def test_check_status_for_response_only_5xx_is_degraded():
    # A 401/403 login prompt (e.g. Bazarr's Basic Auth) or a 404 still means the
    # server answered - only an actual server-side (5xx) error counts as degraded.
    assert app_module._check_status_for_response(_FakeResponse(200), 50, None) == "operational"
    assert app_module._check_status_for_response(_FakeResponse(401), 50, None) == "operational"
    assert app_module._check_status_for_response(_FakeResponse(403), 50, None) == "operational"
    assert app_module._check_status_for_response(_FakeResponse(404), 50, None) == "operational"
    assert app_module._check_status_for_response(_FakeResponse(500), 50, None) == "degraded"
    assert app_module._check_status_for_response(_FakeResponse(503), 50, None) == "degraded"


def test_check_status_for_response_slow_threshold():
    assert app_module._check_status_for_response(_FakeResponse(200), 100, 2000) == "operational"
    assert app_module._check_status_for_response(_FakeResponse(200), 2500, 2000) == "slow"
    assert app_module._check_status_for_response(_FakeResponse(200), 2500, None) == "operational"
    # A slow response that's also a server error stays degraded, not slow.
    assert app_module._check_status_for_response(_FakeResponse(500), 2500, 2000) == "degraded"


def test_enrich_services_attaches_grace_and_retry_flags(isolated_db):
    sid = db.list_services()[0]["id"]
    db.update_service(sid, {**db.get_service(sid), "startup_grace_seconds": 999999})

    services = app_module._enrich_services(db.list_services())
    service = next(s for s in services if s["id"] == sid)
    assert service["in_grace_period"] is True
    assert service["retrying"] is False

    app_module._retry_in_progress.add(sid)
    try:
        services = app_module._enrich_services(db.list_services())
        service = next(s for s in services if s["id"] == sid)
        assert service["retrying"] is True
    finally:
        app_module._retry_in_progress.discard(sid)


def test_within_grace_period(monkeypatch):
    monkeypatch.setattr(app_module, "_APP_START", app_module.time.monotonic())
    assert app_module._within_grace_period({"startup_grace_seconds": 60}) is True
    assert app_module._within_grace_period({"startup_grace_seconds": 0}) is False
    assert app_module._within_grace_period({"startup_grace_seconds": None}) is False

    monkeypatch.setattr(app_module, "_APP_START", app_module.time.monotonic() - 120)
    assert app_module._within_grace_period({"startup_grace_seconds": 60}) is False


def test_check_service_status_no_retry_marks_down_on_first_failure(monkeypatch):
    calls = []

    def fake_get(url, timeout):
        calls.append(url)
        raise app_module.requests.RequestException("connection refused")

    monkeypatch.setattr(app_module.requests, "get", fake_get)
    sleeps = []
    monkeypatch.setattr(app_module.time, "sleep", lambda s: sleeps.append(s))

    service = {"check_url": "http://x", "slow_threshold_ms": None, "retry_count": 0,
               "retry_interval_seconds": 5}
    status, elapsed_ms = app_module._check_service_status(service)

    assert status == "down"
    assert elapsed_ms is None
    assert len(calls) == 1  # no retries when retry_count is 0 - same as historical behavior
    assert sleeps == []


def test_check_service_status_retries_and_recovers(monkeypatch):
    """A blip that resolves itself on the second attempt should never reach 'down'
    at all - this is the whole point of the retry feature."""
    responses = [
        app_module.requests.RequestException("timeout"),
        _FakeResponse(200),
    ]

    def fake_get(url, timeout):
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(app_module.requests, "get", fake_get)
    sleeps = []
    monkeypatch.setattr(app_module.time, "sleep", lambda s: sleeps.append(s))

    service = {"check_url": "http://x", "slow_threshold_ms": None, "retry_count": 3,
               "retry_interval_seconds": 10}
    status, elapsed_ms = app_module._check_service_status(service)

    assert status == "operational"
    assert sleeps == [10]  # stopped retrying the moment it recovered - not all 3 attempts


def test_check_service_status_tracks_retry_in_progress(monkeypatch):
    """While mid-retry (between attempts), the service's id must be visible in
    _retry_in_progress - this is what index() reads to show a "retrying" badge -
    and must be cleared again once the check finishes either way."""
    responses = [
        app_module.requests.RequestException("timeout"),
        _FakeResponse(200),
    ]

    def fake_get(url, timeout):
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(app_module.requests, "get", fake_get)
    seen_during_sleep = []

    def fake_sleep(seconds):
        seen_during_sleep.append(42 in app_module._retry_in_progress)

    monkeypatch.setattr(app_module.time, "sleep", fake_sleep)

    service = {"id": 42, "check_url": "http://x", "slow_threshold_ms": None,
               "retry_count": 3, "retry_interval_seconds": 10}
    app_module._check_service_status(service)

    assert seen_during_sleep == [True]
    assert 42 not in app_module._retry_in_progress  # cleared once the check finished


def test_check_service_status_exhausts_all_retries_then_down(monkeypatch):
    monkeypatch.setattr(app_module.requests, "get",
                         lambda url, timeout: (_ for _ in ()).throw(app_module.requests.RequestException("down")))
    sleeps = []
    monkeypatch.setattr(app_module.time, "sleep", lambda s: sleeps.append(s))

    service = {"check_url": "http://x", "slow_threshold_ms": None, "retry_count": 2,
               "retry_interval_seconds": 3}
    status, elapsed_ms = app_module._check_service_status(service)

    assert status == "down"
    assert sleeps == [3, 3]  # both retries used, still down


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


def test_auto_incident_opens_even_when_previous_status_was_already_down(isolated_db):
    """Regression test: a service whose failures were suppressed by a startup grace
    period still has services.status written to 'down' every cycle (status/response
    time are always recorded) - only the incident-lifecycle *call* was skipped. So
    the first call to this function once grace ends can legitimately be handed
    previous_status='down' (not 'operational') even though no incident has ever
    been opened yet. The open side must not require previous_status != 'down' to
    fire - that edge-trigger previously meant a service could stay down forever
    with no incident, once its downtime happened to start during its own grace
    window. Caught by live-testing the grace period feature end-to-end
    (2026-07-23), not originally caught by unit tests."""
    service = db.list_services()[0]
    assert db.get_open_auto_incident_for_service(service["id"]) is None

    app_module._handle_incident_lifecycle(service, previous_status="down", new_status="down")

    incident = db.get_open_auto_incident_for_service(service["id"])
    assert incident is not None, "an incident must open even without a fresh transition into 'down'"


def test_integration_auto_incident_opens_even_when_previous_reachable_was_already_false(isolated_db):
    """Same regression as test_auto_incident_opens_even_when_previous_status_was_already_down,
    for the integration-driven path (_integration_status_cache is likewise updated
    every cycle regardless of grace)."""
    service_id = db.list_services()[0]["id"]
    integration = {"id": 1, "name": "Jellyfin", "service_id": service_id}
    assert db.get_open_auto_incident_for_service(service_id) is None

    app_module._handle_integration_incident_lifecycle(integration, previous_reachable=False, new_reachable=False)

    incident = db.get_open_auto_incident_for_service(service_id)
    assert incident is not None


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


def test_admin_service_edit_persists_slow_threshold_and_grace_period(client):
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    sid = db.list_services()[0]["id"]

    client.post(f"/admin/services/{sid}/edit", data={
        "name": "Jellyfin", "url": "http://server:8096", "status": "operational",
        "slow_threshold_ms": "1500", "startup_grace_seconds": "90",
    })
    service = db.get_service(sid)
    assert service["slow_threshold_ms"] == 1500
    assert service["startup_grace_seconds"] == 90

    # Blank threshold means "disabled" (None), not 0.
    client.post(f"/admin/services/{sid}/edit", data={
        "name": "Jellyfin", "url": "http://server:8096", "status": "operational",
        "slow_threshold_ms": "", "startup_grace_seconds": "",
    })
    service = db.get_service(sid)
    assert service["slow_threshold_ms"] is None
    assert service["startup_grace_seconds"] == 0


def test_admin_service_edit_persists_retry_settings(client):
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    sid = db.list_services()[0]["id"]

    client.post(f"/admin/services/{sid}/edit", data={
        "name": "Jellyfin", "url": "http://server:8096", "status": "operational",
        "retry_count": "3", "retry_interval_seconds": "15",
    })
    service = db.get_service(sid)
    assert service["retry_count"] == 3
    assert service["retry_interval_seconds"] == 15

    # Blank fields fall back to the disabled/default values, not an error.
    client.post(f"/admin/services/{sid}/edit", data={
        "name": "Jellyfin", "url": "http://server:8096", "status": "operational",
        "retry_count": "", "retry_interval_seconds": "",
    })
    service = db.get_service(sid)
    assert service["retry_count"] == 0
    assert service["retry_interval_seconds"] == 5


def test_grace_period_gate_suppresses_incident_lifecycle(isolated_db, monkeypatch):
    """run_health_checks() itself is an untested infinite loop (pre-existing), but
    its incident-opening gate is `if s["auto_incident"] and not
    _within_grace_period(s): _handle_incident_lifecycle(...)` - this exercises that
    exact combination against a real service row."""
    monkeypatch.setattr(app_module, "_APP_START", app_module.time.monotonic())
    service = db.list_services()[0]
    db.update_service(service["id"], {**service, "startup_grace_seconds": 60, "auto_incident": 1})
    service = db.get_service(service["id"])

    calls = []
    monkeypatch.setattr(app_module, "_handle_incident_lifecycle", lambda *a, **k: calls.append(a))

    if service["auto_incident"] and not app_module._within_grace_period(service):
        app_module._handle_incident_lifecycle(service, "operational", "down")
    assert calls == []  # still within the grace window - suppressed

    monkeypatch.setattr(app_module, "_APP_START", app_module.time.monotonic() - 120)
    if service["auto_incident"] and not app_module._within_grace_period(service):
        app_module._handle_incident_lifecycle(service, "operational", "down")
    assert len(calls) == 1  # grace window elapsed - fires normally


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
    assert windows[0]["service_names"] == db.get_service(sid)["name"]

    resp = client.post(f"/admin/maintenance/{windows[0]['id']}/delete")
    assert resp.status_code == 302
    assert db.list_maintenance_windows() == []


def test_admin_incident_new_accepts_multiple_services(client):
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    services = db.list_services()
    s1, s2 = services[0]["id"], services[1]["id"]

    resp = client.post("/admin/incidents/new", data={
        "title": "Storage outage", "status": "investigating", "service_id": [str(s1), str(s2)],
    })
    assert resp.status_code == 302
    incident = db.list_incidents()[0]
    assert {s["id"] for s in incident["services"]} == {s1, s2}


def test_admin_maintenance_edit_route(client):
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    services = db.list_services()
    s1, s2 = services[0]["id"], services[1]["id"]

    client.post("/admin/maintenance/new", data={
        "service_id": s1, "title": "Upgrade", "starts_at": "2099-01-01T00:00", "ends_at": "2099-01-02T00:00",
    })
    mid = db.list_maintenance_windows()[0]["id"]

    resp = client.get(f"/admin/maintenance/{mid}/edit")
    assert resp.status_code == 200

    resp = client.post(f"/admin/maintenance/{mid}/edit", data={
        "service_id": [str(s1), str(s2)], "title": "Upgrade (rescheduled)",
        "starts_at": "2099-02-01T00:00", "ends_at": "2099-02-02T00:00",
    })
    assert resp.status_code == 302
    window = db.get_maintenance_window(mid)
    assert window["title"] == "Upgrade (rescheduled)"
    assert {s["id"] for s in window["services"]} == {s1, s2}


def test_admin_maintenance_edit_keeps_services_once_applied(client):
    """Once a window is applied (currently forcing maintenance), its service
    selector is disabled client-side and submits nothing - the route must treat a
    missing service_id list as "leave services alone", not "clear them"."""
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    sid = db.list_services()[0]["id"]

    client.post("/admin/maintenance/new", data={
        "service_id": sid, "title": "Ongoing", "starts_at": "2000-01-01T00:00", "ends_at": "2099-01-01T00:00",
    })
    mid = db.list_maintenance_windows()[0]["id"]
    assert db.get_maintenance_window(mid)["applied"] == 1

    resp = client.post(f"/admin/maintenance/{mid}/edit", data={
        "title": "Ongoing (renamed)", "starts_at": "2000-01-01T00:00", "ends_at": "2099-01-01T00:00",
    })
    assert resp.status_code == 302
    window = db.get_maintenance_window(mid)
    assert window["title"] == "Ongoing (renamed)"
    assert [s["id"] for s in window["services"]] == [sid]


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


def test_admin_discord_bot_page_shows_unconfigured_state(client, monkeypatch):
    import config
    monkeypatch.setattr(config, "DISCORD_BOT_TOKEN", "")
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    resp = client.get("/admin/discord-bot")
    assert resp.status_code == 200
    assert b"Not configured" in resp.data


def test_admin_discord_bot_settings_persist(client):
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    resp = client.post("/admin/discord-bot", data={
        "command_name": "!Portal Status", "update_presence": "on", "channel_command_enabled": "on",
        "include_services": "on", "include_incidents": "on", "resource_cpu": "on",
        "allowed_user_ids": "123,456\n789",
    })
    assert resp.status_code == 302
    assert db.get_setting("discordbot_command_name") == "portalstatus"  # sanitized on save
    assert db.get_setting("discordbot_update_presence") == "1"
    assert db.get_setting("discordbot_channel_command_enabled") == "1"
    assert db.get_setting("discordbot_include_announcements") == "0"  # omitted -> off
    assert db.get_setting("discordbot_resource_cpu") == "1"
    assert db.get_setting("discordbot_resource_memory") == "0"  # omitted -> off
    assert db.get_setting("discordbot_allowed_user_ids") == "123, 456, 789"  # normalized on save


def test_admin_discord_bot_guilds_page(client, monkeypatch):
    import config
    import discord_bot
    monkeypatch.setattr(config, "DISCORD_BOT_TOKEN", "fake-token")
    monkeypatch.setattr(discord_bot, "_state", {
        "connected": True, "user": "TestBot#0001", "last_error": None,
        "guilds": [{"id": "111", "name": "Home Lab", "channels": [{"id": "555", "name": "general"}]}],
    })
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})

    resp = client.get("/admin/discord-bot/guilds")
    assert resp.status_code == 200
    assert b"Home Lab" in resp.data
    assert b"general" in resp.data

    resp = client.post("/admin/discord-bot/guilds", data={"channel_whitelist": "555,666"})
    assert resp.status_code == 302
    assert db.get_setting("discordbot_channel_whitelist") == "555, 666"


def test_admin_host_control_route_rejects_unknown_action(client):
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    resp = client.post("/admin/resources/host-control", data={"action": "format-drive"})
    assert resp.status_code == 302
    resp = client.get("/admin/resources")
    assert b"Unknown host action" in resp.data


def test_admin_host_control_route_calls_monitoring_and_flashes_result(client, monkeypatch):
    """Only ever exercises this through a mocked monitoring.control_host() - never
    the real function, which would actually try to run a shutdown/restart command
    on whatever machine runs the test suite."""
    calls = []
    monkeypatch.setattr(app_module.monitoring, "control_host",
                         lambda action: calls.append(action) or (True, f"Host {action} command sent."))
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})

    resp = client.post("/admin/resources/host-control", data={"action": "restart"})

    assert resp.status_code == 302
    assert calls == ["restart"]
    resp = client.get("/admin/resources")
    assert b"Host restart command sent" in resp.data


def test_admin_host_control_requires_login(client):
    resp = client.post("/admin/resources/host-control", data={"action": "restart"})
    assert resp.status_code == 302
    assert "/admin/login" in resp.headers["Location"]


def test_admin_vm_control_route_calls_monitoring_and_flashes_result(client, monkeypatch):
    calls = []
    monkeypatch.setattr(app_module.monitoring, "control_vm",
                         lambda name, action: calls.append((name, action)) or (True, f"'{name}': {action} command sent."))
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})

    resp = client.post("/admin/resources/vm-control", data={"name": "web01", "action": "restart"})

    assert resp.status_code == 302
    assert calls == [("web01", "restart")]
    resp = client.get("/admin/resources")
    assert b"web01" in resp.data and b"command sent" in resp.data


def test_admin_vm_control_route_rejects_unknown_action(client):
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    resp = client.post("/admin/resources/vm-control", data={"name": "web01", "action": "delete-everything"})
    assert resp.status_code == 302
    resp = client.get("/admin/resources")
    assert b"Unknown VM action" in resp.data


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
    # Raw UTC stays as a data attribute (JS fallback / no-JS clients) - the browser
    # converts it to local time client-side, the server can't know the visitor's zone.
    assert b'class="local-time" data-utc="2000-01-01T00:00"' in resp.data


def test_public_page_incident_and_announcement_times_carry_utc_data_attribute(client):
    db.create_announcement({"title": "Heads up", "message": "test"})
    db.create_incident({"title": "Test outage", "status": "resolved"})

    resp = client.get("/")
    assert b'class="local-time" data-utc="' in resp.data
    assert b' UTC</span>' in resp.data  # no-JS fallback text still reads as UTC


def test_public_page_service_last_checked_carries_utc_data_attribute(isolated_db, client):
    service = db.list_services()[0]
    db.update_service(service["id"], {**service, "auto_check": 1, "check_url": "http://x"})
    db.update_service_status_from_check(service["id"], "operational", 42)

    resp = client.get("/")
    assert b'class="local-time-short" data-utc="' in resp.data
    assert b"42 ms" in resp.data


def test_service_without_url_hides_open_button(client):
    db.create_service({"name": "NoLinkService", "url": ""})
    resp = client.get("/")
    assert b"NoLinkService" in resp.data
    # crude but sufficient: no service card should render an Open link with an empty href
    assert b'href="" target="_blank" rel="noopener">Open' not in resp.data


def test_public_page_shows_jellyfin_tasks_when_enabled(client):
    import integrations as integrations_module
    db.set_setting("show_public_jellyfin_tasks", "1")
    integrations_module._jellyfin_activity_cache["running_tasks"] = ["Trickplay Image Extraction"]

    resp = client.get("/")
    assert b"Trickplay Image Extraction" in resp.data
    assert b"Jellyfin activity" in resp.data


def test_public_page_hides_jellyfin_tasks_when_disabled(client):
    import integrations as integrations_module
    # show_public_jellyfin_tasks left unset (default off)
    integrations_module._jellyfin_activity_cache["running_tasks"] = ["Trickplay Image Extraction"]

    resp = client.get("/")
    assert b"Trickplay Image Extraction" not in resp.data


def test_public_page_hides_jellyfin_activity_section_when_no_tasks_running(client):
    db.set_setting("show_public_jellyfin_tasks", "1")
    # _jellyfin_activity_cache["running_tasks"] is [] by default (reset per-test)
    resp = client.get("/")
    assert b"Jellyfin activity" not in resp.data
