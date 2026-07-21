"""
db.py — The entire database layer (SQLite).
No ORM, just plain SQL to stay readable and easy to modify.
"""
import sqlite3
import os
from datetime import datetime, timedelta

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "instance", "portal.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


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
            group_name TEXT DEFAULT ''
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
            enabled INTEGER NOT NULL DEFAULT 1
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
    return datetime.utcnow().isoformat()


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


def create_service(data):
    conn = get_db()
    conn.execute("""
        INSERT INTO services (name, description, url, icon, status, manual_override, auto_check, check_url, sort_order, group_name)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (data["name"], data.get("description", ""), data["url"], data.get("icon", "⚙"),
          data.get("status", "operational"), int(data.get("manual_override", 0)),
          int(data.get("auto_check", 0)), data.get("check_url", ""), int(data.get("sort_order", 0)),
          data.get("group_name", "").strip()))
    conn.commit()
    conn.close()


def update_service(service_id, data):
    conn = get_db()
    conn.execute("""
        UPDATE services SET name=?, description=?, url=?, icon=?, status=?, manual_override=?,
        auto_check=?, check_url=?, sort_order=?, group_name=? WHERE id=?
    """, (data["name"], data.get("description", ""), data["url"], data.get("icon", "⚙"),
          data.get("status", "operational"), int(data.get("manual_override", 0)),
          int(data.get("auto_check", 0)), data.get("check_url", ""),
          int(data.get("sort_order", 0)), data.get("group_name", "").strip(), service_id))
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
def list_incidents(limit=None):
    conn = get_db()
    q = "SELECT * FROM incidents ORDER BY started_at DESC"
    if limit:
        q += f" LIMIT {int(limit)}"
    rows = conn.execute(q).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_incident(iid):
    conn = get_db()
    row = conn.execute("SELECT * FROM incidents WHERE id=?", (iid,)).fetchone()
    conn.close()
    return dict(row) if row else None


def create_incident(data):
    conn = get_db()
    conn.execute("""
        INSERT INTO incidents (service_id, title, description, status, started_at)
        VALUES (?, ?, ?, ?, ?)
    """, (data.get("service_id") or None, data["title"], data.get("description", ""),
          data.get("status", "investigating"), now_iso()))
    conn.commit()
    conn.close()


def update_incident(iid, data):
    conn = get_db()
    resolved_at = now_iso() if data.get("status") == "resolved" else None
    conn.execute("""
        UPDATE incidents SET service_id=?, title=?, description=?, status=?,
        resolved_at=COALESCE(?, resolved_at) WHERE id=?
    """, (data.get("service_id") or None, data["title"], data.get("description", ""),
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
    auto_created so the recovery path only ever auto-resolves incidents it opened itself."""
    conn = get_db()
    cur = conn.execute("""
        INSERT INTO incidents (service_id, title, description, status, started_at, auto_created)
        VALUES (?, ?, '', ?, ?, 1)
    """, (service_id, title, status, now_iso()))
    conn.commit()
    incident_id = cur.lastrowid
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
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
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
    conn.execute("""
        INSERT INTO integrations (name, kind, base_url, api_key, enabled) VALUES (?, ?, ?, ?, ?)
    """, (data["name"], data["kind"], data["base_url"].rstrip("/"), data.get("api_key", ""),
          int(data.get("enabled", 1))))
    conn.commit()
    conn.close()


def update_integration(iid, data):
    conn = get_db()
    if data.get("api_key"):
        conn.execute("""
            UPDATE integrations SET name=?, kind=?, base_url=?, api_key=?, enabled=? WHERE id=?
        """, (data["name"], data["kind"], data["base_url"].rstrip("/"), data["api_key"],
              int(data.get("enabled", 1)), iid))
    else:
        # Blank api_key on the edit form means "keep the existing one" - never overwrite
        # a stored key with an empty string just because the admin left the field blank.
        conn.execute("""
            UPDATE integrations SET name=?, kind=?, base_url=?, enabled=? WHERE id=?
        """, (data["name"], data["kind"], data["base_url"].rstrip("/"),
              int(data.get("enabled", 1)), iid))
    conn.commit()
    conn.close()


def delete_integration(iid):
    conn = get_db()
    conn.execute("DELETE FROM integrations WHERE id=?", (iid,))
    conn.commit()
    conn.close()


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
