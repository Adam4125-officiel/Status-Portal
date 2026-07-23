"""
serve_waitress.py — PRODUCTION entry point.
Uses waitress (a proper WSGI server) instead of Flask's dev server.

Run with:
    python serve_waitress.py

Run THIS script at system startup (systemd, Task Scheduler, supervisord...),
not app.py.
"""
from waitress import serve

import config
import db
import discord_bot
import monitoring
from app import app, start_background_checker

if __name__ == "__main__":
    db.init_db()
    start_background_checker()
    monitoring.start_background_refresh(config.RESOURCE_REFRESH_SECONDS)
    discord_bot.start()
    print(f"status-portal started on http://0.0.0.0:{config.PORT}")
    serve(app, host="0.0.0.0", port=config.PORT)
