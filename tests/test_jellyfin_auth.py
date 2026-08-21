"""Tests for jellyfin_auth.py - the cached user list, the sync task, and the
credential check.

Every outbound call is mocked: there is no real Jellyfin in this sandbox, and there
must never be a test that depends on one. What's being pinned down here is mostly the
*failure* behaviour, because that is where the design decisions live - a failed sync
must not clear the cache, an unreachable server must not be reported as a wrong
password, and an empty cache must not be read as "this user doesn't exist".
"""
import pytest
import requests

import config
import db
import jellyfin_auth
import scheduler


@pytest.fixture
def jellyfin_integration(isolated_db):
    iid = db.create_integration({"name": "Jellyfin", "kind": "jellyfin",
                                  "base_url": "http://jellyfin.invalid", "api_key": "apikey",
                                  "enabled": 1, "service_id": None,
                                  "show_on_public": 0, "auto_incident": 0})
    db.set_setting("jellyfin_auth_enabled", "1")
    return iid


class _Response:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.ok = 200 <= status_code < 400

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def _jf_api_user(uid="u1", name="adam", admin=False, disabled=False):
    return {"Id": uid, "Name": name,
            "Policy": {"IsAdministrator": admin, "IsDisabled": disabled}}


# ---------------------------------------------------------------------------
# Which Jellyfin, and is the feature on
# ---------------------------------------------------------------------------
def test_auth_is_off_by_default_even_with_an_integration_configured(isolated_db):
    """Adding a Jellyfin integration must not spontaneously put a public login form
    on somebody's status page - enabling it has to be a deliberate choice."""
    db.create_integration({"name": "J", "kind": "jellyfin", "base_url": "http://x",
                            "api_key": "k", "enabled": 1, "service_id": None,
                            "show_on_public": 0, "auto_incident": 0})
    assert jellyfin_auth.is_enabled() is False


def test_auth_is_off_when_switched_on_but_no_integration_exists(isolated_db):
    db.set_setting("jellyfin_auth_enabled", "1")
    assert jellyfin_auth.auth_integration() is None
    assert jellyfin_auth.is_enabled() is False


def test_the_first_enabled_jellyfin_integration_is_used_when_none_is_picked(jellyfin_integration):
    assert jellyfin_auth.auth_integration()["id"] == jellyfin_integration
    assert jellyfin_auth.is_enabled() is True


def test_an_explicitly_chosen_integration_wins(jellyfin_integration):
    second = db.create_integration({"name": "Other", "kind": "jellyfin", "base_url": "http://y",
                                     "api_key": "k2", "enabled": 1, "service_id": None,
                                     "show_on_public": 0, "auto_incident": 0})
    db.set_setting("jellyfin_auth_integration_id", str(second))
    assert jellyfin_auth.auth_integration()["id"] == second


def test_a_chosen_integration_that_was_deleted_disables_the_feature(jellyfin_integration):
    """Falling back to some *other* Jellyfin would silently point sign-in at a server
    the admin never chose."""
    db.set_setting("jellyfin_auth_integration_id", "9999")
    assert jellyfin_auth.auth_integration() is None
    assert jellyfin_auth.is_enabled() is False


def test_a_disabled_integration_is_not_used(jellyfin_integration):
    db.update_integration(jellyfin_integration, {"name": "Jellyfin", "kind": "jellyfin",
                                                  "base_url": "http://jellyfin.invalid",
                                                  "enabled": 0, "service_id": None,
                                                  "show_on_public": 0, "auto_incident": 0})
    assert jellyfin_auth.auth_integration() is None


def test_a_non_jellyfin_integration_is_never_used_for_auth(isolated_db):
    db.set_setting("jellyfin_auth_enabled", "1")
    db.create_integration({"name": "Radarr", "kind": "arr", "base_url": "http://x",
                            "api_key": "k", "enabled": 1, "service_id": None,
                            "show_on_public": 0, "auto_incident": 0})
    assert jellyfin_auth.auth_integration() is None


# ---------------------------------------------------------------------------
# The sync task
# ---------------------------------------------------------------------------
def test_sync_stores_the_user_list(jellyfin_integration, monkeypatch):
    monkeypatch.setattr(jellyfin_auth.requests, "get",
                        lambda *a, **k: _Response(200, [_jf_api_user("u1", "adam"),
                                                        _jf_api_user("u2", "sam", admin=True)]))
    message = jellyfin_auth.sync_users()
    assert "2" in message
    assert db.count_jellyfin_users() == 2
    assert db.get_jellyfin_user_by_name("adam")["id"] == "u1"
    assert db.get_jellyfin_user("u2")["is_administrator"] == 1


def test_sync_counts_disabled_users_in_its_message(jellyfin_integration, monkeypatch):
    monkeypatch.setattr(jellyfin_auth.requests, "get",
                        lambda *a, **k: _Response(200, [_jf_api_user("u1", "adam"),
                                                        _jf_api_user("u2", "old", disabled=True)]))
    assert "1 disabled" in jellyfin_auth.sync_users()


def test_sync_without_an_integration_is_skipped_not_failed(isolated_db):
    """"You haven't set this up" and "this is broken" must read differently in the
    task list."""
    with pytest.raises(scheduler.TaskSkipped):
        jellyfin_auth.sync_users()


def test_an_unreachable_jellyfin_leaves_the_previous_list_completely_intact(jellyfin_integration, monkeypatch):
    """The single most important property of the sync task: a failed poll must never
    reduce what the portal knows. This cache is what keeps people signed in during an
    outage."""
    monkeypatch.setattr(jellyfin_auth.requests, "get",
                        lambda *a, **k: _Response(200, [_jf_api_user("u1", "adam")]))
    jellyfin_auth.sync_users()
    synced_at = db.jellyfin_users_synced_at()

    def boom(*a, **k):
        raise requests.ConnectionError("connection refused")

    monkeypatch.setattr(jellyfin_auth.requests, "get", boom)
    with pytest.raises(requests.ConnectionError):
        jellyfin_auth.sync_users()

    assert db.count_jellyfin_users() == 1
    assert db.get_jellyfin_user_by_name("adam") is not None
    assert db.jellyfin_users_synced_at() == synced_at


def test_a_500_from_jellyfin_leaves_the_previous_list_intact(jellyfin_integration, monkeypatch):
    monkeypatch.setattr(jellyfin_auth.requests, "get",
                        lambda *a, **k: _Response(200, [_jf_api_user("u1", "adam")]))
    jellyfin_auth.sync_users()
    monkeypatch.setattr(jellyfin_auth.requests, "get", lambda *a, **k: _Response(503))
    with pytest.raises(requests.HTTPError):
        jellyfin_auth.sync_users()
    assert db.count_jellyfin_users() == 1


def test_an_empty_user_list_is_refused_rather_than_wiping_the_cache(jellyfin_integration, monkeypatch):
    """A Jellyfin with genuinely zero users isn't a thing; an empty 200 is far more
    likely to be a proxy answering instead of Jellyfin. Refusing it keeps the
    previous list, which is the safe direction to be wrong in."""
    monkeypatch.setattr(jellyfin_auth.requests, "get",
                        lambda *a, **k: _Response(200, [_jf_api_user("u1", "adam")]))
    jellyfin_auth.sync_users()
    monkeypatch.setattr(jellyfin_auth.requests, "get", lambda *a, **k: _Response(200, []))
    with pytest.raises(ValueError):
        jellyfin_auth.sync_users()
    assert db.count_jellyfin_users() == 1


def test_a_non_list_response_is_refused(jellyfin_integration, monkeypatch):
    monkeypatch.setattr(jellyfin_auth.requests, "get",
                        lambda *a, **k: _Response(200, {"Items": []}))
    with pytest.raises(ValueError):
        jellyfin_auth.sync_users()


def test_the_sync_task_is_registered_with_the_scheduler():
    task = scheduler.get_task(jellyfin_auth.TASK_NAME)
    assert task is not None
    assert task.defaults["interval_minutes"] == 60


def test_a_failing_sync_is_recorded_by_the_scheduler_rather_than_raising(jellyfin_integration, monkeypatch):
    """End to end through the framework: the task's failure becomes a row, not an
    exception in the scheduler thread."""
    def boom(*a, **k):
        raise requests.ConnectionError("no route to host")

    monkeypatch.setattr(jellyfin_auth.requests, "get", boom)
    status, message = scheduler.run_task(jellyfin_auth.TASK_NAME)
    assert status == "failed"
    assert "no route to host" in message
    assert db.get_task_row(jellyfin_auth.TASK_NAME)["last_status"] == "failed"


def test_the_api_key_is_sent_as_an_emby_token_header(jellyfin_integration, monkeypatch):
    captured = {}

    def fake_get(url, headers=None, timeout=None, **k):
        captured["url"] = url
        captured["headers"] = headers
        captured["timeout"] = timeout
        return _Response(200, [_jf_api_user()])

    monkeypatch.setattr(jellyfin_auth.requests, "get", fake_get)
    jellyfin_auth.sync_users()
    assert captured["url"] == "http://jellyfin.invalid/Users"
    assert captured["headers"]["X-Emby-Token"] == "apikey"
    assert "MediaBrowser Client=" in captured["headers"]["Authorization"]
    assert captured["timeout"] == config.JELLYFIN_AUTH_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# authenticate()
# ---------------------------------------------------------------------------
def _auth_response(monkeypatch, response, capture=None):
    def fake_post(url, **kwargs):
        if capture is not None:
            capture.setdefault("calls", []).append((url, kwargs))
        if url.endswith("/Sessions/Logout"):
            return _Response(204)
        return response() if callable(response) else response

    monkeypatch.setattr(jellyfin_auth.requests, "post", fake_post)


def test_a_valid_password_returns_the_user(jellyfin_integration, monkeypatch):
    _auth_response(monkeypatch, _Response(200, {"User": _jf_api_user("u1", "adam"),
                                                 "AccessToken": "tok"}))
    result = jellyfin_auth.authenticate("adam", "hunter2")
    assert result["ok"] is True
    assert result["user"] == {"id": "u1", "name": "adam",
                              "is_administrator": False, "is_disabled": False}


@pytest.mark.parametrize("status", [400, 401, 403])
def test_a_rejected_password_is_reported_as_invalid(jellyfin_integration, monkeypatch, status):
    _auth_response(monkeypatch, _Response(status))
    assert jellyfin_auth.authenticate("adam", "wrong")["reason"] == "invalid"


def test_an_unreachable_jellyfin_is_never_reported_as_a_wrong_password(jellyfin_integration, monkeypatch):
    """Telling someone their password is wrong when the server is merely down sends
    them off to reset a password that was fine."""
    def boom(*a, **k):
        raise requests.ConnectionError("connection refused")

    monkeypatch.setattr(jellyfin_auth.requests, "post", boom)
    result = jellyfin_auth.authenticate("adam", "hunter2")
    assert result["ok"] is False
    assert result["reason"] == "unreachable"
    assert "connection refused" in result["error"]


@pytest.mark.parametrize("status", [500, 502, 503])
def test_a_server_error_is_unreachable_not_invalid(jellyfin_integration, monkeypatch, status):
    _auth_response(monkeypatch, _Response(status))
    assert jellyfin_auth.authenticate("adam", "hunter2")["reason"] == "unreachable"


def test_a_disabled_account_with_a_correct_password_is_refused(jellyfin_integration, monkeypatch):
    _auth_response(monkeypatch, _Response(200, {"User": _jf_api_user("u1", "adam", disabled=True),
                                                 "AccessToken": "tok"}))
    assert jellyfin_auth.authenticate("adam", "hunter2")["reason"] == "disabled"


def test_authentication_without_configuration_is_reported_as_such(isolated_db):
    assert jellyfin_auth.authenticate("adam", "x")["reason"] == "not_configured"


def test_a_non_json_response_does_not_raise(jellyfin_integration, monkeypatch):
    _auth_response(monkeypatch, _Response(200, ValueError("not json")))
    assert jellyfin_auth.authenticate("adam", "x")["reason"] == "unreachable"


def test_the_short_lived_access_token_is_revoked_immediately(jellyfin_integration, monkeypatch):
    """The token is used once, to end itself. It never reaches the session cookie
    (which is signed but readable), and Jellyfin doesn't accumulate a dead device
    entry per sign-in."""
    capture = {}
    _auth_response(monkeypatch, _Response(200, {"User": _jf_api_user(), "AccessToken": "tok"}),
                   capture=capture)
    jellyfin_auth.authenticate("adam", "hunter2")
    urls = [url for url, _ in capture["calls"]]
    assert urls == ["http://jellyfin.invalid/Users/AuthenticateByName",
                    "http://jellyfin.invalid/Sessions/Logout"]
    assert capture["calls"][1][1]["headers"]["X-Emby-Token"] == "tok"


def test_a_failed_revocation_does_not_fail_the_sign_in(jellyfin_integration, monkeypatch):
    def fake_post(url, **kwargs):
        if url.endswith("/Sessions/Logout"):
            raise requests.ConnectionError("gone")
        return _Response(200, {"User": _jf_api_user(), "AccessToken": "tok"})

    monkeypatch.setattr(jellyfin_auth.requests, "post", fake_post)
    assert jellyfin_auth.authenticate("adam", "hunter2")["ok"] is True


def test_the_password_is_only_ever_sent_to_jellyfin_and_never_returned(jellyfin_integration, monkeypatch):
    capture = {}
    _auth_response(monkeypatch, _Response(200, {"User": _jf_api_user(), "AccessToken": "tok"}),
                   capture=capture)
    result = jellyfin_auth.authenticate("adam", "s3cret")
    assert capture["calls"][0][1]["json"] == {"Username": "adam", "Pw": "s3cret"}
    assert "s3cret" not in repr(result)


# ---------------------------------------------------------------------------
# Session validity against the cache
# ---------------------------------------------------------------------------
def test_an_unpopulated_cache_never_invalidates_a_session(isolated_db):
    """An empty cache is missing information, not evidence the account is gone. If
    this returned False, the very first sign-in before any sync had run would be
    thrown away on the next request."""
    assert db.jellyfin_users_synced_at() is None
    assert jellyfin_auth.session_user_still_valid("anything") is True


def test_a_user_present_in_the_cache_stays_valid(isolated_db):
    db.replace_jellyfin_users([{"id": "u1", "name": "adam"}])
    assert jellyfin_auth.session_user_still_valid("u1") is True


def test_a_user_removed_from_jellyfin_loses_their_session(isolated_db):
    db.replace_jellyfin_users([{"id": "u1", "name": "adam"}])
    db.replace_jellyfin_users([{"id": "u2", "name": "sam"}])
    assert jellyfin_auth.session_user_still_valid("u1") is False


def test_a_user_disabled_in_jellyfin_loses_their_session(isolated_db):
    db.replace_jellyfin_users([{"id": "u1", "name": "adam", "is_disabled": True}])
    assert jellyfin_auth.session_user_still_valid("u1") is False


def test_status_summary_reports_why_the_feature_is_off(isolated_db):
    summary = jellyfin_auth.status_summary()
    assert summary["enabled"] is False
    assert summary["toggle_on"] is False
    assert summary["integration"] is None
    assert summary["cached_users"] == 0
    assert summary["synced_at"] is None
