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
import logging
import logging.handlers
import os
import sys
import threading

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "instance", "logs")
LOG_FILE = os.path.join(LOG_DIR, "app.log")

_configured = False


def init_logging():
    """Idempotent - safe to call from more than one entry point/more than once."""
    global _configured
    if _configured:
        return
    _configured = True

    os.makedirs(LOG_DIR, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")

    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    threading.excepthook = _log_thread_exception


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
