"""
db.py — The entire database layer (SQLite).
No ORM, just plain SQL to stay readable and easy to modify.
"""
import logging
import sqlite3
import os
from datetime import datetime, timedelta, timezone

_logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "instance", "portal.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def backup_to_file(dest_path):
    """Writes a consistent snapshot of the live database to dest_path via SQLite's
    own online backup API - safe to call while the background health-check thread
    (or anything else) is actively writing. A plain file copy of DB_PATH could catch
    a torn/partial write mid-transaction; Connection.backup() can't."""
    source = sqlite3.connect(DB_PATH)
    dest = sqlite3.connect(dest_path)
    with dest:
        source.backup(dest)
    source.close()
    dest.close()


# ---------------------------------------------------------------------------
# Restoring the database from a backup (see app.py's /admin/about/restore-db)
# ---------------------------------------------------------------------------
# Every SQLite file starts with this exact 16-byte string. Checked first because it
# rejects the overwhelmingly common mistake (a zip of the wrong thing, a text file
# renamed) instantly and without handing the bytes to SQLite at all.
SQLITE_HEADER = b"SQLite format 3\x00"

# Tables a file must contain before this app will accept it as *its own* backup. The
# header and an integrity check together only prove "a valid SQLite database" - which
# a Jellyfin library, a browser cookie store or an *Arr database all also are, and
# restoring one of those would silently wipe the portal and leave it unable to start.
RESTORE_REQUIRED_TABLES = ("services", "settings", "incidents", "maintenance_windows")


def validate_backup_file(path):
    """Returns None if `path` is a well-formed SQLite database that looks like this
    app's, otherwise a string explaining why not.

    Deliberately returns a reason rather than raising: the caller is a form handler
    whose entire job is to tell the admin what was wrong with their file, and the
    difference between "that isn't a database" and "that's a database, but not this
    app's" is exactly what they need to hear.

    Runs entirely against a *temporary copy*. Nothing here touches the live database,
    so a file that fails any check has cost nothing."""
    try:
        with open(path, "rb") as f:
            header = f.read(len(SQLITE_HEADER))
    except OSError as e:
        return f"Could not read the uploaded file: {e}"
    if header != SQLITE_HEADER:
        return "That file isn't a SQLite database (its header doesn't match)."

    conn = None
    try:
        # Read-only, and via a URI so SQLite cannot create or modify anything even if
        # the path is wrong - a validation step must never have side effects.
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        result = conn.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            detail = result[0] if result else "no result"
            return f"That database failed SQLite's integrity check ({detail})."
        names = {row[0] for row in
                 conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.DatabaseError as e:
        return f"That file couldn't be opened as a database: {e}"
    finally:
        if conn is not None:
            conn.close()

    missing = [t for t in RESTORE_REQUIRED_TABLES if t not in names]
    if missing:
        return ("That's a valid SQLite database, but it isn't a Status Portal backup - "
                f"it has no {', '.join(missing)} table(s).")
    return None


def restore_from_file(src_path):
    """Replaces the live database with `src_path`. Assumes it has already been
    validated - this function does the dangerous part, not the deciding.

    The WAL sidecars are the subtle bit. The database runs in WAL mode, so
    `portal.db-wal` holds committed pages belonging to the *old* database; leaving it
    in place next to a *new* main file would let SQLite replay one file's journal into
    another, which is a corrupt database rather than a failed restore. So: checkpoint
    the live database to fold its WAL back into the main file, replace the main file,
    then delete both sidecars.

    os.replace() is atomic on both platforms, so a crash mid-restore leaves either the
    old database or the new one - never half of either."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
    except sqlite3.DatabaseError:
        # A live database too broken to checkpoint is precisely when someone is
        # reaching for a restore. Pressing on is correct: the sidecars are removed
        # below either way, and the file is about to be replaced wholesale.
        _logger.warning("Could not checkpoint the database before restoring it", exc_info=True)

    os.replace(src_path, DB_PATH)
    for suffix in ("-wal", "-shm"):
        try:
            os.remove(DB_PATH + suffix)
        except FileNotFoundError:
            pass


def _ensure_column(conn, table, column, ddl):
    """Adds `column` to `table` if an existing database predates it - CREATE TABLE IF
    NOT EXISTS only helps for brand-new databases; a table that already exists never
    gets new columns added to it that way, which breaks every INSERT/UPDATE touching
    that column on any database created before that column was introduced. No real
    migration framework here, just enough to keep existing data intact as the schema
    grows - every column ever added to a pre-existing table must get an entry below."""
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_db()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS services (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            url TEXT NOT NULL,
            icon TEXT DEFAULT '⚙',
            status TEXT NOT NULL DEFAULT 'operational',  -- operational | degraded | maintenance | down
            manual_override INTEGER NOT NULL DEFAULT 0,   -- 1 = admin fixed the status, auto-check ignores it
            auto_check INTEGER NOT NULL DEFAULT 0,
            check_url TEXT DEFAULT '',
            last_checked TEXT DEFAULT '',
            response_ms INTEGER DEFAULT NULL,
            sort_order INTEGER DEFAULT 0,
            group_name TEXT DEFAULT '',
            auto_incident INTEGER NOT NULL DEFAULT 1  -- 1 = auto-open/resolve incidents on down/recovery
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS announcements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            type TEXT NOT NULL DEFAULT 'info',  -- info | warning | critical | success
            pinned INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS incidents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            service_id INTEGER,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'investigating',  -- investigating | identified | monitoring | resolved
            started_at TEXT NOT NULL,
            resolved_at TEXT DEFAULT NULL,
            auto_created INTEGER NOT NULL DEFAULT 0,  -- 1 = opened automatically by the health checker
            FOREIGN KEY (service_id) REFERENCES services (id) ON DELETE SET NULL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS incident_updates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            incident_id INTEGER NOT NULL,
            message TEXT NOT NULL,
            status TEXT NOT NULL,  -- the incident's status at the time of this update
            created_at TEXT NOT NULL,
            FOREIGN KEY (incident_id) REFERENCES incidents (id) ON DELETE CASCADE
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS service_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            service_id INTEGER NOT NULL,
            label TEXT NOT NULL,
            url TEXT NOT NULL,
            sort_order INTEGER DEFAULT 0,
            FOREIGN KEY (service_id) REFERENCES services (id) ON DELETE CASCADE
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS status_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            service_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            response_ms INTEGER DEFAULT NULL,
            checked_at TEXT NOT NULL,
            FOREIGN KEY (service_id) REFERENCES services (id) ON DELETE CASCADE
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS integrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            kind TEXT NOT NULL,  -- arr | jellyfin | jellyseerr
            base_url TEXT NOT NULL,
            api_key TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1,
            service_id INTEGER,
            show_on_public INTEGER NOT NULL DEFAULT 0,
            auto_incident INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (service_id) REFERENCES services (id) ON DELETE SET NULL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS maintenance_windows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            service_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            starts_at TEXT NOT NULL,
            ends_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            applied INTEGER NOT NULL DEFAULT 0,   -- 1 once the service has been flipped to 'maintenance'
            ended INTEGER NOT NULL DEFAULT 0,     -- 1 once the service has been restored
            pre_status TEXT DEFAULT NULL,          -- service.status just before the window started
            pre_manual_override INTEGER DEFAULT NULL,
            FOREIGN KEY (service_id) REFERENCES services (id) ON DELETE CASCADE
        )
    """)

    # service_id/pre_status/pre_manual_override above stay as the "primary" (first
    # selected) service for backward compatibility with any pre-multi-service row -
    # this table is the actual source of truth for which service(s) a window applies
    # to, each with its own pre-state snapshot (a window covering 3 services needs 3
    # independent restore points, not one shared pair of columns).
    c.execute("""
        CREATE TABLE IF NOT EXISTS maintenance_window_services (
            window_id INTEGER NOT NULL,
            service_id INTEGER NOT NULL,
            pre_status TEXT DEFAULT NULL,
            pre_manual_override INTEGER DEFAULT NULL,
            PRIMARY KEY (window_id, service_id),
            FOREIGN KEY (window_id) REFERENCES maintenance_windows (id) ON DELETE CASCADE,
            FOREIGN KEY (service_id) REFERENCES services (id) ON DELETE CASCADE
        )
    """)

    # Same idea for incidents - incidents.service_id stays as the "primary" service
    # (used by RSS/auto-incident/anything that only ever expects one), this table
    # holds the full set for an admin-created incident spanning multiple services.
    c.execute("""
        CREATE TABLE IF NOT EXISTS incident_services (
            incident_id INTEGER NOT NULL,
            service_id INTEGER NOT NULL,
            PRIMARY KEY (incident_id, service_id),
            FOREIGN KEY (incident_id) REFERENCES incidents (id) ON DELETE CASCADE,
            FOREIGN KEY (service_id) REFERENCES services (id) ON DELETE CASCADE
        )
    """)

    # Direct service->service dependencies only (no transitive chain) - a service
    # showing "degraded" because something it depends on is down, not lying
    # (operational) or falsely showing down itself. Brand-new table, so the FK's
    # ON DELETE CASCADE is reliable from the start on both columns (unlike a
    # retrofitted column - see delete_service()'s comment on integrations.service_id).
    c.execute("""
        CREATE TABLE IF NOT EXISTS service_dependencies (
            service_id INTEGER NOT NULL,
            depends_on_id INTEGER NOT NULL,
            PRIMARY KEY (service_id, depends_on_id),
            FOREIGN KEY (service_id) REFERENCES services (id) ON DELETE CASCADE,
            FOREIGN KEY (depends_on_id) REFERENCES services (id) ON DELETE CASCADE
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS info_page (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            content TEXT NOT NULL DEFAULT ''
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS discord_status_messages (
            channel_id TEXT PRIMARY KEY,
            message_id TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)

    # A visitor-submitted "report a problem" - separate from incidents/maintenance,
    # which are admin-authored. service_id is optional (a report can reference a
    # specific service's card, or be general) and ON DELETE SET NULL so deleting a
    # service later doesn't cascade-delete reports about it, just detaches them.
    c.execute("""
        CREATE TABLE IF NOT EXISTS problem_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message TEXT NOT NULL,
            contact TEXT DEFAULT '',
            service_id INTEGER,
            status TEXT NOT NULL DEFAULT 'new',
            created_at TEXT NOT NULL,
            reviewed_at TEXT,
            FOREIGN KEY (service_id) REFERENCES services (id) ON DELETE SET NULL
        )
    """)

    # Generic scheduled-task framework (see scheduler.py). One row per *registered*
    # task, keyed by the registry name rather than an autoincrement id: the code is
    # the source of truth for which tasks exist, this table only holds the parts an
    # admin can change plus the outcome of the last run. A row is created lazily the
    # first time a task is looked at (scheduler._row), so adding a task to the
    # registry needs no migration; a row whose task is later removed from the code
    # simply stops being listed, which is harmless.
    c.execute("""
        CREATE TABLE IF NOT EXISTS scheduled_tasks (
            name TEXT PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 1,
            schedule_kind TEXT NOT NULL DEFAULT 'interval',  -- interval | daily
            interval_minutes INTEGER NOT NULL DEFAULT 60,
            daily_at TEXT NOT NULL DEFAULT '03:00',          -- HH:MM, UTC
            last_run_at TEXT,
            last_status TEXT,                                -- success | failed | skipped
            last_message TEXT NOT NULL DEFAULT '',
            last_duration_ms INTEGER,
            last_trigger TEXT NOT NULL DEFAULT ''            -- schedule | manual
        )
    """)

    # The locally cached Jellyfin user list (see jellyfin_auth.py). Keyed by
    # Jellyfin's own user GUID, which is stable across renames - a username is not.
    # This is a *cache of an external system*, deliberately persisted in SQLite
    # rather than held in memory: it has to survive a restart, because it is what
    # keeps already-signed-in users valid while Jellyfin is unreachable.
    c.execute("""
        CREATE TABLE IF NOT EXISTS jellyfin_users (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            name_lower TEXT NOT NULL,
            is_administrator INTEGER NOT NULL DEFAULT 0,
            is_disabled INTEGER NOT NULL DEFAULT 0,
            first_seen_at TEXT NOT NULL,
            last_synced_at TEXT NOT NULL
        )
    """)

    # The back-and-forth on a problem report. Replaces the single admin_reply column
    # on problem_reports, which could only ever hold one message from one side - that
    # column is left in place (nothing is ever dropped here) and is backfilled into
    # this table below, but nothing reads it any more.
    #
    # `author` is 'admin' or 'user'. `seen` means "seen by the other party", which is
    # unambiguous because every message has exactly one intended reader: the admin
    # writes to the reporter, the reporter writes to the admin.
    c.execute("""
        CREATE TABLE IF NOT EXISTS report_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_id INTEGER NOT NULL,
            author TEXT NOT NULL,
            body TEXT NOT NULL,
            created_at TEXT NOT NULL,
            seen INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (report_id) REFERENCES problem_reports (id) ON DELETE CASCADE
        )
    """)

    # Per-user settings the *user* chooses about themselves, as opposed to
    # jellyfin_users, which mirrors Jellyfin (plus the one admin-owned decision,
    # portal_allowed, that predates this table). Deliberately its own table rather
    # than more columns on jellyfin_users: that one is rewritten wholesale by every
    # sync, so anything stored there has to be explicitly carried across
    # replace_jellyfin_users() or it is silently wiped within the hour. Keeping
    # user-owned data out of it means future preferences can't fall into that trap.
    #
    # Rows are keyed by Jellyfin's stable user id and are deliberately NOT deleted
    # when a user disappears from Jellyfin - if the account comes back (or the sync
    # was simply wrong for a poll), their settings are still here.
    c.execute("""
        CREATE TABLE IF NOT EXISTS notification_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            event TEXT NOT NULL,
            subject TEXT NOT NULL,
            body TEXT NOT NULL,
            created_at TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            sent_at TEXT,
            last_error TEXT NOT NULL DEFAULT ''
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_preferences (
            user_id TEXT PRIMARY KEY,
            theme TEXT NOT NULL DEFAULT 'auto',   -- auto | dark | light
            contact TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        )
    """)

    conn.commit()

    # Retrofit columns added after a table already existed in some earlier version of
    # this schema, so an existing database (with real data) isn't left behind and
    # start failing every write that touches a newer column.
    _ensure_column(conn, "services", "group_name", "TEXT DEFAULT ''")
    _ensure_column(conn, "services", "auto_incident", "INTEGER NOT NULL DEFAULT 1")
    _ensure_column(conn, "incidents", "auto_created", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "integrations", "service_id", "INTEGER")
    _ensure_column(conn, "integrations", "show_on_public", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "integrations", "auto_incident", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "services", "slow_threshold_ms", "INTEGER DEFAULT NULL")
    _ensure_column(conn, "services", "startup_grace_seconds", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "services", "retry_count", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "services", "retry_interval_seconds", "INTEGER NOT NULL DEFAULT 5")
    _ensure_column(conn, "services", "ignore_in_overall_status", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "services", "api_health_mode", "TEXT NOT NULL DEFAULT 'off'")
    _ensure_column(conn, "services", "show_report_button", "INTEGER NOT NULL DEFAULT 1")
    # '' = not mapped, 'host' = the machine this portal itself runs on, 'vm:<name>' =
    # a Hyper-V VM by name (no stable numeric VM id is available - see monitoring.py).
    _ensure_column(conn, "services", "run_target", "TEXT NOT NULL DEFAULT ''")
    # Both off by default - run_target/dependencies started admin-only, these are an
    # explicit per-service opt-in to also show them on the public card.
    _ensure_column(conn, "services", "show_run_target_public", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "services", "show_dependencies_public", "INTEGER NOT NULL DEFAULT 0")
    # Which signed-in Jellyfin user filed a problem report ('' for an anonymous one,
    # which is still the only possibility on an install that hasn't enabled
    # Jellyfin-backed auth). Stored as the username, not the user id: it's shown to
    # the admin, and it has to stay readable for a user who was later removed from
    # Jellyfin entirely.
    # qBittorrent authenticates with a username/password login rather than an API key,
    # so it needs its own two fields. Every other integration leaves them blank.
    # Per-user notification settings. On user_preferences rather than jellyfin_users
    # because that table is rewritten wholesale by every sync - anything user-owned put
    # there is silently wiped within the hour.
    _ensure_column(conn, "user_preferences", "notify_email", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "user_preferences", "notify_discord_id", "TEXT NOT NULL DEFAULT ''")
    # "Things about my own reports" defaults ON: it's a reply to something the person
    # started, and almost always wanted. "Anything about services I use" defaults OFF:
    # nobody wants a message for every maintenance window on every service.
    _ensure_column(conn, "user_preferences", "notify_own_reports", "INTEGER NOT NULL DEFAULT 1")
    _ensure_column(conn, "user_preferences", "notify_service_events", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "user_preferences", "notify_requests", "INTEGER NOT NULL DEFAULT 1")
    # The linked Seerr account id, only ever set from a real Jellyfin<->Seerr link -
    # never guessed from a matching email or username. Empty means "not linked", which
    # is the fail-closed state.
    _ensure_column(conn, "user_preferences", "seerr_user_id", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "integrations", "username", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "integrations", "password", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "problem_reports", "reporter_user", "TEXT NOT NULL DEFAULT ''")
    # Whether this portal lets the user sign in, independently of whether Jellyfin
    # does. On by default: users appear here by being synced from Jellyfin, and
    # having to individually approve each one would make the feature tedious for the
    # normal case. Needs _ensure_column rather than living in the CREATE TABLE above
    # because jellyfin_users already exists on any install running 1.7.0-rc.1.
    _ensure_column(conn, "jellyfin_users", "portal_allowed", "INTEGER NOT NULL DEFAULT 1")
    # Who filed a report, by Jellyfin's stable user id - reporter_user (the name) is
    # kept alongside it for *display*, because it has to stay readable for someone
    # later removed from Jellyfin entirely. The id is what "show me my reports" looks
    # up by, so renaming yourself in Jellyfin doesn't orphan your own report history.
    _ensure_column(conn, "problem_reports", "reporter_user_id", "TEXT NOT NULL DEFAULT ''")
    # The admin's reply, and whether the reporter has seen it yet.
    _ensure_column(conn, "problem_reports", "admin_reply", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "problem_reports", "replied_at", "TEXT")
    _ensure_column(conn, "problem_reports", "reply_seen", "INTEGER NOT NULL DEFAULT 0")
    # Set when an admin turns a report into an incident, so the reporter can be shown
    # what came of it. No FK constraint: _ensure_column can't add one to an existing
    # table, so a deleted incident is handled by the LEFT JOIN reading it instead.
    _ensure_column(conn, "problem_reports", "incident_id", "INTEGER")
    conn.commit()

    # Write-ahead logging. SQLite's default rollback journal takes a database-wide
    # exclusive lock for every write, and readers and writers block each other: while
    # the background health-check thread is writing a status update, every request
    # handler touching the database waits (up to the 5s busy timeout, then fails
    # outright with "database is locked"). On a busy host that is exactly what "the
    # portal stops responding under load" looks like - the production server
    # (waitress) only has a handful of request threads, and they all end up parked on
    # the same lock. Under WAL, readers never block the writer and the writer never
    # blocks readers.
    #
    # A persistent property of the database file, so setting it once here covers
    # every connection from then on. WAL needs real filesystem locking, which network
    # filesystems (SMB/NFS) don't provide - if this database lives on one, SQLite
    # refuses the switch and it stays on the old journal mode, which still works.
    try:
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            _logger.info("SQLite stayed in '%s' journal mode (WAL unavailable here)", mode)
    except sqlite3.Error:
        _logger.exception("Could not enable WAL journal mode")

    # Indexes. Every one of these backs a query that runs on a *public page load*,
    # not just an admin action, and every table below grows without bound. They're
    # created here rather than in the CREATE TABLE blocks above for the same reason
    # _ensure_column() exists: CREATE TABLE IF NOT EXISTS is a silent no-op on a
    # database that already exists, so an index declared inline would never appear on
    # any real user's database. CREATE INDEX IF NOT EXISTS, by contrast, applies to
    # both new and existing databases.
    #
    # status_history is the one that actually hurt: it gains a row per service per
    # check forever (a 120s interval and 15 services is ~324k rows a month), and
    # get_uptime_percentages() reads a 30-day slice of it on every single public page
    # load. Unindexed, that's a full scan of the whole table per page.
    for ddl in (
        # status is in the index, not just the two lookup columns, so
        # get_uptime_percentages() can be answered entirely from the index without
        # touching the table at all (SQLite reports it as a COVERING INDEX). Measured
        # 131ms -> 43ms over 30 days of history for 17 services. A (service_id,
        # checked_at) index is a prefix of this one, so this replaces it rather than
        # sitting alongside it.
        "CREATE INDEX IF NOT EXISTS idx_status_history_service_checked "
        "ON status_history (service_id, checked_at, status)",
        "CREATE INDEX IF NOT EXISTS idx_status_history_checked "
        "ON status_history (checked_at)",
        "CREATE INDEX IF NOT EXISTS idx_service_links_service ON service_links (service_id)",
        "CREATE INDEX IF NOT EXISTS idx_incident_updates_incident ON incident_updates (incident_id)",
        "CREATE INDEX IF NOT EXISTS idx_incident_services_incident ON incident_services (incident_id)",
        "CREATE INDEX IF NOT EXISTS idx_incident_services_service ON incident_services (service_id)",
        "CREATE INDEX IF NOT EXISTS idx_mw_services_window ON maintenance_window_services (window_id)",
        "CREATE INDEX IF NOT EXISTS idx_mw_services_service ON maintenance_window_services (service_id)",
        "CREATE INDEX IF NOT EXISTS idx_service_dependencies_service ON service_dependencies (service_id)",
        "CREATE INDEX IF NOT EXISTS idx_problem_reports_service ON problem_reports (service_id)",
        "CREATE INDEX IF NOT EXISTS idx_integrations_service ON integrations (service_id)",
        # Login looks a user up by name, not by id (the id is what Jellyfin returns
        # *after* a successful authentication). Small table, but this is the one
        # query on the sign-in path, and an index costs nothing here.
        "CREATE INDEX IF NOT EXISTS idx_jellyfin_users_name_lower ON jellyfin_users (name_lower)",
        # Backs "show me my own reports" on the account page, which runs on every
        # visit by a signed-in user.
        "CREATE INDEX IF NOT EXISTS idx_problem_reports_reporter ON problem_reports (reporter_user_id)",
        "CREATE INDEX IF NOT EXISTS idx_report_messages_report ON report_messages (report_id)",
        # The drain query is "unsent, oldest first", run every couple of minutes by the
        # delivery task - covered so it stays an index scan as the table grows.
        "CREATE INDEX IF NOT EXISTS idx_notification_queue_pending "
        "ON notification_queue (sent_at, created_at)",
    ):
        conn.execute(ddl)
    conn.commit()

    # One-time backfill (idempotent - re-running is a no-op once caught up): any
    # maintenance window/incident created before multi-service support has no rows
    # yet in the join tables above - seed one from its own existing single
    # service_id (+ pre_status/pre_manual_override for windows) so every read can go
    # through the join table uniformly, with no dual-path special-casing between
    # "old single-service row" and "new multi-service row".
    conn.execute("""
        INSERT INTO maintenance_window_services (window_id, service_id, pre_status, pre_manual_override)
        SELECT id, service_id, pre_status, pre_manual_override FROM maintenance_windows
        WHERE id NOT IN (SELECT DISTINCT window_id FROM maintenance_window_services)
    """)
    conn.execute("""
        INSERT INTO incident_services (incident_id, service_id)
        SELECT id, service_id FROM incidents
        WHERE service_id IS NOT NULL AND id NOT IN (SELECT DISTINCT incident_id FROM incident_services)
    """)
    # Replies written before report_messages existed (1.7.0-rc.2/rc.3) live in the
    # single admin_reply column. Seed them as the first message of their thread so
    # nobody's existing conversation vanishes when they update. Idempotent via the
    # NOT IN guard, same shape as the two backfills above - safe to re-run on every
    # startup forever.
    conn.execute("""
        INSERT INTO report_messages (report_id, author, body, created_at, seen)
        SELECT id, 'admin', admin_reply, COALESCE(replied_at, created_at), reply_seen
        FROM problem_reports
        WHERE admin_reply != '' AND id NOT IN (SELECT DISTINCT report_id FROM report_messages)
    """)
    conn.commit()

    # Seed defaults if empty
    if c.execute("SELECT COUNT(*) FROM info_page").fetchone()[0] == 0:
        c.execute("INSERT INTO info_page (id, content) VALUES (1, ?)",
                   ("Add practical info here (SMB access, VPN, contact...) from the admin panel.",))
        conn.commit()

    if c.execute("SELECT COUNT(*) FROM services").fetchone()[0] == 0:
        seed = [
            ("Jellyfin", "Movie & TV streaming", "http://SERVER:8096", "🎬", "operational", 0, 1, "http://SERVER:8096/health", 0),
            ("SMB share", "Network file access", "smb://SERVER/share", "📁", "operational", 0, 0, "", 1),
        ]
        c.executemany("""
            INSERT INTO services (name, description, url, icon, status, manual_override, auto_check, check_url, sort_order)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, seed)
        conn.commit()

    conn.close()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# ---------- Services ----------
def list_services():
    conn = get_db()
    rows = conn.execute("SELECT * FROM services ORDER BY sort_order, id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_service(service_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM services WHERE id = ?", (service_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def _slow_threshold_ms(data):
    raw = str(data.get("slow_threshold_ms") or "").strip()
    return int(raw) if raw else None


API_HEALTH_MODES = ("off", "degrade", "down")


def _api_health_mode(data):
    mode = data.get("api_health_mode", "off")
    return mode if mode in API_HEALTH_MODES else "off"


def create_service(data):
    conn = get_db()
    cur = conn.execute("""
        INSERT INTO services (name, description, url, icon, status, manual_override, auto_check, check_url, sort_order, group_name, auto_incident, slow_threshold_ms, startup_grace_seconds, retry_count, retry_interval_seconds, ignore_in_overall_status, api_health_mode, show_report_button, run_target, show_run_target_public, show_dependencies_public)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (data["name"], data.get("description", ""), data.get("url", ""), data.get("icon", "⚙"),
          data.get("status", "operational"), int(data.get("manual_override", 0)),
          int(data.get("auto_check", 0)), data.get("check_url", ""), int(data.get("sort_order", 0)),
          data.get("group_name", "").strip(), int(data.get("auto_incident", 1)),
          _slow_threshold_ms(data), int(data.get("startup_grace_seconds") or 0),
          int(data.get("retry_count") or 0), int(data.get("retry_interval_seconds") or 5),
          int(data.get("ignore_in_overall_status", 0)), _api_health_mode(data),
          int(data.get("show_report_button", 1)), data.get("run_target", "").strip(),
          int(data.get("show_run_target_public", 0)), int(data.get("show_dependencies_public", 0))))
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def update_service(service_id, data):
    conn = get_db()
    conn.execute("""
        UPDATE services SET name=?, description=?, url=?, icon=?, status=?, manual_override=?,
        auto_check=?, check_url=?, sort_order=?, group_name=?, auto_incident=?,
        slow_threshold_ms=?, startup_grace_seconds=?, retry_count=?, retry_interval_seconds=?,
        ignore_in_overall_status=?, api_health_mode=?, show_report_button=?, run_target=?,
        show_run_target_public=?, show_dependencies_public=? WHERE id=?
    """, (data["name"], data.get("description", ""), data["url"], data.get("icon", "⚙"),
          data.get("status", "operational"), int(data.get("manual_override", 0)),
          int(data.get("auto_check", 0)), data.get("check_url", ""),
          int(data.get("sort_order", 0)), data.get("group_name", "").strip(),
          int(data.get("auto_incident", 1)), _slow_threshold_ms(data),
          int(data.get("startup_grace_seconds") or 0), int(data.get("retry_count") or 0),
          int(data.get("retry_interval_seconds") or 5), int(data.get("ignore_in_overall_status", 0)),
          _api_health_mode(data), int(data.get("show_report_button", 1)),
          data.get("run_target", "").strip(), int(data.get("show_run_target_public", 0)),
          int(data.get("show_dependencies_public", 0)), service_id))
    conn.commit()
    conn.close()


def update_service_status_from_check(service_id, status, response_ms):
    conn = get_db()
    conn.execute("""
        UPDATE services SET status=?, response_ms=?, last_checked=?
        WHERE id=? AND manual_override=0
    """, (status, response_ms, now_iso(), service_id))
    conn.commit()
    conn.close()


def delete_service(service_id):
    conn = get_db()
    # Explicit, rather than relying solely on the FK's ON DELETE SET NULL - a database
    # that got the integrations.service_id column via _ensure_column() (i.e. it existed
    # before that column did) never had that FK constraint defined in the first place.
    conn.execute("UPDATE integrations SET service_id=NULL WHERE service_id=?", (service_id,))
    conn.execute("DELETE FROM services WHERE id=?", (service_id,))
    conn.commit()
    conn.close()


# ---------- Announcements ----------
def list_announcements(limit=None):
    conn = get_db()
    q = "SELECT * FROM announcements ORDER BY pinned DESC, created_at DESC"
    if limit:
        q += f" LIMIT {int(limit)}"
    rows = conn.execute(q).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_announcement(aid):
    conn = get_db()
    row = conn.execute("SELECT * FROM announcements WHERE id=?", (aid,)).fetchone()
    conn.close()
    return dict(row) if row else None


def create_announcement(data):
    conn = get_db()
    conn.execute("""
        INSERT INTO announcements (title, message, type, pinned, created_at)
        VALUES (?, ?, ?, ?, ?)
    """, (data["title"], data["message"], data.get("type", "info"),
          int(data.get("pinned", 0)), now_iso()))
    conn.commit()
    conn.close()


def update_announcement(aid, data):
    conn = get_db()
    conn.execute("""
        UPDATE announcements SET title=?, message=?, type=?, pinned=? WHERE id=?
    """, (data["title"], data["message"], data.get("type", "info"),
          int(data.get("pinned", 0)), aid))
    conn.commit()
    conn.close()


def delete_announcement(aid):
    conn = get_db()
    conn.execute("DELETE FROM announcements WHERE id=?", (aid,))
    conn.commit()
    conn.close()


# ---------- Incidents ----------
def _get_incident_services(incident_id):
    conn = get_db()
    rows = conn.execute("""
        SELECT s.id, s.name FROM incident_services isv
        JOIN services s ON s.id = isv.service_id
        WHERE isv.incident_id=? ORDER BY s.sort_order, s.id
    """, (incident_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _attach_incident_services(incidents):
    for i in incidents:
        i["services"] = _get_incident_services(i["id"])
        i["service_names"] = ", ".join(s["name"] for s in i["services"])
    return incidents


def list_incidents(limit=None, exclude_ids=None, max_age_days=None):
    """max_age_days never hides a still-open incident (resolved_at IS NULL)
    regardless of how long it's been going on - only a *resolved* incident's own
    age counts toward the cutoff, since an ongoing problem shouldn't disappear
    from the public page just because it started a while ago.

    exclude_ids drives "load more" pagination (see app.py's api_incidents_more()):
    the client sends the ids it is already displaying, and this returns the next
    page of whatever is left, newest-first, with no age filter applied. That is
    deliberately NOT a positional OFFSET and NOT an id cursor, both of which were
    tried first and both of which lost or duplicated data (2026-08-10):

    - An OFFSET counted against the *age-filtered* initial query doesn't line up
      with an *unfiltered* continuation of it, so it skipped straight past the
      very items "load more" exists to reveal.
    - An `id < cursor` cursor can't express "everything not already shown" when
      the initial view is filtered: the shown ids can have gaps in id-space (a
      still-open old incident sits at a lower id than a newer resolved-and-hidden
      one), so seeding from the oldest shown id silently skipped anything hidden
      inside that gap, while seeding from the newest shown id re-returned every
      already-visible item below it - which is what made "Load more" append the
      whole list again on every click.

    Excluding the ids the client already has is the only one of the three that is
    simultaneously gap-free and duplicate-free, because it states the real intent
    directly ("give me what I'm not already showing") instead of approximating it
    with a position. Ordering by id DESC is equivalent to started_at DESC here and
    avoids timestamp-string-comparison ambiguity - safe because create_incident()
    always stamps started_at with now_iso() at insert time, never backdated
    (unlike maintenance windows, which can be)."""
    conn = get_db()
    q = "SELECT * FROM incidents"
    conditions = []
    params = []
    if max_age_days:
        conditions.append("(resolved_at IS NULL OR resolved_at >= datetime('now', ?))")
        params.append(f"-{int(max_age_days)} days")
    if exclude_ids:
        ids = [int(i) for i in exclude_ids]
        conditions.append(f"id NOT IN ({','.join('?' * len(ids))})")
        params.extend(ids)
    if conditions:
        q += " WHERE " + " AND ".join(conditions)
    q += " ORDER BY id DESC"
    if limit:
        q += f" LIMIT {int(limit)}"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return _attach_incident_services([dict(r) for r in rows])


def get_incident(iid):
    conn = get_db()
    row = conn.execute("SELECT * FROM incidents WHERE id=?", (iid,)).fetchone()
    conn.close()
    if not row:
        return None
    incident = dict(row)
    incident["services"] = _get_incident_services(iid)
    incident["service_names"] = ", ".join(s["name"] for s in incident["services"])
    return incident


def _replace_incident_services(conn, incident_id, service_ids):
    conn.execute("DELETE FROM incident_services WHERE incident_id=?", (incident_id,))
    seen = []
    for sid in service_ids:
        sid = int(sid)
        if sid not in seen:
            seen.append(sid)
    conn.executemany("INSERT INTO incident_services (incident_id, service_id) VALUES (?, ?)",
                      [(incident_id, sid) for sid in seen])
    return seen


def create_incident(data, service_ids=None):
    """service_ids: explicit list of service ids for a multi-service incident. Falls
    back to the single data['service_id'] (or none) when omitted, so existing
    single-service callers/tests keep working unchanged."""
    if service_ids is None:
        service_ids = [data["service_id"]] if data.get("service_id") else []
    conn = get_db()
    primary_id = int(service_ids[0]) if service_ids else None
    cur = conn.execute("""
        INSERT INTO incidents (service_id, title, description, status, started_at)
        VALUES (?, ?, ?, ?, ?)
    """, (primary_id, data["title"], data.get("description", ""),
          data.get("status", "investigating"), now_iso()))
    new_id = cur.lastrowid
    if service_ids:
        _replace_incident_services(conn, new_id, service_ids)
    conn.commit()
    conn.close()
    return new_id


def update_incident(iid, data, service_ids=None):
    """service_ids left as None (the default) means "leave the associated services
    alone" - used by callers that only ever change status (posting a timeline
    update, the auto-incident lifecycle marking something resolved), which have no
    business touching which services an incident covers."""
    conn = get_db()
    resolved_at = now_iso() if data.get("status") == "resolved" else None
    if service_ids is not None:
        seen = _replace_incident_services(conn, iid, service_ids)
        primary_id = seen[0] if seen else None
    else:
        primary_id = data.get("service_id") or None
    conn.execute("""
        UPDATE incidents SET service_id=?, title=?, description=?, status=?,
        resolved_at=COALESCE(?, resolved_at) WHERE id=?
    """, (primary_id, data["title"], data.get("description", ""),
          data.get("status", "investigating"), resolved_at, iid))
    conn.commit()
    conn.close()


def delete_incident(iid):
    conn = get_db()
    conn.execute("DELETE FROM incidents WHERE id=?", (iid,))
    conn.commit()
    conn.close()


def create_auto_incident(service_id, title, status):
    """Opens an incident from the health checker (not the admin form) — flagged
    auto_created so the recovery path only ever auto-resolves incidents it opened itself.
    Always exactly one service (the one whose check failed), unlike the admin-created,
    optionally-multi-service incidents above - still populates incident_services so
    every incident's service(s) can be read the same uniform way regardless of origin."""
    conn = get_db()
    cur = conn.execute("""
        INSERT INTO incidents (service_id, title, description, status, started_at, auto_created)
        VALUES (?, ?, '', ?, ?, 1)
    """, (service_id, title, status, now_iso()))
    incident_id = cur.lastrowid
    conn.execute("INSERT INTO incident_services (incident_id, service_id) VALUES (?, ?)",
                 (incident_id, service_id))
    conn.commit()
    conn.close()
    return incident_id


def get_open_auto_incident_for_service(service_id):
    conn = get_db()
    row = conn.execute("""
        SELECT * FROM incidents WHERE service_id=? AND auto_created=1 AND status != 'resolved'
        ORDER BY started_at DESC LIMIT 1
    """, (service_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


# ---------- Incident updates (per-incident timeline) ----------
def list_incident_updates(incident_id):
    conn = get_db()
    rows = conn.execute("""
        SELECT * FROM incident_updates WHERE incident_id=? ORDER BY created_at DESC, id DESC
    """, (incident_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def create_incident_update(incident_id, message, status):
    conn = get_db()
    conn.execute("""
        INSERT INTO incident_updates (incident_id, message, status, created_at)
        VALUES (?, ?, ?, ?)
    """, (incident_id, message, status, now_iso()))
    conn.commit()
    conn.close()


# ---------- Service links (multiple access URLs per service) ----------
def list_service_links(service_id):
    conn = get_db()
    rows = conn.execute("""
        SELECT * FROM service_links WHERE service_id=? ORDER BY sort_order, id
    """, (service_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def replace_service_links(service_id, links):
    """Replaces the full set of links for a service in one go — the admin form posts
    the whole label/url list at once rather than editing rows individually."""
    conn = get_db()
    conn.execute("DELETE FROM service_links WHERE service_id=?", (service_id,))
    conn.executemany("""
        INSERT INTO service_links (service_id, label, url, sort_order) VALUES (?, ?, ?, ?)
    """, [(service_id, label, url, i) for i, (label, url) in enumerate(links)])
    conn.commit()
    conn.close()


def get_service_dependencies(service_id):
    """Ids of the services this one directly depends on (not transitive)."""
    conn = get_db()
    rows = conn.execute(
        "SELECT depends_on_id FROM service_dependencies WHERE service_id=?", (service_id,)
    ).fetchall()
    conn.close()
    return [r["depends_on_id"] for r in rows]


def set_service_dependencies(service_id, depends_on_ids):
    """Replaces the full dependency set for a service in one go, same pattern as
    replace_service_links(). A service can't depend on itself - filtered out here
    as a server-side backstop even though the admin form's checkbox list already
    excludes the service being edited from its own options."""
    depends_on_ids = [i for i in depends_on_ids if i != service_id]
    conn = get_db()
    conn.execute("DELETE FROM service_dependencies WHERE service_id=?", (service_id,))
    conn.executemany(
        "INSERT INTO service_dependencies (service_id, depends_on_id) VALUES (?, ?)",
        [(service_id, dep_id) for dep_id in depends_on_ids]
    )
    conn.commit()
    conn.close()


# ---------- Status history (append-only; powers the uptime %) ----------
def record_status_history(service_id, status, response_ms):
    conn = get_db()
    conn.execute("""
        INSERT INTO status_history (service_id, status, response_ms, checked_at) VALUES (?, ?, ?, ?)
    """, (service_id, status, response_ms, now_iso()))
    conn.commit()
    conn.close()


UPTIME_WINDOW_DAYS = 30


def get_uptime_percentages(days=UPTIME_WINDOW_DAYS):
    """{service_id: percentage} for every service that has history in the window -
    the share of recorded checks that were NOT 'down'. Checks logged while the
    service was in 'maintenance' are excluded entirely (planned maintenance
    shouldn't count against uptime). A service with no history in the window is
    simply absent from the dict, which callers render as "no data" - same meaning as
    the None that get_uptime_percentage() returns for one service.

    One grouped query for all services, counted by SQLite, rather than one query per
    service each materializing every matching row into Python. That matters because
    this runs on every public page load and every /api/status hit: the per-service
    version measured ~1s for 17 services against 90 days of history, and grew with
    the table forever. Backed by idx_status_history_service_checked (see init_db)."""
    conn = get_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = conn.execute("""
        SELECT service_id,
               COUNT(*) AS total,
               SUM(CASE WHEN status = 'down' THEN 1 ELSE 0 END) AS down_count
        FROM status_history
        WHERE checked_at >= ? AND status != 'maintenance'
        GROUP BY service_id
    """, (cutoff,)).fetchall()
    conn.close()
    return {
        r["service_id"]: round((r["total"] - r["down_count"]) / r["total"] * 100, 1)
        for r in rows if r["total"]
    }


def get_uptime_percentage(service_id, days=UPTIME_WINDOW_DAYS):
    """Single-service form of get_uptime_percentages(). Returns None if there's no
    history yet (e.g. a service with auto-check off, which never gets a
    status_history row). Kept for callers that only need one service; anything
    iterating over every service should use the grouped version instead."""
    conn = get_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    row = conn.execute("""
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN status = 'down' THEN 1 ELSE 0 END) AS down_count
        FROM status_history
        WHERE service_id=? AND checked_at >= ? AND status != 'maintenance'
    """, (service_id, cutoff)).fetchone()
    conn.close()
    if not row or not row["total"]:
        return None
    return round((row["total"] - row["down_count"]) / row["total"] * 100, 1)


def prune_status_history(days):
    """Deletes check results older than `days` and returns how many rows went.

    status_history is the only unbounded table in this schema - one row per service
    per check, forever - and nothing reads further back than UPTIME_WINDOW_DAYS, so
    without this it grows purely to slow itself (and every backup) down. Called from
    the background health-check loop, not a request handler. Backed by
    idx_status_history_checked (see init_db) so the DELETE doesn't scan the table it
    is trying to trim."""
    if not days or days <= 0:
        return 0
    conn = get_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    cur = conn.execute("DELETE FROM status_history WHERE checked_at < ?", (cutoff,))
    conn.commit()
    deleted = cur.rowcount
    conn.close()
    return deleted


# ---------- Integrations (read-only external service status) ----------
def list_integrations():
    conn = get_db()
    rows = conn.execute("SELECT * FROM integrations ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_integration(iid):
    conn = get_db()
    row = conn.execute("SELECT * FROM integrations WHERE id=?", (iid,)).fetchone()
    conn.close()
    return dict(row) if row else None


def create_integration(data):
    conn = get_db()
    cur = conn.execute("""
        INSERT INTO integrations (name, kind, base_url, api_key, enabled, service_id,
                                  show_on_public, auto_incident, username, password)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (data["name"], data["kind"], data["base_url"].rstrip("/"), data.get("api_key", ""),
          int(data.get("enabled", 1)), data.get("service_id") or None, int(data.get("show_on_public", 0)),
          int(data.get("auto_incident", 0)), data.get("username", ""), data.get("password", "")))
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def update_integration(iid, data):
    conn = get_db()
    service_id = data.get("service_id") or None
    show_on_public = int(data.get("show_on_public", 0))
    auto_incident = int(data.get("auto_incident", 0))
    conn.execute("""
        UPDATE integrations SET name=?, kind=?, base_url=?, enabled=?, service_id=?,
        show_on_public=?, auto_incident=?, username=? WHERE id=?
    """, (data["name"], data["kind"], data["base_url"].rstrip("/"),
          int(data.get("enabled", 1)), service_id, show_on_public, auto_incident,
          data.get("username", ""), iid))
    # Blank secret on the edit form means "keep the existing one" - never overwrite a
    # stored credential with an empty string just because the admin left the field
    # blank. Applies to the qBittorrent password for exactly the same reason it always
    # has to the API key; forgetting it there would silently break the integration on
    # every unrelated edit (renaming it, changing its URL).
    if data.get("api_key"):
        conn.execute("UPDATE integrations SET api_key=? WHERE id=?", (data["api_key"], iid))
    if data.get("password"):
        conn.execute("UPDATE integrations SET password=? WHERE id=?", (data["password"], iid))
    conn.commit()
    conn.close()


def list_integrations_for_service(service_id):
    conn = get_db()
    rows = conn.execute("""
        SELECT * FROM integrations WHERE service_id=? AND enabled=1 AND show_on_public=1
    """, (service_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_public_integrations_by_service():
    """{service_id: [integration, ...]} for every publicly-shown, enabled integration
    that's linked to a service. Same filter as list_integrations_for_service(), in
    one query - app.py's _attach_integration_status() runs this over every service on
    every public page load, and one query beats one-per-service (each of which opens
    its own SQLite connection)."""
    conn = get_db()
    rows = conn.execute("""
        SELECT * FROM integrations
        WHERE service_id IS NOT NULL AND enabled=1 AND show_on_public=1
        ORDER BY id
    """).fetchall()
    conn.close()
    grouped = {}
    for r in rows:
        grouped.setdefault(r["service_id"], []).append(dict(r))
    return grouped


def delete_integration(iid):
    conn = get_db()
    conn.execute("DELETE FROM integrations WHERE id=?", (iid,))
    conn.commit()
    conn.close()


# ---------- Maintenance windows ----------
def _get_window_services(window_id):
    conn = get_db()
    rows = conn.execute("""
        SELECT s.id, s.name FROM maintenance_window_services mws
        JOIN services s ON s.id = mws.service_id
        WHERE mws.window_id=? ORDER BY s.sort_order, s.id
    """, (window_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _attach_window_services(windows):
    for w in windows:
        w["services"] = _get_window_services(w["id"])
        w["service_names"] = ", ".join(s["name"] for s in w["services"])
    return windows


def list_maintenance_windows():
    conn = get_db()
    rows = conn.execute("SELECT * FROM maintenance_windows ORDER BY starts_at DESC").fetchall()
    conn.close()
    return _attach_window_services([dict(r) for r in rows])


def list_public_maintenance_windows():
    """Windows still relevant to a visitor: not yet ended, and due to start or already
    active - i.e. everything except ones that have already been restored."""
    conn = get_db()
    rows = conn.execute("""
        SELECT * FROM maintenance_windows WHERE ended = 0 ORDER BY starts_at ASC
    """).fetchall()
    conn.close()
    return _attach_window_services([dict(r) for r in rows])


def list_ended_maintenance_windows(limit=10, offset=0, max_age_days=None):
    """The public "maintenance history" list - windows already restored (ended=1),
    newest-first. Unlike list_public_maintenance_windows() above, always paginated
    (limit is never optional here) since this is only ever consumed by the
    "load more" history endpoint, never the initial page render."""
    conn = get_db()
    q = "SELECT * FROM maintenance_windows WHERE ended = 1"
    params = []
    if max_age_days:
        q += " AND ends_at >= datetime('now', ?)"
        params.append(f"-{int(max_age_days)} days")
    q += " ORDER BY ends_at DESC LIMIT ? OFFSET ?"
    params.extend([int(limit), int(offset)])
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return _attach_window_services([dict(r) for r in rows])


def get_maintenance_window(mid):
    conn = get_db()
    row = conn.execute("SELECT * FROM maintenance_windows WHERE id=?", (mid,)).fetchone()
    conn.close()
    if not row:
        return None
    window = dict(row)
    window["services"] = _get_window_services(mid)
    window["service_names"] = ", ".join(s["name"] for s in window["services"])
    return window


def create_maintenance_window(data, service_ids=None):
    """service_ids: explicit list for a multi-service window. Falls back to the
    single data['service_id'] when omitted, so existing single-service
    callers/tests keep working unchanged - a window always needs at least one
    service (unlike incidents, which may have none)."""
    if service_ids is None:
        service_ids = [data["service_id"]]
    service_ids = [int(sid) for sid in service_ids]
    conn = get_db()
    cur = conn.execute("""
        INSERT INTO maintenance_windows (service_id, title, description, starts_at, ends_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (service_ids[0], data["title"], data.get("description", ""),
          data["starts_at"], data["ends_at"], now_iso()))
    new_id = cur.lastrowid
    conn.executemany("INSERT INTO maintenance_window_services (window_id, service_id) VALUES (?, ?)",
                      [(new_id, sid) for sid in dict.fromkeys(service_ids)])
    conn.commit()
    conn.close()
    return new_id


def update_maintenance_window(mid, data, service_ids=None):
    """service_ids left as None means "leave the associated services alone" - the
    admin route only ever passes an explicit list before a window has been
    applied (see app.py), to avoid migrating an in-progress override's
    pre_status/pre_manual_override snapshot to a different service mid-flight."""
    conn = get_db()
    conn.execute("""
        UPDATE maintenance_windows SET title=?, description=?, starts_at=?, ends_at=? WHERE id=?
    """, (data["title"], data.get("description", ""), data["starts_at"], data["ends_at"], mid))
    if service_ids is not None:
        service_ids = [int(sid) for sid in dict.fromkeys(int(sid) for sid in service_ids)]
        conn.execute("DELETE FROM maintenance_window_services WHERE window_id=?", (mid,))
        conn.executemany("INSERT INTO maintenance_window_services (window_id, service_id) VALUES (?, ?)",
                          [(mid, sid) for sid in service_ids])
        if service_ids:
            conn.execute("UPDATE maintenance_windows SET service_id=? WHERE id=?", (service_ids[0], mid))
    conn.commit()
    conn.close()


def delete_maintenance_window(mid):
    """If the window is currently active (applied but not yet ended), restores every
    associated service first - otherwise deleting an in-progress window would leave
    them stuck showing 'maintenance' forever."""
    window = get_maintenance_window(mid)
    if window and window["applied"] and not window["ended"]:
        _restore_all_services_from_maintenance(mid)
    conn = get_db()
    conn.execute("DELETE FROM maintenance_windows WHERE id=?", (mid,))
    conn.commit()
    conn.close()


def _restore_all_services_from_maintenance(window_id):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM maintenance_window_services WHERE window_id=?", (window_id,)
    ).fetchall()
    for row in rows:
        conn.execute("""
            UPDATE services SET status=?, manual_override=? WHERE id=?
        """, (row["pre_status"] or "operational", row["pre_manual_override"] or 0, row["service_id"]))
    conn.execute("UPDATE maintenance_windows SET ended=1 WHERE id=?", (window_id,))
    conn.commit()
    conn.close()


def process_maintenance_windows():
    """Called once per health-check cycle. Starts any window whose start time has
    passed (snapshotting the service's current status/manual_override so it can be
    restored exactly), and ends any window whose end time has passed. While a window
    is active the service is forced to manual_override=1, so the regular auto-check
    loop leaves its status alone for the duration. Returns a list of
    {"event": "maintenance_started"|"maintenance_ended", "service": ..., "window": ...}
    for anything that changed, so callers can fire notifications off the back of it."""
    now = now_iso()
    events = []
    conn = get_db()

    # Deliberately not filtering on ends_at here: a window entirely missed (e.g. the
    # app was down for its whole scheduled span) should still get applied and then
    # immediately picked up by the due_to_end pass below, rather than being silently
    # orphaned in a permanent "scheduled but never applied" limbo.
    due_to_start = conn.execute("""
        SELECT * FROM maintenance_windows WHERE applied=0 AND starts_at <= ?
    """, (now,)).fetchall()
    for row in due_to_start:
        window = dict(row)
        service_ids = [r["service_id"] for r in conn.execute(
            "SELECT service_id FROM maintenance_window_services WHERE window_id=?", (window["id"],)
        ).fetchall()]
        if not service_ids:
            conn.execute("UPDATE maintenance_windows SET applied=1, ended=1 WHERE id=?", (window["id"],))
            continue
        conn.execute("UPDATE maintenance_windows SET applied=1 WHERE id=?", (window["id"],))
        for service_id in service_ids:
            service = conn.execute("SELECT * FROM services WHERE id=?", (service_id,)).fetchone()
            if not service:
                conn.execute(
                    "DELETE FROM maintenance_window_services WHERE window_id=? AND service_id=?",
                    (window["id"], service_id))
                continue
            service = dict(service)
            conn.execute("""
                UPDATE maintenance_window_services SET pre_status=?, pre_manual_override=?
                WHERE window_id=? AND service_id=?
            """, (service["status"], service["manual_override"], window["id"], service_id))
            if service_id == service_ids[0]:
                # Also mirrored onto the legacy single-service columns for the
                # *primary* service, so a plain read of the maintenance_windows row
                # (no join needed) still shows a sensible pre_status/pre_manual_override,
                # exactly as before multi-service support existed.
                conn.execute("""
                    UPDATE maintenance_windows SET pre_status=?, pre_manual_override=? WHERE id=?
                """, (service["status"], service["manual_override"], window["id"]))
            conn.execute("UPDATE services SET status='maintenance', manual_override=1 WHERE id=?", (service_id,))
            events.append({"event": "maintenance_started", "service": service, "window": window})

    due_to_end = conn.execute("""
        SELECT * FROM maintenance_windows WHERE applied=1 AND ended=0 AND ends_at <= ?
    """, (now,)).fetchall()
    for row in due_to_end:
        window = dict(row)
        service_rows = conn.execute(
            "SELECT * FROM maintenance_window_services WHERE window_id=?", (window["id"],)
        ).fetchall()
        conn.execute("UPDATE maintenance_windows SET ended=1 WHERE id=?", (window["id"],))
        for srow in service_rows:
            service = conn.execute("SELECT * FROM services WHERE id=?", (srow["service_id"],)).fetchone()
            conn.execute("""
                UPDATE services SET status=?, manual_override=? WHERE id=?
            """, (srow["pre_status"] or "operational", srow["pre_manual_override"] or 0, srow["service_id"]))
            if service:
                events.append({"event": "maintenance_ended", "service": dict(service), "window": window})

    conn.commit()
    conn.close()
    return events


# ---------- Info page ----------
def get_info_page():
    conn = get_db()
    row = conn.execute("SELECT content FROM info_page WHERE id=1").fetchone()
    conn.close()
    return row["content"] if row else ""


def set_info_page(content):
    conn = get_db()
    conn.execute("UPDATE info_page SET content=? WHERE id=1", (content,))
    conn.commit()
    conn.close()


# ---------- Settings (admin password hash) ----------
def get_setting(key, default=None):
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key, value):
    conn = get_db()
    conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=?",
                 (key, value, value))
    conn.commit()
    conn.close()


def get_low_disk_alert_state(path):
    """Whether `path` was already below the low-disk threshold as of the last
    check - persisted (via the settings table, namespaced by mountpoint) rather
    than kept only in memory, specifically so a portal restart while a disk is
    still low doesn't re-send a duplicate "low disk space" notification (this app
    is meant to survive its own restart cleanly - see CLAUDE.md)."""
    return get_setting(f"lowdisk_alert_state:{path}") == "1"


def set_low_disk_alert_state(path, is_low):
    set_setting(f"lowdisk_alert_state:{path}", "1" if is_low else "0")


# ---------- Discord bot status-message tracking ----------
# One tracked message per channel the bot has posted a live status message into -
# on each refresh, that same message is edited in place instead of a new one being
# posted, to avoid spamming the channel. channel_id/message_id are stored as TEXT
# since Discord snowflake IDs exceed the safe range some tooling assumes for INTEGER.
def get_discord_status_message(channel_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM discord_status_messages WHERE channel_id=?",
                        (str(channel_id),)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_discord_status_messages():
    conn = get_db()
    rows = conn.execute("SELECT * FROM discord_status_messages").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_discord_status_message(channel_id, message_id):
    conn = get_db()
    conn.execute("""
        INSERT INTO discord_status_messages (channel_id, message_id, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(channel_id) DO UPDATE SET message_id=?, updated_at=?
    """, (str(channel_id), str(message_id), now_iso(), str(message_id), now_iso()))
    conn.commit()
    conn.close()


def delete_discord_status_message(channel_id):
    conn = get_db()
    conn.execute("DELETE FROM discord_status_messages WHERE channel_id=?", (str(channel_id),))
    conn.commit()
    conn.close()


# ---------- Problem reports ----------
# A visitor-submitted bug/issue report, separate from the admin-authored
# incidents/maintenance system entirely - see app.py's public /report route.
def create_problem_report(message, contact="", service_id=None, reporter_user="", reporter_user_id=""):
    conn = get_db()
    cur = conn.execute("""
        INSERT INTO problem_reports (message, contact, service_id, status, created_at,
                                      reporter_user, reporter_user_id)
        VALUES (?, ?, ?, 'new', ?, ?, ?)
    """, (message, contact, service_id, now_iso(), reporter_user, reporter_user_id))
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def _attach_report_service(report):
    if report["service_id"]:
        service = get_service(report["service_id"])
        report["service_name"] = service["name"] if service else None
    else:
        report["service_name"] = None
    return report


def list_problem_reports():
    conn = get_db()
    rows = conn.execute("SELECT * FROM problem_reports ORDER BY created_at DESC").fetchall()
    conn.close()
    return [_attach_report_service(dict(r)) for r in rows]


def get_problem_report(rid):
    conn = get_db()
    row = conn.execute("SELECT * FROM problem_reports WHERE id=?", (rid,)).fetchone()
    conn.close()
    return _attach_report_service(dict(row)) if row else None


def count_unread_problem_reports():
    conn = get_db()
    count = conn.execute("SELECT COUNT(*) FROM problem_reports WHERE status='new'").fetchone()[0]
    conn.close()
    return count


def count_open_reports_by_service():
    """{service_id: count} of open (not yet resolved - "new" or "reviewed") reports
    per service, for the public page's per-service "N report(s)" indicator - same
    "open" meaning as an incident (unresolved), not the narrower "new/unread"
    meaning count_unread_problem_reports() above uses for the admin nav badge.
    General reports (service_id IS NULL) aren't attributable to any one card, so
    they're excluded here rather than counted against every service."""
    conn = get_db()
    rows = conn.execute("""
        SELECT service_id, COUNT(*) as cnt FROM problem_reports
        WHERE status != 'resolved' AND service_id IS NOT NULL
        GROUP BY service_id
    """).fetchall()
    conn.close()
    return {r["service_id"]: r["cnt"] for r in rows}


def update_problem_report_status(rid, status):
    conn = get_db()
    conn.execute("UPDATE problem_reports SET status=?, reviewed_at=? WHERE id=?", (status, now_iso(), rid))
    conn.commit()
    conn.close()


def delete_problem_report(rid):
    conn = get_db()
    conn.execute("DELETE FROM problem_reports WHERE id=?", (rid,))
    conn.commit()
    conn.close()


# ---------- Scheduled tasks (see scheduler.py) ----------
# The registry in scheduler.py decides which tasks *exist*; these functions only
# store the parts an admin can change (enabled, schedule) and the outcome of the
# last run, so that both survive a restart - the whole point of the table.
SCHEDULE_KINDS = ("interval", "daily")
TASK_STATUSES = ("success", "failed", "skipped")


def ensure_task_row(name, defaults):
    """Creates the row for a task the first time it's seen, using the registry's
    declared defaults. Idempotent: INSERT OR IGNORE, so an existing row (with the
    admin's own schedule in it) is never reset by a later restart."""
    conn = get_db()
    conn.execute("""
        INSERT OR IGNORE INTO scheduled_tasks (name, enabled, schedule_kind, interval_minutes, daily_at)
        VALUES (?, ?, ?, ?, ?)
    """, (name, int(defaults.get("enabled", 1)), defaults.get("schedule_kind", "interval"),
          int(defaults.get("interval_minutes", 60)), defaults.get("daily_at", "03:00")))
    conn.commit()
    conn.close()


def get_task_row(name):
    conn = get_db()
    row = conn.execute("SELECT * FROM scheduled_tasks WHERE name=?", (name,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_task_rows():
    conn = get_db()
    rows = conn.execute("SELECT * FROM scheduled_tasks ORDER BY name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_task_schedule(name, enabled, schedule_kind, interval_minutes, daily_at):
    """Only ever touches the admin-editable columns - never last_run_at/last_status.
    Saving a schedule must not look like the task just ran, and must not reset the
    clock that decides when it next will."""
    if schedule_kind not in SCHEDULE_KINDS:
        schedule_kind = "interval"
    conn = get_db()
    conn.execute("""
        UPDATE scheduled_tasks SET enabled=?, schedule_kind=?, interval_minutes=?, daily_at=?
        WHERE name=?
    """, (int(bool(enabled)), schedule_kind, max(1, int(interval_minutes)), daily_at, name))
    conn.commit()
    conn.close()


def record_task_run(name, status, message="", duration_ms=None, trigger="schedule", ran_at=None):
    """Written after *every* completed attempt, successful or not. A failed run still
    stamps last_run_at on purpose: an interval schedule is measured from it, so
    leaving it unchanged on failure would make a permanently-failing task retry on
    every single scheduler tick instead of on its next scheduled run."""
    conn = get_db()
    conn.execute("""
        UPDATE scheduled_tasks SET last_run_at=?, last_status=?, last_message=?,
               last_duration_ms=?, last_trigger=? WHERE name=?
    """, (ran_at or now_iso(), status, message or "", duration_ms, trigger, name))
    conn.commit()
    conn.close()


# ---------- Cached Jellyfin user list (see jellyfin_auth.py) ----------
def replace_jellyfin_users(users):
    """Full replace, in one transaction, of the locally cached Jellyfin user list.

    Only ever called after a *successful* fetch - a failed sync must leave the
    previous list completely intact, since that list is what keeps already
    signed-in users valid while Jellyfin is unreachable. Doing it in a single
    transaction is what stops a crash mid-write leaving a half-populated list,
    which would look exactly like "most of your users were deleted".

    first_seen_at is preserved for a user that already exists, so it keeps meaning
    "when this portal first saw this account" rather than "when the last sync ran".
    """
    stamp = now_iso()
    conn = get_db()
    try:
        with conn:
            existing = {r["id"]: r for r in
                        conn.execute("SELECT id, first_seen_at, portal_allowed FROM jellyfin_users").fetchall()}
            conn.execute("DELETE FROM jellyfin_users")
            conn.executemany("""
                INSERT INTO jellyfin_users (id, name, name_lower, is_administrator, is_disabled,
                                             first_seen_at, last_synced_at, portal_allowed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, [(u["id"], u["name"], u["name"].strip().lower(),
                   int(bool(u.get("is_administrator"))), int(bool(u.get("is_disabled"))),
                   existing[u["id"]]["first_seen_at"] if u["id"] in existing else stamp,
                   stamp,
                   existing[u["id"]]["portal_allowed"] if u["id"] in existing else 1)
                  for u in users])
    finally:
        conn.close()
    return len(users)


def list_jellyfin_users():
    conn = get_db()
    rows = conn.execute("SELECT * FROM jellyfin_users ORDER BY name COLLATE NOCASE").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_jellyfin_user(user_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM jellyfin_users WHERE id=?", (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_jellyfin_user_by_name(name):
    conn = get_db()
    row = conn.execute("SELECT * FROM jellyfin_users WHERE name_lower=?",
                        ((name or "").strip().lower(),)).fetchone()
    conn.close()
    return dict(row) if row else None


def set_jellyfin_user_allowed(user_id, allowed):
    """Blocks or unblocks one user's access to *this portal*, leaving their Jellyfin
    account completely untouched. Deliberately a separate fact from is_disabled: that
    one mirrors Jellyfin and is overwritten by every sync, this one is the admin's own
    decision and is carried across syncs by replace_jellyfin_users()."""
    conn = get_db()
    conn.execute("UPDATE jellyfin_users SET portal_allowed=? WHERE id=?",
                  (int(bool(allowed)), user_id))
    conn.commit()
    conn.close()


def count_jellyfin_users():
    conn = get_db()
    count = conn.execute("SELECT COUNT(*) FROM jellyfin_users").fetchone()[0]
    conn.close()
    return count


def jellyfin_users_synced_at():
    """When the cached list was last refreshed (max last_synced_at), or None if it
    has never been populated. Distinguishing "never synced" from "synced and empty"
    matters: an empty cache must never be read as "this user doesn't exist"."""
    conn = get_db()
    row = conn.execute("SELECT MAX(last_synced_at) FROM jellyfin_users").fetchone()
    conn.close()
    return row[0] if row and row[0] else None


# ---------- Problem report replies and per-user report history ----------
# What turns the report form from a black hole into a conversation: the admin can
# answer, and the reporter can see the answer plus what became of their report.
REPORT_AUTHORS = ("admin", "user")


def add_report_message(report_id, author, body):
    """Appends one message to a report's thread. Returns its id, or None if there was
    nothing to add - an empty message is silently ignored rather than stored, since
    both sides' forms can be submitted blank by accident.

    Messages are append-only on purpose. The previous single-reply design allowed
    editing, which meant the other party could be looking at text that no longer
    existed; a conversation where earlier messages can change under you is worse than
    one where a correction is just another message."""
    body = (body or "").strip()
    if not body or author not in REPORT_AUTHORS:
        return None
    conn = get_db()
    cur = conn.execute("""
        INSERT INTO report_messages (report_id, author, body, created_at, seen)
        VALUES (?, ?, ?, ?, 0)
    """, (report_id, author, body, now_iso()))
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def list_report_messages(report_id):
    conn = get_db()
    rows = conn.execute("""
        SELECT * FROM report_messages WHERE report_id=? ORDER BY created_at, id
    """, (report_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def attach_report_messages(reports):
    """Fills in each report's `messages` list in one query rather than one per report -
    both the admin list and a user's account page render every thread on the page."""
    reports = list(reports)
    if not reports:
        return reports
    ids = [r["id"] for r in reports]
    placeholders = ",".join("?" * len(ids))
    conn = get_db()
    rows = conn.execute(f"""
        SELECT * FROM report_messages WHERE report_id IN ({placeholders})
        ORDER BY created_at, id
    """, ids).fetchall()
    conn.close()
    grouped = {}
    for r in rows:
        grouped.setdefault(r["report_id"], []).append(dict(r))
    for report in reports:
        report["messages"] = grouped.get(report["id"], [])
    return reports


def mark_report_messages_seen(report_ids, author):
    """Marks messages written by `author` as seen, for the given reports. Called when
    the *other* side opens the page - the admin reading /admin/reports marks the
    user's messages, and a user opening their account page marks the admin's.

    Returns how many rows changed so a caller can skip follow-up work when there was
    nothing to mark, which is the common case on a page read far more than written."""
    report_ids = list(report_ids)
    if not report_ids or author not in REPORT_AUTHORS:
        return 0
    placeholders = ",".join("?" * len(report_ids))
    conn = get_db()
    cur = conn.execute(f"""
        UPDATE report_messages SET seen=1
        WHERE seen=0 AND author=? AND report_id IN ({placeholders})
    """, [author] + report_ids)
    conn.commit()
    changed = cur.rowcount
    conn.close()
    return changed


def set_problem_report_incident(rid, incident_id):
    """Links a report to the incident an admin raised from it, so the reporter can be
    told what came of it rather than just seeing the report marked resolved."""
    conn = get_db()
    conn.execute("UPDATE problem_reports SET incident_id=? WHERE id=?", (incident_id, rid))
    conn.commit()
    conn.close()


def list_reports_for_user(user_id):
    """One signed-in user's own reports, newest first, with the linked incident's
    current title/status where there is one.

    Scoped by Jellyfin's stable user id and nothing else. A blank user_id must never
    match anything: every anonymous report has reporter_user_id = '', so a caller
    that passed an empty id would otherwise be handed every anonymous report in the
    database. The guard is here rather than in the route so it can't be forgotten by
    a second caller later.

    LEFT JOIN so a report whose incident was deleted still lists, with no incident
    attached - there's no FK to null it out for us."""
    if not user_id:
        return []
    conn = get_db()
    rows = conn.execute("""
        SELECT r.*, i.title AS incident_title, i.status AS incident_status
        FROM problem_reports r
        LEFT JOIN incidents i ON i.id = r.incident_id
        WHERE r.reporter_user_id = ?
        ORDER BY r.created_at DESC
    """, (user_id,)).fetchall()
    conn.close()
    return [_attach_report_service(dict(r)) for r in rows]


def count_unseen_replies(user_id):
    """Admin messages this user hasn't read yet - backs the dot on the sign-in chip,
    which is the only thing that tells someone an answer is waiting."""
    if not user_id:
        return 0
    conn = get_db()
    count = conn.execute("""
        SELECT COUNT(*) FROM report_messages m
        JOIN problem_reports r ON r.id = m.report_id
        WHERE r.reporter_user_id = ? AND m.author = 'admin' AND m.seen = 0
    """, (user_id,)).fetchone()[0]
    conn.close()
    return count


def count_unseen_user_messages():
    """Replies from reporters the admin hasn't read yet, feeding the admin nav's
    Reports badge. Without this the admin has no way of knowing somebody answered -
    which would make the whole two-way conversation pointless, since only one side
    would ever notice a new message."""
    conn = get_db()
    count = conn.execute("""
        SELECT COUNT(*) FROM report_messages WHERE author = 'user' AND seen = 0
    """).fetchone()[0]
    conn.close()
    return count


def mark_replies_seen(user_id):
    """Called when a user opens their account page: marks the admin's messages on
    *their own* reports as read, and nothing else."""
    if not user_id:
        return 0
    conn = get_db()
    cur = conn.execute("""
        UPDATE report_messages SET seen=1
        WHERE seen=0 AND author='admin' AND report_id IN (
            SELECT id FROM problem_reports WHERE reporter_user_id = ?
        )
    """, (user_id,))
    conn.commit()
    changed = cur.rowcount
    conn.close()
    return changed


# ---------- Per-user preferences (user-owned, unlike jellyfin_users) ----------
USER_THEMES = ("auto", "dark", "light")
# Must list every column set_user_preferences() writes, and with the same default the
# schema declares. get_user_preferences() seeds from this for a user who has never
# saved anything, and set_user_preferences() reads the *current* values to fill in the
# fields a caller didn't name - so a key missing here becomes None, then 0, and an
# "on by default" preference is silently switched off the first time the user saves
# anything at all.
DEFAULT_USER_PREFERENCES = {
    "theme": "auto",
    "contact": "",
    "notify_email": "",
    "notify_discord_id": "",
    "notify_own_reports": True,
    "notify_service_events": False,
    "notify_requests": True,
    "seerr_user_id": "",
}


def get_user_preferences(user_id):
    """Always returns a complete dict, so callers never branch on "has this user ever
    saved anything". An unrecognised stored theme falls back to the default rather
    than being handed to a template that will render it into an HTML attribute."""
    prefs = dict(DEFAULT_USER_PREFERENCES)
    if not user_id:
        return prefs
    conn = get_db()
    row = conn.execute("SELECT * FROM user_preferences WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    if row:
        prefs["theme"] = row["theme"] if row["theme"] in USER_THEMES else "auto"
        prefs["contact"] = row["contact"] or ""
        prefs["notify_email"] = row["notify_email"] or ""
        prefs["notify_discord_id"] = row["notify_discord_id"] or ""
        prefs["notify_own_reports"] = bool(row["notify_own_reports"])
        prefs["notify_service_events"] = bool(row["notify_service_events"])
        prefs["notify_requests"] = bool(row["notify_requests"])
        prefs["seerr_user_id"] = row["seerr_user_id"] or ""
    return prefs


# Every column set_user_preferences() can write, and how to coerce it. Named fields
# only: a caller that knows about one setting (the floating theme toggle) must not be
# able to blank out the others just by not mentioning them - which is exactly what a
# "write the whole row" helper would do.
_USER_PREFERENCE_FIELDS = {
    "notify_email": str,
    "notify_discord_id": str,
    "notify_own_reports": lambda v: int(bool(v)),
    "notify_service_events": lambda v: int(bool(v)),
    "notify_requests": lambda v: int(bool(v)),
    "seerr_user_id": str,
}


def set_user_preferences(user_id, theme=None, contact=None, **fields):
    """Upsert. Only the named fields are written, so a caller that only knows about
    the theme (the toggle button's endpoint) can't blank out the contact."""
    if not user_id:
        return
    unknown = set(fields) - set(_USER_PREFERENCE_FIELDS)
    if unknown:
        raise ValueError(f"Unknown user preference(s): {sorted(unknown)}")
    current = get_user_preferences(user_id)
    theme = theme if theme in USER_THEMES else current["theme"]
    contact = current["contact"] if contact is None else contact

    values = {}
    for name, coerce in _USER_PREFERENCE_FIELDS.items():
        raw = fields.get(name, current.get(name))
        values[name] = coerce(raw if raw is not None else "")

    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    updates = ", ".join(f"{name}=excluded.{name}" for name in values)
    conn = get_db()
    conn.execute(f"""
        INSERT INTO user_preferences (user_id, theme, contact, updated_at, {columns})
        VALUES (?, ?, ?, ?, {placeholders})
        ON CONFLICT(user_id) DO UPDATE SET theme=excluded.theme, contact=excluded.contact,
                                            updated_at=excluded.updated_at, {updates}
    """, (user_id, theme, contact, now_iso(), *values.values()))
    conn.commit()
    conn.close()


# ---------- Per-user notification queue (see user_notify.py) ----------
# Outbound delivery never happens in the request that triggered it - the request only
# writes a row here, and a scheduled task drains it. Same rule every other outbound
# call in this app follows, and the reason a slow SMTP server can't make an admin's
# "reply" button hang.
MAX_NOTIFICATION_ATTEMPTS = 5


def enqueue_notification(user_id, event, subject, body):
    """Queues one notification. Cheap enough to call from a request handler: one INSERT,
    no network."""
    if not user_id:
        return None
    conn = get_db()
    cur = conn.execute("""
        INSERT INTO notification_queue (user_id, event, subject, body, created_at)
        VALUES (?, ?, ?, ?, ?)
    """, (user_id, event, subject, body, now_iso()))
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def pending_notifications(limit=50):
    """Unsent, oldest first, excluding anything that has already failed too many times -
    a permanently undeliverable row must not block the queue behind it forever."""
    conn = get_db()
    rows = conn.execute("""
        SELECT * FROM notification_queue
        WHERE sent_at IS NULL AND attempts < ?
        ORDER BY created_at LIMIT ?
    """, (MAX_NOTIFICATION_ATTEMPTS, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def mark_notification_sent(notification_id):
    conn = get_db()
    conn.execute("UPDATE notification_queue SET sent_at=?, last_error='' WHERE id=?",
                  (now_iso(), notification_id))
    conn.commit()
    conn.close()


def mark_notification_failed(notification_id, error):
    conn = get_db()
    conn.execute("UPDATE notification_queue SET attempts=attempts+1, last_error=? WHERE id=?",
                  (str(error)[:300], notification_id))
    conn.commit()
    conn.close()


def prune_notification_queue(days=30):
    """Delivered rows are history, not state. Kept briefly so an admin can see that
    something went out, then removed - this table would otherwise grow forever like
    status_history did."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = get_db()
    cur = conn.execute("DELETE FROM notification_queue WHERE sent_at IS NOT NULL AND sent_at < ?",
                        (cutoff,))
    conn.commit()
    deleted = cur.rowcount
    conn.close()
    return deleted


def recent_notifications(limit=15):
    """The most recent queue entries, for the admin page - so "did that actually go
    out?" is answerable without reading the log."""
    conn = get_db()
    rows = conn.execute("""
        SELECT * FROM notification_queue ORDER BY created_at DESC, id DESC LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def notification_queue_summary():
    """Counts for the admin page: waiting, sent, and given up on."""
    conn = get_db()
    row = conn.execute("""
        SELECT
          SUM(CASE WHEN sent_at IS NULL AND attempts < ? THEN 1 ELSE 0 END) AS pending,
          SUM(CASE WHEN sent_at IS NOT NULL THEN 1 ELSE 0 END) AS sent,
          SUM(CASE WHEN sent_at IS NULL AND attempts >= ? THEN 1 ELSE 0 END) AS failed
        FROM notification_queue
    """, (MAX_NOTIFICATION_ATTEMPTS, MAX_NOTIFICATION_ATTEMPTS)).fetchone()
    conn.close()
    return {"pending": row["pending"] or 0, "sent": row["sent"] or 0, "failed": row["failed"] or 0}


def users_opted_into(field):
    """Jellyfin user ids who have switched `field` on. Used for the broadcast-style
    events (maintenance), where the alternative is reading every user's preferences
    one at a time."""
    if field not in _USER_PREFERENCE_FIELDS:
        raise ValueError(f"Unknown user preference: {field}")
    conn = get_db()
    rows = conn.execute(f"SELECT user_id FROM user_preferences WHERE {field}=1").fetchall()
    conn.close()
    return [r["user_id"] for r in rows]


def user_id_for_seerr_user(seerr_user_id):
    """The Jellyfin user who has linked this Seerr account, or None. Only ever finds a
    link that was established from Seerr's own jellyfinUserId - never a guess."""
    if not seerr_user_id:
        return None
    conn = get_db()
    row = conn.execute("SELECT user_id FROM user_preferences WHERE seerr_user_id=?",
                        (str(seerr_user_id),)).fetchone()
    conn.close()
    return row["user_id"] if row else None
