"""Tests for the floating admin search (admin_search.py, /admin/search).

The property that matters most here is that **the index cannot drift**. It is derived
from the admin templates rather than hand-written, precisely because this project's most
repeated failure mode is a list that has to be updated alongside something else and
isn't - and a missing search entry looks exactly like a setting that doesn't exist,
which is worse than having no search at all. So the tests below check the derivation,
not a snapshot of its output.
"""
import glob
import os

import pytest

import admin_search
import app as app_module


# ---------------------------------------------------------------------------
# The index, and the one hand-maintained list behind it
# ---------------------------------------------------------------------------
def test_every_admin_template_is_searchable():
    """PAGES is the only hand-maintained part of this feature, so it is the only part
    that can go stale. A new admin page that nobody registers would be invisible to the
    search while looking perfectly fine everywhere else."""
    on_disk = {os.path.basename(p) for p in glob.glob(os.path.join(admin_search.TEMPLATE_DIR,
                                                                   "admin_*.html"))}
    registered = {filename for filename, _endpoint, _label in admin_search.PAGES}
    missing = sorted(on_disk - registered - admin_search.NON_PAGE_TEMPLATES)
    assert not missing, (
        f"these admin templates are not searchable: {missing}. Add each to "
        f"admin_search.PAGES with its endpoint and the label the nav uses, or to "
        f"NON_PAGE_TEMPLATES if it isn't a page of its own.")


def test_every_registered_endpoint_actually_resolves(client, isolated_db):
    """A typo'd endpoint name would only surface as a BuildError the moment somebody
    searched - i.e. in front of the user, not here."""
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    with app_module.app.test_request_context():
        for _filename, endpoint, label in admin_search.PAGES:
            assert app_module.url_for(endpoint), f"{label} ({endpoint}) does not resolve"


def test_the_index_finds_settings_by_label_hint_and_field_name():
    """Three different ways an admin might name the same thing, all of which have to
    work: what the label says, a word that only appears in the explanatory hint, and the
    underlying field name as copied out of a log line or .env.example."""
    def titles(q):
        return [r["title"] for r in admin_search.search(q)]

    assert any("Low disk space" in t for t in titles("low disk"))          # label
    assert any("Jellyfin running background" in t for t in titles("trickplay"))  # hint only
    assert any("GPU" in t for t in titles("show_public_gpu"))              # field name


def test_a_page_is_found_by_the_name_people_actually_type():
    """"2fa" appears nowhere in the Two-factor auth page's own label, but it is what
    somebody searches. Without the alias boost the four step-up code prompts scattered
    around the panel outranked the page itself."""
    first = admin_search.search("2fa")[0]
    assert first["page"].startswith("Two-factor auth")
    assert admin_search.search("seerr")[0]["page"] == "Seerr approvals"


def test_results_span_more_than_one_page():
    """The whole point of the change: it searches the panel, not the page you're on."""
    pages = {r["page"] for r in admin_search.search("password")}
    assert len(pages) > 1, pages


def test_every_term_has_to_match():
    """AND, not OR - "disk alert" should narrow rather than widen."""
    both = admin_search.search("low disk alert")
    assert both and all("disk" in r["haystack"] and "alert" in r["haystack"] for r in both)


@pytest.mark.parametrize("query", ["", "  ", "a"])
def test_a_query_too_short_to_mean_anything_matches_nothing(query):
    """Every entry would match, which is a list, not an answer."""
    assert admin_search.search(query) == []


def test_the_index_is_rebuilt_when_a_template_changes(tmp_path, monkeypatch):
    """Cached on the templates' mtimes rather than forever, so editing a label during
    development doesn't need a restart to show up - the same approach asset_url() uses."""
    admin_search.clear_caches()
    first = admin_search.build_index()
    assert admin_search.build_index() is first          # cached, same object

    monkeypatch.setattr(admin_search, "_template_stamp", lambda: ("changed",))
    assert admin_search.build_index() is not first      # rebuilt


def test_the_index_never_renders_a_page_to_build_itself():
    """Rendering admin pages to index them is the obvious shortcut and is wrong:
    /admin/reports marks messages as read and /admin/logs reads files off disk, so a
    search box would have side effects. Templates are inert; this pins that."""
    source = open(os.path.join(os.path.dirname(admin_search.TEMPLATE_DIR), "admin_search.py"),
                  encoding="utf-8").read()
    for forbidden in ("render_template", "test_client", "import app", "import db"):
        assert forbidden not in source, f"admin_search.py must not use {forbidden}"


# ---------------------------------------------------------------------------
# The route and the fragment
# ---------------------------------------------------------------------------
def test_search_requires_a_login(client, isolated_db):
    """It describes the whole admin panel, including which integrations exist."""
    resp = client.get("/admin/search?q=disk")
    assert resp.status_code in (301, 302)
    assert "/admin/login" in resp.headers["Location"]


def test_search_returns_an_html_fragment_not_json(client, isolated_db):
    """Same convention as /api/incidents/more and /admin/logs/tail: this app has one
    JSON API (/api/status, for external consumers) and no client-side templating."""
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    body = client.get("/admin/search?q=kiosk").get_data(as_text=True)

    assert "admin-search__result" in body
    assert "Kiosk" in body
    assert not body.strip().startswith("{")
    assert "<html" not in body.lower()          # a fragment, not a page


def test_a_result_links_to_the_page_and_names_the_control_to_jump_to(client, isolated_db):
    """`jump` is the input's *name*, so arriving can find the control with
    [name=...] - rather than several hundred fields across twenty templates each
    needing an id added just to be linkable."""
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    body = client.get("/admin/search?q=low+disk").get_data(as_text=True)

    assert "/admin/settings?jump=lowdisk_percent_threshold" in body


def test_an_empty_query_explains_itself_rather_than_listing_everything(client, isolated_db):
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    body = client.get("/admin/search?q=").get_data(as_text=True)

    assert "admin-search__result" not in body
    assert "Search every page" in body


def test_no_matches_says_so(client, isolated_db):
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    body = client.get("/admin/search?q=zzzznotathing").get_data(as_text=True)

    assert "Nothing matches" in body


def test_result_text_is_escaped(client, isolated_db):
    """Everything indexed comes from this project's own templates, so this is defence
    in depth rather than a live threat - but the query is echoed back, and that is
    user input."""
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    body = client.get("/admin/search?q=<script>alert(1)</script>").get_data(as_text=True)

    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


# ---------------------------------------------------------------------------
# The bubble itself
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", ["/admin/settings", "/admin/services", "/admin/logs",
                                   "/admin/about", "/admin/tasks"])
def test_the_search_bubble_is_on_every_admin_page(client, isolated_db, path):
    """It lives in admin_base.html rather than per-template, for the same reason
    local_time.js does: wiring it per page is how one page ends up without it."""
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    body = client.get(path).get_data(as_text=True)

    assert 'id="admin_search"' in body
    assert "admin_search.js" in body


def test_the_bubble_is_hidden_until_javascript_reveals_it(client, isolated_db):
    """Same rule as the log page's Live switch: a search box that can't search is worse
    than no box at all."""
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    body = client.get("/admin/settings").get_data(as_text=True)

    assert "hidden" in body.split('id="admin_search"')[1][:40]
