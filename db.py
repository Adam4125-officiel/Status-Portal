"""
db.py — The entire database layer (SQLite).
No ORM, just plain SQL to stay readable and easy to modify.
"""
import sqlite3
import os
from datetime import datetime, timedelta, timezone

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "instance", "portal.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


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
        INSERT INTO services (name, description, url, icon, status, manual_override, auto_check, check_url, sort_order, group_name, auto_incident, slow_threshold_ms, startup_grace_seconds, retry_count, retry_interval_seconds, ignore_in_overall_status, api_health_mode)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (data["name"], data.get("description", ""), data.get("url", ""), data.get("icon", "⚙"),
          data.get("status", "operational"), int(data.get("manual_override", 0)),
          int(data.get("auto_check", 0)), data.get("check_url", ""), int(data.get("sort_order", 0)),
          data.get("group_name", "").strip(), int(data.get("auto_incident", 1)),
          _slow_threshold_ms(data), int(data.get("startup_grace_seconds") or 0),
          int(data.get("retry_count") or 0), int(data.get("retry_interval_seconds") or 5),
          int(data.get("ignore_in_overall_status", 0)), _api_health_mode(data)))
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
        ignore_in_overall_status=?, api_health_mode=? WHERE id=?
    """, (data["name"], data.get("description", ""), data["url"], data.get("icon", "⚙"),
          data.get("status", "operational"), int(data.get("manual_override", 0)),
          int(data.get("auto_check", 0)), data.get("check_url", ""),
          int(data.get("sort_order", 0)), data.get("group_name", "").strip(),
          int(data.get("auto_incident", 1)), _slow_threshold_ms(data),
          int(data.get("startup_grace_seconds") or 0), int(data.get("retry_count") or 0),
          int(data.get("retry_interval_seconds") or 5), int(data.get("ignore_in_overall_status", 0)),
          _api_health_mode(data), service_id))
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


def list_incidents(limit=None, offset=0, max_age_days=None):
    """max_age_days never hides a still-open incident (resolved_at IS NULL)
    regardless of how long it's been going on - only a *resolved* incident's own
    age counts toward the cutoff, since an ongoing problem shouldn't disappear
    from the public page just because it started a while ago."""
    conn = get_db()
    q = "SELECT * FROM incidents"
    params = []
    if max_age_days:
        q += " WHERE resolved_at IS NULL OR resolved_at >= datetime('now', ?)"
        params.append(f"-{int(max_age_days)} days")
    q += " ORDER BY started_at DESC"
    if limit:
        q += f" LIMIT {int(limit)} OFFSET {int(offset)}"
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


# ---------- Status history (append-only; powers the uptime %) ----------
def record_status_history(service_id, status, response_ms):
    conn = get_db()
    conn.execute("""
        INSERT INTO status_history (service_id, status, response_ms, checked_at) VALUES (?, ?, ?, ?)
    """, (service_id, status, response_ms, now_iso()))
    conn.commit()
    conn.close()


def get_uptime_percentage(service_id, days=30):
    """Share of recorded checks that were NOT 'down' over the last N days. Checks logged
    while the service was in 'maintenance' are excluded entirely (planned maintenance
    shouldn't count against uptime). Returns None if there's no history yet (e.g. a
    service with auto-check off, which never gets a status_history row)."""
    conn = get_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = conn.execute("""
        SELECT status FROM status_history WHERE service_id=? AND checked_at >= ? AND status != 'maintenance'
    """, (service_id, cutoff)).fetchall()
    conn.close()
    total = len(rows)
    if total == 0:
        return None
    up = sum(1 for r in rows if r["status"] != "down")
    return round(up / total * 100, 1)


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
        INSERT INTO integrations (name, kind, base_url, api_key, enabled, service_id, show_on_public, auto_incident)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (data["name"], data["kind"], data["base_url"].rstrip("/"), data.get("api_key", ""),
          int(data.get("enabled", 1)), data.get("service_id") or None, int(data.get("show_on_public", 0)),
          int(data.get("auto_incident", 0))))
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def update_integration(iid, data):
    conn = get_db()
    service_id = data.get("service_id") or None
    show_on_public = int(data.get("show_on_public", 0))
    auto_incident = int(data.get("auto_incident", 0))
    if data.get("api_key"):
        conn.execute("""
            UPDATE integrations SET name=?, kind=?, base_url=?, api_key=?, enabled=?, service_id=?,
            show_on_public=?, auto_incident=? WHERE id=?
        """, (data["name"], data["kind"], data["base_url"].rstrip("/"), data["api_key"],
              int(data.get("enabled", 1)), service_id, show_on_public, auto_incident, iid))
    else:
        # Blank api_key on the edit form means "keep the existing one" - never overwrite
        # a stored key with an empty string just because the admin left the field blank.
        conn.execute("""
            UPDATE integrations SET name=?, kind=?, base_url=?, enabled=?, service_id=?,
            show_on_public=?, auto_incident=? WHERE id=?
        """, (data["name"], data["kind"], data["base_url"].rstrip("/"),
              int(data.get("enabled", 1)), service_id, show_on_public, auto_incident, iid))
    conn.commit()
    conn.close()


def list_integrations_for_service(service_id):
    conn = get_db()
    rows = conn.execute("""
        SELECT * FROM integrations WHERE service_id=? AND enabled=1 AND show_on_public=1
    """, (service_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


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
def create_problem_report(message, contact="", service_id=None):
    conn = get_db()
    cur = conn.execute("""
        INSERT INTO problem_reports (message, contact, service_id, status, created_at)
        VALUES (?, ?, ?, 'new', ?)
    """, (message, contact, service_id, now_iso()))
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
