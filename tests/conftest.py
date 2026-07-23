import pytest

import db
import app as app_module
import integrations as integrations_module


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Points db.py at a fresh, empty SQLite file per test instead of the real
    instance/portal.db - tests never touch real data and never see each other's."""
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test_portal.db"))
    db.init_db()
    # The integration status cache is a module-level global keyed by integration id -
    # clear it so one test's cached entries can't leak into another test that happens
    # to create an integration with the same (autoincrement, per-fresh-db) id.
    app_module._integration_status_cache.clear()
    # Same idea for the Jellyfin activity cache (transcode count/running tasks) -
    # module-level, not tied to any one integration id, so it can't be reset just by
    # a fresh DB.
    integrations_module._jellyfin_activity_cache["transcoding"] = 0
    integrations_module._jellyfin_activity_cache["running_tasks"] = []
    return db.DB_PATH


@pytest.fixture
def client(isolated_db, monkeypatch):
    # The login lockout counter is a module-level global - reset it so one test's
    # failed attempts can't lock out the next test.
    monkeypatch.setitem(app_module._login_state, "failures", 0)
    monkeypatch.setitem(app_module._login_state, "locked_until", 0.0)
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c
