"""Kiosk mode: the /kiosk display, its fragment endpoint, and the settings behind it.

The rotation itself is browser behaviour and isn't testable here - what these cover is
everything the server decides: whether the page exists at all, which views it hands
over, and that a view can't reach the wall display past a public-visibility setting.
"""
import re

import pytest

import app as app_module
import db


def _enable(views=None, rotation=None):
    db.set_setting("kiosk_enabled", "1")
    if views is not None:
        db.set_setting("kiosk_views", ",".join(views))
    if rotation is not None:
        db.set_setting("kiosk_rotation_seconds", str(rotation))


def _views_on_screen(client):
    """The view keys /kiosk/views actually rendered, in order."""
    body = client.get("/kiosk/views").data.decode()
    return re.findall(r'data-view="([a-z]+)"', body)


# ---------------------------------------------------------------------------
# The master switch
# ---------------------------------------------------------------------------
def test_kiosk_is_off_by_default(client):
    """Enabling kiosk publishes a full-screen page on a portal that may be reachable
    from anywhere, so it has to be a deliberate choice - the same off-by-default rule
    every other opt-in public surface here follows."""
    assert app_module.kiosk_enabled() is False
    assert client.get("/kiosk").status_code == 404


@pytest.mark.parametrize("path", ["/kiosk", "/kiosk/views"])
def test_disabled_kiosk_404s_rather_than_redirecting_or_rendering_empty(client, path):
    """Both routes, not just the page: an unprotected fragment endpoint would keep
    serving the display's contents to anyone who kept polling it after the admin
    switched kiosk mode off.

    404 rather than an empty page for the same reason a switched-off public sub-page
    does it - an empty page confirms the feature is there and merely hidden."""
    db.set_setting("kiosk_enabled", "0")
    assert client.get(path).status_code == 404


def test_enabling_makes_both_routes_available(client):
    _enable()
    assert client.get("/kiosk").status_code == 200
    assert client.get("/kiosk/views").status_code == 200


def test_the_public_page_only_offers_the_link_when_kiosk_is_on(client):
    """The other half of the switch: a link to a route that 404s is worse than no
    link at all."""
    assert b'href="/kiosk"' not in client.get("/").data
    _enable()
    assert b'href="/kiosk"' in client.get("/").data


# ---------------------------------------------------------------------------
# Which views are shown
# ---------------------------------------------------------------------------
def test_only_ticked_views_are_rendered(client):
    _enable(views=["services"])
    assert _views_on_screen(client) == ["services"]


def test_views_render_in_the_canonical_order_not_the_stored_one(client):
    """The checkboxes say which views, not in what sequence - so a stored value in a
    different order must not silently become a reordering UI nobody built."""
    _enable(views=["incidents", "services"])
    assert _views_on_screen(client) == ["services", "incidents"]


def test_an_unknown_stored_view_key_is_ignored(client):
    """A key left behind by a removed view must not reach render_template() and 500
    the display with a TemplateNotFound."""
    _enable(views=["services", "somethingelse"])
    assert _views_on_screen(client) == ["services"]
    assert client.get("/kiosk").status_code == 200


def test_a_view_with_nothing_to_show_is_skipped(client):
    """Rotating onto a blank screen for twenty seconds is worse than having one view
    fewer - which is why the builders return None rather than an empty page the way
    /activity's deliberately does."""
    _enable(views=["services", "announcements"])
    assert _views_on_screen(client) == ["services"]

    db.create_announcement({"title": "Disk swap Sunday", "message": "Back by noon."})
    assert _views_on_screen(client) == ["services", "announcements"]


# ---------------------------------------------------------------------------
# Kiosk must not become a way around a public-visibility setting
# ---------------------------------------------------------------------------
def test_vms_view_needs_the_public_vm_setting_too(client, monkeypatch):
    """Ticking "Virtual machines" here must not publish VM names on a wall display
    when the admin has switched them off for /vms. Kiosk shows nothing a visitor
    could not already see on the public pages."""
    monkeypatch.setattr(app_module.monitoring, "get_cached_vm_snapshot",
                        lambda: [{"name": "WIN-MEDIA", "state": "Running", "uptime": "1d"}])
    _enable(views=["services", "vms"])

    db.set_setting("show_public_vms", "0")
    assert "vms" not in _views_on_screen(client)
    assert b"WIN-MEDIA" not in client.get("/kiosk/views").data

    db.set_setting("show_public_vms", "1")
    assert "vms" in _views_on_screen(client)
    assert b"WIN-MEDIA" in client.get("/kiosk/views").data


def test_resources_view_needs_a_public_resource_setting_too(client):
    _enable(views=["services", "resources"])
    for key in app_module._PUBLIC_RESOURCE_KEYS:
        db.set_setting(key, "0")
    assert "resources" not in _views_on_screen(client)

    db.set_setting("show_public_cpu", "1")
    assert "resources" in _views_on_screen(client)


# ---------------------------------------------------------------------------
# Nothing to show at all
# ---------------------------------------------------------------------------
def test_unticking_every_view_falls_back_to_services(client):
    """A display bolted to a wall must not be able to go blank because of a settings
    mistake - and "" here is an admin who unticked everything, which has to be told
    apart from a setting that was never saved."""
    _enable(views=[])
    assert db.get_setting("kiosk_views") == ""
    assert _views_on_screen(client) == ["services"]
    assert client.get("/kiosk").status_code == 200


def test_a_never_saved_setting_uses_the_defaults(client):
    """Distinct from the empty string above: an install that has never opened the
    kiosk settings gets Services and Incidents, not the fallback."""
    db.set_setting("kiosk_enabled", "1")
    assert db.get_setting("kiosk_views") is None
    assert _views_on_screen(client) == ["services", "incidents"]


def test_a_view_that_raises_is_skipped_rather_than_500ing_the_display(client, monkeypatch):
    """One broken view must not take the whole display down - the same reasoning as
    the public pages' summary guard."""
    def boom():
        raise RuntimeError("VM query exploded")
    monkeypatch.setattr(app_module, "_kiosk_vms_context", boom)
    monkeypatch.setattr(app_module, "KIOSK_VIEWS", [
        ("services", "Services", lambda: True, app_module._kiosk_services_context),
        ("vms", "Virtual machines", lambda: True, boom),
    ])
    _enable(views=["services", "vms"])
    assert client.get("/kiosk").status_code == 200
    assert _views_on_screen(client) == ["services"]


# ---------------------------------------------------------------------------
# Rotation interval
# ---------------------------------------------------------------------------
def test_rotation_interval_is_clamped_both_ways(client):
    """Below the floor a view is gone before it can be read from across a room; above
    the ceiling the display has effectively stopped rotating."""
    _enable(rotation=1)
    assert app_module._kiosk_rotation_seconds() == app_module.KIOSK_MIN_ROTATION_SECONDS
    _enable(rotation=99999)
    assert app_module._kiosk_rotation_seconds() == app_module.KIOSK_MAX_ROTATION_SECONDS


def test_a_non_numeric_rotation_falls_back_to_the_default(client):
    _enable()
    db.set_setting("kiosk_rotation_seconds", "soon")
    assert app_module._kiosk_rotation_seconds() == app_module.KIOSK_DEFAULT_ROTATION_SECONDS


def test_the_fragment_carries_the_rotation_interval(client):
    """A display mounted on a wall shouldn't need reloading by hand after a settings
    change, so the interval rides along on every poll."""
    _enable(rotation=45)
    assert b'data-rotation-seconds="45"' in client.get("/kiosk/views").data


# ---------------------------------------------------------------------------
# The admin settings form
# ---------------------------------------------------------------------------
def _login(client):
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})


def test_settings_form_saves_every_kiosk_field(client):
    _login(client)
    client.post("/admin/settings/general", data={
        "site_name": "Home", "kiosk_enabled": "on", "kiosk_rotation_seconds": "25",
        "kiosk_views": ["services", "vms"]})
    assert db.get_setting("kiosk_enabled") == "1"
    assert db.get_setting("kiosk_rotation_seconds") == "25"
    assert db.get_setting("kiosk_views") == "services,vms"


def test_saving_with_the_box_unchecked_turns_kiosk_off(client):
    _login(client)
    db.set_setting("kiosk_enabled", "1")
    client.post("/admin/settings/general", data={"site_name": "Home"})
    assert db.get_setting("kiosk_enabled") == "0"
    assert client.get("/kiosk").status_code == 404


def test_an_unknown_view_key_is_never_stored(client):
    """Whitelisted against the declared views, so a hand-crafted POST can't put a key
    into the setting that later reaches render_template()."""
    _login(client)
    client.post("/admin/settings/general", data={
        "site_name": "Home", "kiosk_views": ["services", "../../etc/passwd", "nope"]})
    assert db.get_setting("kiosk_views") == "services"


def test_the_settings_page_flags_a_view_its_visibility_setting_is_hiding(client):
    """A ticked view that still won't appear reads as a broken setting unless the
    form says why."""
    _login(client)
    db.set_setting("show_public_vms", "0")
    choices = {c["key"]: c for c in app_module._kiosk_view_choices()}
    assert choices["vms"]["available"] is False
    assert choices["services"]["available"] is True
    assert b"currently hidden by the" in client.get("/admin/settings").data


# ---------------------------------------------------------------------------
# The page itself
# ---------------------------------------------------------------------------
def test_the_kiosk_page_carries_no_public_or_admin_chrome(client):
    """The whole point of the page: no nav, no footer, and none of base.html's fixed
    controls, which are chrome for someone holding a device."""
    _enable()
    body = client.get("/kiosk").data
    assert b'class="page-nav"' not in body
    assert b'class="page-actions"' not in body
    assert b'id="theme-toggle"' not in body
    assert b'class="foot"' not in body


def test_the_ordinary_public_page_still_has_its_controls(client):
    """The kiosk_mode flag is read by base.html, which every page extends - so this is
    the regression that would otherwise go unnoticed."""
    assert b'class="page-actions"' in client.get("/").data
    assert b'id="theme-toggle"' in client.get("/").data
