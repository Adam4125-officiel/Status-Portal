"""
Tests for updater.py.

Everything that touches the filesystem runs against a throwaway "app directory"
under tmp_path - config.APP_ROOT and every path derived from it are monkeypatched,
so no test can ever write into the real repository. Nothing here makes a network
call (requests is always mocked) and nothing here ever restarts anything.
"""
import io
import os
import zipfile

import pytest

import config
import db
import updater


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def fake_app_dir(tmp_path, monkeypatch, isolated_db):
    """A stand-in app directory with the files an update would replace, plus the
    three things an update must never touch."""
    root = tmp_path / "app"
    (root / "templates").mkdir(parents=True)
    (root / "instance").mkdir()
    (root / "static" / "uploads").mkdir(parents=True)
    (root / "app.py").write_text("old app\n")
    (root / "VERSION").write_text("1.0.0\n")
    (root / "requirements.txt").write_text("Flask>=3.0\n")
    (root / "templates" / "index.html").write_text("<p>old</p>")
    # The protected trio - assertions below check these are byte-identical afterwards.
    (root / "instance" / "portal.db").write_text("PRECIOUS DATABASE")
    (root / ".env").write_text("PORTAL_SECRET_KEY=secret")
    (root / "static" / "uploads" / "logo.png").write_text("PRECIOUS LOGO")

    monkeypatch.setattr(config, "APP_ROOT", str(root))
    monkeypatch.setattr(config, "VERSION", "1.0.0")
    monkeypatch.setattr(config, "VERSION_DISPLAY", "1.0.0")
    monkeypatch.setattr(config, "IS_GIT_CHECKOUT", False)
    monkeypatch.setattr(updater, "INSTANCE_DIR", str(root / "instance"))
    monkeypatch.setattr(updater, "BACKUP_ROOT", str(root / "instance" / "update_backups"))
    monkeypatch.setattr(updater, "PENDING_MARKER_PATH", str(root / "instance" / "update_pending.json"))
    updater._update_cache["result"] = None
    updater._update_cache["refreshed_monotonic"] = None
    return root


def make_zip(files, prefix=""):
    """A release archive in memory. prefix="" mimics `git archive` (files at the
    root, which is how this project's releases are built); a non-empty prefix mimics
    GitHub's auto-generated zipball."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, content in files.items():
            zf.writestr(prefix + name, content)
    return buffer.getvalue()


def release_payload(version, prerelease=False, data=b"", name=None):
    return {
        "tag_name": f"v{version}",
        "name": name or f"v{version}",
        "prerelease": prerelease,
        "draft": False,
        "published_at": "2026-08-10T12:00:00Z",
        "html_url": f"https://github.com/x/y/releases/tag/v{version}",
        "body": "notes",
        "assets": [{
            "name": f"status-portal-v{version}.zip",
            "browser_download_url": f"https://github.com/x/y/releases/download/v{version}/a.zip",
            "size": len(data),
        }],
        "zipball_url": f"https://api.github.com/repos/x/y/zipball/v{version}",
    }


class FakeResponse:
    def __init__(self, json_data=None, content=b"", url="https://objects.githubusercontent.com/a.zip"):
        self._json = json_data
        self.content = content
        self.url = url

    def raise_for_status(self):
        return None

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i:i + chunk_size]


# ---------------------------------------------------------------------------
# Version parsing / comparison
# ---------------------------------------------------------------------------
def test_parse_version_orders_prereleases_below_their_final_release():
    assert updater.parse_version("1.5.0-rc.1") < updater.parse_version("1.5.0")
    assert updater.parse_version("1.5.0-rc.1") < updater.parse_version("1.5.0-rc.2")
    assert updater.parse_version("1.4.0") < updater.parse_version("1.5.0-rc.1")
    assert updater.parse_version("v1.5.0") == updater.parse_version("1.5.0")
    assert updater.parse_version("1.10.0") > updater.parse_version("1.9.0")


def test_parse_version_sorts_garbage_to_the_bottom_instead_of_raising():
    """A malformed tag must never be mistaken for something newer than what's running."""
    assert updater.parse_version("not-a-version") < updater.parse_version("0.0.1")
    assert updater.parse_version(None) == (0, 0, 0, 0, 0)
    assert updater.parse_version("") == (0, 0, 0, 0, 0)


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------
def test_stable_channel_ignores_prereleases_and_unstable_includes_them(monkeypatch, isolated_db):
    releases = [release_payload("1.4.0"), release_payload("1.5.0-rc.2", prerelease=True)]
    monkeypatch.setattr(updater.requests, "get", lambda *a, **k: FakeResponse(json_data=releases))

    assert updater.fetch_latest_release("stable")["version"] == "1.4.0"
    assert updater.fetch_latest_release("unstable")["version"] == "1.5.0-rc.2"


def test_latest_release_is_picked_by_version_not_publish_order(monkeypatch, isolated_db):
    """Republishing an old release must not look like an update."""
    releases = [release_payload("1.2.0"), release_payload("1.5.0"), release_payload("1.3.0")]
    monkeypatch.setattr(updater.requests, "get", lambda *a, **k: FakeResponse(json_data=releases))
    assert updater.fetch_latest_release("stable")["version"] == "1.5.0"


def test_drafts_are_never_offered(monkeypatch, isolated_db):
    draft = release_payload("9.9.9")
    draft["draft"] = True
    monkeypatch.setattr(updater.requests, "get",
                        lambda *a, **k: FakeResponse(json_data=[release_payload("1.4.0"), draft]))
    assert updater.fetch_latest_release("stable")["version"] == "1.4.0"


def test_get_channel_falls_back_to_stable_when_the_database_is_unreadable(monkeypatch, isolated_db):
    """The CLI is the tool you use when things are broken - including the DB."""
    monkeypatch.setattr(db, "get_setting", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert updater.get_channel() == "stable"


def test_set_channel_rejects_an_unknown_channel(isolated_db):
    with pytest.raises(updater.UpdateError):
        updater.set_channel("nightly")


def test_reading_settings_never_creates_a_stray_database(tmp_path, monkeypatch):
    """sqlite3.connect() creates an empty file for a path that doesn't exist - so
    `update.py check` on a fresh install would otherwise leave a zero-table
    portal.db behind for init_db() to trip over."""
    missing = tmp_path / "nope" / "portal.db"
    (tmp_path / "nope").mkdir()
    monkeypatch.setattr(db, "DB_PATH", str(missing))
    assert updater.get_channel() == "stable"
    assert updater.update_check_enabled() is True
    assert not missing.exists()


def test_set_channel_without_a_database_is_an_explainable_error(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "portal.db"))
    with pytest.raises(updater.UpdateError, match="No database"):
        updater.set_channel("unstable")


# ---------------------------------------------------------------------------
# check_for_update
# ---------------------------------------------------------------------------
def test_check_for_update_reports_an_available_update(monkeypatch, fake_app_dir):
    monkeypatch.setattr(updater.requests, "get",
                        lambda *a, **k: FakeResponse(json_data=[release_payload("1.5.0")]))
    result = updater.check_for_update("stable")
    assert result["ok"] and result["update_available"] and not result["ahead"]
    assert result["latest"] == "1.5.0"


def test_check_for_update_reports_up_to_date(monkeypatch, fake_app_dir):
    monkeypatch.setattr(updater.requests, "get",
                        lambda *a, **k: FakeResponse(json_data=[release_payload("1.0.0")]))
    result = updater.check_for_update("stable")
    assert result["ok"] and not result["update_available"] and not result["ahead"]


def test_check_for_update_reports_running_ahead_separately_from_up_to_date(monkeypatch, fake_app_dir):
    """A dev build, or a stable box handed a prerelease - its own state, not silently
    flattened into "up to date"."""
    monkeypatch.setattr(updater.requests, "get",
                        lambda *a, **k: FakeResponse(json_data=[release_payload("0.9.0")]))
    result = updater.check_for_update("stable")
    assert result["ok"] and result["ahead"] and not result["update_available"]


def test_check_for_update_degrades_gracefully_when_github_is_unreachable(monkeypatch, fake_app_dir):
    def boom(*a, **k):
        raise updater.requests.RequestException("no route to host")
    monkeypatch.setattr(updater.requests, "get", boom)
    result = updater.check_for_update("stable")
    assert result["ok"] is False
    assert "no route to host" in result["error"]
    # Still a fully-formed result the About page can render.
    assert result["current"] == "1.0.0" and result["update_available"] is False


# ---------------------------------------------------------------------------
# The cached read (never check inline from a request handler)
# ---------------------------------------------------------------------------
def test_cache_is_only_refreshed_once_per_ttl(monkeypatch, fake_app_dir):
    calls = []

    def fake_get(*a, **k):
        calls.append(1)
        return FakeResponse(json_data=[release_payload("1.5.0")])
    monkeypatch.setattr(updater.requests, "get", fake_get)

    updater.refresh_update_cache_if_stale(ttl_seconds=3600)
    updater.refresh_update_cache_if_stale(ttl_seconds=3600)
    updater.refresh_update_cache_if_stale(ttl_seconds=3600)
    assert len(calls) == 1
    assert updater.get_cached_update_status()["latest"] == "1.5.0"

    updater.refresh_update_cache_if_stale(ttl_seconds=3600, force=True)
    assert len(calls) == 2


def test_background_refresh_is_skipped_when_automatic_checking_is_off(monkeypatch, fake_app_dir):
    calls = []
    monkeypatch.setattr(updater.requests, "get",
                        lambda *a, **k: calls.append(1) or FakeResponse(json_data=[release_payload("1.5.0")]))
    db.set_setting("update_check_enabled", "0")
    assert updater.refresh_update_cache_if_stale() is None
    assert calls == []
    # ...but an explicit "Check now" still works.
    updater.refresh_update_cache_if_stale(force=True)
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Download URL validation and integrity
# ---------------------------------------------------------------------------
def test_download_refuses_plain_http_and_unknown_hosts():
    with pytest.raises(updater.UpdateError, match="HTTPS only"):
        updater._validate_download_url("http://github.com/a.zip", "test")
    with pytest.raises(updater.UpdateError, match="unexpected host"):
        updater._validate_download_url("https://evil.example.com/a.zip", "test")
    # The real thing must still pass.
    updater._validate_download_url(
        "https://objects.githubusercontent.com/x/status-portal-v1.5.0.zip", "test")


def test_download_rejects_a_redirect_to_an_unexpected_host(monkeypatch, fake_app_dir):
    monkeypatch.setattr(updater.requests, "get",
                        lambda *a, **k: FakeResponse(content=b"x", url="https://evil.example.com/a.zip"))
    with pytest.raises(updater.UpdateError, match="unexpected host"):
        updater._download_asset(
            {"url": "https://github.com/a.zip", "name": "a.zip", "size": 1}, lambda m: None)


def test_download_rejects_a_size_mismatch(monkeypatch, fake_app_dir):
    monkeypatch.setattr(updater.requests, "get", lambda *a, **k: FakeResponse(content=b"12345"))
    with pytest.raises(updater.UpdateError, match="Size mismatch"):
        updater._download_asset(
            {"url": "https://github.com/a.zip", "name": "a.zip", "size": 99}, lambda m: None)


def test_download_rejects_a_sha256_mismatch(monkeypatch, fake_app_dir):
    monkeypatch.setattr(updater.requests, "get", lambda *a, **k: FakeResponse(content=b"12345"))
    with pytest.raises(updater.UpdateError, match="SHA-256 mismatch"):
        updater._download_asset({"url": "https://github.com/a.zip", "name": "a.zip",
                                 "size": 5, "digest": "sha256:" + "0" * 64}, lambda m: None)


def test_download_accepts_a_matching_sha256(monkeypatch, fake_app_dir):
    import hashlib
    payload = b"hello release"
    monkeypatch.setattr(updater.requests, "get", lambda *a, **k: FakeResponse(content=payload))
    messages = []
    data, digest = updater._download_asset(
        {"url": "https://github.com/a.zip", "name": "a.zip", "size": len(payload),
         "digest": "sha256:" + hashlib.sha256(payload).hexdigest()}, messages.append)
    assert data == payload
    assert digest == hashlib.sha256(payload).hexdigest()
    assert any("SHA-256 verified" in m for m in messages)


def test_download_is_capped_so_a_runaway_response_cannot_fill_the_disk(monkeypatch, fake_app_dir):
    monkeypatch.setattr(updater, "MAX_DOWNLOAD_BYTES", 10)
    monkeypatch.setattr(updater.requests, "get", lambda *a, **k: FakeResponse(content=b"x" * 100))
    with pytest.raises(updater.UpdateError, match="safety cap"):
        updater._download_asset({"url": "https://github.com/a.zip", "name": "a.zip", "size": None},
                                lambda m: None)


# ---------------------------------------------------------------------------
# Archive inspection
# ---------------------------------------------------------------------------
def test_git_archive_layout_is_read_as_is(fake_app_dir):
    with zipfile.ZipFile(io.BytesIO(make_zip({"app.py": "x", "templates/index.html": "y"}))) as zf:
        names = [name for _info, name in updater._archive_members(zf)]
    assert sorted(names) == ["app.py", "templates/index.html"]


def test_github_zipball_top_level_directory_is_stripped(fake_app_dir):
    data = make_zip({"app.py": "x", "templates/index.html": "y"}, prefix="owner-repo-abc123/")
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = [name for _info, name in updater._archive_members(zf)]
    assert sorted(names) == ["app.py", "templates/index.html"]


@pytest.mark.parametrize("bad_name", ["../evil.py", "sub/../../evil.py"])
def test_archive_with_a_parent_directory_path_is_refused(fake_app_dir, bad_name):
    with zipfile.ZipFile(io.BytesIO(make_zip({"app.py": "x", bad_name: "pwned"}))) as zf:
        with pytest.raises(updater.UpdateError, match="parent-directory"):
            updater._archive_members(zf)


@pytest.mark.parametrize("protected", ["instance/portal.db", ".env", "static/uploads/logo.png"])
def test_archive_containing_a_protected_path_aborts_the_whole_update(fake_app_dir, protected):
    """The archive's file list is the whitelist, but a release built wrong must fail
    loudly rather than have one entry quietly skipped."""
    with zipfile.ZipFile(io.BytesIO(make_zip({"app.py": "x", protected: "bad"}))) as zf:
        with pytest.raises(updater.UpdateError, match="protected path"):
            updater._archive_members(zf)


def test_archive_member_limit_is_enforced(fake_app_dir, monkeypatch):
    monkeypatch.setattr(updater, "MAX_ARCHIVE_MEMBERS", 2)
    data = make_zip({"a.py": "1", "b.py": "2", "c.py": "3"})
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        with pytest.raises(updater.UpdateError, match="safety limit"):
            updater._archive_members(zf)


def test_empty_archive_is_refused(fake_app_dir):
    with zipfile.ZipFile(io.BytesIO(make_zip({}))) as zf:
        with pytest.raises(updater.UpdateError, match="empty"):
            updater._archive_members(zf)


# ---------------------------------------------------------------------------
# perform_update
# ---------------------------------------------------------------------------
def _wire_release(monkeypatch, version, files, prerelease=False):
    """Points both requests.get calls (API list, then asset download) at a fake."""
    data = make_zip(files)
    payload = [release_payload(version, prerelease=prerelease, data=data)]

    def fake_get(url, *a, **k):
        if url == updater.RELEASES_API_URL:
            return FakeResponse(json_data=payload)
        return FakeResponse(content=data)
    monkeypatch.setattr(updater.requests, "get", fake_get)
    return data


def test_update_refuses_to_overwrite_a_git_checkout(monkeypatch, fake_app_dir):
    monkeypatch.setattr(config, "IS_GIT_CHECKOUT", True)
    with pytest.raises(updater.UpdateError, match="git checkout"):
        updater.perform_update(progress=lambda m: None)


def test_update_is_a_no_op_when_already_up_to_date(monkeypatch, fake_app_dir):
    _wire_release(monkeypatch, "1.0.0", {"app.py": "new", "VERSION": "1.0.0\n"})
    result = updater.perform_update(progress=lambda m: None)
    assert result["applied"] is False
    assert "up to date" in result["reason"]
    assert (fake_app_dir / "app.py").read_text() == "old app\n"


def test_update_is_a_no_op_when_running_ahead_of_the_channel(monkeypatch, fake_app_dir):
    _wire_release(monkeypatch, "0.9.0", {"app.py": "new", "VERSION": "0.9.0\n"})
    result = updater.perform_update(progress=lambda m: None)
    assert result["applied"] is False
    assert "newer than" in result["reason"]


def test_update_replaces_files_and_never_touches_instance_env_or_uploads(monkeypatch, fake_app_dir):
    _wire_release(monkeypatch, "1.5.0", {
        "app.py": "new app\n",
        "VERSION": "1.5.0\n",
        "requirements.txt": "Flask>=3.0\n",
        "templates/index.html": "<p>new</p>",
        "brand_new_file.py": "hello\n",
    })
    result = updater.perform_update(progress=lambda m: None)

    assert result["applied"] is True and result["latest"] == "1.5.0"
    assert (fake_app_dir / "app.py").read_text() == "new app\n"
    assert (fake_app_dir / "templates" / "index.html").read_text() == "<p>new</p>"
    assert (fake_app_dir / "brand_new_file.py").read_text() == "hello\n"
    # The whole point: these three survive verbatim.
    assert (fake_app_dir / "instance" / "portal.db").read_text() == "PRECIOUS DATABASE"
    assert (fake_app_dir / ".env").read_text() == "PORTAL_SECRET_KEY=secret"
    assert (fake_app_dir / "static" / "uploads" / "logo.png").read_text() == "PRECIOUS LOGO"
    # No stray temp files left behind.
    assert not any(p.name.endswith(".update-tmp") for p in fake_app_dir.rglob("*"))


def test_update_is_safe_to_run_twice(monkeypatch, fake_app_dir):
    _wire_release(monkeypatch, "1.5.0", {"app.py": "new app\n", "VERSION": "1.5.0\n",
                                         "requirements.txt": "Flask>=3.0\n"})
    first = updater.perform_update(progress=lambda m: None)
    assert first["applied"] is True
    # config.VERSION is what the *running* process reports; a real second run would be
    # a fresh process reading the new VERSION file, which this reproduces.
    monkeypatch.setattr(config, "VERSION", "1.5.0")
    second = updater.perform_update(progress=lambda m: None)
    assert second["applied"] is False
    assert (fake_app_dir / "app.py").read_text() == "new app\n"


def test_update_takes_a_backup_of_every_file_it_replaces(monkeypatch, fake_app_dir):
    _wire_release(monkeypatch, "1.5.0", {"app.py": "new app\n", "VERSION": "1.5.0\n",
                                         "requirements.txt": "Flask>=3.0\n", "added.py": "x"})
    result = updater.perform_update(progress=lambda m: None)

    backups = updater.list_backups()
    assert len(backups) == 1
    manifest = backups[0]
    assert manifest["name"] == result["backup"]
    assert manifest["from_version"] == "1.0.0" and manifest["to_version"] == "1.5.0"
    assert "app.py" in manifest["replaced"] and "added.py" in manifest["added"]
    backed_up = os.path.join(manifest["path"], "app.py")
    assert open(backed_up).read() == "old app\n"


def test_a_failed_write_part_way_through_rolls_the_whole_update_back(monkeypatch, fake_app_dir):
    """A half-updated tree is worse than either version. Models the realistic
    Windows case: one file is locked while it's being replaced, then freed."""
    _wire_release(monkeypatch, "1.5.0", {
        "app.py": "new app\n", "VERSION": "1.5.0\n", "requirements.txt": "Flask>=3.0\n",
        "templates/index.html": "<p>new</p>", "added.py": "x",
    })
    real_write = updater._atomic_write
    failed_once = []

    def flaky_write(destination, data, mode=None):
        if destination.endswith("index.html") and not failed_once:
            failed_once.append(1)
            raise updater.UpdateError("simulated Windows file lock")
        return real_write(destination, data, mode)
    monkeypatch.setattr(updater, "_atomic_write", flaky_write)

    with pytest.raises(updater.UpdateError, match="rolled back"):
        updater.perform_update(progress=lambda m: None)

    # Every file is back to how it started, and the newly-added one is gone again.
    assert (fake_app_dir / "app.py").read_text() == "old app\n"
    assert (fake_app_dir / "templates" / "index.html").read_text() == "<p>old</p>"
    assert not (fake_app_dir / "added.py").exists()
    assert (fake_app_dir / "instance" / "portal.db").read_text() == "PRECIOUS DATABASE"


def test_a_rollback_that_also_fails_says_so_instead_of_claiming_success(monkeypatch, fake_app_dir):
    """The worst case: a file stays locked, so even the automatic rollback can't put
    it back. The admin gets told exactly which backup folder to restore by hand -
    the one thing worse than this would be reporting it as handled."""
    _wire_release(monkeypatch, "1.5.0", {
        "app.py": "new app\n", "VERSION": "1.5.0\n", "requirements.txt": "Flask>=3.0\n",
        "templates/index.html": "<p>new</p>",
    })

    def always_locked(destination, data, mode=None):
        raise updater.UpdateError("permanently locked")
    monkeypatch.setattr(updater, "_atomic_write", always_locked)

    with pytest.raises(updater.UpdateError, match="automatic rollback failed"):
        updater.perform_update(progress=lambda m: None)


def test_dependencies_are_installed_only_when_requirements_changed(monkeypatch, fake_app_dir):
    calls = []
    monkeypatch.setattr(updater, "_install_dependencies", lambda progress: calls.append(1))

    _wire_release(monkeypatch, "1.5.0", {"app.py": "a", "VERSION": "1.5.0\n",
                                         "requirements.txt": "Flask>=3.0\n"})
    result = updater.perform_update(progress=lambda m: None)
    assert result["deps_changed"] is False and calls == []

    monkeypatch.setattr(config, "VERSION", "1.5.0")
    _wire_release(monkeypatch, "1.6.0", {"app.py": "b", "VERSION": "1.6.0\n",
                                         "requirements.txt": "Flask>=3.0\nnewdep>=1.0\n"})
    result = updater.perform_update(progress=lambda m: None)
    assert result["deps_changed"] is True and calls == [1]


def test_a_failed_dependency_install_rolls_the_update_back(monkeypatch, fake_app_dir):
    """Restarting into code whose dependencies aren't installed would just fail to
    start - so this is caught while there is still something able to undo it."""
    def boom(progress):
        raise updater.UpdateError("pip install failed: no such package")
    monkeypatch.setattr(updater, "_install_dependencies", boom)
    _wire_release(monkeypatch, "1.5.0", {"app.py": "new app\n", "VERSION": "1.5.0\n",
                                         "requirements.txt": "Flask>=3.0\nnewdep>=1.0\n"})

    with pytest.raises(updater.UpdateError, match="rolled back"):
        updater.perform_update(progress=lambda m: None)
    assert (fake_app_dir / "app.py").read_text() == "old app\n"
    assert (fake_app_dir / "requirements.txt").read_text() == "Flask>=3.0\n"


def test_install_deps_can_be_skipped(monkeypatch, fake_app_dir):
    calls = []
    monkeypatch.setattr(updater, "_install_dependencies", lambda progress: calls.append(1))
    _wire_release(monkeypatch, "1.5.0", {"app.py": "a", "VERSION": "1.5.0\n",
                                         "requirements.txt": "Flask>=3.0\nnewdep>=1.0\n"})
    result = updater.perform_update(install_deps=False, progress=lambda m: None)
    assert result["deps_changed"] is True and calls == []


def test_old_backups_are_pruned(monkeypatch, fake_app_dir):
    monkeypatch.setattr(updater, "KEEP_BACKUPS", 2)
    monkeypatch.setattr(updater, "_install_dependencies", lambda progress: None)
    for i, version in enumerate(["1.1.0", "1.2.0", "1.3.0", "1.4.0"]):
        monkeypatch.setattr(config, "VERSION", f"1.{i}.0")
        _wire_release(monkeypatch, version, {"app.py": version, "VERSION": version + "\n",
                                             "requirements.txt": "Flask>=3.0\n"})
        updater.perform_update(progress=lambda m: None)
    assert len(updater.list_backups()) == 2


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------
def test_rollback_restores_replaced_files_and_removes_added_ones(monkeypatch, fake_app_dir):
    _wire_release(monkeypatch, "1.5.0", {"app.py": "new app\n", "VERSION": "1.5.0\n",
                                         "requirements.txt": "Flask>=3.0\n", "added.py": "x"})
    updater.perform_update(progress=lambda m: None)
    assert (fake_app_dir / "app.py").read_text() == "new app\n"

    outcome = updater.rollback(progress=lambda m: None)
    assert outcome["version"] == "1.0.0"
    assert (fake_app_dir / "app.py").read_text() == "old app\n"
    assert (fake_app_dir / "VERSION").read_text() == "1.0.0\n"
    assert not (fake_app_dir / "added.py").exists()


def test_rollback_can_target_a_named_backup(monkeypatch, fake_app_dir):
    monkeypatch.setattr(updater, "_install_dependencies", lambda progress: None)
    _wire_release(monkeypatch, "1.5.0", {"app.py": "v15\n", "VERSION": "1.5.0\n",
                                         "requirements.txt": "Flask>=3.0\n"})
    first = updater.perform_update(progress=lambda m: None)
    monkeypatch.setattr(config, "VERSION", "1.5.0")
    _wire_release(monkeypatch, "1.6.0", {"app.py": "v16\n", "VERSION": "1.6.0\n",
                                         "requirements.txt": "Flask>=3.0\n"})
    updater.perform_update(progress=lambda m: None)

    updater.rollback(first["backup"], progress=lambda m: None)
    assert (fake_app_dir / "app.py").read_text() == "old app\n"


def test_rollback_with_no_backups_is_an_explainable_error(fake_app_dir):
    with pytest.raises(updater.UpdateError, match="nothing to roll back"):
        updater.rollback(progress=lambda m: None)


def test_rollback_rejects_an_unknown_backup_name(monkeypatch, fake_app_dir):
    _wire_release(monkeypatch, "1.5.0", {"app.py": "a", "VERSION": "1.5.0\n",
                                         "requirements.txt": "Flask>=3.0\n"})
    updater.perform_update(progress=lambda m: None)
    with pytest.raises(updater.UpdateError, match="No backup named"):
        updater.rollback("does-not-exist", progress=lambda m: None)


# ---------------------------------------------------------------------------
# The pending-update marker (what little is achievable across a restart)
# ---------------------------------------------------------------------------
def test_pending_marker_is_cleared_when_the_app_comes_back_on_the_new_version(fake_app_dir, monkeypatch):
    updater.write_pending_marker("20260810-120000-1.0.0-to-1.5.0", "1.5.0")
    assert os.path.isfile(updater.PENDING_MARKER_PATH)
    monkeypatch.setattr(config, "VERSION", "1.5.0")

    outcome = updater.check_pending_marker()
    assert outcome["status"] == "confirmed"
    assert not os.path.isfile(updater.PENDING_MARKER_PATH)


def test_pending_marker_survives_and_logs_when_the_version_did_not_change(fake_app_dir, caplog):
    """The app came back on the OLD code - the update didn't take. The marker stays
    so `update.py rollback` still knows which backup to use."""
    updater.write_pending_marker("20260810-120000-1.0.0-to-1.5.0", "1.5.0")
    with caplog.at_level("ERROR"):
        outcome = updater.check_pending_marker()
    assert outcome["status"] == "mismatch"
    assert os.path.isfile(updater.PENDING_MARKER_PATH)
    assert "update.py rollback" in caplog.text


def test_check_pending_marker_is_a_no_op_without_a_marker(fake_app_dir):
    assert updater.check_pending_marker() is None


def test_rollback_clears_a_pending_marker(monkeypatch, fake_app_dir):
    _wire_release(monkeypatch, "1.5.0", {"app.py": "a", "VERSION": "1.5.0\n",
                                         "requirements.txt": "Flask>=3.0\n"})
    result = updater.perform_update(progress=lambda m: None)
    updater.write_pending_marker(result["backup"], "1.5.0")
    updater.rollback(progress=lambda m: None)
    assert not os.path.isfile(updater.PENDING_MARKER_PATH)


# ---------------------------------------------------------------------------
# The update source is fixed
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# The CLI's emergency rollback (must work when the app's own code won't import)
# ---------------------------------------------------------------------------
def test_cli_channel_list_mirrors_the_updater(fake_app_dir):
    """update.py duplicates the channel names as a literal so argparse can be built
    without importing the app - this is what stops the two drifting apart."""
    import update
    assert update.CHANNELS == updater.CHANNELS


def test_emergency_rollback_restores_a_backup_without_importing_the_app(monkeypatch, fake_app_dir):
    """The failure mode this exists for: an update replaced code that no longer
    imports, so updater.rollback() can't be reached at all. This path reads only the
    manifest.json that updater.py already wrote."""
    import update
    _wire_release(monkeypatch, "1.5.0", {"app.py": "new app\n", "VERSION": "1.5.0\n",
                                         "requirements.txt": "Flask>=3.0\n", "added.py": "x"})
    result = updater.perform_update(progress=lambda m: None)
    updater.write_pending_marker(result["backup"], "1.5.0")
    assert (fake_app_dir / "app.py").read_text() == "new app\n"

    monkeypatch.setattr(update, "APP_ROOT", str(fake_app_dir))
    assert update._emergency_rollback() == 0

    assert (fake_app_dir / "app.py").read_text() == "old app\n"
    assert not (fake_app_dir / "added.py").exists()
    assert not (fake_app_dir / "instance" / "update_pending.json").exists()
    assert (fake_app_dir / "instance" / "portal.db").read_text() == "PRECIOUS DATABASE"


def test_emergency_rollback_uses_the_backup_named_in_the_pending_marker(monkeypatch, fake_app_dir):
    import update
    monkeypatch.setattr(updater, "_install_dependencies", lambda progress: None)
    _wire_release(monkeypatch, "1.5.0", {"app.py": "v15\n", "VERSION": "1.5.0\n",
                                         "requirements.txt": "Flask>=3.0\n"})
    first = updater.perform_update(progress=lambda m: None)
    monkeypatch.setattr(config, "VERSION", "1.5.0")
    _wire_release(monkeypatch, "1.6.0", {"app.py": "v16\n", "VERSION": "1.6.0\n",
                                         "requirements.txt": "Flask>=3.0\n"})
    updater.perform_update(progress=lambda m: None)
    # Marker points at the FIRST backup, not the most recent one.
    updater.write_pending_marker(first["backup"], "1.5.0")

    monkeypatch.setattr(update, "APP_ROOT", str(fake_app_dir))
    update._emergency_rollback()
    assert (fake_app_dir / "app.py").read_text() == "old app\n"


def test_emergency_rollback_with_no_backups_exits_nonzero(monkeypatch, fake_app_dir):
    import update
    monkeypatch.setattr(update, "APP_ROOT", str(fake_app_dir))
    assert update._emergency_rollback() == 1


def test_cli_output_stays_ascii_only():
    """An em dash in update.py's header raised UnicodeEncodeError on a Windows
    console using codepage 437. This is the tool you run when the portal is already
    broken - it must not be able to fail on a decorative character. Covers
    updater.py's progress()/error messages too, since the CLI prints those."""
    root = os.path.join(os.path.dirname(__file__), "..")
    offenders = []
    for filename in ("update.py", "updater.py"):
        with open(os.path.join(root, filename), encoding="utf-8") as f:
            for number, line in enumerate(f, 1):
                if not any(k in line for k in ("print(", "progress(", "UpdateError(")):
                    continue
                non_ascii = [c for c in line if ord(c) > 127]
                if non_ascii:
                    offenders.append(f"{filename}:{number}: {''.join(non_ascii)!r}")
    assert not offenders, "non-ASCII in CLI output: " + "; ".join(offenders)


def test_the_update_source_is_hardcoded_to_this_repository():
    """Guards the single most important property of this whole feature: nothing an
    admin (or anyone who compromises the admin panel) can set redirects where code
    is downloaded from."""
    assert updater.GITHUB_OWNER == "Adam4125-officiel"
    assert updater.GITHUB_REPO == "Status-Portal"
    assert updater.RELEASES_API_URL.startswith("https://api.github.com/repos/")
    source = open(os.path.join(os.path.dirname(__file__), "..", "updater.py")).read()
    assert "verify=False" not in source


# ---------------------------------------------------------------------------
# Release notes (the changelog shown on /admin/about)
# ---------------------------------------------------------------------------
def _release(tag, body="notes", prerelease=False):
    return {"tag_name": tag, "name": tag, "prerelease": prerelease, "draft": False,
            "published_at": "2026-08-01T00:00:00Z", "body": body,
            "html_url": f"https://example/{tag}", "assets": [], "zipball_url": None}


def test_release_notes_cover_every_version_between_current_and_latest(monkeypatch):
    """The point of showing notes at all: if several releases have accumulated, the
    admin needs what changed in each, not only in the newest."""
    releases = [_release("v1.8.0"), _release("v1.7.1"), _release("v1.7.0"), _release("v1.6.0")]
    monkeypatch.setattr(updater, "fetch_releases", lambda channel=None:
                        [updater._normalise_release(r, "stable") for r in releases])
    monkeypatch.setattr(updater, "current_version", lambda: "1.7.0")
    result = updater.check_for_update("stable")
    assert [r["version"] for r in result["release_notes"]] == ["1.8.0", "1.7.1"]
    assert result["release_notes_omitted"] == 0


def test_release_notes_include_the_version_being_run(monkeypatch):
    """"Should I take this?" is much easier to answer with the notes for what you're
    already on sitting next to the notes for what's on offer."""
    releases = [_release("v1.8.0"), _release("v1.7.0", body="what 1.7.0 changed")]
    monkeypatch.setattr(updater, "fetch_releases", lambda channel=None:
                        [updater._normalise_release(r, "stable") for r in releases])
    monkeypatch.setattr(updater, "current_version", lambda: "1.7.0")
    result = updater.check_for_update("stable")
    assert result["current_notes"]["body"] == "what 1.7.0 changed"


def test_release_notes_are_capped_and_report_what_was_omitted(monkeypatch):
    """A portal left un-updated for a very long time must not render - or cache - a
    page of unbounded length."""
    releases = [_release(f"v1.{n}.0") for n in range(40, 0, -1)]
    monkeypatch.setattr(updater, "fetch_releases", lambda channel=None:
                        [updater._normalise_release(r, "stable") for r in releases])
    monkeypatch.setattr(updater, "current_version", lambda: "1.0.0")
    result = updater.check_for_update("stable")
    assert len(result["release_notes"]) == updater.MAX_RELEASE_NOTES
    # All 40 are newer than 1.0.0, so everything past the cap is reported as omitted
    # rather than silently dropped.
    assert result["release_notes_omitted"] == len(releases) - updater.MAX_RELEASE_NOTES


def test_releases_are_ordered_by_version_not_publish_date(monkeypatch):
    """Same rule fetch_latest_release() has always followed, now applied to the whole
    list: republishing an old release must not reorder the changelog."""
    releases = [_release("v1.7.0"), _release("v1.9.0"), _release("v1.8.0")]
    monkeypatch.setattr(updater.requests, "get",
                        lambda *a, **k: FakeResponse(json_data=releases))
    assert [r["version"] for r in updater.fetch_releases("stable")] == ["1.9.0", "1.8.0", "1.7.0"]


def test_a_failed_check_still_has_empty_note_fields(monkeypatch):
    """The About page reads these unconditionally - a network failure must degrade to
    "couldn't check", not to a template error."""
    def boom(channel=None):
        raise updater.UpdateError("no network")
    monkeypatch.setattr(updater, "fetch_releases", boom)
    result = updater.check_for_update("stable")
    assert result["ok"] is False
    assert result["release_notes"] == [] and result["current_notes"] is None
