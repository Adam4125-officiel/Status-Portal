"""Tests for unified search across Jellyfin and Seerr, and requesting what's missing.

Search is the one place this app makes a live outbound call from a request handler, so
the tests lean on the things that make that acceptable: a short timeout, independent
per-source degradation, sign-in, and a per-session rate limit.

No real Jellyfin or Seerr here - both are driven against stand-in servers built from
their documented response shapes.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

import pytest

import app as app_module
import config
import db
import integrations
import media_search


ROUTES = {}


class _Stub(BaseHTTPRequestHandler):
    def _respond(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self):
        path = urlparse(self.path).path
        entry = ROUTES.get(path)
        if entry is None:
            self.send_response(404)
            self.end_headers()
            return
        status, payload = entry
        self._respond(payload, status)

    do_GET = _handle
    do_POST = _handle

    def log_message(self, *args):
        pass


@pytest.fixture
def stub():
    ROUTES.clear()
    server = HTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    ROUTES.clear()


def route(path, payload, status=200):
    ROUTES[path] = (status, payload)


# ---------------------------------------------------------------------------
# The two searches
# ---------------------------------------------------------------------------
def test_jellyfin_search_returns_library_items(stub):
    route("/Items", {"Items": [
        {"Id": "abc", "Name": "Dune", "Type": "Movie", "ProductionYear": 2021},
        {"Id": "def", "Name": "Some Show", "Type": "Series", "ProductionYear": 2020},
    ]})
    items = integrations.search_jellyfin(stub, "key", "dune")
    assert items[0]["in_library"] is True
    assert items[0]["jellyfin_id"] == "abc"
    assert items[1]["media_type"] == "tv"


def test_seerr_search_skips_results_that_cannot_be_requested(stub):
    """Seerr's search returns people as well as media; a person has no request action
    and would render as a row that does nothing."""
    route("/api/v1/search", {"results": [
        {"id": 1, "mediaType": "person", "name": "Denis Villeneuve"},
        {"id": 438631, "mediaType": "movie", "title": "Dune", "releaseDate": "2021-09-15",
         "overview": "A noble family...", "mediaInfo": {"status": 5}},
    ]})
    items = integrations.search_seerr(stub, "key", "dune")
    assert len(items) == 1
    assert items[0]["tmdb_id"] == 438631
    assert items[0]["in_library"] is True     # Seerr says status 5 = available
    assert items[0]["year"] == 2021


def test_search_uses_its_own_short_timeout(stub, monkeypatch):
    """The whole safety story for the one live outbound call in a request handler: a
    slow Jellyfin must not hold a request thread for the 5-30s the other timeouts allow."""
    seen = {}
    real_get = integrations.requests.get

    def spy(*args, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        return real_get(*args, **kwargs)

    route("/Items", {"Items": []})
    monkeypatch.setattr(integrations.requests, "get", spy)
    integrations.search_jellyfin(stub, "key", "x")
    assert seen["timeout"] == config.SEARCH_TIMEOUT_SECONDS
    assert config.SEARCH_TIMEOUT_SECONDS < integrations.TIMEOUT + 5


# ---------------------------------------------------------------------------
# Merging
# ---------------------------------------------------------------------------
def _jf(title, year=2021, item_id="abc"):
    return {"source": "jellyfin", "title": title, "year": year, "media_type": "movie",
            "jellyfin_id": item_id, "in_library": True}


def _sr(title, year=2021, tmdb_id=1, in_library=False, requested=False):
    return {"source": "seerr", "title": title, "year": year, "media_type": "movie",
            "tmdb_id": tmdb_id, "overview": "o", "poster_path": "",
            "in_library": in_library, "requested": requested}


def test_the_same_title_from_both_sources_appears_once(stub):
    """The dedupe step. Without it every film you already have shows up twice."""
    merged = media_search.merge([_jf("Dune")], [_sr("dune")])
    assert len(merged) == 1
    assert merged[0]["source"] == "both"


def test_a_merged_result_keeps_both_ids(stub):
    """Jellyfin's id is what makes "Watch now" possible; Seerr's TMDB id is what makes
    a request possible. Losing either would silently disable one of the two actions."""
    merged = media_search.merge([_jf("Dune", item_id="jf-1")], [_sr("dune", tmdb_id=438631)])
    assert merged[0]["jellyfin_id"] == "jf-1"
    assert merged[0]["tmdb_id"] == 438631


def test_a_remake_is_not_merged_with_the_original():
    """Title alone isn't identity - merging a 1984 film into its 2021 remake would be
    worse than showing both."""
    merged = media_search.merge([_jf("Dune", year=1984)], [_sr("Dune", year=2021)])
    assert len(merged) == 2


def test_jellyfin_wins_on_whether_something_is_actually_present():
    """Jellyfin having the file is proof; Seerr's idea of availability is an opinion."""
    merged = media_search.merge([_jf("Dune")], [_sr("dune", in_library=False)])
    assert merged[0]["in_library"] is True


def test_in_library_results_sort_first():
    merged = media_search.merge([_jf("Zebra")], [_sr("Aardvark", tmdb_id=2)])
    assert [i["title"] for i in merged] == ["Zebra", "Aardvark"]


# ---------------------------------------------------------------------------
# Degradation - each source fails on its own
# ---------------------------------------------------------------------------
def _configure(stub_url, jellyfin=True, seerr=True):
    if jellyfin:
        db.create_integration({"name": "Jellyfin", "kind": "jellyfin", "base_url": stub_url,
                                "api_key": "k", "enabled": 1})
    if seerr:
        db.create_integration({"name": "Seerr", "kind": "jellyseerr", "base_url": stub_url,
                                "api_key": "k", "enabled": 1})


def test_one_dead_source_still_returns_the_others_results(isolated_db, stub):
    _configure(stub)
    route("/Items", {"Items": [{"Id": "abc", "Name": "Dune", "Type": "Movie",
                                 "ProductionYear": 2021}]})
    # No /api/v1/search route registered, so Seerr 404s.
    outcome = media_search.search("dune")
    assert [i["title"] for i in outcome["results"]] == ["Dune"]
    assert "Seerr" in outcome["errors"]
    assert outcome["available"] is True


def test_both_sources_down_reports_unavailable_rather_than_empty(isolated_db, stub):
    """"Search is unavailable right now" and "we don't have that" must not look the
    same - one is a system problem and the other is an answer."""
    _configure("http://127.0.0.1:1")
    outcome = media_search.search("dune")
    assert outcome["available"] is False
    assert outcome["results"] == []


def test_an_empty_query_searches_nothing(isolated_db, stub):
    _configure(stub)
    route("/Items", {"Items": [{"Id": "x", "Name": "Nope", "Type": "Movie"}]})
    assert media_search.search("   ")["results"] == []


# ---------------------------------------------------------------------------
# Requesting
# ---------------------------------------------------------------------------
def test_a_request_is_attributed_to_the_linked_seerr_user(isolated_db, stub, monkeypatch):
    """The reason to follow the link at all: Seerr's approval queue should say who
    actually asked, not just name whoever owns the API key."""
    _configure(stub, jellyfin=False)
    db.set_user_preferences("u1", seerr_user_id="7")
    sent = {}
    monkeypatch.setattr(integrations, "request_via_seerr",
                        lambda url, key, mt, tid, uid=None: sent.update(
                            {"media_type": mt, "tmdb_id": tid, "user": uid}))
    ok, message = media_search.request("movie", 438631, "u1", "adam")
    assert ok is True
    assert sent == {"media_type": "movie", "tmdb_id": 438631, "user": "7"}


def test_an_unlinked_user_still_gets_their_request_but_is_told_it_is_unattributed(
        isolated_db, stub, monkeypatch):
    """Never guess the link from a matching name or email: attributing one person's
    request to another is far worse than an unattributed request."""
    _configure(stub, jellyfin=False)
    route("/api/v1/user", {"results": [
        {"id": 9, "displayName": "adam", "email": "adam@example.invalid",
         "jellyfinUserId": "", "settings": {}},
    ]})
    sent = {}
    monkeypatch.setattr(integrations, "request_via_seerr",
                        lambda url, key, mt, tid, uid=None: sent.update({"user": uid}))
    ok, message = media_search.request("movie", 1, "u1", "adam")
    assert ok is True
    assert sent["user"] is None
    assert "without your name" in message


def test_requesting_something_already_requested_is_a_plain_message(isolated_db, stub, monkeypatch):
    """Pressing the button twice is ordinary behaviour, not an error worth alarming
    anyone with."""
    _configure(stub, jellyfin=False)

    class _Response:
        status_code = 409

    def conflict(*a, **k):
        raise integrations.requests.HTTPError(response=_Response())

    monkeypatch.setattr(integrations, "request_via_seerr", conflict)
    ok, message = media_search.request("movie", 1, "u1")
    assert ok is False
    assert "already been requested" in message


def test_an_unreachable_seerr_is_a_try_again_message(isolated_db, stub, monkeypatch):
    _configure(stub, jellyfin=False)

    def boom(*a, **k):
        raise integrations.requests.RequestException("down")

    monkeypatch.setattr(integrations, "request_via_seerr", boom)
    ok, message = media_search.request("movie", 1, "u1")
    assert ok is False and "try again" in message


@pytest.mark.parametrize("media_type, tmdb_id", [("person", 1), ("movie", "not-a-number")])
def test_a_nonsense_request_is_refused_before_reaching_seerr(isolated_db, stub, monkeypatch,
                                                              media_type, tmdb_id):
    _configure(stub, jellyfin=False)
    monkeypatch.setattr(integrations, "request_via_seerr",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not be called")))
    ok, _ = media_search.request(media_type, tmdb_id, "u1")
    assert ok is False


def test_a_series_request_asks_for_every_season(isolated_db, stub, monkeypatch):
    """Seerr rejects a TV request with no season selection, and this portal isn't going
    to render a season picker."""
    _configure(stub, jellyfin=False)
    sent = {}
    monkeypatch.setattr(integrations.requests, "post",
                        lambda url, headers=None, json=None, timeout=None:
                        sent.update(json or {}) or type("R", (), {
                            "raise_for_status": lambda self: None, "content": b"",
                        })())
    integrations.request_via_seerr(stub, "k", "tv", 1399)
    assert sent["seasons"] == "all"


# ---------------------------------------------------------------------------
# Access control and rate limiting
# ---------------------------------------------------------------------------
@pytest.fixture
def visitor(client, monkeypatch):
    db.create_integration({"name": "Jellyfin", "kind": "jellyfin",
                            "base_url": "http://jellyfin.invalid", "api_key": "k", "enabled": 1})
    db.set_setting("jellyfin_auth_enabled", "1")
    db.replace_jellyfin_users([{"id": "u1", "name": "adam"}])
    monkeypatch.setattr(app_module.jellyfin_auth, "authenticate",
                        lambda u, p: {"ok": True, "user": {"id": "u1", "name": "adam",
                                                            "is_administrator": False,
                                                            "is_disabled": False}})
    client.post("/login", data={"username": "adam", "password": "pw"})
    return client


def test_search_requires_a_signed_in_visitor(client):
    """The result set reveals the whole library, and an open search box wired to two
    external APIs is a denial-of-service amplifier."""
    assert client.get("/search").status_code == 302
    assert client.post("/search/request", data={}).status_code == 302


def test_a_signed_in_visitor_can_reach_search(visitor):
    assert visitor.get("/search").status_code == 200


def test_search_is_not_linked_for_signed_out_visitors(client, isolated_db):
    db.create_integration({"name": "Jellyfin", "kind": "jellyfin", "base_url": "http://x",
                            "api_key": "k", "enabled": 1})
    assert b'href="/search"' not in client.get("/").data


def test_search_is_linked_in_the_nav_for_a_signed_in_visitor(visitor):
    """The box moved out of the main page's scroll into the shared page nav, alongside
    the other pages - /search was always its own route."""
    assert b'href="/search"' in visitor.get("/").data


def test_searching_is_rate_limited_per_session(visitor, monkeypatch):
    """Per session rather than process-global, unlike the login and report limiters:
    this route is already behind a sign-in, so the meaningful unit is "this person" -
    and a global counter would let one enthusiastic searcher lock everybody out."""
    monkeypatch.setattr(config, "SEARCH_RATE_LIMIT", 2)
    monkeypatch.setattr(media_search, "search",
                        lambda q, jellyfin_user_id=None: {"results": [], "errors": {},
                                                           "available": True})
    for _ in range(2):
        assert b"lot of searching" not in visitor.get("/search?q=dune").data
    assert b"lot of searching" in visitor.get("/search?q=dune").data


def test_an_empty_query_does_not_count_against_the_limit(visitor, monkeypatch):
    """Otherwise loading the page repeatedly would exhaust someone's allowance without
    them having searched for anything."""
    monkeypatch.setattr(config, "SEARCH_RATE_LIMIT", 1)
    for _ in range(5):
        visitor.get("/search")
    assert b"lot of searching" not in visitor.get("/search?q=dune").data


def test_the_request_route_is_csrf_protected():
    """It's an authenticated write against another service, so it belongs in the
    protected set - a new public POST route is a deliberate decision, not a default."""
    assert app_module._csrf_required_for("/search/request", "POST") is True


# ---------------------------------------------------------------------------
# Why search can report Seerr down while the Integrations page says it's up
# ---------------------------------------------------------------------------
# A real report. The two facts come from genuinely different calls: /api/v1/status is
# served by Seerr itself, while /api/v1/search makes Seerr go out to TMDB. The timeouts
# are *not* the difference - search already gets the longer of the two - so the fix was
# to build something that measures both rather than widening anything.
def test_the_search_timeout_is_not_shorter_than_the_health_checks():
    """Pinning the thing that would otherwise be the obvious wrong guess."""
    assert config.SEARCH_TIMEOUT_SECONDS >= integrations.TIMEOUT


@pytest.mark.parametrize("exc, expected", [
    (integrations.requests.Timeout(), "timed out"),
    (integrations.requests.ConnectionError(), "couldn't be connected to"),
    (ValueError(), "answered with something that isn't JSON"),
])
def test_failures_are_described_in_words(exc, expected):
    """"Couldn't be reached" reads the same whether the server refused, hung, or
    answered 500 - and those have completely different fixes."""
    assert integrations.describe_request_error(exc) == expected


def test_an_http_error_names_its_status():
    class _R:
        status_code = 500
    assert "500" in integrations.describe_request_error(
        integrations.requests.HTTPError(response=_R()))


def test_a_rejected_api_key_is_called_out_specifically():
    class _R:
        status_code = 403
    assert "API key" in integrations.describe_request_error(
        integrations.requests.HTTPError(response=_R()))


def test_the_diagnosis_separates_a_healthy_seerr_from_a_broken_search(isolated_db, stub):
    """The exact reported symptom: status answers, search doesn't."""
    route("/api/v1/status", {"version": "2.1.0"})
    route("/api/v1/search", {"message": "TMDB unreachable"}, status=500)
    report = integrations.diagnose_seerr(stub, "k")
    healthy, searchable = report["checks"]
    assert healthy["ok"] is True
    assert searchable["ok"] is False and "500" in searchable["error"]
    assert "outbound internet" in report["verdict"]


def test_the_diagnosis_points_at_caching_when_both_calls_work(isolated_db, stub):
    """If both succeed now, the likely explanation is that the Integrations page was
    showing a cached result - it refreshes on the health-check interval, so the two were
    never looking at the same moment."""
    route("/api/v1/status", {"version": "2.1.0"})
    route("/api/v1/search", {"results": []})
    assert "cached" in integrations.diagnose_seerr(stub, "k")["verdict"]


def test_the_search_page_says_how_a_source_failed_not_just_that_it_did(visitor, monkeypatch):
    monkeypatch.setattr(media_search, "search",
                        lambda q, jellyfin_user_id=None: {
                            "results": [], "errors": {"Seerr": "timed out"}, "available": True})
    body = visitor.get("/search?q=dune").data
    assert b"Seerr timed out" in body


def test_the_diagnose_button_only_appears_for_seerr(client, isolated_db):
    _admin = client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    db.create_integration({"name": "Radarr", "kind": "arr", "base_url": "http://x",
                            "api_key": "k", "enabled": 1})
    assert b"Diagnose search" not in client.get("/admin/integrations").data
    db.create_integration({"name": "Seerr", "kind": "jellyseerr", "base_url": "http://x",
                            "api_key": "k", "enabled": 1})
    assert b"Diagnose search" in client.get("/admin/integrations").data


def test_diagnosing_a_non_seerr_integration_is_refused(client, isolated_db):
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    iid = db.create_integration({"name": "Radarr", "kind": "arr", "base_url": "http://x",
                                  "api_key": "k", "enabled": 1})
    resp = client.post(f"/admin/integrations/{iid}/diagnose", follow_redirects=True)
    assert b"only apply to a Jellyseerr" in resp.data
