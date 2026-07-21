import pytest

import db
import app as app_module


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Points db.py at a fresh, empty SQLite file per test instead of the real
    instance/portal.db - tests never touch real data and never see each other's."""
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test_portal.db"))
    db.init_db()
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
