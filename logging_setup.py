"""
logging_setup.py — Persistent crash/error logging.

Everything in this app used to log via bare print() - fine for watching a
foreground terminal, but a crashed background thread or an unhandled request
exception left no trace once the terminal scrolled past or the terminal was
closed. init_logging() configures Python's standard logging module once at
startup: a rotating file under instance/logs/ (the same gitignored directory
as the DB) plus the console, so python app.py / python serve_waitress.py keep
their live output but also get a durable record to check after the fact.

Only called from app.py/serve_waitress.py's __main__ blocks - never at plain
import time, so running the test suite (which imports these modules but never
starts the background threads or the dev/production server) doesn't create
log files as a side effect.
"""
import datetime
import logging
import logging.handlers
import os
import re
import sys
import threading

import config

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "instance", "logs")
LOG_FILE = os.path.join(LOG_DIR, "app.log")

# Rotation is by *day*, not by size (changed 2026-09-03). Size-based rotation kept
# 2 MB x 3 files, which on a quiet portal is months of history - so the log page's
# first screen was full of entries from weeks ago, and "what happened this morning"
# meant scrolling past all of it. A day is a unit a person actually reasons in
# ("check yesterday's log"), and config.LOG_RETENTION_DAYS then means something
# obvious rather than translating into an unknown span of time.
#
# Rotating on every start was the other candidate and is worse here: this app
# restarts itself (the in-app updater, /admin/system, a systemd restart), so a
# crash-restart loop would blow through every retained file and delete exactly the
# history that explains the crash.
LOG_BASENAME = "app.log"

# app.log is current; TimedRotatingFileHandler names the rest app.log.YYYY-MM-DD. The
# trailing "\.\d+" alternative matches the files the old size-based handler left
# behind on an existing install - they are still readable and downloadable, they just
# stop being produced. Nothing outside this pattern is ever listed, read or offered
# for download, whatever else happens to be sitting in instance/logs/.
_LOG_NAME = re.compile(r"^" + re.escape(LOG_BASENAME) + r"(?:\.(\d{4}-\d{2}-\d{2})|\.(\d+))?$")

# How much of the end of a file the tail reader will look at. A log line here is
# well under 200 bytes, so this comfortably covers the largest page size offered
# while putting a hard ceiling on the work a request can ask for - the whole point
# of tailing rather than reading the file.
TAIL_MAX_BYTES = 512 * 1024

# The start of a new log entry, as init_logging()'s formatter writes it:
# "2026-09-03 14:05:01,123 WARNING [discord_bot] ...". Anything not matching is a
# continuation line - a traceback body, most importantly - and belongs to the entry
# above it rather than being an entry of its own.
_ENTRY_START = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} "
    r"(DEBUG|INFO|WARNING|ERROR|CRITICAL) ")

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

# Terminal colour codes, stripped when a log is *rendered* (never from the file
# itself or from a download - those stay byte-for-byte what was written). Some
# libraries colour their own output for a console and don't know one of our handlers
# writes to a file: werkzeug's "WARNING: This is a development server" arrives as
# "\x1b[31m\x1b[1mWARNING...", which a browser shows as literal escape gibberish in
# the middle of the line. Found by reading the real log file, not by a test.
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

_configured = False


def init_logging():
    """Idempotent - safe to call from more than one entry point/more than once."""
    global _configured
    if _configured:
        return
    _configured = True

    os.makedirs(LOG_DIR, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")

    file_handler = logging.handlers.TimedRotatingFileHandler(
        LOG_FILE, when="midnight", backupCount=config.LOG_RETENTION_DAYS, encoding="utf-8")
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    threading.excepthook = _log_thread_exception

    # A visible marker for where one run of the portal begins. Rotation is by day, so
    # a single file routinely spans several restarts; without this, working out which
    # entries belong to the run you are debugging means inferring it from timestamps.
    logging.getLogger(__name__).info(
        "--- portal starting (version %s, keeping %s day(s) of logs) ---",
        getattr(config, "VERSION_DISPLAY", "?"), config.LOG_RETENTION_DAYS)


def _log_thread_exception(args):
    """A background thread (health-check loop, monitoring's Windows refresh, the
    Discord bot) that dies from something outside its own try/except would
    otherwise vanish silently - the rest of the app keeps running, but that one
    thread's work just stops with zero trace anywhere. This is the failure mode
    most likely to confuse the user ("services just stopped updating, no error
    anywhere"), so it's logged explicitly here rather than relying only on
    threading's default stderr dump."""
    name = args.thread.name if args.thread else "unknown-thread"
    logging.getLogger(name).critical(
        "Uncaught exception - this thread is now dead",
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback))


# ---------------------------------------------------------------------------
# Reading the logs back (the /admin/logs page)
# ---------------------------------------------------------------------------
# These are deliberately here rather than in app.py: this module already owns where
# the log files live and how they are named, and that is exactly the knowledge a
# reader needs. It also keeps them testable without a Flask app.
#
# Every read is bounded. A caller asks for at most N entries and the tail reader
# looks at no more than TAIL_MAX_BYTES from the end of one file, so this is the same
# class of cheap local file work as updater.list_backups() or asset_url()'s
# getmtime() - not the slow outbound I/O the "never in a request handler" rule is
# about. Don't turn it into a cache; the whole value of a log page is that it shows
# what is in the file right now.


def _log_sort_key(name):
    """Newest first: the current file, then dated backups most-recent-first, then any
    files the old size-based handler left behind (.1 is newer than .3)."""
    match = _LOG_NAME.match(name)
    dated, legacy = match.group(1), match.group(2)
    if dated:
        return (1, [-int(part) for part in dated.split("-")], 0)
    if legacy:
        return (2, [], int(legacy))
    return (0, [], 0)


def log_files():
    """Every log file we wrote, newest first, as {name, path, size_bytes, label}.

    The directory listing is filtered through _LOG_NAME, so a file that merely
    happens to sit in instance/logs/ is never listed - and therefore never
    downloadable - just because it is there. A download request's name is checked
    against *this* list rather than being joined onto a path (see log_file_path), so
    nothing a caller sends ever reaches the filesystem.
    """
    try:
        names = [n for n in os.listdir(LOG_DIR) if _LOG_NAME.match(n)]
    except OSError:
        return []
    files = []
    for name in sorted(names, key=_log_sort_key):
        path = os.path.join(LOG_DIR, name)
        try:
            size = os.path.getsize(path)
        except OSError:
            continue  # rotated away between listing and reading
        match = _LOG_NAME.match(name)
        files.append({"name": name, "path": path, "size_bytes": size,
                       "label": match.group(1) or ("older" if match.group(2) else "current")})
    return files


def log_file_path(name):
    """The path for a download request's file name, or None if it isn't one of ours.

    Membership in log_files() is the whole check: the name is compared against a
    generated list, never joined onto a directory and hoped for. That makes path
    traversal structurally impossible here rather than something to filter out.
    """
    for entry in log_files():
        if entry["name"] == name:
            return entry["path"]
    return None


def read_tail(path, max_bytes=TAIL_MAX_BYTES):
    """The last max_bytes of a file, decoded leniently.

    Seeks from the end rather than reading the file: the current log can be 2 MB and
    a page only ever shows the end of it. errors="replace" because a log file can be
    cut mid-character by rotation, and a page that fails to render is worse than one
    showing a replacement character.
    """
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                blob = fh.read()
                # The seek almost certainly landed mid-line; that partial first line
                # would render as a truncated entry, so drop it.
                blob = blob.split(b"\n", 1)[1] if b"\n" in blob else b""
            else:
                blob = fh.read()
    except OSError:
        return ""
    return blob.decode("utf-8", errors="replace")


def parse_entries(text):
    """Groups raw log text into entries: one dict per logged event, with any
    continuation lines (a traceback, most often) kept attached to the entry they
    belong to, and terminal colour codes stripped.

    Filtering by level line-by-line would split a traceback away from the ERROR that
    produced it and then drop it, which is precisely the case someone opens a log
    page for."""
    entries = []
    for line in _ANSI.sub("", text).splitlines():
        match = _ENTRY_START.match(line)
        if match:
            entries.append({"level": match.group(1), "text": line})
        elif entries:
            entries[-1]["text"] += "\n" + line
        else:
            # Text before the first recognisable entry - a partial line, or output
            # from something that doesn't use our formatter. Shown, not swallowed.
            entries.append({"level": "", "text": line})
    return entries


def tail_entries(path=None, limit=200, min_level=None):
    """The last `limit` entries of one log file, oldest first, optionally only those
    at or above `min_level`.

    Oldest-first because that is the order a log reads in, and the page scrolls to
    the bottom - reversing it would make a multi-line traceback read backwards."""
    entries = parse_entries(read_tail(path or LOG_FILE))
    if min_level and min_level in LEVELS:
        threshold = LEVELS.index(min_level)
        entries = [e for e in entries
                    if e["level"] in LEVELS and LEVELS.index(e["level"]) >= threshold]
    return entries[-limit:] if limit else entries


def iter_all_log_bytes(chunk_size=64 * 1024):
    """Every log file we have, oldest first, as one stream of bytes - what "download
    the full log" means once rotation has happened.

    A generator rather than one joined blob: with the default rotation settings this
    is up to 8 MB, and a download route has no reason to hold that in memory at once.
    """
    for entry in reversed(log_files()):  # app.log.3 ... app.log.1, app.log
        try:
            with open(entry["path"], "rb") as fh:
                while True:
                    chunk = fh.read(chunk_size)
                    if not chunk:
                        break
                    yield chunk
        except OSError:
            continue  # rotated away or removed between listing and reading
