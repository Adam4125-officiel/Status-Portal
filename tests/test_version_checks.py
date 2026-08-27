"""Tests for version_checks.py - "is Radarr/Sonarr/Prowlarr behind?".

No real Servarr instance exists in this sandbox, so the app half is exercised against
a stand-in HTTP server built from the documented /api/v3/system/status shape, and the
GitHub half against a mocked releases response. The *tag formats* were confirmed
against the three projects' real release feeds (v6.4.1.10545, v4.0.19.3001,
v2.6.1.5509) - which is what the four-component parser below exists for.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import db
import scheduler
import updater
import version_checks


# ---------------------------------------------------------------------------
# Version parsing - the reason this module doesn't reuse updater.parse_version
# ---------------------------------------------------------------------------
def test_the_build_number_is_what_actually_moves_between_servarr_releases():
    """The whole reason for a separate parser. Two real consecutive Radarr releases
    differ only in the fourth component; updater.parse_version keeps three and spends
    its fourth slot on a prerelease rank, so it parses both identically and would
    report a months-old Radarr as up to date."""
    assert updater.parse_version("6.4.0.10540") == updater.parse_version("6.4.0.10523")
    assert version_checks._compare("6.4.0.10523", "6.4.0.10540") == -1


@pytest.mark.parametrize("tag, expected", [
    ("v6.4.1.10545", (6, 4, 1, 10545)),   # Radarr, real tag
    ("v4.0.19.3001", (4, 0, 19, 3001)),   # Sonarr, real tag
    ("v2.6.1.5509", (2, 6, 1, 5509)),     # Prowlarr, real tag
    ("5.14.0", (5, 14, 0)),
    ("4.0.1.929-develop", (4, 0, 1, 929)),
])
def test_parses_real_servarr_tag_formats(tag, expected):
    assert version_checks.parse_version(tag) == expected


def test_unparseable_versions_never_look_newer():
    """Same defensive choice as updater's: a malformed tag sorts at the bottom, so it
    can't be mistaken for an available update."""
    assert version_checks._compare("5.0.0.1", "not-a-version") == 1


def test_shorter_and_longer_versions_compare_by_value_not_length():
    assert version_checks._compare("5.14", "5.14.0.0") == 0


# ---------------------------------------------------------------------------
# A stand-in Servarr, since there isn't a real one here
# ---------------------------------------------------------------------------
class _FakeServarr(BaseHTTPRequestHandler):
    app_name = "Radarr"
    version = "6.4.0.10523"
    status_code = 200

    def do_GET(self):
        if not self.path.startswith("/api/v3/system/status"):
            self.send_response(404); self.end_headers(); return
        if self.status_code != 200:
            self.send_response(self.status_code); self.end_headers(); return
        body = json.dumps({"appName": self.app_name, "version": self.version}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def servarr():
    server = HTTPServer(("127.0.0.1", 0), _FakeServarr)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def _integration(base_url, name="Radarr"):
    return {"id": 1, "name": name, "kind": "arr", "base_url": base_url,
            "api_key": "k", "enabled": 1}


def test_identifies_the_app_from_its_own_answer_not_the_admins_label(servarr, monkeypatch):
    """An integration an admin called "movies-4k" is still Radarr, and one they called
    "radarr" might not be. Guessing from the label would eventually check the wrong
    project's releases and confidently report the wrong answer."""
    monkeypatch.setattr(version_checks, "_fetch_latest_release",
                        lambda repo: ("6.4.1.10545", "https://example/rel"))
    result = version_checks.check_one(_integration(servarr, name="movies-4k"))
    assert result["app"] == "Radarr"
    assert result["repo"] == "Radarr/Radarr"
    assert result["update_available"] is True


def test_reports_up_to_date_when_the_build_matches(servarr, monkeypatch):
    monkeypatch.setattr(version_checks, "_fetch_latest_release",
                        lambda repo: ("6.4.0.10523", "https://example/rel"))
    result = version_checks.check_one(_integration(servarr))
    assert result["update_available"] is False
    assert result["error"] is None


def test_an_app_with_no_release_feed_is_reported_not_swallowed(servarr, monkeypatch):
    """Bazarr and friends simply aren't covered. Saying so beats them silently
    vanishing from a list the admin is using to decide what's up to date."""
    monkeypatch.setattr(_FakeServarr, "app_name", "Bazarr")
    result = version_checks.check_one(_integration(servarr))
    assert result["update_available"] is False
    assert "No release feed configured" in result["error"]


def test_an_unreachable_app_is_an_error_row_not_an_exception():
    result = version_checks.check_one(_integration("http://127.0.0.1:1"))
    assert result["error"]
    assert result["update_available"] is False


def test_a_github_failure_does_not_lose_the_installed_version(servarr, monkeypatch):
    """Knowing what you're running is still useful when the release lookup fails - a
    rate-limited GitHub must not blank the whole row."""
    def boom(repo):
        raise version_checks.requests.RequestException("rate limit exceeded")
    monkeypatch.setattr(version_checks, "_fetch_latest_release", boom)
    result = version_checks.check_one(_integration(servarr))
    assert result["installed"] == "6.4.0.10523"
    assert "rate limit" in result["error"]


# ---------------------------------------------------------------------------
# The scheduled task and its persisted result
# ---------------------------------------------------------------------------
def test_task_skips_rather_than_fails_with_nothing_to_check(isolated_db):
    with pytest.raises(scheduler.TaskSkipped):
        version_checks.run_check_task()


def test_one_broken_app_does_not_stop_the_others(isolated_db, servarr, monkeypatch):
    db.create_integration({"name": "Radarr", "kind": "arr", "base_url": servarr,
                            "api_key": "k", "enabled": 1})
    db.create_integration({"name": "Dead", "kind": "arr", "base_url": "http://127.0.0.1:1",
                            "api_key": "k", "enabled": 1})
    monkeypatch.setattr(version_checks, "_fetch_latest_release",
                        lambda repo: ("6.4.1.10545", "https://example/rel"))
    message = version_checks.run_check_task()
    stored = version_checks.get_results()
    assert len(stored["results"]) == 2
    assert "update available" in message and "couldn't be checked" in message


def test_a_run_where_everything_failed_is_recorded_as_a_failure(isolated_db):
    """Caught by live-testing against a rate-limited GitHub: the first version showed
    green in the task list for a run that had answered no question at all."""
    db.create_integration({"name": "Dead", "kind": "arr", "base_url": "http://127.0.0.1:1",
                            "api_key": "k", "enabled": 1})
    with pytest.raises(RuntimeError, match="Nothing could be checked"):
        version_checks.run_check_task()


def test_a_fully_failed_run_still_records_why_per_app(isolated_db):
    """The per-app reasons are stored before the failure is raised, so the admin page
    explains what went wrong instead of just going blank."""
    db.create_integration({"name": "Dead", "kind": "arr", "base_url": "http://127.0.0.1:1",
                            "api_key": "k", "enabled": 1})
    with pytest.raises(RuntimeError):
        version_checks.run_check_task()
    assert version_checks.get_results()["results"][0]["error"]


def test_results_survive_a_restart(isolated_db, servarr, monkeypatch):
    """Persisted in the settings table rather than a module-level cache: this is a
    daily task, so an in-memory result would leave the admin page saying "not checked
    yet" for up to a day after every restart."""
    db.create_integration({"name": "Radarr", "kind": "arr", "base_url": servarr,
                            "api_key": "k", "enabled": 1})
    monkeypatch.setattr(version_checks, "_fetch_latest_release",
                        lambda repo: ("6.4.1.10545", "https://example/rel"))
    version_checks.run_check_task()
    # Nothing in memory is consulted - this is a fresh read straight from the database.
    assert version_checks.updates_available() == 1
    assert version_checks.get_results()["results"][0]["latest"] == "6.4.1.10545"


def test_a_corrupted_stored_result_is_ignored_not_raised(isolated_db):
    db.set_setting(version_checks.RESULT_SETTING, "{not json")
    assert version_checks.get_results() is None
    assert version_checks.updates_available() == 0


def test_the_repo_list_is_a_constant_not_a_setting():
    """Same rule as updater.py's: a configurable "where do I look up versions" is a
    request-forgery primitive handed to anyone who can write a setting."""
    source = open(version_checks.__file__, encoding="utf-8").read()
    assert "get_setting" not in source.split("KNOWN_APPS")[1].split("def parse_version")[0]
    assert "verify=True" in source


# ---------------------------------------------------------------------------
# Jellyfin and Seerr, whose integration kind already says what they are
# ---------------------------------------------------------------------------
class _DirectApp(BaseHTTPRequestHandler):
    payloads = {}

    def do_GET(self):
        for path, body in _DirectApp.payloads.items():
            if self.path.startswith(path):
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
        self.send_response(404); self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def direct_app():
    server = HTTPServer(("127.0.0.1", 0), _DirectApp)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    _DirectApp.payloads = {}


def test_jellyfin_version_comes_from_system_info(direct_app, monkeypatch):
    _DirectApp.payloads = {"/System/Info": {"Version": "10.11.10"}}
    monkeypatch.setattr(version_checks, "_fetch_latest_release",
                        lambda repo: ("10.11.11", "https://example/rel"))
    result = version_checks.check_one(
        {"id": 1, "name": "My Jellyfin", "kind": "jellyfin", "base_url": direct_app,
         "api_key": "k", "enabled": 1})
    assert result["app"] == "Jellyfin"
    assert result["repo"] == "jellyfin/jellyfin"
    assert result["installed"] == "10.11.10"
    assert result["update_available"] is True


def test_seerr_version_comes_from_its_status_endpoint(direct_app, monkeypatch):
    _DirectApp.payloads = {"/api/v1/status": {"version": "3.4.1"}}
    monkeypatch.setattr(version_checks, "_fetch_latest_release",
                        lambda repo: ("3.4.1", "https://example/rel"))
    result = version_checks.check_one(
        {"id": 1, "name": "Requests", "kind": "jellyseerr", "base_url": direct_app,
         "api_key": "k", "enabled": 1})
    assert result["app"] == "Seerr"
    assert result["repo"] == "seerr-team/seerr"
    assert result["update_available"] is False


def test_the_kind_identifies_the_app_so_the_name_is_irrelevant(direct_app, monkeypatch):
    """No appName lookup for these, so an integration called anything at all still
    resolves to the right project."""
    _DirectApp.payloads = {"/System/Info": {"Version": "10.11.11"}}
    monkeypatch.setattr(version_checks, "_fetch_latest_release",
                        lambda repo: ("10.11.11", "https://example/rel"))
    result = version_checks.check_one(
        {"id": 1, "name": "totally-not-jellyfin", "kind": "jellyfin",
         "base_url": direct_app, "api_key": "k", "enabled": 1})
    assert result["repo"] == "jellyfin/jellyfin"


def test_a_direct_app_with_no_version_field_is_an_error_row(direct_app):
    _DirectApp.payloads = {"/System/Info": {}}
    result = version_checks.check_one(
        {"id": 1, "name": "J", "kind": "jellyfin", "base_url": direct_app,
         "api_key": "k", "enabled": 1})
    assert "No 'Version'" in result["error"]


def test_seerrs_preview_tags_would_not_be_mistaken_for_a_version():
    """Seerr carries tags like `preview-pgsql-starvation-fix` alongside its real
    releases. Anything unparseable sorts at the bottom, so it can never look newer than
    what's installed - and /releases/latest ignores tags with no release anyway."""
    assert version_checks.parse_version("preview-pgsql-starvation-fix") == (0,)
    assert version_checks._compare("3.4.1", "preview-pgsql-starvation-fix") == 1


def test_all_three_kinds_are_picked_up_for_checking(isolated_db):
    for kind in ("arr", "jellyfin", "jellyseerr"):
        db.create_integration({"name": kind, "kind": kind, "base_url": "http://x",
                                "api_key": "k", "enabled": 1})
    db.create_integration({"name": "tdarr", "kind": "tdarr", "base_url": "http://x",
                            "api_key": "", "enabled": 1})
    kinds = {i["kind"] for i in version_checks._checkable_integrations()}
    assert kinds == {"arr", "jellyfin", "jellyseerr"}
