"""
db.py — Toute la couche base de données (SQLite).
Pas d'ORM, juste du SQL simple pour rester lisible et facile à modifier.
"""
import sqlite3
import os
from datetime import datetime

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
            sort_order INTEGER DEFAULT 0
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
            FOREIGN KEY (service_id) REFERENCES services (id) ON DELETE SET NULL
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
                   ("Ajoute ici des infos pratiques (accès SMB, VPN, contact...) depuis le panel admin.",))
        conn.commit()

    if c.execute("SELECT COUNT(*) FROM services").fetchone()[0] == 0:
        seed = [
            ("Jellyfin", "Streaming films & séries", "http://SERVEUR:8096", "🎬", "operational", 0, 1, "http://SERVEUR:8096/health", 0),
            ("Partage SMB", "Accès fichiers réseau", "smb://SERVEUR/partage", "📁", "operational", 0, 0, "", 1),
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
        INSERT INTO services (name, description, url, icon, status, manual_override, auto_check, check_url, sort_order)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (data["name"], data.get("description", ""), data["url"], data.get("icon", "⚙"),
          data.get("status", "operational"), int(data.get("manual_override", 0)),
          int(data.get("auto_check", 0)), data.get("check_url", ""), int(data.get("sort_order", 0))))
    conn.commit()
    conn.close()


def update_service(service_id, data):
    conn = get_db()
    conn.execute("""
        UPDATE services SET name=?, description=?, url=?, icon=?, status=?, manual_override=?,
        auto_check=?, check_url=?, sort_order=? WHERE id=?
    """, (data["name"], data.get("description", ""), data["url"], data.get("icon", "⚙"),
          data.get("status", "operational"), int(data.get("manual_override", 0)),
          int(data.get("auto_check", 0)), data.get("check_url", ""),
          int(data.get("sort_order", 0)), service_id))
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
