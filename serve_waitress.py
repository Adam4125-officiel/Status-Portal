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
import logging_setup
import monitoring
import scheduler
import updater
from app import app, start_background_checker

if __name__ == "__main__":
    logging_setup.init_logging()
    db.init_db()
    # If the previous shutdown was an in-app update restarting into a new version,
    # this is where that gets confirmed (or reported as not having taken effect).
    updater.check_pending_marker()
    start_background_checker()
    monitoring.start_background_refresh(config.RESOURCE_REFRESH_SECONDS)
    scheduler.start()
    discord_bot.start()
    print(f"status-portal started on http://0.0.0.0:{config.PORT}")
    serve(app, host="0.0.0.0", port=config.PORT, threads=config.WAITRESS_THREADS)
