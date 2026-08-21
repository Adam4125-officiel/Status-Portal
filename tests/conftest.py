import pytest

import db
import app as app_module
import integrations as integrations_module
import monitoring as monitoring_module
import scheduler as scheduler_module
import updater as updater_module


def _reset_module_state():
    """Every module-level global that would otherwise outlive a single test.

    Centralised here rather than reset ad-hoc inside individual tests, because the
    ad-hoc version only protects the tests that remembered to do it - and a leaked
    cache doesn't fail loudly, it makes some *later* test read the previous test's
    data and pass or fail for the wrong reason.

    Where a module already owns a clear_caches()/clear_update_cache() helper (the
    same ones the admin panel's clear-caches button calls), that helper is used
    instead of poking at its globals from here: a cache added to that module later is
    then covered by these tests automatically, with no matching edit needed in this
    file. tests/test_conventions.py enforces that every such global is named here."""
    # app.py's own caches and rate-limit counters, which have no shared helper: the
    # clear-caches button deliberately doesn't reset login/report throttling.
    app_module._integration_status_cache.clear()
    app_module._uptime_cache["value"] = {}
    app_module._uptime_cache["fetched_at"] = 0.0
    app_module._asset_salt["value"] = None
    app_module._login_state["failures"] = 0
    app_module._login_state["locked_until"] = 0.0
    app_module._report_state["count"] = 0
    app_module._report_state["window_start"] = 0.0
    # Modules that own their own clearing.
    integrations_module.clear_caches()
    monitoring_module.clear_caches()
    scheduler_module.clear_caches()
    updater_module.clear_update_cache()


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Points db.py at a fresh, empty SQLite file per test instead of the real
    instance/portal.db - tests never touch real data and never see each other's."""
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test_portal.db"))
    db.init_db()
    _reset_module_state()
    return db.DB_PATH


@pytest.fixture
def client(isolated_db, monkeypatch):
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c
