import io
import os
import re
import time

import pyotp
import pytest

import app as app_module
import db
import twofactor
import updater


def db_all_incident_ids():
    """Every incident id currently in the DB, newest first - stands in for "the
    ids the page is currently displaying" in the /api/incidents/more tests."""
    return [i["id"] for i in db.list_incidents()]


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
    # server answered - only an actual server-side (5xx) error counts against it.
    assert app_module._check_status_for_response(_FakeResponse(200), 50, None) == "operational"
    assert app_module._check_status_for_response(_FakeResponse(401), 50, None) == "operational"
    assert app_module._check_status_for_response(_FakeResponse(403), 50, None) == "operational"
    assert app_module._check_status_for_response(_FakeResponse(404), 50, None) == "operational"
    assert app_module._check_status_for_response(_FakeResponse(500), 50, None) == "degraded"
    assert app_module._check_status_for_response(_FakeResponse(503), 50, None) == "degraded"


def test_check_status_for_response_502_is_down():
    # 502 means whatever's in front of the service (reverse proxy/gateway)
    # couldn't reach it - treated as "down", not "degraded", unlike other 5xx.
    assert app_module._check_status_for_response(_FakeResponse(502), 50, None) == "down"
    assert app_module._check_status_for_response(_FakeResponse(502), 5000, 2000) == "down"


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


def _enable_totp_directly(secret=None):
    secret = secret or twofactor.generate_secret()
    db.set_setting("admin_totp_secret", secret)
    db.set_setting("admin_totp_enabled", "1")
    return secret


def test_login_with_2fa_requires_second_step(client):
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    secret = _enable_totp_directly()
    client.get("/admin/logout")

    # Correct password alone must not log in yet - a second, code-entry step follows.
    resp = client.post("/admin/login", data={"password": "testpass123"}, follow_redirects=True)
    assert b"Two-factor code" in resp.data
    resp = client.get("/admin/services")
    assert resp.status_code == 302  # still not logged in

    resp = client.post("/admin/login", data={"totp_code": pyotp.TOTP(secret).now()})
    assert resp.status_code == 302
    resp = client.get("/admin/services")
    assert resp.status_code == 200  # now actually logged in


def test_login_with_2fa_wrong_code_does_not_grant_access(client):
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    _enable_totp_directly()
    client.get("/admin/logout")
    client.post("/admin/login", data={"password": "testpass123"})

    resp = client.post("/admin/login", data={"totp_code": "000000"}, follow_redirects=True)
    assert b"Incorrect code" in resp.data
    resp = client.get("/admin/services")
    assert resp.status_code == 302  # still not logged in


def test_login_without_2fa_stays_single_step(client):
    """Regression guard: 2FA must remain fully optional - a fresh install with it
    never touched logs in from the password step alone, same as before it existed."""
    resp = client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    assert resp.status_code == 302
    resp = client.get("/admin/services")
    assert resp.status_code == 200


def test_admin_2fa_page_shows_disabled_by_default(client):
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    resp = client.get("/admin/2fa")
    assert b"Disabled" in resp.data
    assert b"Strongly recommended" in resp.data


def test_admin_2fa_enroll_and_enable_flow(client):
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    resp = client.get("/admin/2fa/enable")
    assert resp.status_code == 200
    assert b"<svg" in resp.data

    with client.session_transaction() as sess:
        secret = sess["pending_totp_secret"]

    resp = client.post("/admin/2fa/enable", data={"totp_code": pyotp.TOTP(secret).now()})
    assert resp.status_code == 302
    assert twofactor.is_enabled() is True

    resp = client.get("/admin/2fa")
    assert b"Enabled" in resp.data


def test_admin_2fa_enroll_wrong_code_does_not_enable(client):
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    client.get("/admin/2fa/enable")
    resp = client.post("/admin/2fa/enable", data={"totp_code": "000000"}, follow_redirects=True)
    assert b"Incorrect code" in resp.data
    assert twofactor.is_enabled() is False


def test_admin_2fa_enroll_post_without_a_prior_get_does_not_crash(client):
    """Regression test for a real 500 caught by live testing: a POST to this route
    with no pending secret in the session at all (e.g. it expired between GET and
    POST, or a direct POST) used to raise a bare KeyError instead of recovering."""
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    resp = client.post("/admin/2fa/enable", data={"totp_code": "000000"}, follow_redirects=True)
    assert resp.status_code == 200
    assert b"Incorrect code" in resp.data
    assert twofactor.is_enabled() is False


def test_admin_2fa_disable_requires_correct_code(client):
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    secret = _enable_totp_directly()

    resp = client.post("/admin/2fa/disable", data={"totp_code": "000000"}, follow_redirects=True)
    assert b"was not disabled" in resp.data
    assert twofactor.is_enabled() is True

    resp = client.post("/admin/2fa/disable", data={"totp_code": pyotp.TOTP(secret).now()})
    assert resp.status_code == 302
    assert twofactor.is_enabled() is False


def test_admin_host_control_step_up_2fa_blocks_without_code(client, monkeypatch):
    calls = []
    monkeypatch.setattr(app_module.monitoring, "control_host",
                         lambda action: calls.append(action) or (True, "Host restart command sent."))
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    _enable_totp_directly()

    resp = client.post("/admin/resources/host-control", data={"action": "restart"}, follow_redirects=True)
    assert b"2FA code" in resp.data
    assert calls == []  # control_host must never have been called


def test_admin_host_control_step_up_2fa_allows_with_correct_code(client, monkeypatch):
    calls = []
    monkeypatch.setattr(app_module.monitoring, "control_host",
                         lambda action: calls.append(action) or (True, "Host restart command sent."))
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    secret = _enable_totp_directly()

    resp = client.post("/admin/resources/host-control",
                        data={"action": "restart", "totp_code": pyotp.TOTP(secret).now()})
    assert resp.status_code == 302
    assert calls == ["restart"]


def test_admin_2fa_reset_flag_file(client, monkeypatch, tmp_path):
    flag_path = tmp_path / "RESET_2FA"
    monkeypatch.setattr(twofactor, "RESET_FLAG_PATH", str(flag_path))
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    _enable_totp_directly()
    client.get("/admin/logout")

    flag_path.write_text("")
    client.get("/admin/login")  # the reset check runs on every hit of this route
    assert twofactor.is_enabled() is False
    assert not flag_path.exists()

    # Login now works in a single step again, password alone.
    resp = client.post("/admin/login", data={"password": "testpass123"})
    assert resp.status_code == 302
    resp = client.get("/admin/services")
    assert resp.status_code == 200


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


def test_merge_api_health_off_passes_through_unchanged():
    assert app_module._merge_api_health("operational", "off", False) == "operational"
    assert app_module._merge_api_health("operational", "degrade", None) == "operational"


def test_merge_api_health_ignored_while_integration_reachable():
    assert app_module._merge_api_health("operational", "down", True) == "operational"


def test_merge_api_health_degrade_mode_raises_operational_and_slow():
    assert app_module._merge_api_health("operational", "degrade", False) == "degraded"
    assert app_module._merge_api_health("slow", "degrade", False) == "degraded"


def test_merge_api_health_degrade_mode_never_overrides_down():
    assert app_module._merge_api_health("down", "degrade", False) == "down"


def test_merge_api_health_down_mode_always_wins():
    assert app_module._merge_api_health("operational", "down", False) == "down"
    assert app_module._merge_api_health("degraded", "down", False) == "down"


def test_linked_integration_reachable_none_when_no_integration(isolated_db):
    sid = db.create_service({"name": "Sonarr", "url": "http://sonarr.example"})
    assert app_module._linked_integration_reachable(sid) is None


def test_linked_integration_reachable_none_when_not_yet_checked(isolated_db):
    sid = db.create_service({"name": "Sonarr", "url": "http://sonarr.example"})
    db.create_integration({"name": "Sonarr", "kind": "arr", "base_url": "http://sonarr.example",
                            "api_key": "x", "enabled": 1, "service_id": sid, "show_on_public": 1})
    assert app_module._linked_integration_reachable(sid) is None


def test_linked_integration_reachable_reads_cached_status(isolated_db):
    sid = db.create_service({"name": "Sonarr", "url": "http://sonarr.example"})
    iid = db.create_integration({"name": "Sonarr", "kind": "arr", "base_url": "http://sonarr.example",
                                  "api_key": "x", "enabled": 1, "service_id": sid, "show_on_public": 1})
    app_module._integration_status_cache[iid] = {
        "status": {"reachable": False, "version": None, "issues": [], "error": "down"},
        "checked_at": "2026-01-01T00:00:00",
    }
    assert app_module._linked_integration_reachable(sid) is False


def test_linked_integration_reachable_ignores_hidden_integration(isolated_db):
    """api_health_mode only reacts to an integration a visitor can actually see the
    sub-badge for (enabled + show_on_public) - a hidden one is ignored, same as
    list_integrations_for_service already filters for the public card itself."""
    sid = db.create_service({"name": "Sonarr", "url": "http://sonarr.example"})
    iid = db.create_integration({"name": "Sonarr", "kind": "arr", "base_url": "http://sonarr.example",
                                  "api_key": "x", "enabled": 1, "service_id": sid, "show_on_public": 0})
    app_module._integration_status_cache[iid] = {
        "status": {"reachable": False, "version": None, "issues": [], "error": "down"},
        "checked_at": "2026-01-01T00:00:00",
    }
    assert app_module._linked_integration_reachable(sid) is None


def test_admin_service_form_saves_api_health_mode(client):
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    resp = client.post("/admin/services/new", data={
        "name": "Sonarr", "status": "operational", "api_health_mode": "down",
    })
    assert resp.status_code == 302
    service = [s for s in db.list_services() if s["name"] == "Sonarr"][0]
    assert service["api_health_mode"] == "down"


def test_service_api_health_mode_defaults_to_off_for_invalid_value(isolated_db):
    sid = db.create_service({"name": "Sonarr", "url": "x", "api_health_mode": "bogus"})
    assert db.get_service(sid)["api_health_mode"] == "off"


def test_report_problem_get_renders_form(client):
    resp = client.get("/report")
    assert resp.status_code == 200
    assert b'name="message"' in resp.data
    assert b'name="website"' in resp.data  # honeypot


def test_report_problem_page_shows_site_name(client):
    """Regression test: the initial version of this route forgot to pass site_name
    to the template at all, leaving the topbar brand text and page title silently
    blank - caught by live-testing in a real browser, not by the route-level tests
    above (which only checked for the presence of form fields, not branding)."""
    db.set_setting("site_name", "My Home Server")
    resp = client.get("/report")
    html = resp.data.decode()
    assert "My Home Server" in html
    assert "<title>Report a problem — My Home Server</title>" in html

    with client.session_transaction() as sess:
        sess["report_form_rendered_at"] = time.time() - 10
    resp = client.post("/report", data={"message": ""})  # re-renders the form inline
    assert b"My Home Server" in resp.data


def test_report_problem_post_creates_report_and_notifies(client, monkeypatch):
    notified = []
    monkeypatch.setattr(app_module.notifications, "notify", lambda title, msg: notified.append((title, msg)))
    sid = db.list_services()[0]["id"]

    client.get(f"/report?service_id={sid}")  # sets the anti-spam timing session value
    app_module._report_state["window_start"] = 0.0
    app_module._report_state["count"] = 0
    with client.session_transaction() as sess:
        sess["report_form_rendered_at"] = time.time() - 10

    resp = client.post("/report", data={
        "message": "The Jellyfin card shows the wrong icon.", "service_id": str(sid), "contact": "me@example.com",
    })
    assert resp.status_code == 302
    reports = db.list_problem_reports()
    assert len(reports) == 1
    assert reports[0]["message"] == "The Jellyfin card shows the wrong icon."
    assert reports[0]["service_id"] == sid
    assert reports[0]["status"] == "new"
    assert notified and "Jellyfin card" in notified[0][1]


def test_report_problem_honeypot_silently_discards(client):
    with client.session_transaction() as sess:
        sess["report_form_rendered_at"] = time.time() - 10
    resp = client.post("/report", data={"message": "spam", "website": "http://spam.example"})
    assert resp.status_code == 302
    assert db.list_problem_reports() == []


def test_report_problem_rejects_too_fast_submission(client):
    with client.session_transaction() as sess:
        sess["report_form_rendered_at"] = time.time()  # submitted instantly
    resp = client.post("/report", data={"message": "too fast"})
    assert resp.status_code == 302
    assert db.list_problem_reports() == []


def test_report_problem_rate_limit(client, monkeypatch):
    monkeypatch.setattr(app_module, "_report_state", {"count": 0, "window_start": time.time()})
    monkeypatch.setattr(app_module, "REPORT_RATE_LIMIT", 2)
    for n in range(2):
        with client.session_transaction() as sess:
            sess["report_form_rendered_at"] = time.time() - 10
        client.post("/report", data={"message": f"report {n}"})
    assert len(db.list_problem_reports()) == 2

    with client.session_transaction() as sess:
        sess["report_form_rendered_at"] = time.time() - 10
    client.post("/report", data={"message": "one too many"})
    assert len(db.list_problem_reports()) == 2


def test_admin_reports_page_lists_and_updates_status(client):
    db.create_problem_report("Something broke", "", None)
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    resp = client.get("/admin/reports")
    assert resp.status_code == 200
    assert b"Something broke" in resp.data

    rid = db.list_problem_reports()[0]["id"]
    resp = client.post(f"/admin/reports/{rid}/status", data={"status": "resolved"})
    assert resp.status_code == 302
    assert db.get_problem_report(rid)["status"] == "resolved"


def test_admin_reports_unread_badge_shows_in_nav(client):
    db.create_problem_report("Unread one", "", None)
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    html = client.get("/admin/services").data.decode()
    assert 'class="nav-badge"' in html
    assert ">1<" in html


def test_public_service_card_shows_open_reports_count(client):
    sid = db.list_services()[0]["id"]
    other_sid = db.list_services()[1]["id"]
    db.create_problem_report("First issue", "", sid)
    db.create_problem_report("Second issue", "", sid)
    db.create_problem_report("General, no service", "", None)
    resolved_rid = db.create_problem_report("Old, already handled", "", sid)
    db.update_problem_report_status(resolved_rid, "resolved")

    html = client.get("/").data.decode()
    assert "2 open reports" in html  # resolved one excluded, general one not counted here

    other_html_section = html[html.index(f'href="/report?service_id={other_sid}"') - 400:
                               html.index(f'href="/report?service_id={other_sid}"')]
    assert "open report" not in other_html_section


def test_public_service_card_singular_report_wording(client):
    sid = db.list_services()[0]["id"]
    db.create_problem_report("Just one issue", "", sid)
    html = client.get("/").data.decode()
    assert "1 open report" in html
    assert "1 open reports" not in html


def test_public_service_card_no_indicator_with_zero_open_reports(client):
    html = client.get("/").data.decode()
    assert "open report" not in html


def test_api_status_includes_open_reports_count(client):
    sid = db.list_services()[0]["id"]
    db.create_problem_report("Issue", "", sid)
    data = client.get("/api/status").get_json()
    service = next(s for s in data["services"] if s["id"] == sid)
    assert service["open_reports_count"] == 1


def test_admin_report_create_incident(client):
    sid = db.list_services()[0]["id"]
    rid = db.create_problem_report("Jellyfin login is broken", "", sid)
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})

    resp = client.post(f"/admin/reports/{rid}/create-incident")
    assert resp.status_code == 302
    incidents = db.list_incidents()
    assert len(incidents) == 1
    assert "Jellyfin login is broken" in incidents[0]["title"]
    assert {s["id"] for s in incidents[0]["services"]} == {sid}
    assert db.get_problem_report(rid)["status"] == "resolved"


def test_admin_report_delete(client):
    rid = db.create_problem_report("To delete", "", None)
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    resp = client.post(f"/admin/reports/{rid}/delete")
    assert resp.status_code == 302
    assert db.get_problem_report(rid) is None


def test_public_service_card_links_to_report_form(client):
    sid = db.list_services()[0]["id"]
    html = client.get("/").data.decode()
    assert f"/report?service_id={sid}" in html


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


def _checked_service_checkbox_values(html):
    """Every <input type="checkbox" name="service_id" value="N" ...> tag's value,
    keyed to whether that specific tag carries the "checked" attribute - avoids
    false positives from unrelated "checked" substrings elsewhere on the page
    (e.g. field-hint prose)."""
    checked = set()
    for m in re.finditer(r'<input type="checkbox" name="service_id" value="(\d+)"([^>]*)>', html):
        if re.search(r'(?<![\w-])checked(?![\w-])', m.group(2)):
            checked.add(int(m.group(1)))
    return checked


def test_admin_maintenance_new_form_has_no_services_preselected(client):
    """Regression test for the operator-precedence bug where the "new" form's
    checkbox/option list defaulted every service to selected/checked regardless
    of what the admin actually wanted (see admin_maintenance_form.html)."""
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    resp = client.get("/admin/maintenance/new")
    assert resp.status_code == 200
    assert _checked_service_checkbox_values(resp.data.decode()) == set()


def test_admin_maintenance_edit_form_preselects_only_its_own_services(client):
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    services = db.list_services()
    s1, s2 = services[0]["id"], services[1]["id"]

    client.post("/admin/maintenance/new", data={
        "service_id": [str(s1)], "title": "Upgrade",
        "starts_at": "2099-01-01T00:00", "ends_at": "2099-01-02T00:00",
    })
    mid = db.list_maintenance_windows()[0]["id"]

    resp = client.get(f"/admin/maintenance/{mid}/edit")
    assert _checked_service_checkbox_values(resp.data.decode()) == {s1}


def test_admin_maintenance_new_rejects_zero_services(client):
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    resp = client.post("/admin/maintenance/new", data={
        "title": "Upgrade", "starts_at": "2099-01-01T00:00", "ends_at": "2099-01-02T00:00",
    })
    assert resp.status_code == 200
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


def test_admin_resources_vm_table_does_not_use_inline_onsubmit_with_vm_name(client, monkeypatch):
    """Regression test for a real XSS finding from this session's security review:
    a Hyper-V VM name (not necessarily set by the portal's own admin - anyone able
    to create/rename a VM on the host) used to be interpolated directly into an
    inline onsubmit="confirm('...')" handler. Jinja's HTML-attribute escaping
    doesn't protect a value that's re-parsed as JS after the browser HTML-decodes
    the attribute, so a name containing a quote could break out and inject script.
    Confirmation must be wired up from a plain data-* attribute + a JS listener
    instead, never string-built into an executable JS context server-side."""
    monkeypatch.setattr(app_module.monitoring, "get_cached_vm_snapshot", lambda: [
        {"name": "evil'); alert(1); //", "state": "Running", "uptime": "1h"},
    ])
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})

    resp = client.get("/admin/resources")

    assert resp.status_code == 200
    assert b"onsubmit" not in resp.data
    assert b'class="vm-control-form"' in resp.data
    assert b'data-vm-name="evil' in resp.data


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


def test_admin_system_page_renders(client):
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    resp = client.get("/admin/system")
    assert resp.status_code == 200
    assert b"Restart app" in resp.data
    assert b"Restart Discord bot" in resp.data


def test_admin_system_restart_app_calls_restart_process(client, monkeypatch):
    """Only ever exercises this through a mocked _restart_process() - never the
    real function, which would actually os.execv the running test-suite process."""
    calls = []
    monkeypatch.setattr(app_module, "_restart_process", lambda: calls.append("restart"))
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})

    resp = client.post("/admin/system/restart", data={"component": "app"})
    assert resp.status_code == 302
    assert calls == ["restart"]


def test_admin_system_restart_discord_bot_calls_discord_bot_restart(client, monkeypatch):
    calls = []
    monkeypatch.setattr(app_module.config, "DISCORD_BOT_TOKEN", "fake-token")
    monkeypatch.setattr(app_module.discord_bot, "restart", lambda: calls.append("restart"))
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})

    resp = client.post("/admin/system/restart", data={"component": "discord-bot"})
    assert resp.status_code == 302
    assert calls == ["restart"]


def test_admin_system_restart_discord_bot_requires_configured_token(client, monkeypatch):
    calls = []
    monkeypatch.setattr(app_module.config, "DISCORD_BOT_TOKEN", "")
    monkeypatch.setattr(app_module.discord_bot, "restart", lambda: calls.append("restart"))
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})

    resp = client.post("/admin/system/restart", data={"component": "discord-bot"}, follow_redirects=True)
    assert b"not configured" in resp.data
    assert calls == []


def test_admin_system_restart_rejects_unknown_component(client, monkeypatch):
    calls = []
    monkeypatch.setattr(app_module, "_restart_process", lambda: calls.append("restart"))
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})

    resp = client.post("/admin/system/restart", data={"component": "bogus"}, follow_redirects=True)
    assert b"Unknown restart target" in resp.data
    assert calls == []


def test_admin_system_restart_requires_login(client):
    resp = client.post("/admin/system/restart", data={"component": "app"})
    assert resp.status_code == 302
    assert "/admin/login" in resp.headers["Location"]


def test_admin_system_restart_step_up_2fa_blocks_without_code(client, monkeypatch):
    calls = []
    monkeypatch.setattr(app_module, "_restart_process", lambda: calls.append("restart"))
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    _enable_totp_directly()

    resp = client.post("/admin/system/restart", data={"component": "app"}, follow_redirects=True)
    assert b"2FA code" in resp.data
    assert calls == []


def test_admin_system_restart_step_up_2fa_allows_with_correct_code(client, monkeypatch):
    calls = []
    monkeypatch.setattr(app_module, "_restart_process", lambda: calls.append("restart"))
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    secret = _enable_totp_directly()

    resp = client.post("/admin/system/restart",
                        data={"component": "app", "totp_code": pyotp.TOTP(secret).now()})
    assert resp.status_code == 302
    assert calls == ["restart"]


# ---------------------------------------------------------------------------
# About page / in-app update
# ---------------------------------------------------------------------------
# Every test here mocks updater.perform_update() and app._restart_process(). A real
# update is never downloaded and the test-suite process is never os.execv'd - same
# standing rule as monitoring.control_host().
def _login(client):
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})


def _fake_update_available(latest="9.9.9"):
    app_module.updater._update_cache["result"] = {
        "ok": True, "error": None, "channel": "stable", "current": app_module.config.VERSION,
        "current_display": app_module.config.VERSION_DISPLAY, "latest": latest,
        "latest_url": "https://example.invalid/release", "latest_name": f"v{latest}",
        "published_at": "2026-08-10T12:00:00Z", "prerelease": False,
        "update_available": True, "ahead": False, "checked_at": "2026-08-10T12:00:00+00:00",
    }


@pytest.fixture(autouse=True)
def _update_test_environment(monkeypatch):
    """Two module-level globals that would otherwise leak between tests:

    * the update cache - one test's fake result must not show up in the next (or in
      the admin nav's "update available" badge);
    * config.IS_GIT_CHECKOUT, which is genuinely True while running the suite from
      this repo. Every update route and the About page's button correctly refuse in
      that state, so tests exercising them have to stand in for a normal install
      (an extracted release zip, no .git). The dedicated
      test_update_route_refuses_on_a_git_checkout sets it back to True.
    """
    monkeypatch.setattr(app_module.config, "IS_GIT_CHECKOUT", False)
    app_module.updater._update_cache["result"] = None
    app_module.updater._update_cache["refreshed_monotonic"] = None
    yield
    app_module.updater._update_cache["result"] = None
    app_module.updater._update_cache["refreshed_monotonic"] = None


def test_about_page_renders_version_and_paths(client):
    _login(client)
    resp = client.get("/admin/about")
    assert resp.status_code == 200
    assert app_module.config.VERSION_DISPLAY.encode() in resp.data
    assert b"AGPL" in resp.data
    assert b"portal.db" in resp.data


def test_about_page_requires_login(client):
    resp = client.get("/admin/about")
    assert resp.status_code == 302
    assert "/admin/login" in resp.headers["Location"]


def test_about_page_does_not_check_github_inline(client, monkeypatch):
    """The standing no-slow-I/O-in-a-request-handler rule: rendering the page must
    never make an outbound call, only read the cache the background loop fills."""
    def boom(*a, **k):
        raise AssertionError("the About page made an outbound HTTP call")
    monkeypatch.setattr(app_module.updater.requests, "get", boom)
    _login(client)
    resp = client.get("/admin/about")
    assert resp.status_code == 200
    assert b"Not checked yet" in resp.data


def test_about_page_degrades_gracefully_when_the_check_failed(client):
    app_module.updater._update_cache["result"] = {
        "ok": False, "error": "Could not reach GitHub: timed out", "channel": "stable",
        "current": "1.0.0", "current_display": "1.0.0", "latest": None,
        "latest_url": "", "published_at": None, "prerelease": None,
        "update_available": False, "ahead": False, "checked_at": "2026-08-10T12:00:00+00:00",
    }
    _login(client)
    resp = client.get("/admin/about")
    assert resp.status_code == 200
    assert "Couldn’t check".encode() in resp.data
    assert b"timed out" in resp.data


def test_about_check_now_refreshes_the_cache(client, monkeypatch):
    calls = []
    monkeypatch.setattr(app_module.updater, "refresh_update_cache_if_stale",
                        lambda **k: calls.append(k) or {"ok": True, "update_available": False,
                                                        "ahead": False, "current": "1.0.0",
                                                        "channel": "stable", "latest": "1.0.0"})
    _login(client)
    resp = client.post("/admin/about/check", follow_redirects=True)
    assert b"Up to date" in resp.data
    assert calls == [{"force": True}]


def test_about_settings_saves_channel_and_clears_the_stale_cache(client):
    _fake_update_available()
    _login(client)
    resp = client.post("/admin/about/settings",
                        data={"update_channel": "unstable", "update_check_enabled": "on"},
                        follow_redirects=True)
    assert b"Update preferences saved" in resp.data
    assert db.get_setting("update_channel") == "unstable"
    assert db.get_setting("update_check_enabled") == "1"
    # The cached "latest" belonged to the old channel.
    assert app_module.updater.get_cached_update_status() is None


def test_about_settings_rejects_an_unknown_channel(client):
    _login(client)
    resp = client.post("/admin/about/settings", data={"update_channel": "nightly"},
                        follow_redirects=True)
    assert b"Unknown update channel" in resp.data


def test_update_route_runs_the_update_and_restarts(client, monkeypatch):
    updates, restarts, markers = [], [], []
    monkeypatch.setattr(app_module.updater, "perform_update",
                        lambda **k: updates.append(k) or {"applied": True, "current": "1.0.0",
                                                          "latest": "9.9.9", "backup": "bk1"})
    monkeypatch.setattr(app_module.updater, "write_pending_marker",
                        lambda backup, version: markers.append((backup, version)))
    monkeypatch.setattr(app_module, "_restart_process", lambda: restarts.append("restart"))
    _login(client)

    resp = client.post("/admin/about/update")
    assert resp.status_code == 302
    assert len(updates) == 1
    assert restarts == ["restart"]
    # The marker is written BEFORE the restart, so the next start can confirm it.
    assert markers == [("bk1", "9.9.9")]


def test_update_route_does_not_restart_when_there_was_nothing_to_do(client, monkeypatch):
    restarts = []
    monkeypatch.setattr(app_module.updater, "perform_update",
                        lambda **k: {"applied": False, "reason": "already up to date"})
    monkeypatch.setattr(app_module, "_restart_process", lambda: restarts.append("restart"))
    _login(client)

    resp = client.post("/admin/about/update", follow_redirects=True)
    assert b"Nothing to update" in resp.data
    assert restarts == []


def test_update_route_reports_a_failure_without_restarting(client, monkeypatch):
    restarts = []

    def boom(**k):
        raise app_module.updater.UpdateError("SHA-256 mismatch against the release metadata.")
    monkeypatch.setattr(app_module.updater, "perform_update", boom)
    monkeypatch.setattr(app_module, "_restart_process", lambda: restarts.append("restart"))
    _login(client)

    resp = client.post("/admin/about/update", follow_redirects=True)
    assert b"SHA-256 mismatch" in resp.data
    assert restarts == []


def test_update_route_requires_login(client):
    resp = client.post("/admin/about/update")
    assert resp.status_code == 302
    assert "/admin/login" in resp.headers["Location"]


def test_update_route_step_up_2fa_blocks_without_code(client, monkeypatch):
    calls = []
    monkeypatch.setattr(app_module.updater, "perform_update", lambda **k: calls.append(k))
    monkeypatch.setattr(app_module, "_restart_process", lambda: calls.append("restart"))
    _login(client)
    _enable_totp_directly()

    resp = client.post("/admin/about/update", follow_redirects=True)
    assert b"2FA code" in resp.data
    assert calls == []


def test_update_route_step_up_2fa_allows_with_correct_code(client, monkeypatch):
    calls = []
    monkeypatch.setattr(app_module.updater, "perform_update",
                        lambda **k: {"applied": True, "current": "1.0.0", "latest": "9.9.9",
                                     "backup": "bk1"})
    monkeypatch.setattr(app_module.updater, "write_pending_marker", lambda *a: None)
    monkeypatch.setattr(app_module, "_restart_process", lambda: calls.append("restart"))
    _login(client)
    secret = _enable_totp_directly()

    resp = client.post("/admin/about/update", data={"totp_code": pyotp.TOTP(secret).now()})
    assert resp.status_code == 302
    assert calls == ["restart"]


def test_update_route_honours_the_env_kill_switch(client, monkeypatch):
    """PORTAL_ENABLE_INAPP_UPDATE=false must block the button even for a fully
    authenticated admin - the whole point is that it isn't flippable from the panel."""
    calls = []
    monkeypatch.setattr(app_module.config, "ENABLE_INAPP_UPDATE", False)
    monkeypatch.setattr(app_module.updater, "perform_update", lambda **k: calls.append(k))
    monkeypatch.setattr(app_module, "_restart_process", lambda: calls.append("restart"))
    _login(client)

    resp = client.post("/admin/about/update", follow_redirects=True)
    assert b"In-app updates are disabled" in resp.data
    assert calls == []


def test_update_route_refuses_on_a_git_checkout(client, monkeypatch):
    calls = []
    monkeypatch.setattr(app_module.config, "IS_GIT_CHECKOUT", True)
    monkeypatch.setattr(app_module.updater, "perform_update", lambda **k: calls.append(k))
    monkeypatch.setattr(app_module, "_restart_process", lambda: calls.append("restart"))
    _login(client)

    resp = client.post("/admin/about/update", follow_redirects=True)
    assert b"git checkout" in resp.data
    assert calls == []


def test_admin_nav_shows_a_badge_only_when_an_update_is_available(client):
    _login(client)
    assert b'href="/admin/about"' in client.get("/admin/services").data

    _fake_update_available()
    data = client.get("/admin/services").data
    about_link = data.split(b'href="/admin/about"')[1][:120]
    assert b"nav-badge" in about_link


def test_admin_update_button_is_disabled_until_a_check_finds_something(client):
    _login(client)
    resp = client.get("/admin/about")
    assert b'id="update-trigger"' in resp.data
    button = resp.data.split(b'id="update-trigger"')[1][:200]
    assert b"disabled" in button


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


def test_public_section_order_defaults_to_original_order(isolated_db):
    order = app_module._public_section_order()
    assert order == ["announcements", "services", "incidents", "info", "resources", "vms", "jellyfin_activity"]


def test_public_section_order_respects_stored_setting(isolated_db):
    db.set_setting("public_layout_order", "vms,services,announcements")
    order = app_module._public_section_order()
    # Stored order first, then every other valid key appended in its default position.
    assert order[:3] == ["vms", "services", "announcements"]
    assert set(order) == {"announcements", "services", "incidents", "info", "resources", "vms", "jellyfin_activity"}


def test_public_section_order_drops_unknown_keys(isolated_db):
    db.set_setting("public_layout_order", "vms,not-a-real-section,services")
    order = app_module._public_section_order()
    assert "not-a-real-section" not in order
    assert order[:2] == ["vms", "services"]


def test_admin_settings_general_saves_layout_order(client):
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    resp = client.post("/admin/settings/general", data={"layout_order": "vms,services,incidents,announcements,info,resources,jellyfin_activity"})
    assert resp.status_code == 302
    assert db.get_setting("public_layout_order") == "vms,services,incidents,announcements,info,resources,jellyfin_activity"
    assert app_module._public_section_order()[0] == "vms"


def test_admin_settings_general_saves_service_defaults(client):
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    resp = client.post("/admin/settings/general", data={
        "service_default_slow_threshold_ms": "1500",
        "service_default_startup_grace_seconds": "30",
        "service_default_retry_count": "2",
        "service_default_retry_interval_seconds": "10",
        # service_default_auto_incident intentionally omitted (unchecked)
    })
    assert resp.status_code == 302
    defaults = app_module._service_defaults()
    assert defaults == {
        "slow_threshold_ms": "1500", "startup_grace_seconds": "30",
        "retry_count": "2", "retry_interval_seconds": "10", "auto_incident": False,
        "api_health_mode": "off",
    }


def test_admin_service_new_form_prefills_from_defaults(client):
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    client.post("/admin/settings/general", data={"service_default_slow_threshold_ms": "1500"})
    resp = client.get("/admin/services/new")
    assert b'id="slow_threshold_ms"' in resp.data
    assert b'value="1500"' in resp.data


def test_admin_service_new_defaults_do_not_affect_existing_services(client):
    """Pre-fill only, never live-cascading - changing defaults after a service
    already exists must not retroactively change that service's own values."""
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    sid = db.list_services()[0]["id"]
    before = db.get_service(sid)["slow_threshold_ms"]
    client.post("/admin/settings/general", data={"service_default_slow_threshold_ms": "9999"})
    assert db.get_service(sid)["slow_threshold_ms"] == before


def test_admin_settings_logo_upload_and_render(client, tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "LOGO_UPLOAD_DIR", str(tmp_path / "uploads"))
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})

    resp = client.post("/admin/settings/logo", data={
        "logo": (io.BytesIO(b"fake-png-bytes"), "mylogo.png"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 302
    assert db.get_setting("site_logo_filename") == "logo.png"
    assert os.path.exists(os.path.join(str(tmp_path / "uploads"), "logo.png"))

    # Both the public page and an admin page should render the logo <img>/favicon.
    public_html = client.get("/").data.decode()
    assert 'class="brand-logo"' in public_html
    assert "uploads/logo.png" in public_html
    admin_html = client.get("/admin/settings").data.decode()
    assert 'rel="icon"' in admin_html
    assert "uploads/logo.png" in admin_html


def test_admin_settings_logo_rejects_bad_extension(client, tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "LOGO_UPLOAD_DIR", str(tmp_path / "uploads"))
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    resp = client.post("/admin/settings/logo", data={
        "logo": (io.BytesIO(b"#!/bin/sh\n"), "evil.sh"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 302
    assert db.get_setting("site_logo_filename", "") == ""


def test_admin_settings_logo_remove(client, tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "LOGO_UPLOAD_DIR", str(tmp_path / "uploads"))
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    client.post("/admin/settings/logo", data={
        "logo": (io.BytesIO(b"fake-png-bytes"), "mylogo.png"),
    }, content_type="multipart/form-data")
    logo_path = os.path.join(str(tmp_path / "uploads"), "logo.png")
    assert os.path.exists(logo_path)

    resp = client.post("/admin/settings/logo/remove")
    assert resp.status_code == 302
    assert db.get_setting("site_logo_filename", "") == ""
    assert not os.path.exists(logo_path)

    public_html = client.get("/").data.decode()
    assert 'class="brand-logo"' not in public_html
    assert '<span class="dot-server">' in public_html


def test_admin_settings_logo_reupload_different_extension_removes_old_file(client, tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "LOGO_UPLOAD_DIR", str(tmp_path / "uploads"))
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    client.post("/admin/settings/logo", data={
        "logo": (io.BytesIO(b"fake-png-bytes"), "mylogo.png"),
    }, content_type="multipart/form-data")
    png_path = os.path.join(str(tmp_path / "uploads"), "logo.png")
    assert os.path.exists(png_path)

    client.post("/admin/settings/logo", data={
        "logo": (io.BytesIO(b"<svg></svg>"), "mylogo.svg"),
    }, content_type="multipart/form-data")
    assert not os.path.exists(png_path)
    assert os.path.exists(os.path.join(str(tmp_path / "uploads"), "logo.svg"))
    assert db.get_setting("site_logo_filename") == "logo.svg"


def test_api_incidents_more_returns_html_fragment(client):
    sid = db.list_services()[0]["id"]
    ids = [db.create_incident({"service_id": sid, "title": f"Incident {n}", "status": "resolved"})
           for n in range(3)]

    resp = client.get(f"/api/incidents/more?seen={ids[2]}")
    assert resp.status_code == 200
    assert resp.content_type.startswith("text/html")
    html = resp.data.decode()
    assert "Incident 0" in html
    assert "Incident 1" in html
    assert "Incident 2" not in html  # already shown, must not come back
    # Not a full page - no <html>/<head>, just the item markup.
    assert "<html" not in html


def test_api_incidents_more_empty_past_the_end(client):
    sid = db.list_services()[0]["id"]
    ids = [db.create_incident({"service_id": sid, "title": f"Incident {n}", "status": "resolved"})
           for n in range(2)]
    all_seen = ",".join(str(i) for i in db_all_incident_ids())
    resp = client.get(f"/api/incidents/more?seen={all_seen}")
    assert resp.status_code == 200
    assert resp.data.decode().strip() == ""


def test_api_incidents_more_never_repeats_a_visible_incident(client):
    """Regression test for the user-reported symptom that made "Load more" look
    completely broken: with nothing hidden, it re-appended the whole visible
    list on every click, indefinitely."""
    sid = db.list_services()[0]["id"]
    for n in range(3):
        db.create_incident({"service_id": sid, "title": f"Visible {n}", "status": "resolved"})

    visible = ",".join(str(i) for i in db_all_incident_ids())
    resp = client.get(f"/api/incidents/more?seen={visible}")
    assert resp.data.decode().strip() == ""


def test_api_incidents_more_fails_closed_without_seen_list(client):
    """A request with no `seen` list is never a real "load more" click - it's a
    stale cached copy of an older public_history.js still sending the previous
    release's ?offset= parameter. Answering it with the newest page is what made
    such a client append the same incidents forever, so it must return nothing."""
    sid = db.list_services()[0]["id"]
    db.create_incident({"service_id": sid, "title": "Should not leak", "status": "resolved"})

    assert client.get("/api/incidents/more").data.decode().strip() == ""
    assert client.get("/api/incidents/more?offset=0").data.decode().strip() == ""
    assert client.get("/api/incidents/more?offset=10").data.decode().strip() == ""
    assert client.get("/api/incidents/more?seen=").data.decode().strip() == ""


def test_api_incidents_more_rejects_an_oversized_seen_list(client):
    too_many = ",".join(str(n) for n in range(app_module.SEEN_IDS_LIMIT + 1))
    assert client.get(f"/api/incidents/more?seen={too_many}").data.decode().strip() == ""


def test_api_incidents_more_reveals_incidents_hidden_by_history_days(client):
    """Regression test for a real bug (2026-08-10): "load more" used to re-apply
    the same max_age_days filter as the initial view, so an incident older than
    the cutoff was hidden by the initial page AND by "load more" - permanently
    unreachable, defeating the entire point of the history feature. Reported by
    the user testing a 3-day cutoff with 2 old + 1 recent incident: only the
    recent one ever showed, "load more" always came back empty."""
    sid = db.list_services()[0]["id"]
    old = db.create_incident({"service_id": sid, "title": "Old one", "status": "resolved"})
    recent = db.create_incident({"service_id": sid, "title": "Recent one", "status": "resolved"})
    conn = db.get_db()
    conn.execute("UPDATE incidents SET resolved_at='2000-01-01T00:00:00' WHERE id=?", (old,))
    conn.commit()
    conn.close()
    db.set_setting("public_history_days", "30")

    index_html = client.get("/").data.decode()
    assert "Recent one" in index_html
    assert "Old one" not in index_html  # correctly hidden from the initial view

    resp = client.get(f"/api/incidents/more?seen={recent}")
    assert "Old one" in resp.data.decode()  # but reachable via "load more"


def test_api_maintenance_history_returns_ended_windows_only(client):
    sid = db.list_services()[0]["id"]
    db.create_maintenance_window({
        "service_id": sid, "title": "Past window", "starts_at": "2000-01-01T00:00", "ends_at": "2000-01-02T00:00",
    })
    db.process_maintenance_windows()

    resp = client.get("/api/maintenance/history?offset=0")
    assert resp.status_code == 200
    html = resp.data.decode()
    assert "Past window" in html
    assert "Ended" in html


def test_api_maintenance_history_ignores_history_days_setting(client):
    """Same fix as api_incidents_more() above: maintenance history must not be
    filtered by public_history_days either, or an old ended window would become
    permanently unreachable through "Show maintenance history"."""
    sid = db.list_services()[0]["id"]
    db.create_maintenance_window({
        "service_id": sid, "title": "Long ago", "starts_at": "2000-01-01T00:00", "ends_at": "2000-01-02T00:00",
    })
    db.process_maintenance_windows()
    db.set_setting("public_history_days", "1")

    resp = client.get("/api/maintenance/history?offset=0")
    assert "Long ago" in resp.data.decode()


def test_public_index_never_shows_ended_maintenance_by_default(client):
    sid = db.list_services()[0]["id"]
    db.create_maintenance_window({
        "service_id": sid, "title": "Past window", "starts_at": "2000-01-01T00:00", "ends_at": "2000-01-02T00:00",
    })
    db.process_maintenance_windows()

    html = client.get("/").data.decode()
    assert "Past window" not in html
    assert "Maintenance history" in html


def test_admin_settings_general_saves_public_history_days(client):
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    resp = client.post("/admin/settings/general", data={"public_history_days": "45"})
    assert resp.status_code == 302
    assert db.get_setting("public_history_days") == "45"
    assert app_module._public_history_days() == 45


def test_public_history_days_blank_means_unlimited(client):
    assert app_module._public_history_days() is None


def test_public_page_renders_sections_in_configured_order(client):
    """End-to-end: the stored order must actually change relative position in the
    rendered HTML, not just the value _public_section_order() returns."""
    db.set_setting("public_layout_order", "info,services,incidents,vms,resources,jellyfin_activity,announcements")

    resp = client.get("/")
    html = resp.data.decode()
    # "Practical info" and "Services" both always render regardless of data present -
    # "info" was moved ahead of "services" in the order above (default order has it
    # the other way around).
    assert html.index("Practical info") < html.index(">Services<")


def test_public_page_footer_links_to_github_repo(client):
    resp = client.get("/")
    html = resp.data.decode()
    assert updater.REPO_URL in html
    assert "Check it out on GitHub" in html
    # Sits next to the RSS feed link at the bottom of the page, not somewhere else.
    assert html.index("RSS feed") < html.index(updater.REPO_URL)


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
