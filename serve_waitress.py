"""
serve_waitress.py — PRODUCTION entry point.
Uses waitress (a proper WSGI server) instead of Flask's dev server.

Run with:
    python serve_waitress.py

Run THIS script at system startup (systemd, Task Scheduler, supervisord...),
not app.py.
"""
import os
from waitress import serve

import db
from app import app, start_background_checker

PORT = int(os.environ.get("PORTAL_PORT", "5000"))

if __name__ == "__main__":
    db.init_db()
    start_background_checker()
    print(f"status-portal started on http://0.0.0.0:{PORT}")
    serve(app, host="0.0.0.0", port=PORT)
