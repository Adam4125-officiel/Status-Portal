import io
import os
import re
import sqlite3
import tempfile
import time
import zipfile

import pyotp
import pytest
from werkzeug.security import generate_password_hash

import app as app_module
import db
import scheduler
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


def test_wizard_combined_form_prefills_service_defaults(client):
    """Regression test for a real bug: the wizard used to only ever render/submit
    name/icon/description/url/group_name, so the configured Service defaults were
    never reachable from it at all (db.create_service() silently fell back to its
    own hardcoded literals instead)."""
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    client.post("/admin/settings/general", data={
        "service_default_slow_threshold_ms": "1500", "service_default_retry_count": "2",
    })
    resp = client.get("/admin/new/combined")
    assert b'id="slow_threshold_ms"' in resp.data
    assert b'value="1500"' in resp.data
    assert b'id="retry_count"' in resp.data
    assert b'value="2"' in resp.data


def test_wizard_combined_form_prefills_run_target_and_visibility_defaults(client):
    """Regression test: run_target/show_run_target_public/show_dependencies_public
    were added to the main service form and Service defaults but initially forgotten
    on the wizard, exactly the class of bug the two regression tests above already
    guard against for the original field set - same fix, same shape."""
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    client.post("/admin/settings/general", data={
        "service_default_run_target": "host",
        "service_default_show_run_target_public": "on",
        "service_default_show_dependencies_public": "on",
    })
    resp = client.get("/admin/new/combined")
    html = resp.data.decode()
    assert 'name="run_target"' in html
    assert 'value="host" selected' in html or 'selected>This host' in html
    assert 'name="show_run_target_public" checked' in html
    assert 'name="show_dependencies_public" checked' in html


def test_admin_service_new_form_prefills_run_target_and_visibility_defaults(client):
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    client.post("/admin/settings/general", data={
        "service_default_run_target": "host",
        "service_default_show_run_target_public": "on",
        "service_default_show_dependencies_public": "on",
    })
    resp = client.get("/admin/services/new")
    html = resp.data.decode()
    assert 'name="show_run_target_public" checked' in html
    assert 'name="show_dependencies_public" checked' in html


def test_admin_settings_general_saves_run_target_and_visibility_defaults(client):
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    resp = client.post("/admin/settings/general", data={
        "service_default_run_target": "vm:VM-Media02",
        "service_default_show_run_target_public": "on",
        "service_default_show_dependencies_public": "on",
    })
    assert resp.status_code == 302
    defaults = app_module._service_defaults()
    assert defaults["run_target"] == "vm:VM-Media02"
    assert defaults["show_run_target_public"] is True
    assert defaults["show_dependencies_public"] is True


def test_wizard_combined_applies_configured_service_defaults_on_create(client):
    """End-to-end version of the regression above: submitting exactly what a real
    browser would submit for the (collapsed but still-present-in-the-DOM) Advanced
    settings section - i.e. the server-prefilled default values, untouched by the
    admin - must land on the created service, not app.py's/db.py's own hardcoded
    fallbacks."""
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    client.post("/admin/settings/general", data={
        "service_default_slow_threshold_ms": "1500", "service_default_startup_grace_seconds": "30",
        "service_default_retry_count": "3", "service_default_retry_interval_seconds": "15",
    })
    resp = client.post("/admin/new/combined", data={
        "name": "Radarr", "icon": "🎬", "url": "http://localhost:2",
        "kind": "arr", "api_key": "testkey", "show_on_public": "on",
        "status": "operational", "slow_threshold_ms": "1500", "retry_count": "3",
        "retry_interval_seconds": "15", "auto_incident": "on", "startup_grace_seconds": "30",
        "api_health_mode": "off", "sort_order": "0",
    })
    assert resp.status_code == 302

    service = next(s for s in db.list_services() if s["name"] == "Radarr")
    assert service["slow_threshold_ms"] == 1500
    assert service["startup_grace_seconds"] == 30
    assert service["retry_count"] == 3
    assert service["retry_interval_seconds"] == 15


def test_wizard_combined_full_advanced_fields_are_saved(client):
    """The wizard must expose (and actually persist) every field the plain 'New
    service' form and the 'New integration' form have, not just the original
    subset - including the fields with no configured-default counterpart."""
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    resp = client.post("/admin/new/combined", data={
        "name": "Prowlarr", "icon": "🔍", "description": "Indexer manager",
        "url": "http://localhost:3", "group_name": "Media",
        "kind": "arr", "api_key": "testkey", "show_on_public": "on",
        "status": "down", "manual_override": "on", "auto_check": "on",
        "check_url": "http://localhost:3/health", "slow_threshold_ms": "2500",
        "retry_count": "4", "retry_interval_seconds": "20", "auto_incident": "on",
        "startup_grace_seconds": "60", "ignore_in_overall_status": "on",
        "api_health_mode": "degrade", "sort_order": "5", "check_auto_incident": "on",
        "run_target": "host", "show_run_target_public": "on", "show_dependencies_public": "on",
    })
    assert resp.status_code == 302

    service = next(s for s in db.list_services() if s["name"] == "Prowlarr")
    assert service["status"] == "down"
    assert service["manual_override"] == 1
    assert service["auto_check"] == 1
    assert service["check_url"] == "http://localhost:3/health"
    assert service["slow_threshold_ms"] == 2500
    assert service["retry_count"] == 4
    assert service["retry_interval_seconds"] == 20
    assert service["auto_incident"] == 1
    assert service["startup_grace_seconds"] == 60
    assert service["ignore_in_overall_status"] == 1
    assert service["api_health_mode"] == "degrade"
    assert service["sort_order"] == 5
    assert service["run_target"] == "host"
    assert service["show_run_target_public"] == 1
    assert service["show_dependencies_public"] == 1

    integs = db.list_integrations_for_service(service["id"])
    assert len(integs) == 1
    assert integs[0]["auto_incident"] == 1


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


def test_merge_dependency_health_raises_operational_and_slow_on_down_dependency():
    assert app_module._merge_dependency_health("operational", ["down"]) == "degraded"
    assert app_module._merge_dependency_health("slow", ["down"]) == "degraded"


def test_merge_dependency_health_ignores_merely_degraded_dependency():
    assert app_module._merge_dependency_health("operational", ["degraded"]) == "operational"


def test_merge_dependency_health_never_overrides_down_or_maintenance():
    assert app_module._merge_dependency_health("down", ["down"]) == "down"
    assert app_module._merge_dependency_health("maintenance", ["down"]) == "maintenance"


def test_merge_dependency_health_passes_through_with_no_dependencies():
    assert app_module._merge_dependency_health("operational", []) == "operational"


def test_admin_service_edit_saves_dependencies(client):
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    seerr_id = db.create_service({"name": "Seerr", "url": ""})
    radarr_id = db.create_service({"name": "Radarr", "url": ""})
    sonarr_id = db.create_service({"name": "Sonarr", "url": ""})

    resp = client.post(f"/admin/services/{seerr_id}/edit", data={
        "name": "Seerr", "url": "", "depends_on": [str(radarr_id), str(sonarr_id)],
    })
    assert resp.status_code == 302
    assert sorted(db.get_service_dependencies(seerr_id)) == sorted([radarr_id, sonarr_id])

    form_html = client.get(f"/admin/services/{seerr_id}/edit").data.decode()
    assert "Depends on" in form_html
    assert "Radarr" in form_html and "Sonarr" in form_html


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


def test_admin_service_form_saves_public_visibility_checkboxes(client):
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    resp = client.post("/admin/services/new", data={
        "name": "Overseerr", "url": "", "run_target": "host",
        "show_run_target_public": "on", "show_dependencies_public": "on",
    })
    assert resp.status_code == 302
    service = [s for s in db.list_services() if s["name"] == "Overseerr"][0]
    assert service["show_run_target_public"] == 1
    assert service["show_dependencies_public"] == 1

    # Unchecked on the next save -> both go back to off.
    resp = client.post(f"/admin/services/{service['id']}/edit", data={"name": "Overseerr", "url": ""})
    assert resp.status_code == 302
    service = db.get_service(service["id"])
    assert service["show_run_target_public"] == 0
    assert service["show_dependencies_public"] == 0


def test_run_target_label_formats_host_and_vm():
    assert app_module._run_target_label("") is None
    assert app_module._run_target_label("host") == f"Host ({app_module.platform.node()})"
    assert app_module._run_target_label("vm:VM-Media02") == "VM: VM-Media02"


def test_enrich_services_exposes_run_target_and_dependencies_only_when_opted_in(isolated_db):
    radarr = db.create_service({"name": "Radarr", "url": ""})
    seerr = db.create_service({
        "name": "Seerr", "url": "", "run_target": "host",
        "show_run_target_public": 1, "show_dependencies_public": 1,
    })
    db.set_service_dependencies(seerr, [radarr])

    enriched = {s["id"]: s for s in app_module._enrich_services(db.list_services())}
    assert enriched[seerr]["run_target_label"] == f"Host ({app_module.platform.node()})"
    assert enriched[seerr]["dependency_names"] == ["Radarr"]
    # Radarr itself never opted in - both stay empty/off.
    assert enriched[radarr]["run_target_label"] is None
    assert enriched[radarr]["dependency_names"] == []


def test_public_page_renders_run_target_and_dependencies_when_opted_in(client):
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    radarr_id = db.create_service({"name": "Radarr", "url": ""})
    seerr_id = db.create_service({
        "name": "Seerr", "url": "", "run_target": "host",
        "show_run_target_public": 1, "show_dependencies_public": 1,
    })
    db.set_service_dependencies(seerr_id, [radarr_id])

    html = client.get("/").data.decode()
    assert "Runs on:" in html
    assert "Depends on: Radarr" in html


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


def test_public_service_card_hides_report_button_when_disabled_for_service(client):
    service = db.list_services()[0]
    db.update_service(service["id"], {**service, "show_report_button": 0})

    html = client.get("/").data.decode()
    assert f"/report?service_id={service['id']}" not in html


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


def test_lowdisk_threshold_blank_means_disabled(isolated_db):
    assert app_module._lowdisk_threshold() is None
    db.set_setting("lowdisk_percent_threshold", "90")
    assert app_module._lowdisk_threshold() == 90


def test_check_low_disk_space_fires_once_on_cross_and_once_on_recovery(isolated_db, monkeypatch):
    db.set_setting("lowdisk_percent_threshold", "90")
    calls = []
    monkeypatch.setattr(app_module.notifications, "notify", lambda title, msg: calls.append((title, msg)))

    low = {"disks": [{"path": "/", "display_name": "/", "percent": 95, "free_gb": 2}]}
    app_module._check_low_disk_space(low)
    assert [c[0] for c in calls] == ["Low disk space"]

    # Still low on the next cycle - no repeat notification.
    app_module._check_low_disk_space(low)
    assert [c[0] for c in calls] == ["Low disk space"]

    recovered = {"disks": [{"path": "/", "display_name": "/", "percent": 50, "free_gb": 40}]}
    app_module._check_low_disk_space(recovered)
    assert [c[0] for c in calls] == ["Low disk space", "Disk space back to normal"]


def test_check_low_disk_space_noop_when_threshold_unset(isolated_db, monkeypatch):
    calls = []
    monkeypatch.setattr(app_module.notifications, "notify", lambda title, msg: calls.append((title, msg)))
    app_module._check_low_disk_space({"disks": [{"path": "/", "display_name": "/", "percent": 99, "free_gb": 1}]})
    assert calls == []


def test_low_disk_alert_state_survives_restart_no_duplicate_notification(isolated_db, monkeypatch):
    """Regression test: state must be read from the DB, not an in-process cache -
    a restart while a disk is still low must not re-send the notification. Simulated
    here by writing the "already low" state directly via db.set_low_disk_alert_state
    (standing in for a previous process's cycle) rather than going through
    _check_low_disk_space first, since there's no module-level state left to reset."""
    db.set_setting("lowdisk_percent_threshold", "90")
    db.set_low_disk_alert_state("/", True)
    calls = []
    monkeypatch.setattr(app_module.notifications, "notify", lambda title, msg: calls.append((title, msg)))

    still_low = {"disks": [{"path": "/", "display_name": "/", "percent": 95, "free_gb": 2}]}
    app_module._check_low_disk_space(still_low)

    assert calls == []


def test_admin_settings_general_saves_lowdisk_threshold(client):
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    resp = client.post("/admin/settings/general", data={"lowdisk_percent_threshold": "85"})
    assert resp.status_code == 302
    assert db.get_setting("lowdisk_percent_threshold") == "85"


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
        "api_health_mode": "off", "run_target": "",
        "show_run_target_public": False, "show_dependencies_public": False,
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
    """A request with no `seen` key at all is never a real "load more" click -
    it's a stale cached copy of an older public_history.js still sending the
    previous release's ?offset= parameter. Answering it with the newest page is
    what made such a client append the same incidents forever, so it must return
    nothing. `?seen=` (key present, empty value) is a different, legitimate case
    now - see test_api_incidents_more_returns_content_for_empty_seen_when_hidden."""
    sid = db.list_services()[0]["id"]
    db.create_incident({"service_id": sid, "title": "Should not leak", "status": "resolved"})

    assert client.get("/api/incidents/more").data.decode().strip() == ""
    assert client.get("/api/incidents/more?offset=0").data.decode().strip() == ""
    assert client.get("/api/incidents/more?offset=10").data.decode().strip() == ""


def test_api_incidents_more_returns_content_for_empty_seen_when_hidden(client):
    """`?seen=` (key present, empty value) is what the real button sends when
    nothing is currently visible on the page - e.g. every incident is hidden by
    public_history_days (see index()'s incidents_hidden and the "elif
    incidents_hidden" branch in sections/incidents.html). Unlike a missing `seen`
    key entirely (the stale-client case above), this must return real content or
    the "all hidden" empty state's load-more button would be permanently dead."""
    sid = db.list_services()[0]["id"]
    old = db.create_incident({"service_id": sid, "title": "Old hidden incident", "status": "resolved"})
    conn = db.get_db()
    conn.execute("UPDATE incidents SET resolved_at='2000-01-01T00:00:00' WHERE id=?", (old,))
    conn.commit()
    conn.close()

    resp = client.get("/api/incidents/more?seen=")
    assert "Old hidden incident" in resp.data.decode()


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


def test_index_shows_load_more_when_all_incidents_hidden_by_history_days(client):
    """Regression test: when EVERY incident is older than public_history_days,
    the initial filtered list is empty, but incidents still exist - the page
    must not claim "No incidents recorded. All clear." (that's only true when
    nothing exists at all), and the load-more button must still render so the
    hidden incidents are reachable, exactly as when only some are hidden."""
    sid = db.list_services()[0]["id"]
    old = db.create_incident({"service_id": sid, "title": "Old one", "status": "resolved"})
    conn = db.get_db()
    conn.execute("UPDATE incidents SET resolved_at='2000-01-01T00:00:00' WHERE id=?", (old,))
    conn.commit()
    conn.close()
    db.set_setting("public_history_days", "3")

    index_html = client.get("/").data.decode()
    assert "No incidents recorded. All clear." not in index_html
    assert 'id="incidents-load-more"' in index_html

    resp = client.get("/api/incidents/more?seen=")
    assert "Old one" in resp.data.decode()


def test_index_shows_all_clear_when_no_incidents_exist_at_all(client):
    db.set_setting("public_history_days", "3")
    index_html = client.get("/").data.decode()
    assert "No incidents recorded. All clear." in index_html
    assert 'id="incidents-load-more"' not in index_html


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


def test_admin_settings_test_notification_sends_via_shared_dispatch(client, monkeypatch):
    monkeypatch.setattr(app_module.config, "DISCORD_WEBHOOK_URL", "https://discord.example/webhook")
    calls = []
    monkeypatch.setattr(app_module.notifications, "notify", lambda title, msg: calls.append((title, msg)))
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    resp = client.post("/admin/settings/test-notification", follow_redirects=True)
    assert resp.status_code == 200
    assert b"Test notification sent" in resp.data
    assert len(calls) == 1
    assert calls[0][0] == "Test notification"


def test_admin_settings_backup_db_returns_zip_with_consistent_snapshot(client):
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    resp = client.get("/admin/settings/backup-db")
    assert resp.status_code == 200
    assert resp.mimetype == "application/zip"
    assert "attachment" in resp.headers.get("Content-Disposition", "")
    with zipfile.ZipFile(io.BytesIO(resp.data)) as zf:
        assert zf.namelist() == ["portal.db"]
        # A real, openable SQLite file, not just arbitrary bytes under that name -
        # confirms Connection.backup() actually produced a valid database.
        extracted = zf.read("portal.db")
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        f.write(extracted)
        f.flush()
        conn = sqlite3.connect(f.name)
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
    assert "services" in tables
    assert "settings" in tables


def test_admin_settings_test_notification_refuses_when_unconfigured(client, monkeypatch):
    monkeypatch.setattr(app_module.config, "DISCORD_WEBHOOK_URL", "")
    monkeypatch.setattr(app_module.config, "NTFY_URL", "")
    calls = []
    monkeypatch.setattr(app_module.notifications, "notify", lambda title, msg: calls.append((title, msg)))
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    resp = client.post("/admin/settings/test-notification", follow_redirects=True)
    assert resp.status_code == 200
    assert b"No notification channel configured" in resp.data
    assert calls == []


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


# ---------------------------------------------------------------------------
# Admin session lifetime
# ---------------------------------------------------------------------------
def test_login_marks_the_session_permanent_with_a_last_seen_stamp(client):
    """A permanent session is what gives the cookie an explicit Max-Age. Without it
    Flask emits a browser-session cookie, which dies on browser close (desktop) but
    survives indefinitely on a device whose browser is never closed (phone) - the
    "inconsistent across devices" behavior this replaced."""
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    with client.session_transaction() as sess:
        assert sess["logged_in"] is True
        assert sess.permanent is True
        assert sess["last_seen"] > 0


def test_session_expires_after_the_configured_idle_timeout(client):
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    db.set_setting("admin_session_timeout_hours", "1")
    with client.session_transaction() as sess:
        sess["last_seen"] = time.time() - 3601  # one second past the hour

    resp = client.get("/admin/services")
    assert resp.status_code == 302
    assert "/admin/login" in resp.headers["Location"]
    with client.session_transaction() as sess:
        assert "logged_in" not in sess


def test_session_survives_activity_inside_the_idle_timeout(client):
    """The window slides: any request inside it re-stamps last_seen, so an admin who
    keeps using the panel is never signed out mid-session."""
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    db.set_setting("admin_session_timeout_hours", "1")
    with client.session_transaction() as sess:
        sess["last_seen"] = time.time() - 3500  # inside the hour

    assert client.get("/admin/services").status_code == 200
    with client.session_transaction() as sess:
        assert sess["logged_in"] is True
        # Re-stamped to roughly now, not left at the old value.
        assert time.time() - sess["last_seen"] < 5


def test_zero_timeout_disables_idle_expiry(client):
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    db.set_setting("admin_session_timeout_hours", "0")
    with client.session_transaction() as sess:
        sess["last_seen"] = time.time() - 400 * 24 * 3600  # over a year idle

    assert client.get("/admin/services").status_code == 200


def test_expired_session_on_a_post_redirects_to_login_rather_than_failing_csrf(isolated_db, monkeypatch):
    """The timeout hook is registered before _check_csrf specifically so this case
    reads as "your session expired" instead of a bare 400 - the CSRF token lives in
    the very session being cleared."""
    monkeypatch.setitem(app_module.app.config, "TESTING", False)
    monkeypatch.setitem(app_module._login_state, "failures", 0)
    monkeypatch.setitem(app_module._login_state, "locked_until", 0.0)
    with app_module.app.test_client() as c:
        token = _extract_csrf_token(c.get("/admin/login").data)
        c.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123",
                                      "csrf_token": token})
        db.set_setting("admin_session_timeout_hours", "1")
        with c.session_transaction() as sess:
            sess["last_seen"] = time.time() - 7200

        resp = c.post("/admin/settings/general", data={"site_name": "Nope", "csrf_token": token})
        assert resp.status_code == 302
        assert "/admin/login" in resp.headers["Location"]


def test_session_timeout_setting_round_trips_and_falls_back_when_invalid(client):
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    client.post("/admin/settings/general", data={"site_name": "S", "admin_session_timeout_hours": "6"})
    assert db.get_setting("admin_session_timeout_hours") == "6"
    assert app_module._session_timeout_seconds() == 6 * 3600

    client.post("/admin/settings/general", data={"site_name": "S", "admin_session_timeout_hours": "not-a-number"})
    assert db.get_setting("admin_session_timeout_hours") == str(app_module.DEFAULT_SESSION_TIMEOUT_HOURS)


def test_session_timeout_is_clamped_to_the_cookie_lifetime(isolated_db):
    """A timeout longer than the cookie's own Max-Age is a promise the server can't
    keep - the cookie would be gone before the idle clock ever ran out."""
    db.set_setting("admin_session_timeout_hours", "100000")
    assert app_module._session_timeout_seconds() == app_module.MAX_SESSION_TIMEOUT_HOURS * 3600


def test_public_page_is_unaffected_by_an_expired_admin_session(client):
    """An expired session on a public page is just cleared - no redirect, the page
    renders exactly as it does for any signed-out visitor."""
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    db.set_setting("admin_session_timeout_hours", "1")
    with client.session_transaction() as sess:
        sess["last_seen"] = time.time() - 7200

    assert client.get("/").status_code == 200
    with client.session_transaction() as sess:
        assert "logged_in" not in sess


def test_secret_key_persists_across_reimport(tmp_path, monkeypatch):
    """The whole point: a restart must not invalidate every session cookie. This is
    what "a refresh randomly logs me out" was - config.SECRET_KEY used to be a fresh
    os.urandom() per process whenever PORTAL_SECRET_KEY wasn't set."""
    import importlib
    import config as config_module

    monkeypatch.delenv("PORTAL_SECRET_KEY", raising=False)
    monkeypatch.setattr(config_module, "SECRET_KEY_FILE", str(tmp_path / "secret_key"))
    first = config_module._load_or_create_secret_key()
    second = config_module._load_or_create_secret_key()
    assert first == second and len(first) >= 32
    # And it's not left world-readable.
    assert oct(os.stat(tmp_path / "secret_key").st_mode)[-3:] == "600"

    monkeypatch.setenv("PORTAL_SECRET_KEY", "explicit-key-wins")
    assert config_module._load_or_create_secret_key() == "explicit-key-wins"


def test_secret_key_degrades_to_a_process_key_when_it_cannot_be_written(tmp_path, monkeypatch):
    """A read-only filesystem must not crash the app on import - it just goes back to
    the old per-process behavior."""
    import config as config_module
    monkeypatch.delenv("PORTAL_SECRET_KEY", raising=False)
    unwritable = tmp_path / "nope" / "secret_key"
    monkeypatch.setattr(config_module, "SECRET_KEY_FILE", str(unwritable))
    monkeypatch.setattr(config_module.os, "makedirs", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    key = config_module._load_or_create_secret_key()
    assert len(key) >= 32


# ---------------------------------------------------------------------------
# Uptime caching / history retention
# ---------------------------------------------------------------------------
def test_uptime_percentages_are_cached_between_calls(isolated_db, monkeypatch):
    sid = db.create_service({"name": "S", "url": "", "check_url": "", "description": ""})
    db.record_status_history(sid, "operational", 10)
    assert app_module._cached_uptime_percentages() == {sid: 100.0}

    calls = []
    monkeypatch.setattr(db, "get_uptime_percentages", lambda *a, **k: calls.append(1) or {})
    app_module._cached_uptime_percentages()
    assert calls == [], "a second call inside the TTL should not re-query"


def test_uptime_cache_expires_after_its_ttl(isolated_db, monkeypatch):
    sid = db.create_service({"name": "S", "url": "", "check_url": "", "description": ""})
    db.record_status_history(sid, "operational", 10)
    app_module._cached_uptime_percentages()
    monkeypatch.setitem(app_module._uptime_cache, "fetched_at",
                        time.monotonic() - app_module.UPTIME_CACHE_TTL_SECONDS - 1)
    db.record_status_history(sid, "down", None)
    assert app_module._cached_uptime_percentages() == {sid: 50.0}


def test_history_pruning_runs_once_a_day_not_every_cycle(isolated_db, monkeypatch):
    calls = []
    monkeypatch.setattr(db, "prune_status_history", lambda days: calls.append(days) or 0)
    monkeypatch.setattr(app_module, "_last_history_prune", 0.0)
    app_module._prune_status_history_if_due()
    app_module._prune_status_history_if_due()
    assert calls == [app_module.DEFAULT_HISTORY_RETENTION_DAYS]


def test_history_retention_setting_is_honoured(isolated_db, monkeypatch):
    db.set_setting("status_history_retention_days", "7")
    assert app_module._history_retention_days() == 7
    db.set_setting("status_history_retention_days", "0")
    assert app_module._history_retention_days() == app_module.DEFAULT_HISTORY_RETENTION_DAYS


# ---------------------------------------------------------------------------
# Clear cached data (admin -> System)
# ---------------------------------------------------------------------------
def test_clear_caches_empties_every_in_memory_cache(client):
    import integrations as integrations_module
    import monitoring as monitoring_module

    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})

    app_module._uptime_cache["value"] = {1: 99.0}
    app_module._uptime_cache["fetched_at"] = time.monotonic()
    app_module._integration_status_cache[1] = {
        "status": {"reachable": True, "version": "1", "issues": [], "error": None},
        "checked_at": db.now_iso()}
    integrations_module._jellyfin_activity_cache["running_tasks"] = ["Scan"]
    monitoring_module._volume_label_cache["/dev/sda1"] = "Media"
    monitoring_module._CPU_CACHE["per_core"] = [1.0]
    monitoring_module._CPU_CACHE["updated_at"] = time.time()
    updater._update_cache["result"] = {"anything": True}

    resp = client.post("/admin/system/clear-caches")
    assert resp.status_code == 302

    assert app_module._uptime_cache["value"] == {}
    assert app_module._integration_status_cache == {}
    assert integrations_module._jellyfin_activity_cache["running_tasks"] == []
    assert monitoring_module._volume_label_cache == {}
    assert monitoring_module._CPU_CACHE["updated_at"] is None
    assert updater._update_cache["result"] is None


def test_clear_caches_bumps_the_static_asset_cache_buster(client):
    """The browser-side half: an mtime-only ?v= can't invalidate a file whose
    timestamp didn't change (a rollback, a restored file), which is exactly the
    "I shipped a fix and they still see the old page" case this button is for."""
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    with app_module.app.test_request_context():
        before = app_module.asset_url("js/main.js")
    client.post("/admin/system/clear-caches")
    with app_module.app.test_request_context():
        after = app_module.asset_url("js/main.js")
    assert before != after
    assert db.get_setting("asset_cache_salt")


def test_asset_cache_salt_survives_a_restart(client):
    """Stored in settings, not just memory - a restart that forgot it would hand
    every browser back the URL it already has cached, silently undoing the bump."""
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    client.post("/admin/system/clear-caches")
    with app_module.app.test_request_context():
        after_clear = app_module.asset_url("js/main.js")
    app_module._asset_salt["value"] = None  # stands in for a fresh process
    with app_module.app.test_request_context():
        assert app_module.asset_url("js/main.js") == after_clear


def test_clear_caches_requires_login(client):
    resp = client.post("/admin/system/clear-caches")
    assert resp.status_code == 302
    assert "/admin/login" in resp.headers["Location"]


def test_system_page_lists_caches_and_integration_reachability(client):
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    sid = db.create_service({"name": "Media", "url": "", "check_url": "", "description": ""})
    iid = db.create_integration({"name": "Jellyfin", "kind": "jellyfin", "base_url": "http://x",
                                  "api_key": "k", "enabled": 1, "service_id": sid,
                                  "show_on_public": 1, "auto_incident": 0})
    app_module._integration_status_cache[iid] = {
        "status": {"reachable": False, "version": None, "issues": [], "error": "boom"},
        "checked_at": db.now_iso()}

    body = client.get("/admin/system").data.decode()
    assert "Cached data" in body
    assert "Uptime percentages" in body
    assert "Jellyfin" in body and "not reachable" in body


def test_clear_browser_cache_sends_clear_site_data_without_touching_cookies(client):
    """The 'cookies' directive would sign the admin out as a side effect of a cache
    action - the one directive this must never send."""
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    resp = client.post("/admin/system/clear-browser-cache", data={"theme": "dark"})
    assert resp.status_code == 200
    header = resp.headers["Clear-Site-Data"]
    assert '"cache"' in header and '"storage"' in header
    assert "cookies" not in header
    with client.session_transaction() as sess:
        assert sess["logged_in"] is True


def test_clear_browser_cache_page_lists_every_static_asset(client):
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    body = client.post("/admin/system/clear-browser-cache", data={"theme": ""}).data.decode()
    # Handed to the page as a data attribute, not interpolated into inline JS.
    assert "data-assets=" in body
    for name in ("js/main.js", "js/theme.js", "css/style.css"):
        assert name in body
    assert "data-theme=\"\"" in body


def test_clear_browser_cache_carries_the_theme_through(client):
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    body = client.post("/admin/system/clear-browser-cache", data={"theme": "light"}).data.decode()
    assert 'data-theme="light"' in body


def test_clear_browser_cache_requires_login(client):
    resp = client.post("/admin/system/clear-browser-cache", data={})
    assert resp.status_code == 302
    assert "/admin/login" in resp.headers["Location"]


def test_static_asset_list_covers_new_files_automatically(client, tmp_path, monkeypatch):
    """Enumerated from the directory, not a hand-maintained list - a JS file added
    later must not be silently left out of the re-fetch."""
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    new_file = os.path.join(app_module.app.root_path, "static", "js", "_tmp_probe.js")
    with open(new_file, "w", encoding="utf-8") as f:
        f.write("// temporary probe file\n")
    try:
        with app_module.app.test_request_context():
            urls = app_module._all_static_asset_urls()
        assert any("_tmp_probe.js" in u for u in urls)
        # And every entry is cache-busted, not a bare static URL.
        assert all("?v=" in u for u in urls)
    finally:
        os.remove(new_file)


# ---------------------------------------------------------------------------
# Scheduled tasks admin page (/admin/tasks) - the framework itself is covered in
# tests/test_scheduler.py; these are the route-level checks.
# ---------------------------------------------------------------------------
@pytest.fixture
def demo_task(isolated_db):
    """A throwaway task registered into the real registry for the duration of one
    test, so the routes can be exercised without depending on the Jellyfin sync
    task's own behaviour."""
    saved = dict(scheduler._registry)
    scheduler._registry.clear()
    scheduler.clear_caches()
    calls = []
    scheduler.register("demo_task", "Demo task", "Does nothing in particular.",
                       lambda: (calls.append(1), "did nothing")[1],
                       default_interval_minutes=45)
    yield calls
    scheduler._registry.clear()
    scheduler._registry.update(saved)
    scheduler.clear_caches()


def test_admin_tasks_requires_login(client, demo_task):
    assert client.get("/admin/tasks").status_code == 302
    assert client.post("/admin/tasks/demo_task/run").status_code == 302
    assert client.post("/admin/tasks/demo_task/save").status_code == 302
    assert not demo_task, "an unauthenticated POST actually ran the task"


def test_admin_tasks_page_lists_registered_tasks(client, demo_task):
    _login(client)
    resp = client.get("/admin/tasks")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "Demo task" in body
    assert "Does nothing in particular." in body
    assert "no runs recorded yet" in body


def test_run_now_executes_the_task_and_reports_the_result(client, demo_task):
    _login(client)
    resp = client.post("/admin/tasks/demo_task/run", follow_redirects=True)
    assert resp.status_code == 200
    assert demo_task == [1], "the task did not actually run"
    assert "did nothing" in resp.data.decode()
    row = db.get_task_row("demo_task")
    assert row["last_status"] == "success"
    assert row["last_trigger"] == "manual"


def test_run_now_on_a_failing_task_reports_the_failure_without_a_500(client, isolated_db):
    saved = dict(scheduler._registry)
    scheduler._registry.clear()
    scheduler.clear_caches()
    scheduler.register("boom_task", "Boom", "Always fails.",
                       lambda: (_ for _ in ()).throw(RuntimeError("kaboom")))
    try:
        _login(client)
        resp = client.post("/admin/tasks/boom_task/run", follow_redirects=True)
        assert resp.status_code == 200
        assert "kaboom" in resp.data.decode()
        assert db.get_task_row("boom_task")["last_status"] == "failed"
    finally:
        scheduler._registry.clear()
        scheduler._registry.update(saved)
        scheduler.clear_caches()


def test_saving_a_schedule_persists_it_without_looking_like_a_run(client, demo_task):
    """Saving settings must never touch last_run_at - that would both fake a run
    that didn't happen and silently push the next one an interval into the future."""
    _login(client)
    client.post("/admin/tasks/demo_task/run")
    before = db.get_task_row("demo_task")["last_run_at"]

    client.post("/admin/tasks/demo_task/save", data={
        "enabled": "on", "schedule_kind": "daily", "interval_minutes": "90", "daily_at": "04:15"})
    row = db.get_task_row("demo_task")
    assert (row["schedule_kind"], row["daily_at"], row["interval_minutes"]) == ("daily", "04:15", 90)
    assert row["last_run_at"] == before


def test_unchecking_enabled_disables_the_task(client, demo_task):
    _login(client)
    client.post("/admin/tasks/demo_task/save", data={
        "schedule_kind": "interval", "interval_minutes": "45", "daily_at": "03:00"})
    assert db.get_task_row("demo_task")["enabled"] == 0
    assert scheduler.next_run_at(scheduler.get_task("demo_task")) is None


def test_unknown_task_names_are_rejected_rather_than_500ing(client, demo_task):
    _login(client)
    for path in ("/admin/tasks/nope/run", "/admin/tasks/nope/save"):
        resp = client.post(path, follow_redirects=True)
        assert resp.status_code == 200
        assert "No such scheduled task." in resp.data.decode()


# ---------------------------------------------------------------------------
# User accounts admin page (/admin/users) - the Jellyfin side is covered in
# tests/test_jellyfin_auth.py; these are the route-level checks.
# ---------------------------------------------------------------------------
def _jellyfin_integration():
    return db.create_integration({"name": "Jellyfin", "kind": "jellyfin",
                                   "base_url": "http://jellyfin.invalid", "api_key": "k",
                                   "enabled": 1, "service_id": None,
                                   "show_on_public": 0, "auto_incident": 0})


def test_admin_users_page_requires_login(client):
    assert client.get("/admin/users").status_code == 302
    assert client.post("/admin/users/settings").status_code == 302


def test_admin_users_page_explains_why_sign_in_is_off(client):
    _login(client)
    body = client.get("/admin/users").data.decode()
    assert "Sign-in is off" in body
    assert "never been synced" in body


def test_admin_users_page_warns_when_enabled_without_an_integration(client):
    _login(client)
    db.set_setting("jellyfin_auth_enabled", "1")
    body = client.get("/admin/users").data.decode()
    assert "Switched on, but not usable" in body


def test_saving_user_settings_persists_them(client):
    _login(client)
    iid = _jellyfin_integration()
    client.post("/admin/users/settings", data={
        "jellyfin_auth_enabled": "on", "jellyfin_auth_integration_id": str(iid),
        "report_requires_login": "on", "user_session_timeout_hours": "48"})
    assert db.get_setting("jellyfin_auth_enabled") == "1"
    assert db.get_setting("jellyfin_auth_integration_id") == str(iid)
    assert db.get_setting("report_requires_login") == "1"
    assert db.get_setting("user_session_timeout_hours") == "48"
    assert app_module.jellyfin_auth.is_enabled() is True


def test_unchecking_the_toggles_turns_them_off(client):
    _login(client)
    _jellyfin_integration()
    db.set_setting("jellyfin_auth_enabled", "1")
    db.set_setting("report_requires_login", "1")
    client.post("/admin/users/settings", data={"user_session_timeout_hours": "48"})
    assert db.get_setting("jellyfin_auth_enabled") == "0"
    assert db.get_setting("report_requires_login") == "0"


def test_a_non_numeric_session_timeout_falls_back_to_the_default(client):
    _login(client)
    client.post("/admin/users/settings", data={"user_session_timeout_hours": "banana"})
    assert db.get_setting("user_session_timeout_hours") == str(
        app_module.DEFAULT_USER_SESSION_TIMEOUT_HOURS)


def test_the_cached_user_list_is_shown(client):
    _login(client)
    db.replace_jellyfin_users([{"id": "u1", "name": "adam"},
                               {"id": "u2", "name": "sam", "is_administrator": True},
                               {"id": "u3", "name": "old", "is_disabled": True}])
    body = client.get("/admin/users").data.decode()
    assert "adam" in body and "sam" in body
    assert "administrator" in body and "disabled" in body


# ---------------------------------------------------------------------------
# Jellyfin-backed visitor sign-in (/login, /logout) and the /report gate.
#
# The single most important property in this section is isolation: a signed-in
# Jellyfin user is a visitor with a name, never a lesser admin. Several tests below
# exist only to pin that down.
# ---------------------------------------------------------------------------
@pytest.fixture
def user_auth(isolated_db):
    """Jellyfin sign-in switched on, with a cached user list already populated."""
    db.create_integration({"name": "Jellyfin", "kind": "jellyfin",
                            "base_url": "http://jellyfin.invalid", "api_key": "k",
                            "enabled": 1, "service_id": None,
                            "show_on_public": 0, "auto_incident": 0})
    db.set_setting("jellyfin_auth_enabled", "1")
    db.replace_jellyfin_users([{"id": "u1", "name": "adam"}])
    return None


def _auth_returns(monkeypatch, result):
    monkeypatch.setattr(app_module.jellyfin_auth, "authenticate", lambda u, p: result)


def _sign_in(client, monkeypatch, name="adam", uid="u1", admin=False):
    _auth_returns(monkeypatch, {"ok": True, "user": {"id": uid, "name": name,
                                                      "is_administrator": admin,
                                                      "is_disabled": False}})
    return client.post("/login", data={"username": name, "password": "pw"})


def test_the_sign_in_page_is_absent_entirely_when_the_feature_is_off(client):
    assert client.get("/login").status_code == 404
    assert client.post("/login", data={"username": "a", "password": "b"}).status_code == 404


def test_the_public_page_shows_no_sign_in_link_when_the_feature_is_off(client):
    assert "Sign in" not in client.get("/").data.decode()


def test_the_public_page_offers_sign_in_when_enabled(client, user_auth):
    body = client.get("/").data.decode()
    assert "Sign in" in body
    assert "/login" in body


def test_a_valid_sign_in_creates_a_visitor_session(client, user_auth, monkeypatch):
    resp = _sign_in(client, monkeypatch)
    assert resp.status_code == 302
    with client.session_transaction() as sess:
        assert sess["portal_user"]["name"] == "adam"
        assert sess["portal_user"]["id"] == "u1"
    assert "adam" in client.get("/").data.decode()


def test_signing_in_never_creates_an_admin_session(client, user_auth, monkeypatch):
    """The property the whole design rests on."""
    _sign_in(client, monkeypatch)
    with client.session_transaction() as sess:
        assert "logged_in" not in sess
    assert client.get("/admin/services").status_code == 302
    assert client.get("/admin/settings").status_code == 302
    assert client.get("/admin/users").status_code == 302


def test_a_jellyfin_administrator_is_still_not_a_portal_admin(client, user_auth, monkeypatch):
    """Being an administrator *in Jellyfin* says nothing about this portal. The flag
    is stored as groundwork for per-content visibility and must not be a back door."""
    _sign_in(client, monkeypatch, admin=True)
    with client.session_transaction() as sess:
        assert sess["portal_user"]["jellyfin_admin"] is True
        assert "logged_in" not in sess
    assert client.get("/admin/services").status_code == 302


def test_no_password_or_token_is_ever_stored_in_the_session(client, user_auth, monkeypatch):
    _auth_returns(monkeypatch, {"ok": True, "user": {"id": "u1", "name": "adam",
                                                      "is_administrator": False,
                                                      "is_disabled": False}})
    client.post("/login", data={"username": "adam", "password": "s3cretpw"})
    with client.session_transaction() as sess:
        assert "s3cretpw" not in repr(dict(sess))
        assert set(sess["portal_user"]) == {"id", "name", "jellyfin_admin", "authenticated_at"}


def test_a_wrong_password_is_reported_as_such(client, user_auth, monkeypatch):
    _auth_returns(monkeypatch, {"ok": False, "reason": "invalid"})
    resp = client.post("/login", data={"username": "adam", "password": "no"},
                       follow_redirects=True)
    assert "Incorrect username or password" in resp.data.decode()
    with client.session_transaction() as sess:
        assert "portal_user" not in sess


def test_an_unreachable_jellyfin_says_so_instead_of_blaming_the_password(client, user_auth, monkeypatch):
    """Telling someone their password is wrong when the server is down sends them off
    to reset a password that was fine."""
    _auth_returns(monkeypatch, {"ok": False, "reason": "unreachable", "error": "refused"})
    resp = client.post("/login", data={"username": "adam", "password": "pw"},
                       follow_redirects=True)
    body = resp.data.decode()
    assert "Can&#39;t reach Jellyfin" in body or "Can't reach Jellyfin" in body
    assert "isn&#39;t a problem with your password" in body or "isn't a problem with your password" in body
    assert "Incorrect" not in body


def test_an_outage_does_not_count_towards_the_lockout(client, user_auth, monkeypatch):
    """Otherwise an outage fills the counter and locks everybody out for five
    minutes after Jellyfin comes back."""
    _auth_returns(monkeypatch, {"ok": False, "reason": "unreachable", "error": "refused"})
    for _ in range(app_module.USER_LOGIN_LOCKOUT_THRESHOLD + 2):
        client.post("/login", data={"username": "adam", "password": "pw"})
    assert app_module._user_login_locked() is False
    assert _sign_in(client, monkeypatch).status_code == 302


def test_repeated_wrong_passwords_trigger_a_lockout(client, user_auth, monkeypatch):
    _auth_returns(monkeypatch, {"ok": False, "reason": "invalid"})
    for _ in range(app_module.USER_LOGIN_LOCKOUT_THRESHOLD):
        client.post("/login", data={"username": "adam", "password": "no"})
    assert app_module._user_login_locked() is True
    resp = client.post("/login", data={"username": "adam", "password": "no"},
                       follow_redirects=True)
    assert "Too many failed sign-ins" in resp.data.decode()


def test_the_visitor_lockout_does_not_lock_the_admin_out(client, user_auth, monkeypatch):
    """Two separate counters, for exactly this reason."""
    _auth_returns(monkeypatch, {"ok": False, "reason": "invalid"})
    for _ in range(app_module.USER_LOGIN_LOCKOUT_THRESHOLD + 1):
        client.post("/login", data={"username": "adam", "password": "no"})
    assert app_module._user_login_locked() is True
    assert app_module._login_locked() is False
    _login(client)
    assert client.get("/admin/services").status_code == 200


def test_the_admin_lockout_does_not_lock_visitors_out(client, user_auth, monkeypatch):
    db.set_setting("admin_password_hash", generate_password_hash("realpassword"))
    for _ in range(app_module.LOGIN_LOCKOUT_THRESHOLD):
        client.post("/admin/login", data={"password": "wrong"})
    assert app_module._login_locked() is True
    assert app_module._user_login_locked() is False
    assert _sign_in(client, monkeypatch).status_code == 302


def test_a_disabled_account_is_refused(client, user_auth, monkeypatch):
    _auth_returns(monkeypatch, {"ok": False, "reason": "disabled"})
    resp = client.post("/login", data={"username": "adam", "password": "pw"},
                       follow_redirects=True)
    assert "disabled" in resp.data.decode()


def test_signing_out_ends_only_the_visitor_session(client, user_auth, monkeypatch):
    """An admin signed in as a Jellyfin user in the same browser must not be logged
    out of the admin panel by clicking "Sign out" on the public page."""
    _login(client)
    _sign_in(client, monkeypatch)
    client.get("/logout")
    with client.session_transaction() as sess:
        assert "portal_user" not in sess
        assert sess.get("logged_in") is True
    assert client.get("/admin/services").status_code == 200


def test_a_signed_in_visitor_is_redirected_away_from_the_sign_in_page(client, user_auth, monkeypatch):
    _sign_in(client, monkeypatch)
    assert client.get("/login").status_code == 302


def test_the_next_parameter_cannot_redirect_off_site(client, user_auth, monkeypatch):
    """This route is reachable with no authentication at all, so an open redirect
    here is a phishing primitive."""
    for hostile in ("https://evil.invalid/x", "//evil.invalid/x"):
        _auth_returns(monkeypatch, {"ok": True, "user": {"id": "u1", "name": "adam",
                                                          "is_administrator": False,
                                                          "is_disabled": False}})
        resp = client.post("/login", data={"username": "adam", "password": "pw",
                                            "next": hostile})
        assert "evil.invalid" not in resp.headers["Location"]
        client.get("/logout")


def test_a_relative_next_parameter_is_honoured(client, user_auth, monkeypatch):
    _auth_returns(monkeypatch, {"ok": True, "user": {"id": "u1", "name": "adam",
                                                      "is_administrator": False,
                                                      "is_disabled": False}})
    resp = client.post("/login", data={"username": "adam", "password": "pw", "next": "/report"})
    assert resp.headers["Location"] == "/report"


# ---- Session revocation via the cached user list ----
def test_a_user_removed_from_jellyfin_loses_their_session(client, user_auth, monkeypatch):
    _sign_in(client, monkeypatch)
    db.replace_jellyfin_users([{"id": "someone-else", "name": "sam"}])
    client.get("/")
    with client.session_transaction() as sess:
        assert "portal_user" not in sess


def test_a_user_disabled_in_jellyfin_loses_their_session(client, user_auth, monkeypatch):
    _sign_in(client, monkeypatch)
    db.replace_jellyfin_users([{"id": "u1", "name": "adam", "is_disabled": True}])
    client.get("/")
    with client.session_transaction() as sess:
        assert "portal_user" not in sess


def test_an_outage_never_signs_an_existing_visitor_out(client, user_auth, monkeypatch):
    """The whole "reduced functionality" story: nobody new gets in, everybody already
    in stays in. Session validity is checked against the local cache, never Jellyfin."""
    _sign_in(client, monkeypatch)

    def unreachable(*a, **k):
        raise AssertionError("Jellyfin must never be contacted to validate a session")

    monkeypatch.setattr(app_module.jellyfin_auth, "authenticate", unreachable)
    for _ in range(3):
        assert client.get("/").status_code == 200
    with client.session_transaction() as sess:
        assert sess["portal_user"]["name"] == "adam"


def test_an_idle_visitor_session_expires(client, user_auth, monkeypatch):
    _sign_in(client, monkeypatch)
    db.set_setting("user_session_timeout_hours", "1")
    with client.session_transaction() as sess:
        sess["portal_user_last_seen"] = time.time() - 7200
    client.get("/")
    with client.session_transaction() as sess:
        assert "portal_user" not in sess


def test_a_zero_visitor_timeout_means_no_idle_expiry(client, user_auth, monkeypatch):
    _sign_in(client, monkeypatch)
    db.set_setting("user_session_timeout_hours", "0")
    with client.session_transaction() as sess:
        sess["portal_user_last_seen"] = time.time() - 86400 * 20
    client.get("/")
    with client.session_transaction() as sess:
        assert sess["portal_user"]["name"] == "adam"


def test_an_expiring_visitor_session_does_not_touch_the_admin_session(client, user_auth, monkeypatch):
    _login(client)
    _sign_in(client, monkeypatch)
    db.set_setting("user_session_timeout_hours", "1")
    with client.session_transaction() as sess:
        sess["portal_user_last_seen"] = time.time() - 7200
    client.get("/")
    assert client.get("/admin/services").status_code == 200


# ---- /report gating (Part 4) ----
def test_report_stays_open_to_everyone_when_sign_in_is_not_enabled(client):
    """An install that hasn't set up Jellyfin sign-in must behave exactly as before -
    the gate must never make the form unreachable behind a login that doesn't exist."""
    assert db.get_setting("report_requires_login", "1") == "1"
    assert client.get("/report").status_code == 200
    with client.session_transaction() as sess:
        sess["report_form_rendered_at"] = time.time() - 60  # clear the min-fill-time check
    resp = client.post("/report", data={"message": "something is broken"},
                       follow_redirects=True)
    assert "your report has been submitted" in resp.data.decode()
    assert len(db.list_problem_reports()) == 1


def test_report_requires_sign_in_once_the_feature_is_on(client, user_auth):
    resp = client.get("/report")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]
    resp = client.post("/report", data={"message": "broken"}, follow_redirects=True)
    assert "sign in" in resp.data.decode().lower()
    assert db.list_problem_reports() == []


def test_a_signed_in_visitor_can_report_and_is_recorded_as_the_reporter(client, user_auth, monkeypatch):
    _sign_in(client, monkeypatch)
    client.get("/report")
    with client.session_transaction() as sess:
        sess["report_form_rendered_at"] = time.time() - 60
    resp = client.post("/report", data={"message": "buffering constantly"},
                       follow_redirects=True)
    assert "your report has been submitted" in resp.data.decode()
    report = db.list_problem_reports()[0]
    assert report["reporter_user"] == "adam"


def test_the_gate_can_be_turned_off_so_outage_reports_still_work(client, user_auth):
    """The escape hatch for the awkward interaction this feature has with itself: if
    Jellyfin is down, nobody who hasn't already signed in can sign in, so nobody new
    could otherwise report the outage."""
    db.set_setting("report_requires_login", "0")
    assert client.get("/report").status_code == 200
    with client.session_transaction() as sess:
        sess["report_form_rendered_at"] = time.time() - 60
    resp = client.post("/report", data={"message": "jellyfin is down"},
                       follow_redirects=True)
    assert "your report has been submitted" in resp.data.decode()
    assert db.list_problem_reports()[0]["reporter_user"] == ""


def test_the_report_gate_redirects_back_to_the_right_service(client, user_auth):
    resp = client.get("/report?service_id=3")
    assert "service_id%3D3" in resp.headers["Location"] or "service_id=3" in resp.headers["Location"]


def test_the_admin_reports_page_shows_who_filed_a_report(client, user_auth, monkeypatch):
    _sign_in(client, monkeypatch)
    db.create_problem_report("something", "", None, reporter_user="adam")
    client.get("/logout")
    _login(client)
    assert "adam" in client.get("/admin/reports").data.decode()


# ---------------------------------------------------------------------------
# Per-user portal access, at the route level
# ---------------------------------------------------------------------------
def test_blocking_a_user_ends_the_session_they_are_sitting_in(client, user_auth, monkeypatch):
    """Blocking that only takes effect at the next sign-in would leave someone with
    days of access left on their current session."""
    _sign_in(client, monkeypatch)
    assert "adam" in client.get("/").data.decode()
    db.set_jellyfin_user_allowed("u1", False)
    client.get("/")
    with client.session_transaction() as sess:
        assert "portal_user" not in sess


def test_a_blocked_user_cannot_sign_in_again(client, user_auth, monkeypatch):
    db.set_jellyfin_user_allowed("u1", False)
    monkeypatch.setattr(app_module.jellyfin_auth.requests, "post",
                        lambda url, **k: type("R", (), {
                            "status_code": 200, "ok": True,
                            "json": lambda self=None: {
                                "User": {"Id": "u1", "Name": "adam",
                                          "Policy": {"IsAdministrator": False, "IsDisabled": False}},
                                "AccessToken": "tok"}})())
    resp = client.post("/login", data={"username": "adam", "password": "pw"},
                       follow_redirects=True)
    body = resp.data.decode()
    assert "turned off by the administrator" in body
    assert "Jellyfin account itself is unaffected" in body
    with client.session_transaction() as sess:
        assert "portal_user" not in sess


def test_the_admin_can_block_and_unblock_from_the_users_page(client, user_auth):
    _login(client)
    resp = client.post("/admin/users/u1/access", data={"allow": "0"}, follow_redirects=True)
    assert "no longer sign in" in resp.data.decode()
    assert db.get_jellyfin_user("u1")["portal_allowed"] == 0

    resp = client.post("/admin/users/u1/access", data={"allow": "1"}, follow_redirects=True)
    assert "now sign in" in resp.data.decode()
    assert db.get_jellyfin_user("u1")["portal_allowed"] == 1


def test_blocking_requires_admin_login(client, user_auth):
    assert client.post("/admin/users/u1/access", data={"allow": "0"}).status_code == 302
    assert db.get_jellyfin_user("u1")["portal_allowed"] == 1


def test_blocking_an_unknown_user_is_rejected_cleanly(client, user_auth):
    _login(client)
    resp = client.post("/admin/users/nope/access", data={"allow": "0"}, follow_redirects=True)
    assert "No such user" in resp.data.decode()


def test_the_users_page_distinguishes_blocked_here_from_disabled_in_jellyfin(client, isolated_db):
    """Two different facts, fixed in two different places - the page must not make
    them look like the same thing."""
    _login(client)
    db.replace_jellyfin_users([{"id": "u1", "name": "blockedhere"},
                               {"id": "u2", "name": "offinjellyfin", "is_disabled": True}])
    db.set_jellyfin_user_allowed("u1", False)
    body = client.get("/admin/users").data.decode()
    assert "blocked here" in body
    assert "disabled in Jellyfin" in body


# ---------------------------------------------------------------------------
# The sign-in control in the fixed page-actions cluster
# ---------------------------------------------------------------------------
def test_the_sign_in_control_is_a_button_not_a_bare_link(client, user_auth):
    body = client.get("/").data.decode()
    assert 'class="page-actions"' in body
    assert 'class="btn signin"' in body


def test_the_sign_in_control_carries_the_current_page_as_next(client, user_auth):
    """Signing in from a page should return you to it, not dump you on the index."""
    body = client.get("/report").data.decode()
    assert "next=%2Freport" in body or "next=/report" in body


def test_a_signed_in_visitor_gets_a_user_chip_with_sign_out(client, user_auth, monkeypatch):
    _sign_in(client, monkeypatch)
    body = client.get("/").data.decode()
    assert 'class="user-chip"' in body
    assert "adam" in body
    assert "Sign out" in body
    assert 'class="btn signin"' not in body


def test_the_sign_in_button_is_absent_on_the_sign_in_page(client, user_auth):
    """Offering "Sign in" on the sign-in page is noise."""
    assert 'class="btn signin"' not in client.get("/login").data.decode()


def test_visitor_controls_never_appear_on_admin_pages(client, user_auth):
    """Admin pages have their own nav; a visitor sign-in button there is confusing at
    best and looks like a second way into the admin panel at worst."""
    _login(client)
    body = client.get("/admin/services").data.decode()
    assert 'class="btn signin"' not in body
    assert 'class="user-chip"' not in body


def test_the_theme_toggle_still_renders_inside_the_cluster(client):
    """It moved out of its own inline-positioned element - if the cluster ever stops
    rendering it, the theme button silently disappears from every page."""
    body = client.get("/").data.decode()
    assert 'id="theme-toggle"' in body
    assert body.index('class="page-actions"') < body.index('id="theme-toggle"')


# ---------------------------------------------------------------------------
# The reporter survives into an incident
# ---------------------------------------------------------------------------
def test_creating_an_incident_from_a_report_keeps_the_reporter(client, isolated_db):
    _login(client)
    rid = db.create_problem_report("Playback stutters", "", None, reporter_user="adam")
    client.post(f"/admin/reports/{rid}/create-incident", follow_redirects=True)
    incident = db.list_incidents()[0]
    assert 'Reported by Jellyfin user "adam"' in incident["description"]
    assert "Playback stutters" in incident["description"]


def test_an_anonymous_report_says_so_in_the_incident(client, isolated_db):
    _login(client)
    rid = db.create_problem_report("Something broke")
    client.post(f"/admin/reports/{rid}/create-incident", follow_redirects=True)
    assert "Reported anonymously" in db.list_incidents()[0]["description"]


# ---------------------------------------------------------------------------
# The signed-in user's own account page (/account)
# ---------------------------------------------------------------------------
def _file_report(client, message="something is broken"):
    client.get("/report")
    with client.session_transaction() as sess:
        sess["report_form_rendered_at"] = time.time() - 60
    return client.post("/report", data={"message": message}, follow_redirects=True)


def test_the_account_page_requires_a_signed_in_visitor(client, user_auth):
    resp = client.get("/account")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_being_the_admin_does_not_grant_the_account_page(client, user_auth):
    """user_login_required reads portal_user and nothing else - the admin session is a
    different identity, not a superset of this one."""
    _login(client)
    assert client.get("/account").status_code == 302


def test_the_account_page_shows_the_signed_in_user(client, user_auth, monkeypatch):
    _sign_in(client, monkeypatch)
    body = client.get("/account").data.decode()
    assert "Your account" in body
    assert "adam" in body


def test_the_username_in_the_chip_links_to_the_account_page(client, user_auth, monkeypatch):
    _sign_in(client, monkeypatch)
    body = client.get("/").data.decode()
    assert 'href="/account"' in body


def test_a_user_sees_their_own_report_and_its_status(client, user_auth, monkeypatch):
    _sign_in(client, monkeypatch)
    _file_report(client, "Playback keeps buffering")
    body = client.get("/account").data.decode()
    assert "Playback keeps buffering" in body
    assert "Waiting to be looked at" in body


def test_a_user_never_sees_someone_elses_report(client, user_auth, monkeypatch):
    """The security property of this whole page."""
    db.replace_jellyfin_users([{"id": "u1", "name": "adam"}, {"id": "u2", "name": "sam"}])
    db.create_problem_report("sam's private report", "", None,
                             reporter_user="sam", reporter_user_id="u2")
    db.create_problem_report("an anonymous report")
    _sign_in(client, monkeypatch)
    body = client.get("/account").data.decode()
    assert "sam's private report" not in body
    assert "an anonymous report" not in body


def test_the_status_wording_is_aimed_at_the_reporter(client, user_auth, monkeypatch):
    """"new"/"reviewed" are triage words that mean nothing to the person waiting."""
    _sign_in(client, monkeypatch)
    _file_report(client)
    rid = db.list_problem_reports()[0]["id"]
    db.update_problem_report_status(rid, "reviewed")
    assert "Being looked into" in client.get("/account").data.decode()


def test_an_admin_reply_is_shown_to_the_reporter(client, user_auth, monkeypatch):
    _sign_in(client, monkeypatch)
    _file_report(client)
    rid = db.list_problem_reports()[0]["id"]
    client.get("/logout")

    _login(client)
    client.post(f"/admin/reports/{rid}/reply", data={"reply": "Restarted the transcoder."})
    client.get("/admin/logout")

    _sign_in(client, monkeypatch)
    body = client.get("/account").data.decode()
    assert "Restarted the transcoder." in body
    assert "The admin" in body


def test_an_unread_reply_puts_a_dot_on_the_chip_and_opening_the_page_clears_it(client, user_auth, monkeypatch):
    """Without the dot nobody knows to go and look, which makes replying pointless."""
    _sign_in(client, monkeypatch)
    _file_report(client)
    rid = db.list_problem_reports()[0]["id"]
    db.add_report_message(rid, "admin", "Answered.")

    assert "user-chip__dot" in client.get("/").data.decode()
    client.get("/account")
    assert "user-chip__dot" not in client.get("/").data.decode()


def test_a_linked_incident_is_shown_on_the_account_page(client, user_auth, monkeypatch):
    _sign_in(client, monkeypatch)
    _file_report(client, "Jellyfin is down")
    rid = db.list_problem_reports()[0]["id"]
    client.get("/logout")

    _login(client)
    client.post(f"/admin/reports/{rid}/create-incident")
    client.get("/admin/logout")

    _sign_in(client, monkeypatch)
    body = client.get("/account").data.decode()
    assert "An incident was opened from this report" in body
    assert "investigating" in body


def test_saving_preferences_persists_them(client, user_auth, monkeypatch):
    _sign_in(client, monkeypatch)
    resp = client.post("/account", data={"theme": "light", "contact": "me@example.invalid"},
                       follow_redirects=True)
    assert "settings have been saved" in resp.data.decode()
    assert db.get_user_preferences("u1") == {"theme": "light", "contact": "me@example.invalid"}


def test_an_explicit_theme_is_rendered_into_the_html_tag(client, user_auth, monkeypatch):
    """So a device that has never seen this portal doesn't flash the wrong colours
    before any JavaScript runs."""
    _sign_in(client, monkeypatch)
    db.set_user_preferences("u1", theme="light")
    assert 'data-server-theme="light"' in client.get("/").data.decode()


# The bare string "data-server-theme" also appears inside the inline FOUC script,
# which reads the attribute - so these assert on the *attribute* form specifically
# (the script uses single quotes), not on the name appearing anywhere in the page.
def test_auto_renders_no_server_theme_attribute(client, user_auth, monkeypatch):
    _sign_in(client, monkeypatch)
    db.set_user_preferences("u1", theme="auto")
    assert 'data-server-theme="' not in client.get("/").data.decode()


def test_a_signed_out_visitor_gets_no_server_theme(client, user_auth):
    assert 'data-server-theme="' not in client.get("/").data.decode()


def test_the_theme_endpoint_updates_only_the_theme(client, user_auth, monkeypatch):
    _sign_in(client, monkeypatch)
    db.set_user_preferences("u1", theme="dark", contact="keep me")
    resp = client.post("/account/theme", data={"theme": "light"})
    assert resp.status_code == 204
    assert db.get_user_preferences("u1") == {"theme": "light", "contact": "keep me"}


def test_the_theme_endpoint_requires_a_signed_in_visitor(client, user_auth):
    assert client.post("/account/theme", data={"theme": "light"}).status_code == 302


def test_the_saved_flag_is_passed_through_for_the_local_sync(client, user_auth, monkeypatch):
    """account.js only syncs this browser's stored theme right after a save - the flag
    is how it knows, and without it the setting appears to do nothing on the very
    device it was changed from."""
    _sign_in(client, monkeypatch)
    assert 'data-just-saved="1"' in client.get("/account?saved=1").data.decode()
    assert 'data-just-saved="0"' in client.get("/account").data.decode()


def test_the_report_form_prefills_the_saved_contact(client, user_auth, monkeypatch):
    _sign_in(client, monkeypatch)
    db.set_user_preferences("u1", contact="adam@example.invalid")
    assert 'value="adam@example.invalid"' in client.get("/report").data.decode()


# ---- The admin side of replying ----
def test_replying_to_an_anonymous_report_warns_that_nobody_will_see_it(client, isolated_db):
    _login(client)
    rid = db.create_problem_report("anonymous complaint")
    resp = client.post(f"/admin/reports/{rid}/reply", data={"reply": "hello?"},
                       follow_redirects=True)
    assert "nobody can see it" in resp.data.decode()


def test_replying_requires_admin_login(client, isolated_db):
    rid = db.create_problem_report("x", "", None, reporter_user="adam", reporter_user_id="u1")
    assert client.post(f"/admin/reports/{rid}/reply", data={"reply": "sneaky"}).status_code == 302
    assert db.get_problem_report(rid)["admin_reply"] == ""


def test_replying_to_a_missing_report_is_rejected_cleanly(client, isolated_db):
    _login(client)
    resp = client.post("/admin/reports/9999/reply", data={"reply": "x"}, follow_redirects=True)
    assert "Report not found" in resp.data.decode()


def test_the_admin_list_shows_whether_a_reply_has_been_read(client, user_auth, monkeypatch):
    _sign_in(client, monkeypatch)
    _file_report(client)
    rid = db.list_problem_reports()[0]["id"]
    client.get("/logout")
    _login(client)
    client.post(f"/admin/reports/{rid}/reply", data={"reply": "On it."})
    assert "unread" in client.get("/admin/reports").data.decode()

    client.get("/admin/logout")
    _sign_in(client, monkeypatch)
    client.get("/account")
    client.get("/logout")
    _login(client)
    body = client.get("/admin/reports").data.decode()
    assert "· read" in body or "read</div>" in body


# ---- The reporter's side of the conversation ----
def test_a_user_can_reply_to_their_own_report(client, user_auth, monkeypatch):
    _sign_in(client, monkeypatch)
    _file_report(client, "Subtitles are off")
    rid = db.list_problem_reports()[0]["id"]
    db.add_report_message(rid, "admin", "Which show?")

    resp = client.post(f"/account/reports/{rid}/reply",
                       data={"body": "Any of them, since yesterday."}, follow_redirects=True)
    assert "Your reply has been sent" in resp.data.decode()
    assert [m["author"] for m in db.list_report_messages(rid)] == ["admin", "user"]
    assert "Any of them, since yesterday." in client.get("/account").data.decode()


def test_a_user_cannot_reply_to_someone_elses_report(client, user_auth, monkeypatch):
    """Ownership is checked against the report's stored user id, and a report that
    isn't yours answers exactly like one that doesn't exist - "that exists but isn't
    yours" is itself information about other people's reports."""
    db.replace_jellyfin_users([{"id": "u1", "name": "adam"}, {"id": "u2", "name": "sam"}])
    rid = db.create_problem_report("sam's report", "", None,
                                    reporter_user="sam", reporter_user_id="u2")
    _sign_in(client, monkeypatch)
    resp = client.post(f"/account/reports/{rid}/reply", data={"body": "butting in"},
                       follow_redirects=True)
    assert "could not be found" in resp.data.decode()
    assert db.list_report_messages(rid) == []


def test_replying_to_a_nonexistent_report_looks_identical(client, user_auth, monkeypatch):
    _sign_in(client, monkeypatch)
    resp = client.post("/account/reports/9999/reply", data={"body": "hello"},
                       follow_redirects=True)
    assert "could not be found" in resp.data.decode()


def test_an_empty_user_reply_is_rejected(client, user_auth, monkeypatch):
    _sign_in(client, monkeypatch)
    _file_report(client)
    rid = db.list_problem_reports()[0]["id"]
    resp = client.post(f"/account/reports/{rid}/reply", data={"body": "   "},
                       follow_redirects=True)
    assert "Write something first" in resp.data.decode()
    assert db.list_report_messages(rid) == []


def test_replying_requires_a_signed_in_visitor(client, user_auth):
    rid = db.create_problem_report("x", "", None, reporter_user="adam", reporter_user_id="u1")
    assert client.post(f"/account/reports/{rid}/reply", data={"body": "hi"}).status_code == 302
    assert db.list_report_messages(rid) == []


def test_a_user_reply_shows_up_on_the_admin_nav_badge(client, user_auth, monkeypatch):
    """Without this the admin never learns anybody answered, and the conversation is
    one-directional in practice."""
    _sign_in(client, monkeypatch)
    _file_report(client)
    rid = db.list_problem_reports()[0]["id"]
    db.update_problem_report_status(rid, "reviewed")  # no longer "new", so the badge is 0
    client.post(f"/account/reports/{rid}/reply", data={"body": "Still happening."})
    client.get("/logout")

    _login(client)
    assert db.count_unseen_user_messages() == 1
    assert "nav-badge" in client.get("/admin/services").data.decode()
    # Opening the Reports page is what clears it.
    client.get("/admin/reports")
    assert db.count_unseen_user_messages() == 0


def test_a_user_reply_does_not_reopen_a_closed_report(client, user_auth, monkeypatch):
    """A status changing itself underneath the admin would be surprising; the unread
    badge is the signal, and reopening is their call."""
    _sign_in(client, monkeypatch)
    _file_report(client)
    rid = db.list_problem_reports()[0]["id"]
    db.update_problem_report_status(rid, "resolved")
    client.post(f"/account/reports/{rid}/reply", data={"body": "It's back."})
    assert db.get_problem_report(rid)["status"] == "resolved"


def test_the_thread_shows_both_sides_to_the_reporter(client, user_auth, monkeypatch):
    _sign_in(client, monkeypatch)
    _file_report(client)
    rid = db.list_problem_reports()[0]["id"]
    db.add_report_message(rid, "admin", "Looking now.")
    client.post(f"/account/reports/{rid}/reply", data={"body": "Thanks!"})
    body = client.get("/account").data.decode()
    assert "Looking now." in body and "Thanks!" in body
    assert "The admin" in body and "You" in body
