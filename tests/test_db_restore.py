"""Tests for restoring the database from an uploaded backup.

This is the most destructive action in the admin panel - it replaces every piece of
data the portal holds - so the tests are weighted towards the refusals. The single
most important property is the one asserted over and over below: **a rejected upload
must leave the live database completely untouched.**

_restart_process is mocked everywhere. Per CLAUDE.md it is never invoked for real.
"""
import io
import os
import sqlite3
import zipfile

import pyotp
import pytest

import app as app_module
import db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _valid_backup_bytes(tmp_path, marker="restored-site"):
    """A real, complete Status Portal database, distinguishable from the live one by a
    setting only it contains."""
    path = tmp_path / "donor.db"
    original = db.DB_PATH
    db.DB_PATH = str(path)
    try:
        db.init_db()
        db.set_setting("site_name", marker)
    finally:
        db.DB_PATH = original
    return path.read_bytes()


def _zip_of(name, data):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, data)
    return buf.getvalue()


def _upload(client, data, filename="backup.zip", **extra):
    payload = {"backup": (io.BytesIO(data), filename)}
    payload.update(extra)
    return client.post("/admin/about/restore-db", data=payload,
                        content_type="multipart/form-data", follow_redirects=True)


@pytest.fixture
def admin(client):
    client.post("/admin/login", data={"password": "testpass123", "confirm": "testpass123"})
    return client


@pytest.fixture
def no_restart(monkeypatch):
    """A restore that succeeds calls os.execv via _restart_process(). Never let that
    happen in a test - it would replace the pytest process image."""
    calls = []
    monkeypatch.setattr(app_module, "_restart_process", lambda: calls.append(True))
    return calls


# ---------------------------------------------------------------------------
# Validation - what db.validate_backup_file() will and won't accept
# ---------------------------------------------------------------------------
def test_rejects_a_file_that_isnt_sqlite_at_all(tmp_path):
    path = tmp_path / "notadb"
    path.write_bytes(b"this is just some text, honestly")
    assert "isn't a SQLite database" in db.validate_backup_file(str(path))


def test_rejects_a_corrupted_sqlite_file(tmp_path, isolated_db):
    """A truncated/mangled database has the right header and still has to be caught,
    which is what PRAGMA integrity_check is for."""
    good = _valid_backup_bytes(tmp_path)
    path = tmp_path / "corrupt.db"
    # Keep the header, destroy the middle - a plausible result of a bad transfer.
    corrupted = bytearray(good)
    for i in range(2048, min(len(corrupted), 16384)):
        corrupted[i] = 0xFF
    path.write_bytes(bytes(corrupted))
    assert db.validate_backup_file(str(path)) is not None


def test_rejects_a_valid_database_that_isnt_this_apps(tmp_path):
    """The header and an integrity check together only prove "a valid SQLite file",
    which a Jellyfin library or a browser cookie store also is. Restoring one of those
    would silently wipe the portal and leave it unable to start."""
    path = tmp_path / "someone_elses.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE movies (id INTEGER PRIMARY KEY, title TEXT)")
    conn.commit()
    conn.close()
    reason = db.validate_backup_file(str(path))
    assert "isn't a Status Portal backup" in reason


def test_accepts_a_real_backup(tmp_path, isolated_db):
    path = tmp_path / "good.db"
    path.write_bytes(_valid_backup_bytes(tmp_path))
    assert db.validate_backup_file(str(path)) is None


def test_validation_never_creates_or_modifies_the_file_it_checks(tmp_path):
    """Opened read-only via a URI, so a validation step cannot have side effects - in
    particular it must not bring a not-quite-database into existence."""
    missing = tmp_path / "nope.db"
    db.validate_backup_file(str(missing))
    assert not missing.exists()


# ---------------------------------------------------------------------------
# The replace itself, including the WAL sidecars
# ---------------------------------------------------------------------------
def test_restore_removes_the_stale_wal_sidecars(tmp_path, isolated_db):
    """The subtle one. The database runs in WAL mode, so portal.db-wal holds committed
    pages belonging to the *old* database. Leaving it beside a new main file lets
    SQLite replay one file's journal into another, which is a corrupt database rather
    than a failed restore."""
    db.set_setting("site_name", "original")
    # Force sidecars into existence with an open write transaction's worth of work.
    conn = sqlite3.connect(db.DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('x', 'y')")
    conn.commit()
    conn.close()
    open(db.DB_PATH + "-wal", "wb").write(b"stale wal contents")

    donor = tmp_path / "new.db"
    donor.write_bytes(_valid_backup_bytes(tmp_path, marker="the-new-one"))
    db.restore_from_file(str(donor))

    assert not os.path.exists(db.DB_PATH + "-wal")
    assert not os.path.exists(db.DB_PATH + "-shm")
    assert db.get_setting("site_name") == "the-new-one"


def test_restore_consumes_the_staged_file(tmp_path, isolated_db):
    """os.replace is a rename, so the staged file must not still be sitting there
    afterwards - otherwise every restore leaves a full copy of a database behind."""
    donor = tmp_path / "new.db"
    donor.write_bytes(_valid_backup_bytes(tmp_path))
    db.restore_from_file(str(donor))
    assert not donor.exists()


# ---------------------------------------------------------------------------
# The route: refusals leave the live database alone
# ---------------------------------------------------------------------------
def test_restore_requires_login(client):
    resp = client.post("/admin/about/restore-db", data={}, follow_redirects=False)
    assert resp.status_code == 302
    assert "/admin/login" in resp.headers["Location"]


def test_a_junk_upload_is_refused_and_changes_nothing(admin, tmp_path, no_restart):
    db.set_setting("site_name", "untouched")
    resp = _upload(admin, b"not a database at all", filename="evil.db")
    assert b"Restore refused" in resp.data
    assert db.get_setting("site_name") == "untouched"
    assert no_restart == []


def test_a_foreign_database_is_refused_and_changes_nothing(admin, tmp_path, no_restart):
    db.set_setting("site_name", "untouched")
    other = tmp_path / "other.db"
    conn = sqlite3.connect(str(other))
    conn.execute("CREATE TABLE films (id INTEGER)")
    conn.commit()
    conn.close()
    resp = _upload(admin, other.read_bytes(), filename="other.db")
    assert b"isn" in resp.data  # "isn't a Status Portal backup"
    assert db.get_setting("site_name") == "untouched"
    assert no_restart == []


def test_a_zip_with_no_database_inside_is_refused(admin, no_restart):
    db.set_setting("site_name", "untouched")
    resp = _upload(admin, _zip_of("readme.txt", b"hello"))
    assert b"doesn" in resp.data  # "doesn't contain a .db file"
    assert db.get_setting("site_name") == "untouched"


def test_a_zip_with_several_databases_is_refused_rather_than_guessed(admin, tmp_path, no_restart):
    """Picking one would be guessing which database the admin meant, on the one action
    where guessing wrong destroys their data."""
    good = _valid_backup_bytes(tmp_path)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("portal.db", good)
        zf.writestr("portal-old.db", good)
    resp = _upload(admin, buf.getvalue())
    assert b"expected exactly one" in resp.data
    assert no_restart == []


def test_a_zip_bomb_is_refused_before_it_is_written_out(admin, monkeypatch, no_restart):
    """A small zip that expands enormously. Capped on what is actually read, not on the
    size the zip header declares, because a hostile archive can lie about that."""
    monkeypatch.setattr(app_module, "MAX_EXTRACTED_DB_BYTES", 4096)
    resp = _upload(admin, _zip_of("portal.db", b"\0" * (1024 * 1024)))
    assert b"too large" in resp.data
    assert no_restart == []


def test_no_file_chosen_is_a_plain_message_not_a_crash(admin, no_restart):
    resp = admin.post("/admin/about/restore-db", data={},
                       content_type="multipart/form-data", follow_redirects=True)
    assert b"Choose a backup file" in resp.data


# ---------------------------------------------------------------------------
# The route: the happy path, and the safety snapshot that makes it survivable
# ---------------------------------------------------------------------------
def test_a_valid_backup_replaces_the_database_and_restarts(admin, tmp_path, no_restart):
    db.set_setting("site_name", "the-old-one")
    resp = _upload(admin, _zip_of("portal.db", _valid_backup_bytes(tmp_path, "the-new-one")))
    assert b"Database restored" in resp.data
    assert db.get_setting("site_name") == "the-new-one"
    # Every existing connection still points at the replaced file, so the restart is
    # part of the feature, not an optional nicety.
    assert no_restart == [True]


def test_a_bare_db_file_is_accepted_too(admin, tmp_path, no_restart):
    """Someone who unzipped their own backup to look inside shouldn't be told it's
    invalid."""
    _upload(admin, _valid_backup_bytes(tmp_path, "from-bare-db"), filename="portal.db")
    assert db.get_setting("site_name") == "from-bare-db"


def test_the_current_database_is_snapshotted_before_being_replaced(admin, tmp_path, no_restart, monkeypatch):
    """The thing that makes a regretted restore recoverable. Taken after validation
    (no point snapshotting for a file about to be rejected) and before the live
    database is touched."""
    snapshot_dir = tmp_path / "snaps"
    monkeypatch.setattr(app_module, "DB_SAFETY_BACKUP_DIR", str(snapshot_dir))
    db.set_setting("site_name", "worth-keeping")
    _upload(admin, _zip_of("portal.db", _valid_backup_bytes(tmp_path, "replacement")))

    saved = list(snapshot_dir.glob("*.db"))
    assert len(saved) == 1
    # Not just a file of the right name - the actual previous data, readable.
    conn = sqlite3.connect(str(saved[0]))
    value = conn.execute("SELECT value FROM settings WHERE key='site_name'").fetchone()[0]
    conn.close()
    assert value == "worth-keeping"


def test_a_refused_restore_takes_no_snapshot(admin, tmp_path, no_restart, monkeypatch):
    """Rejecting an upload must cost nothing at all - not even a stray snapshot file."""
    snapshot_dir = tmp_path / "snaps"
    monkeypatch.setattr(app_module, "DB_SAFETY_BACKUP_DIR", str(snapshot_dir))
    _upload(admin, b"junk", filename="junk.db")
    assert not snapshot_dir.exists() or list(snapshot_dir.glob("*.db")) == []


def test_old_snapshots_are_pruned(admin, tmp_path, monkeypatch, no_restart):
    snapshot_dir = tmp_path / "snaps"
    snapshot_dir.mkdir()
    monkeypatch.setattr(app_module, "DB_SAFETY_BACKUP_DIR", str(snapshot_dir))
    monkeypatch.setattr(app_module, "KEEP_DB_SAFETY_BACKUPS", 2)
    for n in range(4):
        path = snapshot_dir / f"portal-before-restore-2026010{n}-000000.db"
        path.write_bytes(b"x")
        os.utime(path, (1_700_000_000 + n, 1_700_000_000 + n))
    _upload(admin, _zip_of("portal.db", _valid_backup_bytes(tmp_path)))
    assert len(list(snapshot_dir.glob("*.db"))) == 2


def test_no_staged_file_is_left_behind_after_a_refusal(admin, isolated_db, no_restart):
    """The staged upload is written next to the database; a refusal must clean it up
    rather than accumulating a rejected copy per attempt."""
    instance_dir = os.path.dirname(db.DB_PATH)
    _upload(admin, b"junk", filename="junk.db")
    assert [n for n in os.listdir(instance_dir) if n.startswith("restore-")] == []


# ---------------------------------------------------------------------------
# Step-up 2FA
# ---------------------------------------------------------------------------
def test_restore_is_blocked_without_a_2fa_code_when_2fa_is_on(admin, tmp_path, no_restart):
    """A stolen session cookie alone must not be enough to replace the entire
    database - the same bar as host restart, app restart and self-update."""
    secret = pyotp.random_base32()
    db.set_setting("admin_totp_secret", secret)
    db.set_setting("admin_totp_enabled", "1")
    db.set_setting("site_name", "untouched")
    _upload(admin, _zip_of("portal.db", _valid_backup_bytes(tmp_path, "should-not-apply")))
    assert db.get_setting("site_name") == "untouched"
    assert no_restart == []


def test_restore_proceeds_with_a_valid_2fa_code(admin, tmp_path, no_restart):
    secret = pyotp.random_base32()
    db.set_setting("admin_totp_secret", secret)
    db.set_setting("admin_totp_enabled", "1")
    _upload(admin, _zip_of("portal.db", _valid_backup_bytes(tmp_path, "applied")),
            totp_code=pyotp.TOTP(secret).now())
    assert db.get_setting("site_name") == "applied"
    assert no_restart == [True]


# ---------------------------------------------------------------------------
# The upload size limit is raised for this one route, not for the whole app
# ---------------------------------------------------------------------------
def test_the_big_upload_limit_applies_only_to_the_restore_route(client):
    """Raising MAX_CONTENT_LENGTH app-wide would hand every form on the site - the
    public report form included - a 64 MB body allowance. Flask 3.1 lets the limit be
    raised for one request instead."""
    with app_module.app.test_request_context("/admin/about/restore-db", method="POST"):
        app_module._allow_large_upload_for_restore()
        from flask import request
        assert request.max_content_length == app_module.DB_RESTORE_MAX_BYTES
    with app_module.app.test_request_context("/report", method="POST"):
        app_module._allow_large_upload_for_restore()
        from flask import request
        assert request.max_content_length == app_module.app.config["MAX_CONTENT_LENGTH"]


def test_the_limit_hook_runs_before_the_csrf_hook():
    """Ordering constraint, not a preference: _check_csrf reads request.form, which
    parses the body - so it would hit the old 2 MB limit and 413 a perfectly good
    upload before the view that raises the limit ever runs."""
    names = [f.__name__ for f in app_module.app.before_request_funcs[None]]
    assert names.index("_allow_large_upload_for_restore") < names.index("_check_csrf")


# ---------------------------------------------------------------------------
# The restart the restore depends on
# ---------------------------------------------------------------------------
def test_the_dev_server_socket_is_released_before_exec(monkeypatch):
    """Found by live-testing a real restore, which the mocked tests could not have
    caught: the portal restarted straight into "Address already in use" and died.

    werkzeug.serving.run_simple() calls socket.set_inheritable(True) on its listening
    socket and exports it as WERKZEUG_SERVER_FD so its auto-reloader can pass the bound
    port to a child. The socket therefore survives os.execv, but the re-executed
    process only adopts that descriptor when the reloader is running - which it never
    is here. Waitress (what production uses) never marks its socket inheritable, which
    is why this only ever bit `python app.py`."""
    import socket
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    fd = sock.fileno()
    monkeypatch.setenv("WERKZEUG_SERVER_FD", str(fd))

    app_module._release_dev_server_socket()

    assert "WERKZEUG_SERVER_FD" not in os.environ
    with pytest.raises(OSError):
        # Already closed - proving the descriptor was actually released, not just
        # forgotten about.
        os.fstat(fd)
    sock.detach()


def test_releasing_the_socket_is_a_no_op_under_waitress(monkeypatch):
    """No WERKZEUG_SERVER_FD means nothing to close, which is the production path."""
    monkeypatch.delenv("WERKZEUG_SERVER_FD", raising=False)
    app_module._release_dev_server_socket()  # must not raise


def test_a_bad_socket_descriptor_never_blocks_a_restart(monkeypatch):
    """Not being able to close a socket must never be the reason a restart doesn't
    happen - especially this restart, which follows a database having been replaced."""
    monkeypatch.setenv("WERKZEUG_SERVER_FD", "not-a-number")
    app_module._release_dev_server_socket()  # must not raise
