"""Tests for the media-activity integrations: Radarr/Sonarr calendar, Jellyseerr
requests, qBittorrent downloads and Prowlarr indexer health.

No real Radarr, Sonarr, Jellyseerr, Prowlarr or qBittorrent exists in this sandbox, so
every fetcher is driven against a stand-in HTTP server built from each project's
documented response shape. That proves the parsing matches the documented shape and
that failures degrade the way they're meant to; it does not prove the shapes match a
given real instance. See docs/HISTORY.md.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

import pytest

import db
import integrations
import scheduler


# ---------------------------------------------------------------------------
# One configurable stand-in serving every endpoint these fetchers use
# ---------------------------------------------------------------------------
ROUTES = {}


class _Stub(BaseHTTPRequestHandler):
    def _respond(self, payload, status=200, raw=None):
        body = raw if raw is not None else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        handler = ROUTES.get(("GET", path))
        if handler is None:
            self.send_response(404)
            self.end_headers()
            return
        status, payload, raw = handler(parse_qs(urlparse(self.path).query))
        self._respond(payload, status, raw)

    def do_POST(self):
        path = urlparse(self.path).path
        handler = ROUTES.get(("POST", path))
        if handler is None:
            self.send_response(404)
            self.end_headers()
            return
        status, payload, raw = handler({})
        self._respond(payload, status, raw)

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


def route(method, path, payload=None, status=200, raw=None):
    ROUTES[(method, path)] = lambda query: (status, payload, raw)


# ---------------------------------------------------------------------------
# Radarr / Sonarr calendar
# ---------------------------------------------------------------------------
def test_calendar_reads_radarr_movies(isolated_db, stub):
    route("GET", "/api/v3/calendar", [
        {"title": "Dune: Part Three", "year": 2026, "hasFile": False,
         "digitalRelease": "2026-09-01T00:00:00Z", "inCinemas": "2026-07-01T00:00:00Z"},
    ])
    items = integrations.fetch_arr_calendar(stub, "key")
    assert items[0]["kind"] == "movie"
    assert items[0]["title"] == "Dune: Part Three"
    assert items[0]["date"].startswith("2026-09-01")


def test_calendar_reads_sonarr_episodes(isolated_db, stub):
    """Same endpoint, different shape. The app is identified from the *item* - a Sonarr
    entry carries a `series` object - so this needs no extra /system/status call, and
    can't get it wrong the way guessing from the integration's name could."""
    route("GET", "/api/v3/calendar", [
        {"seriesId": 1, "seasonNumber": 2, "episodeNumber": 5, "title": "The One With The Thing",
         "airDateUtc": "2026-09-03T20:00:00Z", "hasFile": False,
         "series": {"title": "Some Show"}},
    ])
    items = integrations.fetch_arr_calendar(stub, "key")
    assert items[0]["kind"] == "episode"
    assert items[0]["title"] == "Some Show"
    assert "S02E05" in items[0]["detail"]


def test_a_film_with_only_a_cinema_date_is_not_coming_to_the_server(isolated_db, stub):
    """inCinemas is deliberately ignored: this section answers "what's arriving here",
    and a cinema date says nothing about that."""
    route("GET", "/api/v3/calendar", [
        {"title": "Cinema Only", "year": 2026, "inCinemas": "2026-07-01T00:00:00Z"},
    ])
    assert integrations.fetch_arr_calendar(stub, "key") == []


def test_calendar_is_sorted_soonest_first(isolated_db, stub):
    route("GET", "/api/v3/calendar", [
        {"title": "Later", "year": 2026, "digitalRelease": "2026-12-01T00:00:00Z"},
        {"title": "Sooner", "year": 2026, "digitalRelease": "2026-09-01T00:00:00Z"},
    ])
    assert [i["title"] for i in integrations.fetch_arr_calendar(stub, "key")] == ["Sooner", "Later"]


# ---------------------------------------------------------------------------
# Jellyseerr / Overseerr requests
# ---------------------------------------------------------------------------
def test_requests_translate_numeric_statuses_into_words(stub):
    """The API speaks in integers; a status page has to speak in words."""
    route("GET", "/api/v1/request", {"results": [
        {"id": 7, "status": 1, "createdAt": "2026-08-01T10:00:00Z",
         "requestedBy": {"displayName": "Adam"},
         "media": {"mediaType": "movie", "status": 3, "title": "Some Film", "tmdbId": 42}},
    ]})
    items = integrations.fetch_seerr_requests(stub, "key")
    assert items[0]["request_status"] == "Pending approval"
    assert items[0]["media_status_label"] == "Processing"
    # The raw code is kept alongside the label, so request-progress tracking can
    # compare states without parsing English back into numbers.
    assert items[0]["media_status"] == 3
    assert items[0]["pending"] is True
    assert items[0]["requested_by"] == "Adam"


def test_a_request_with_no_title_field_falls_back_to_its_tmdb_id(stub):
    """Which title fields come back with the embedded media object varies by Overseerr
    version, so a missing one must render as something identifiable rather than a blank
    row."""
    route("GET", "/api/v1/request", {"results": [
        {"id": 8, "status": 2, "media": {"mediaType": "tv", "status": 5, "tmdbId": 1399}},
    ]})
    assert integrations.fetch_seerr_requests(stub, "key")[0]["title"] == "TMDB #1399"


def test_an_unexpected_requests_shape_raises_rather_than_half_parsing(stub):
    route("GET", "/api/v1/request", {"nope": True})
    with pytest.raises(ValueError):
        integrations.fetch_seerr_requests(stub, "key")


# ---------------------------------------------------------------------------
# qBittorrent
# ---------------------------------------------------------------------------
def _qbit_login_ok():
    route("POST", "/api/v2/auth/login", raw=b"Ok.")


def test_qbittorrent_reports_download_progress(stub):
    _qbit_login_ok()
    route("GET", "/api/v2/torrents/info", [
        {"name": "Some.Film.2026", "progress": 0.4237, "state": "downloading",
         "size": 8 * 1024 ** 3, "dlspeed": 5 * 1024 ** 2, "eta": 900},
    ])
    items = integrations.fetch_qbittorrent_downloads(stub, "admin", "pw")
    assert items[0]["progress"] == 42.4          # 0..1 converted for the template
    assert items[0]["size_gb"] == 8.0
    assert items[0]["dl_speed_mbs"] == 5.0
    assert items[0]["eta_seconds"] == 900


def test_qbittorrents_unknown_eta_sentinel_is_reported_as_unknown(stub):
    """qBittorrent uses 8640000 (100 days) to mean "no idea", which would otherwise
    render as a nonsense countdown."""
    _qbit_login_ok()
    route("GET", "/api/v2/torrents/info", [
        {"name": "Stalled", "progress": 0.1, "size": 1, "dlspeed": 0, "eta": 8640000},
    ])
    assert integrations.fetch_qbittorrent_downloads(stub, "admin", "pw")[0]["eta_seconds"] is None


def test_downloads_are_ordered_by_progress(stub):
    _qbit_login_ok()
    route("GET", "/api/v2/torrents/info", [
        {"name": "Just started", "progress": 0.05, "size": 1, "dlspeed": 0, "eta": 10},
        {"name": "Nearly there", "progress": 0.95, "size": 1, "dlspeed": 0, "eta": 10},
    ])
    names = [i["name"] for i in integrations.fetch_qbittorrent_downloads(stub, "admin", "pw")]
    assert names == ["Nearly there", "Just started"]


def test_a_refused_qbittorrent_login_is_reported_as_such(stub):
    route("POST", "/api/v2/auth/login", raw=b"Fails.")
    status = integrations.fetch_qbittorrent_status(stub, "admin", "wrong")
    assert status["reachable"] is False
    assert "Login refused" in status["error"]


def test_qbittorrent_health_reports_its_version(stub):
    _qbit_login_ok()
    route("GET", "/api/v2/app/version", raw=b"v4.6.5")
    status = integrations.fetch_qbittorrent_status(stub, "admin", "pw")
    assert status["reachable"] is True
    assert status["version"] == "v4.6.5"


def test_qbittorrent_without_a_username_skips_the_login(stub):
    """A local qBittorrent with "Bypass authentication for clients on localhost" needs
    no login at all, so a blank username is a valid configuration rather than an error.
    No /auth/login route is registered here, so this only passes if none is attempted."""
    route("GET", "/api/v2/app/version", raw=b"v4.6.5")
    assert integrations.fetch_qbittorrent_status(stub, "", "")["reachable"] is True


def test_qbittorrent_is_wired_into_the_shared_dispatch(stub):
    """It takes username/password rather than an api_key, so fetch_integration_status()
    has to route it differently - easy to add a fetcher and forget this."""
    _qbit_login_ok()
    route("GET", "/api/v2/app/version", raw=b"v5.0.0")
    result = integrations.fetch_integration_status(
        {"kind": "qbittorrent", "base_url": stub, "api_key": "", "username": "u", "password": "p"})
    assert result["version"] == "v5.0.0"


# ---------------------------------------------------------------------------
# Prowlarr indexers
# ---------------------------------------------------------------------------
def test_indexers_mark_the_failing_ones(stub):
    """"Prowlarr is up" hides the failure that actually happens: one or two indexers
    going stale or rate-limited while Prowlarr itself runs fine."""
    route("GET", "/api/v1/indexer", [
        {"id": 1, "name": "Healthy One", "enable": True},
        {"id": 2, "name": "Broken One", "enable": True},
    ])
    route("GET", "/api/v1/indexerstatus", [
        {"indexerId": 2, "disabledTill": "2026-08-22T00:00:00Z", "mostRecentFailure": "429 rate limited"},
    ])
    items = integrations.fetch_prowlarr_indexers(stub, "key")
    by_name = {i["name"]: i for i in items}
    assert by_name["Broken One"]["failing"] is True
    assert by_name["Healthy One"]["failing"] is False
    # Failing indexers sort first - they're the reason anyone looks at this list.
    assert items[0]["name"] == "Broken One"


def test_a_missing_indexerstatus_still_lists_the_indexers(stub):
    """The status call is a bonus, exactly like the *Arr health list: failing to fetch
    it must not fail the whole list, it just means no failure detail."""
    route("GET", "/api/v1/indexer", [{"id": 1, "name": "Only One", "enable": True}])
    items = integrations.fetch_prowlarr_indexers(stub, "key")
    assert len(items) == 1 and items[0]["failing"] is False


# ---------------------------------------------------------------------------
# The refresh task that assembles all of it
# ---------------------------------------------------------------------------
def test_task_skips_when_nothing_relevant_is_configured(isolated_db):
    with pytest.raises(scheduler.TaskSkipped):
        integrations.refresh_media_cache()


def test_one_broken_source_leaves_the_others_intact(isolated_db, stub):
    """The whole point of recording errors per source: an unreachable qBittorrent must
    not blank the calendar."""
    route("GET", "/api/v3/calendar", [
        {"title": "Still Works", "year": 2026, "digitalRelease": "2026-09-01T00:00:00Z"},
    ])
    db.create_integration({"name": "Radarr", "kind": "arr", "base_url": stub,
                            "api_key": "k", "enabled": 1})
    db.create_integration({"name": "qBit", "kind": "qbittorrent", "base_url": "http://127.0.0.1:1",
                            "username": "u", "password": "p", "enabled": 1})
    message = integrations.refresh_media_cache()
    cached = integrations.get_cached_media()
    assert [i["title"] for i in cached["calendar"]] == ["Still Works"]
    assert "downloads" in cached["errors"]
    assert "1 source(s) failed" in message


def test_an_app_without_a_calendar_is_not_reported_as_a_failure(isolated_db, stub):
    """Caught by live-testing: Prowlarr has no /api/v3/calendar and 404s it, which the
    first version recorded as "1 source(s) failed". A 404 here means "this app doesn't
    have that feature", which is not an error - Radarr and Sonarr 404 the indexer list
    for the same reason, in the other direction."""
    route("GET", "/api/v1/indexer", [{"id": 1, "name": "Alpha", "enable": True}])
    db.create_integration({"name": "Prowlarr", "kind": "arr", "base_url": stub,
                            "api_key": "k", "enabled": 1})
    message = integrations.refresh_media_cache()
    assert integrations.get_cached_media()["errors"] == {}
    assert "failed" not in message


def test_a_genuinely_broken_arr_app_still_reports_an_error(isolated_db, stub):
    """The flip side: swallowing every error to hide the 404s would mean a broken
    Prowlarr quietly showing an empty indexer list."""
    route("GET", "/api/v3/calendar", None, status=500)
    route("GET", "/api/v1/indexer", None, status=500)
    db.create_integration({"name": "Sick", "kind": "arr", "base_url": stub,
                            "api_key": "k", "enabled": 1})
    integrations.refresh_media_cache()
    assert integrations.get_cached_media()["errors"]


def test_the_cache_records_when_it_was_refreshed(isolated_db, stub):
    route("GET", "/api/v3/calendar", [])
    db.create_integration({"name": "Radarr", "kind": "arr", "base_url": stub,
                            "api_key": "k", "enabled": 1})
    integrations.refresh_media_cache()
    assert integrations.get_cached_media()["refreshed_at"] is not None


def test_clear_caches_empties_the_media_cache(isolated_db, stub):
    route("GET", "/api/v3/calendar", [
        {"title": "Gone After Clear", "year": 2026, "digitalRelease": "2026-09-01T00:00:00Z"},
    ])
    db.create_integration({"name": "Radarr", "kind": "arr", "base_url": stub,
                            "api_key": "k", "enabled": 1})
    integrations.refresh_media_cache()
    integrations.clear_caches()
    assert integrations.get_cached_media()["calendar"] == []


def test_get_cached_media_returns_copies(isolated_db):
    """A template iterating one of these lists must not be tripped by the refresh task
    replacing it mid-render."""
    first = integrations.get_cached_media()
    first["calendar"].append("mutated")
    assert integrations.get_cached_media()["calendar"] == []


# ---------------------------------------------------------------------------
# Who can actually see the section
# ---------------------------------------------------------------------------
import app as app_module  # noqa: E402


def _enable_media(keys=("show_public_calendar",)):
    for key in keys:
        db.set_setting(key, "1")
    integrations._media_cache["calendar"] = [
        {"kind": "movie", "title": "Visible Film", "detail": "2026",
         "date": "2026-09-01T00:00:00Z", "have": False}]
    integrations._media_cache["refreshed_at"] = db.now_iso()


def _sign_in(client, user_id="u1", name="adam"):
    with client.session_transaction() as sess:
        sess["portal_user"] = {"id": user_id, "name": name, "jellyfin_admin": False,
                               "authenticated_at": 0}


def test_the_media_page_is_off_by_default(client):
    """All four parts default to off. Unlike the resource cards, this says something
    about what's in (or heading into) the library and who asked for it, so it has to be
    opted into rather than appearing the moment an integration is configured."""
    integrations._media_cache["calendar"] = [
        {"kind": "movie", "title": "Should Not Appear", "detail": "", "date": "2026-09-01", "have": False}]
    assert client.get("/media").status_code == 404
    assert b"Should Not Appear" not in client.get("/").data


def test_the_media_page_renders_once_switched_on(client):
    _enable_media()
    assert b"Visible Film" in client.get("/media").data
    # The main page links to it and summarises it, rather than carrying the block.
    main = client.get("/").data
    assert b"Media activity" in main and b"/media" in main
    assert b"Visible Film" not in main


def test_only_the_switched_on_parts_render(client, isolated_db):
    """Four independent toggles, not one."""
    _enable_media(("show_public_calendar",))
    integrations._media_cache["downloads"] = [
        {"name": "Secret Download", "progress": 10.0, "state": "downloading",
         "size_gb": 1.0, "dl_speed_mbs": 0.0, "eta_seconds": None}]
    body = client.get("/media").data
    assert b"Visible Film" in body
    assert b"Secret Download" not in body


def test_the_section_is_hidden_from_anonymous_visitors_when_sign_in_is_enabled(client, monkeypatch):
    _enable_media()
    monkeypatch.setattr(app_module.jellyfin_auth, "is_enabled", lambda: True)
    db.set_setting("media_requires_login", "1")
    # 404 for a signed-out visitor, and not linked from the main page either.
    assert client.get("/media").status_code == 404
    assert b"/media" not in client.get("/").data


def test_a_signed_in_visitor_sees_it(client, monkeypatch):
    _enable_media()
    monkeypatch.setattr(app_module.jellyfin_auth, "is_enabled", lambda: True)
    db.set_setting("media_requires_login", "1")
    _sign_in(client)
    assert b"Visible Film" in client.get("/media").data


def test_requiring_login_does_nothing_when_sign_in_isnt_configured(client, monkeypatch):
    """Otherwise the default would hide the section from everybody on every install
    that never set Jellyfin sign-in up - restricted to nobody rather than restricted.
    Same shape as report_requires_login."""
    _enable_media()
    monkeypatch.setattr(app_module.jellyfin_auth, "is_enabled", lambda: False)
    db.set_setting("media_requires_login", "1")
    assert b"Visible Film" in client.get("/media").data


def test_the_section_can_be_made_public_while_sign_in_is_enabled(client, monkeypatch):
    _enable_media()
    monkeypatch.setattr(app_module.jellyfin_auth, "is_enabled", lambda: True)
    db.set_setting("media_requires_login", "0")
    assert b"Visible Film" in client.get("/media").data


def test_media_is_a_registered_public_page(isolated_db):
    """It moved off the main page into one of its own, so it belongs in PUBLIC_PAGES -
    which is what drives the route, the nav link and the summary alike."""
    assert "media" in [entry[0] for entry in app_module.PUBLIC_PAGES]
    assert "media" not in [key for key, _ in app_module.PUBLIC_SECTIONS]


# ---------------------------------------------------------------------------
# The "Coming soon" window, and the fixes to how requests render
# ---------------------------------------------------------------------------
def test_the_calendar_window_is_an_admin_setting(isolated_db, stub):
    """Without a window, a Sonarr tracking a long-running series returns everything it
    knows about, indefinitely."""
    assert integrations.calendar_days() == integrations.DEFAULT_CALENDAR_DAYS
    db.set_setting("media_calendar_days", "30")
    assert integrations.calendar_days() == 30


@pytest.mark.parametrize("stored, expected", [
    ("0", integrations.MIN_CALENDAR_DAYS),
    ("9999", integrations.MAX_CALENDAR_DAYS),
    ("not a number", integrations.DEFAULT_CALENDAR_DAYS),
])
def test_a_nonsense_calendar_window_is_clamped(isolated_db, stored, expected):
    db.set_setting("media_calendar_days", stored)
    assert integrations.calendar_days() == expected


def test_the_window_is_actually_sent_to_the_arr_app(isolated_db, stub, monkeypatch):
    seen = {}
    real_get = integrations.requests.get

    def spy(url, **kwargs):
        seen.update(kwargs.get("params") or {})
        return real_get(url, **kwargs)

    route("GET", "/api/v3/calendar", [])
    db.set_setting("media_calendar_days", "3")
    monkeypatch.setattr(integrations.requests, "get", spy)
    integrations.fetch_arr_calendar(stub, "key")
    from datetime import datetime, timezone
    start = datetime.fromisoformat(seen["start"]).date()
    end = datetime.fromisoformat(seen["end"]).date()
    assert (end - start).days == 3


def test_a_request_with_no_title_is_resolved_from_seerr(isolated_db, stub):
    """The reported bug: this list rendered "TMDB #438631" instead of a title, because
    Overseerr's request payload embeds media by id and frequently carries no name."""
    route("GET", "/api/v1/request", {"results": [
        {"id": 1, "status": 2, "media": {"mediaType": "movie", "status": 5, "tmdbId": 438631}},
    ]})
    route("GET", "/api/v1/movie/438631", {"title": "Dune", "releaseDate": "2021-09-15"})
    items = integrations.fetch_seerr_requests(stub, "key")
    assert items[0]["title"] == "Dune"
    assert items[0]["year"] == 2021


def test_a_resolved_title_is_only_fetched_once(isolated_db, stub):
    """Twenty pending requests must not mean twenty extra HTTP calls on every refresh,
    for answers keyed by an immutable TMDB id."""
    route("GET", "/api/v1/request", {"results": [
        {"id": 1, "status": 2, "media": {"mediaType": "movie", "status": 5, "tmdbId": 42}},
    ]})
    route("GET", "/api/v1/movie/42", {"title": "Cached Film", "releaseDate": "2020-01-01"})
    integrations.fetch_seerr_requests(stub, "key")
    ROUTES.pop(("GET", "/api/v1/movie/42"))     # a second lookup would now 404
    assert integrations.fetch_seerr_requests(stub, "key")[0]["title"] == "Cached Film"


def test_an_unresolvable_title_is_not_cached(isolated_db, stub):
    """A transient failure must not pin "TMDB #..." in place until the next restart."""
    route("GET", "/api/v1/request", {"results": [
        {"id": 1, "status": 2, "media": {"mediaType": "movie", "status": 5, "tmdbId": 77}},
    ]})
    assert integrations.fetch_seerr_requests(stub, "key")[0]["title"] == "TMDB #77"
    route("GET", "/api/v1/movie/77", {"title": "Now Available", "releaseDate": "2019-01-01"})
    assert integrations.fetch_seerr_requests(stub, "key")[0]["title"] == "Now Available"


def test_pending_requests_also_resolve_a_bare_tmdb_title(isolated_db, stub):
    """The reported bug, specifically in the pending list: fetch_seerr_pending() built
    its title with _seerr_title() alone, with no fallback for a bare "TMDB #12345"
    placeholder - unlike fetch_seerr_requests(), which already resolved it. That's what
    made the Discord approval DM (built from this list via seerr_alerts.format_alert())
    read "TMDB #11279 (tv) for Pakuo" instead of a real title."""
    route("GET", "/api/v1/request", {
        "pageInfo": {"results": 1},
        "results": [{"id": 9, "media": {"mediaType": "tv", "tmdbId": 11279},
                     "requestedBy": {"displayName": "Pakuo"}}],
    })
    route("GET", "/api/v1/tv/11279", {"name": "Attack on Titan", "firstAirDate": "2013-04-07"})
    items, total = integrations.fetch_seerr_pending(stub, "key")
    assert items[0]["title"] == "Attack on Titan"
    assert total == 1


def test_every_status_has_a_colour_key(isolated_db, stub):
    """The badges are coloured from a stable key, not by matching the English label, so
    every code in both maps needs one - a missing key renders as the grey "unknown"
    pill and silently loses the distinction the colours exist for."""
    assert set(integrations.SEERR_REQUEST_STATUS) == set(integrations.SEERR_REQUEST_STATUS_KEY)
    assert set(integrations.SEERR_MEDIA_STATUS) == set(integrations.SEERR_MEDIA_STATUS_KEY)


def test_an_unrecognised_status_falls_back_to_unknown(isolated_db, stub):
    route("GET", "/api/v1/request", {"results": [
        {"id": 1, "status": 99, "media": {"mediaType": "movie", "status": 99, "title": "X"}},
    ]})
    item = integrations.fetch_seerr_requests(stub, "key")[0]
    assert item["request_status_key"] == "unknown"
    assert item["media_status_key"] == "unknown"
