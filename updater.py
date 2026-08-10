"""
updater.py — Self-update: check GitHub for a newer release, download it, verify it,
replace this app's own files, and roll back if any of that goes wrong.

This module is the single implementation. Both entry points are thin wrappers:

  * `update.py`      - the standalone CLI, usable over SSH when the web UI is broken.
  * `/admin/about`   - the in-app "Update now" button (app.py).

Nothing here imports app.py (app.py imports this), and nothing here touches Flask -
so the CLI works on a portal that won't even start.

--------------------------------------------------------------------------------
Security posture - read this before changing anything below
--------------------------------------------------------------------------------
Auto-update is the largest security surface this app has: it turns "someone got into
the admin panel" into persistent arbitrary code execution on the host. The controls
here are deliberate, and each one is load-bearing:

  * The repository is a **module constant**, never configurable - not from the admin
    UI, not from an env var, not from a CLI flag. A configurable update source would
    let anyone who can write a setting point the portal at their own "release".
  * Every request is HTTPS with certificate verification left on. Disabling
    certificate verification must never appear in this file - there is a test that
    fails if it ever does.
  * Every URL actually fetched is re-validated against a host allow-list *after* the
    API hands it to us, because `browser_download_url` arrives over the network and
    is therefore untrusted input, not a constant.
  * Downloads are size-capped and integrity-checked (see `_download_asset`).
  * The set of files replaced comes from the release archive's own member list -
    a whitelist by construction - and is additionally checked against a hard
    deny-list (`instance/`, `.env`, `static/uploads/`) which aborts the whole update
    if a release archive ever contains one of those.

What integrity verification here does and does not buy you is documented on
`_download_asset()`. Short version: it protects the transfer, not the publisher.
"""
import hashlib
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import zipfile
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

import config
import db

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Where updates come from - NOT configurable, on purpose (see module docstring)
# ---------------------------------------------------------------------------
GITHUB_OWNER = "Adam4125-officiel"
GITHUB_REPO = "Status-Portal"
RELEASES_API_URL = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases"
REPO_URL = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}"
RELEASES_PAGE_URL = f"{REPO_URL}/releases"

# Hosts a release download is allowed to actually come from. GitHub serves release
# assets by redirecting api.github.com/github.com to its object storage, so the
# redirect targets have to be here too. Checked against the initial URL *and* the
# final URL after redirects - `requests` follows redirects transparently, and a
# redirect chain is attacker-influenced input the moment any hop is compromised.
ALLOWED_DOWNLOAD_HOSTS = {
    "api.github.com",
    "github.com",
    "codeload.github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}

CHANNELS = ("stable", "unstable")
DEFAULT_CHANNEL = "stable"

API_TIMEOUT_SECONDS = 15
DOWNLOAD_TIMEOUT_SECONDS = 120

# Hard caps so a hostile or broken response can't fill the disk. A release of this
# project is well under 1 MB; these are three orders of magnitude of headroom.
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 200 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 5000

# Paths an update must never write to, whatever the archive claims. These are all
# gitignored and so cannot legitimately appear in a `git archive` zip - if one ever
# does, the release was built wrong and the update is aborted rather than trusted.
PROTECTED_PREFIXES = ("instance/", "static/uploads/")
PROTECTED_FILES = (".env",)

INSTANCE_DIR = os.path.join(config.APP_ROOT, "instance")
BACKUP_ROOT = os.path.join(INSTANCE_DIR, "update_backups")
PENDING_MARKER_PATH = os.path.join(INSTANCE_DIR, "update_pending.json")
KEEP_BACKUPS = 5

# Windows can refuse a rename while another process still has the destination open
# (an editor, an antivirus scanner, or this very app streaming a static file). Not a
# thing on POSIX, where a rename over an open file always succeeds - but harmless to
# retry there too, so there's one code path rather than an os.name branch.
REPLACE_RETRY_ATTEMPTS = 5
REPLACE_RETRY_DELAY_SECONDS = 0.5


class UpdateError(Exception):
    """Any expected, explainable failure - a network error, a bad archive, a refused
    precondition. Raised with a message meant to be shown to the admin verbatim."""


# ---------------------------------------------------------------------------
# Version handling
# ---------------------------------------------------------------------------
def parse_version(text):
    """'v1.5.0-rc.2' -> (1, 5, 0, 0, 2); 'v1.5.0' -> (1, 5, 0, 1, 0).

    The 4th element is the release/prerelease rank, which is what makes
    1.5.0-rc.2 < 1.5.0 come out right (a prerelease sorts *below* the final
    release of the same number, and above the previous release). Anything
    unparseable sorts at the very bottom rather than raising, so a malformed tag
    on a release can never be mistaken for "newer than what you're running"."""
    if not text:
        return (0, 0, 0, 0, 0)
    raw = str(text).strip().lstrip("vV")
    prerelease_number = 0
    is_final = 1
    for separator in ("-rc.", "-rc", "-"):
        if separator in raw:
            raw, _, suffix = raw.partition(separator)
            is_final = 0
            digits = "".join(ch for ch in suffix if ch.isdigit())
            prerelease_number = int(digits) if digits else 0
            break
    parts = raw.split(".")
    numbers = []
    for part in parts[:3]:
        digits = "".join(ch for ch in part if ch.isdigit())
        numbers.append(int(digits) if digits else 0)
    while len(numbers) < 3:
        numbers.append(0)
    return (numbers[0], numbers[1], numbers[2], is_final, prerelease_number)


def current_version():
    return config.VERSION


# ---------------------------------------------------------------------------
# Channel (a routine admin toggle -> DB setting, per this project's config split)
# ---------------------------------------------------------------------------
def _read_setting(key, default):
    """Reads a setting without ever creating or touching a database that isn't
    already there.

    Both guards matter for the standalone CLI specifically - it is the tool you
    reach for when things are broken, including possibly the database itself, and
    `sqlite3.connect()` silently *creates* an empty file for a path that doesn't
    exist. Running `update.py check` on a fresh install must not leave a stray
    zero-table portal.db behind for init_db() to trip over later."""
    if not os.path.isfile(db.DB_PATH):
        return default
    try:
        return db.get_setting(key, default)
    except Exception:
        _logger.warning("Could not read the '%s' setting; assuming %r", key, default)
        return default


def get_channel():
    value = _read_setting("update_channel", DEFAULT_CHANNEL)
    return value if value in CHANNELS else DEFAULT_CHANNEL


def set_channel(channel):
    if channel not in CHANNELS:
        raise UpdateError(f"Unknown channel '{channel}' (expected one of: {', '.join(CHANNELS)}).")
    if not os.path.isfile(db.DB_PATH):
        raise UpdateError(
            f"No database at {db.DB_PATH} yet - start the portal once, then set the channel "
            "(or change it from /admin/about).")
    try:
        db.set_setting("update_channel", channel)
    except Exception as e:
        raise UpdateError(f"Could not save the channel setting: {e}")


def update_check_enabled():
    """Whether the background thread is allowed to poll GitHub at all. Off means the
    About page only ever checks when the admin presses "Check now" - for someone who
    would rather their home server not make a periodic outbound call."""
    return _read_setting("update_check_enabled", "1") != "0"


# ---------------------------------------------------------------------------
# Talking to GitHub
# ---------------------------------------------------------------------------
def _validate_download_url(url, what):
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise UpdateError(f"Refusing to fetch {what} over '{parsed.scheme}' - HTTPS only.")
    if parsed.hostname not in ALLOWED_DOWNLOAD_HOSTS:
        raise UpdateError(f"Refusing to fetch {what} from unexpected host '{parsed.hostname}'.")


def _release_asset(release):
    """The release zip built by the release process (`status-portal-vX.Y.Z.zip`).

    Falls back to the tag's auto-generated zipball if a release has no uploaded
    asset. The fallback is still pinned to a *tag* in this repo (so it still has a
    version identity, which is what the whole check/compare/rollback story depends
    on), but it carries no published size or digest, so integrity verification is
    weaker for it - see `_download_asset`."""
    for asset in release.get("assets") or []:
        name = (asset.get("name") or "").lower()
        if name.endswith(".zip") and asset.get("browser_download_url"):
            return {
                "url": asset["browser_download_url"],
                "name": asset.get("name"),
                "size": asset.get("size"),
                "digest": asset.get("digest"),
                "kind": "asset",
            }
    if release.get("zipball_url"):
        return {
            "url": release["zipball_url"],
            "name": f"{release.get('tag_name')}.zip",
            "size": None,
            "digest": None,
            "kind": "zipball",
        }
    return None


def fetch_latest_release(channel=None):
    """The highest-versioned release on the given channel, as a plain dict.

    stable   -> non-prerelease GitHub releases only.
    unstable -> prereleases too (this project's own `-rc.N` releases).

    Note this picks by *version*, not by publish date: republishing an old release
    must never look like an update, and a prerelease of an older line must never
    outrank a newer stable one."""
    channel = channel or get_channel()
    if channel not in CHANNELS:
        channel = DEFAULT_CHANNEL
    try:
        response = requests.get(
            RELEASES_API_URL,
            params={"per_page": 30},
            headers={"Accept": "application/vnd.github+json",
                     "User-Agent": f"status-portal/{current_version()}"},
            timeout=API_TIMEOUT_SECONDS,
            verify=True,  # never disable; stated explicitly so a future edit has to be deliberate
        )
        response.raise_for_status()
        releases = response.json()
    except requests.RequestException as e:
        raise UpdateError(f"Could not reach GitHub: {e}")
    except ValueError as e:
        raise UpdateError(f"GitHub returned something that isn't JSON: {e}")

    if not isinstance(releases, list):
        raise UpdateError("Unexpected response from the GitHub releases API.")

    candidates = []
    for release in releases:
        if not isinstance(release, dict) or release.get("draft"):
            continue
        if channel == "stable" and release.get("prerelease"):
            continue
        tag = release.get("tag_name")
        if not tag:
            continue
        candidates.append(release)

    if not candidates:
        raise UpdateError(f"No {channel} release found in this repository yet.")

    latest = max(candidates, key=lambda r: parse_version(r.get("tag_name")))
    return {
        "tag": latest.get("tag_name"),
        "version": str(latest.get("tag_name") or "").lstrip("vV"),
        "name": latest.get("name") or latest.get("tag_name"),
        "prerelease": bool(latest.get("prerelease")),
        "published_at": latest.get("published_at"),
        "html_url": latest.get("html_url") or RELEASES_PAGE_URL,
        "body": latest.get("body") or "",
        "asset": _release_asset(latest),
        "channel": channel,
    }


def check_for_update(channel=None):
    """Compares the running version against the latest on the channel. Never raises
    for a network failure - returns an `ok: False` result with an `error` string, so
    every caller (About page included) degrades to "couldn't check" instead of
    breaking."""
    channel = channel or get_channel()
    result = {
        "ok": False,
        "error": None,
        "channel": channel,
        "current": current_version(),
        "current_display": config.VERSION_DISPLAY,
        "latest": None,
        "latest_url": RELEASES_PAGE_URL,
        "published_at": None,
        "prerelease": None,
        "update_available": False,
        "ahead": False,
        "checked_at": db.now_iso(),
    }
    try:
        release = fetch_latest_release(channel)
    except UpdateError as e:
        result["error"] = str(e)
        return result

    comparison = _compare(current_version(), release["version"])
    result.update({
        "ok": True,
        "latest": release["version"],
        "latest_tag": release["tag"],
        "latest_url": release["html_url"],
        "latest_name": release["name"],
        "published_at": release["published_at"],
        "prerelease": release["prerelease"],
        "update_available": comparison < 0,
        # Running something newer than anything published on this channel - normal on
        # a dev checkout, and on a stable-channel box that was handed a prerelease.
        # Reported as its own state rather than being flattened into "up to date",
        # which would be misleading.
        "ahead": comparison > 0,
    })
    return result


def _compare(current, latest):
    a, b = parse_version(current), parse_version(latest)
    return (a > b) - (a < b)


# ---------------------------------------------------------------------------
# Cached update check (read by request handlers - never checked inline)
# ---------------------------------------------------------------------------
# Same rule and same shape as app.py's _integration_status_cache: an outbound HTTP
# call must never happen inside a Flask request handler. The background health-check
# loop calls refresh_update_cache_if_stale() on every tick; it no-ops until the TTL
# has elapsed, so the loop's own (much shorter) interval doesn't turn into a GitHub
# request every 2 minutes.
_update_cache = {"result": None, "refreshed_monotonic": None}


def get_cached_update_status():
    """Reads the cache only. None means nothing has been checked yet this process."""
    return _update_cache["result"]


def refresh_update_cache(channel=None):
    result = check_for_update(channel)
    _update_cache["result"] = result
    _update_cache["refreshed_monotonic"] = time.monotonic()
    return result


def refresh_update_cache_if_stale(ttl_seconds=None, force=False, channel=None):
    if not force and not update_check_enabled():
        return None
    ttl = config.UPDATE_CHECK_INTERVAL_SECONDS if ttl_seconds is None else ttl_seconds
    last = _update_cache["refreshed_monotonic"]
    if not force and last is not None and (time.monotonic() - last) < ttl:
        return _update_cache["result"]
    return refresh_update_cache(channel)


# ---------------------------------------------------------------------------
# Downloading and verifying
# ---------------------------------------------------------------------------
def _download_asset(asset, progress):
    """Downloads the release archive into memory and verifies it before a single
    byte is written anywhere near the app directory.

    Three independent checks:
      1. HTTPS with certificate verification (never disabled), and the URL's host
         re-validated against ALLOWED_DOWNLOAD_HOSTS both before the request and
         after redirects have been followed.
      2. A hard byte cap while streaming, so a hostile or runaway response can't
         fill the disk.
      3. The downloaded bytes checked against the size and (when GitHub publishes
         one) the SHA-256 digest the releases API declared for that asset.

    WHAT THIS PROTECTS AGAINST: a corrupted, truncated, or partially-written
    download; a proxy or CDN mangling the body; and - via TLS certificate
    verification - a network attacker substituting content in transit.

    WHAT THIS DOES NOT PROTECT AGAINST: a malicious or backdoored release. The
    size and digest come from the same API as the bytes themselves, so whoever can
    publish a release can publish matching metadata; this is an integrity check on
    the transfer, not an authenticity check on the publisher. Real publisher
    authenticity would need a detached signature verified against a public key
    shipped with the app and not derived from the download. It equally does not
    protect against an attacker who already controls the admin panel - see
    config.ENABLE_INAPP_UPDATE for the control that addresses that one."""
    url = asset["url"]
    _validate_download_url(url, "the release archive")
    progress(f"Downloading {asset.get('name') or 'release archive'}...")
    try:
        response = requests.get(
            url,
            stream=True,
            timeout=DOWNLOAD_TIMEOUT_SECONDS,
            headers={"Accept": "application/octet-stream",
                     "User-Agent": f"status-portal/{current_version()}"},
            verify=True,
        )
        response.raise_for_status()
        # requests followed any redirects itself; the URL that actually served the
        # bytes is the one that matters, so it gets the same host check.
        _validate_download_url(response.url, "the redirected release archive")
        buffer = io.BytesIO()
        total = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_DOWNLOAD_BYTES:
                raise UpdateError(
                    f"Download exceeded the {MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB safety cap - aborting.")
            buffer.write(chunk)
    except requests.RequestException as e:
        raise UpdateError(f"Download failed: {e}")

    data = buffer.getvalue()
    digest = hashlib.sha256(data).hexdigest()
    progress(f"Downloaded {len(data)} bytes (sha256 {digest[:16]}...).")

    expected_size = asset.get("size")
    if expected_size:
        if len(data) != expected_size:
            raise UpdateError(
                f"Size mismatch: GitHub declared {expected_size} bytes, got {len(data)}. Aborting.")
        progress(f"Size verified against the release metadata ({expected_size} bytes).")

    expected_digest = (asset.get("digest") or "").strip().lower()
    if expected_digest.startswith("sha256:"):
        if digest != expected_digest.split(":", 1)[1]:
            raise UpdateError("SHA-256 mismatch against the release metadata. Aborting.")
        progress("SHA-256 verified against the release metadata.")
    elif not expected_size:
        # The zipball fallback publishes neither. Say so plainly instead of letting
        # "verified" be assumed - the transfer is still TLS-protected and tag-pinned,
        # there is just nothing independent to compare the bytes against.
        progress("NOTE: this release publishes no size or checksum "
                 "(auto-generated zipball) - only TLS and the tag pin the content.")
    return data, digest


# ---------------------------------------------------------------------------
# Archive inspection
# ---------------------------------------------------------------------------
def _archive_members(zf):
    """Every regular file in the archive, as (member, relative_destination_path).

    Handles both archive shapes this project can produce: `git archive` (files at
    the root, which is what the release process builds) and GitHub's auto-generated
    zipball (everything under one `owner-repo-sha/` directory, stripped here).

    Every path is validated: no absolute paths, no drive letters, no `..`, nothing
    that escapes the app directory (zip-slip), and nothing under a protected path.
    The release zip is our own and none of these should ever trigger - which is
    exactly why they abort the update loudly instead of being skipped quietly."""
    infos = [i for i in zf.infolist() if not i.is_dir()]
    if len(infos) > MAX_ARCHIVE_MEMBERS:
        raise UpdateError(f"Archive has {len(infos)} files, over the {MAX_ARCHIVE_MEMBERS} safety limit.")
    total_uncompressed = sum(i.file_size for i in infos)
    if total_uncompressed > MAX_UNCOMPRESSED_BYTES:
        raise UpdateError(
            f"Archive expands to {total_uncompressed} bytes, over the "
            f"{MAX_UNCOMPRESSED_BYTES // (1024 * 1024)} MB safety limit.")
    if not infos:
        raise UpdateError("The release archive is empty.")

    # Strip a single common top-level directory if (and only if) every member shares
    # it - true for a GitHub zipball, false for a `git archive` zip.
    first_segments = {name.split("/")[0] for name in (i.filename for i in infos) if "/" in name}
    root_only = [i.filename for i in infos if "/" not in i.filename]
    strip_prefix = None
    if len(first_segments) == 1 and not root_only:
        strip_prefix = next(iter(first_segments)) + "/"

    members = []
    for info in infos:
        name = info.filename
        if strip_prefix and name.startswith(strip_prefix):
            name = name[len(strip_prefix):]
        name = name.replace("\\", "/").lstrip("/")
        if not name:
            continue
        if os.path.isabs(name) or ":" in name.split("/")[0]:
            raise UpdateError(f"Archive contains an absolute path ('{info.filename}') - aborting.")
        if ".." in name.split("/"):
            raise UpdateError(f"Archive contains a parent-directory path ('{info.filename}') - aborting.")
        if name in PROTECTED_FILES or name.startswith(PROTECTED_PREFIXES):
            raise UpdateError(
                f"Archive contains a protected path ('{name}') that an update must never overwrite. "
                "This means the release was built wrong - aborting without touching anything.")
        destination = os.path.normpath(os.path.join(config.APP_ROOT, name))
        if not (destination == config.APP_ROOT or destination.startswith(config.APP_ROOT + os.sep)):
            raise UpdateError(f"Archive member '{info.filename}' escapes the app directory - aborting.")
        members.append((info, name))
    return members


def _version_from_archive(zf, members):
    """Reads the VERSION file out of the archive - the incoming release's own idea of
    what it is, without importing any of its code."""
    for info, name in members:
        if name == "VERSION":
            try:
                return zf.read(info).decode("utf-8").strip()
            except Exception:
                return None
    return None


# ---------------------------------------------------------------------------
# Applying, backing up, rolling back
# ---------------------------------------------------------------------------
def _atomic_write(destination, data, mode=None):
    """Write to a sibling temp file, then rename over the destination.

    The rename is the only step that touches the real path, so a crash mid-write
    leaves the old file intact rather than a truncated one. On Windows the rename
    itself can still fail while another process holds the destination open (an
    editor, an antivirus scanner, or this app streaming a static file to a browser),
    hence the retry - and if it still fails, the caller rolls the whole update back
    rather than leaving a half-updated tree."""
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    temp_path = destination + ".update-tmp"
    with open(temp_path, "wb") as f:
        f.write(data)
    if mode and os.name != "nt":
        try:
            os.chmod(temp_path, mode)
        except OSError:
            pass
    last_error = None
    for attempt in range(REPLACE_RETRY_ATTEMPTS):
        try:
            os.replace(temp_path, destination)
            return
        except OSError as e:
            last_error = e
            time.sleep(REPLACE_RETRY_DELAY_SECONDS * (attempt + 1))
    try:
        os.remove(temp_path)
    except OSError:
        pass
    raise UpdateError(
        f"Could not replace '{os.path.relpath(destination, config.APP_ROOT)}' after "
        f"{REPLACE_RETRY_ATTEMPTS} attempts: {last_error}. "
        "On Windows this usually means another program has the file open.")


def _make_backup(members, from_version, to_version, channel):
    """Copies every file that is about to be overwritten into a timestamped folder
    under instance/ (which updates never touch, so a backup can't be clobbered by
    the next one), plus a manifest recording which files were *added* so a rollback
    can remove them again rather than leaving them behind."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup_dir = os.path.join(BACKUP_ROOT, f"{stamp}-{from_version}-to-{to_version}")
    os.makedirs(backup_dir, exist_ok=True)
    replaced, added = [], []
    for _info, name in members:
        source = os.path.join(config.APP_ROOT, name)
        if os.path.isfile(source):
            target = os.path.join(backup_dir, name)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(source, target)
            replaced.append(name)
        else:
            added.append(name)
    manifest = {
        "created_at": db.now_iso(),
        "from_version": from_version,
        "to_version": to_version,
        "channel": channel,
        "replaced": replaced,
        "added": added,
    }
    with open(os.path.join(backup_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    return backup_dir, manifest


def _prune_backups(keep=None):
    keep = KEEP_BACKUPS if keep is None else keep
    try:
        entries = sorted(d for d in os.listdir(BACKUP_ROOT)
                         if os.path.isdir(os.path.join(BACKUP_ROOT, d)))
    except OSError:
        return
    for stale in entries[:-keep] if len(entries) > keep else []:
        shutil.rmtree(os.path.join(BACKUP_ROOT, stale), ignore_errors=True)


def list_backups():
    """Newest last. Each entry is the manifest plus its directory name."""
    results = []
    try:
        names = sorted(os.listdir(BACKUP_ROOT))
    except OSError:
        return results
    for name in names:
        manifest_path = os.path.join(BACKUP_ROOT, name, "manifest.json")
        if not os.path.isfile(manifest_path):
            continue
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except (OSError, ValueError):
            continue
        manifest["name"] = name
        manifest["path"] = os.path.join(BACKUP_ROOT, name)
        results.append(manifest)
    return results


def rollback(backup_name=None, progress=None):
    """Restores a backup over the app directory: replaced files copied back, added
    files deleted. Defaults to the most recent backup.

    This is the recovery path for the one failure mode nothing inside the app can
    handle on its own - see `write_pending_marker()` for why."""
    progress = progress or _default_progress
    backups = list_backups()
    if not backups:
        raise UpdateError("No update backups found - nothing to roll back to.")
    if backup_name:
        chosen = next((b for b in backups if b["name"] == backup_name), None)
        if not chosen:
            raise UpdateError(f"No backup named '{backup_name}'. Known: "
                              + ", ".join(b["name"] for b in backups))
    else:
        chosen = backups[-1]

    progress(f"Rolling back to {chosen['from_version']} (backup {chosen['name']})...")
    restored = removed = 0
    for name in chosen.get("replaced", []):
        source = os.path.join(chosen["path"], name)
        if not os.path.isfile(source):
            progress(f"  ! missing from backup, skipping: {name}")
            continue
        with open(source, "rb") as f:
            _atomic_write(os.path.join(config.APP_ROOT, name), f.read())
        restored += 1
    for name in chosen.get("added", []):
        target = os.path.join(config.APP_ROOT, name)
        try:
            if os.path.isfile(target):
                os.remove(target)
                removed += 1
        except OSError as e:
            progress(f"  ! could not remove {name}: {e}")
    _clear_pending_marker()
    progress(f"Rolled back: {restored} file(s) restored, {removed} added file(s) removed.")
    progress("Restart the portal for the rolled-back code to take effect.")
    return {"backup": chosen["name"], "version": chosen["from_version"],
            "restored": restored, "removed": removed}


# ---------------------------------------------------------------------------
# The post-restart problem
# ---------------------------------------------------------------------------
def write_pending_marker(backup_name, to_version):
    """Records "an update was just applied and the app is about to restart into it".

    Be clear-eyed about what this does: it lets the *next successful start* confirm
    it came up on the new version, and it records which backup to roll back to. It
    cannot detect a failed start. Once os.execv replaces the process image, no code
    from the old version exists any more - if the new code dies on import, nothing
    of ours is running to notice or to undo it. That is a property of in-place
    restart, not an oversight: genuine automatic post-restart rollback requires a
    supervisor *outside* this process (a systemd unit with a health check, or a
    wrapper script), which this project deliberately doesn't ship because it would
    change how everyone launches the portal.

    So the honest split is:
      * every failure BEFORE the restart (download, verification, a file that won't
        replace, a failed dependency install) is rolled back automatically, in
        process, by perform_update();
      * the failure AFTER the restart is recovered manually and reliably with one
        command over SSH: `python update.py rollback`, which reads this marker to
        know exactly which backup to restore."""
    try:
        os.makedirs(INSTANCE_DIR, exist_ok=True)
        with open(PENDING_MARKER_PATH, "w", encoding="utf-8") as f:
            json.dump({"backup": backup_name, "to_version": to_version,
                       "from_version": current_version(), "written_at": db.now_iso()}, f, indent=2)
    except OSError:
        _logger.exception("Could not write the pending-update marker")


def _clear_pending_marker():
    try:
        if os.path.isfile(PENDING_MARKER_PATH):
            os.remove(PENDING_MARKER_PATH)
    except OSError:
        _logger.exception("Could not remove the pending-update marker")


def read_pending_marker():
    try:
        with open(PENDING_MARKER_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def check_pending_marker():
    """Called once at startup from every entry point (app.py, serve_waitress.py).

    If the running version matches what the update was aiming for, the restart
    worked - log it and clear the marker. If it doesn't, the process came back on
    the old code (a partially applied update, or a launcher pointing somewhere
    else): the marker is left in place and the problem logged loudly, so the admin
    finds it in instance/logs/app.log and `update.py rollback` still knows which
    backup to use."""
    marker = read_pending_marker()
    if not marker:
        return None
    expected = str(marker.get("to_version") or "")
    if parse_version(expected) == parse_version(current_version()):
        _logger.info("Update to %s completed - the app restarted successfully on the new version. "
                     "Backup kept at instance/update_backups/%s", current_version(), marker.get("backup"))
        _clear_pending_marker()
        return {"status": "confirmed", "version": current_version(), "backup": marker.get("backup")}
    _logger.error(
        "An update to %s was applied but this process is running %s. The update may not have taken "
        "effect. Roll back with: python update.py rollback --to %s",
        expected, current_version(), marker.get("backup"))
    return {"status": "mismatch", "expected": expected, "running": current_version(),
            "backup": marker.get("backup")}


# ---------------------------------------------------------------------------
# The update itself
# ---------------------------------------------------------------------------
def _default_progress(message):
    print(message, flush=True)
    _logger.info("[update] %s", message)


def _install_dependencies(progress):
    """Runs pip against the *new* requirements.txt. Failure here is recoverable
    precisely because it happens before the restart - the caller rolls the code back
    rather than restarting into a version whose dependencies aren't installed."""
    progress("requirements.txt changed - installing dependencies...")
    command = [sys.executable, "-m", "pip", "install", "-r",
               os.path.join(config.APP_ROOT, "requirements.txt")]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=600)
    except Exception as e:
        raise UpdateError(f"Could not run pip: {e}")
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-5:]
        raise UpdateError("pip install failed:\n" + "\n".join(tail))
    progress("Dependencies installed.")


def perform_update(channel=None, force=False, install_deps=True, allow_dev_checkout=False,
                   progress=None):
    """Download, verify, back up, and replace. Returns a result dict; raises
    UpdateError with an explainable message on any refused precondition or failure.

    Safe to run twice and safe to run when already up to date: the version check
    short-circuits with `applied: False` unless `force` is set, and the whole
    file-replacement step is idempotent (the same archive written twice produces the
    same tree).

    Does NOT restart anything - that's the caller's call, because the CLI can't know
    how the portal is launched and the web route wants to use the app's own existing
    in-place restart."""
    progress = progress or _default_progress
    channel = channel or get_channel()

    if config.IS_GIT_CHECKOUT and not allow_dev_checkout:
        raise UpdateError(
            "This looks like a git checkout (.git is present), not an extracted release. "
            "Updating would overwrite tracked files and silently clobber uncommitted work. "
            "Use `git pull` instead, or pass --allow-dev-checkout if you really mean it.")

    progress(f"Current version: {config.VERSION_DISPLAY}  (channel: {channel})")
    release = fetch_latest_release(channel)
    progress(f"Latest {channel} release: {release['version']}"
             + (" (prerelease)" if release["prerelease"] else ""))

    comparison = _compare(current_version(), release["version"])
    if comparison >= 0 and not force:
        state = "already up to date" if comparison == 0 else \
            f"running {current_version()}, which is newer than the latest {channel} release"
        progress(f"Nothing to do - {state}.")
        return {"applied": False, "reason": state, "current": current_version(),
                "latest": release["version"], "channel": channel}

    if not release["asset"]:
        raise UpdateError(f"Release {release['version']} has no downloadable archive.")

    data, digest = _download_asset(release["asset"], progress)

    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise UpdateError(f"The downloaded file is not a valid zip archive: {e}")

    with zf:
        members = _archive_members(zf)
        archive_version = _version_from_archive(zf, members) or release["version"]
        progress(f"Archive verified: {len(members)} file(s), version {archive_version}.")

        old_requirements = _read_file_bytes(os.path.join(config.APP_ROOT, "requirements.txt"))

        backup_dir, manifest = _make_backup(members, current_version(), archive_version, channel)
        progress(f"Backed up {len(manifest['replaced'])} existing file(s) to "
                 f"instance/update_backups/{os.path.basename(backup_dir)}")

        written = 0
        try:
            for info, name in members:
                mode = (info.external_attr >> 16) & 0o7777
                _atomic_write(os.path.join(config.APP_ROOT, name), zf.read(info), mode or None)
                written += 1
        except Exception as e:
            # A half-updated tree is worse than either version, so any failure part
            # way through unwinds everything before returning.
            progress(f"FAILED after writing {written} file(s): {e}")
            progress("Rolling back automatically...")
            try:
                rollback(os.path.basename(backup_dir), progress)
            except Exception:
                _logger.exception("Automatic rollback failed")
                raise UpdateError(
                    f"Update failed ({e}) AND the automatic rollback failed. "
                    f"Restore by hand from instance/update_backups/{os.path.basename(backup_dir)}.")
            raise UpdateError(f"Update failed and was rolled back: {e}")

    progress(f"Replaced {written} file(s).")

    new_requirements = _read_file_bytes(os.path.join(config.APP_ROOT, "requirements.txt"))
    deps_changed = old_requirements != new_requirements
    if deps_changed and install_deps:
        try:
            _install_dependencies(progress)
        except UpdateError as e:
            progress(f"{e}")
            progress("Rolling back automatically - restarting now would start on code whose "
                     "dependencies aren't installed.")
            rollback(os.path.basename(backup_dir), progress)
            raise UpdateError(f"Dependency install failed and the update was rolled back: {e}")
    elif deps_changed:
        progress("NOTE: requirements.txt changed - run `pip install -r requirements.txt` "
                 "before restarting.")

    _prune_backups()
    progress(f"Updated {current_version()} -> {archive_version}. Restart the portal to run it.")
    return {
        "applied": True,
        "current": current_version(),
        "latest": archive_version,
        "channel": channel,
        "files_written": written,
        "backup": os.path.basename(backup_dir),
        "deps_changed": deps_changed,
        "sha256": digest,
        "release_url": release["html_url"],
    }


def _read_file_bytes(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None
