"""
scheduler.py — A small generic framework for recurring background jobs.

The shape, and why it is this shape
-----------------------------------
This app already had three background threads (health checks, resource polling,
the Discord bot) and every one of them hardcodes both *what* it does and *when* it
does it. That's fine for one job; it stops being fine the moment a second job wants
a different cadence, an on/off switch and a "run it now" button - at which point you
either duplicate the loop, or you build this. So:

- **The code owns the registry, the database owns the settings.** `register()` is
  called at import time by whichever module owns the job (see `jellyfin_auth.py`);
  the `scheduled_tasks` table holds only what an admin can change (enabled,
  schedule) plus the outcome of the last run. Adding a task therefore needs no
  migration, and removing one leaves nothing behind that matters.
- **A task is a plain callable** returning a short human-readable message. It
  signals "couldn't run, and that isn't an error" by raising `TaskSkipped`, and
  failure by raising anything else. No base class to subclass, no config object.
- **Every run is isolated.** Each due task runs in its own short-lived thread, so a
  slow or hung task delays nothing but itself, and every exception is caught and
  recorded against that task - the scheduler thread itself cannot die from a task,
  and one broken task cannot stop the others. A failure caused by a remote service
  being down is logged as a one-line warning rather than a traceback (see
  `TaskUnavailable`); anything else still gets the full traceback, because that is
  the case where the traceback is the point.
- **A task never runs twice concurrently**, whatever the trigger. Each has its own
  lock; the scheduler skips a task whose lock is held, and the admin panel's "Run
  now" reports it as already running rather than starting a second copy.

Schedules are deliberately "every N minutes" or "daily at HH:MM (UTC)" rather than
cron expressions: cron would mean either a new dependency or a hand-rolled parser
with its own bug surface, and neither buys anything the two modes here don't already
cover. See CLAUDE.md if that ever needs revisiting.

This module imports only `config` and `db` - never `app.py` (which imports it), and
never Flask - so a task body can be exercised from a plain unit test with no request
context, and the scheduler can in principle run without the web layer at all.
"""
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

import requests

import config
import db

_logger = logging.getLogger(__name__)


class TaskSkipped(Exception):
    """Raised by a task body meaning "there was nothing to do, and that is not a
    failure" - e.g. the feature it serves isn't configured yet. Recorded as
    'skipped' rather than 'failed' so an admin can tell "you haven't set this up"
    apart from "this is broken", which are very different things to see in a list."""


class TaskUnavailable(Exception):
    """Raised by a task body meaning "this failed because something outside this portal
    was unavailable, and the reason is already in the message".

    Recorded as **failed**, not skipped, because it is one: the work did not happen and
    /admin/tasks must say so. What changes is only how it is logged. A traceback is a
    description of a fault in *this* code, so printing a two-deep chained one because
    Seerr answered 502 sends whoever reads the log hunting for a bug in the portal -
    which is exactly what happened when this didn't exist (docs/HISTORY.md -> "a 502
    that read like a crash"). There is nothing in the traceback that the message doesn't
    already say."""


def _caused_by_network_failure(exc):
    """Whether `exc` happened because a remote service was unreachable or erroring.

    Walks the exception chain, so a task that wraps a requests error in its own
    RuntimeError to attach a better message still gets the concise treatment without
    having to know this exists - which is what most of them do, and what the two sync
    tasks get for free by simply letting the original propagate."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, requests.RequestException):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


class Task:
    """One registered job. Immutable once registered - everything an admin can
    change lives in the database row, not here."""

    def __init__(self, name, label, description, run, default_enabled=True,
                 default_schedule_kind="interval", default_interval_minutes=60,
                 default_daily_at="03:00"):
        self.name = name
        self.label = label
        self.description = description
        self.run = run
        self.defaults = {
            "enabled": int(bool(default_enabled)),
            "schedule_kind": default_schedule_kind,
            "interval_minutes": default_interval_minutes,
            "daily_at": default_daily_at,
        }


# name -> Task. Populated at import time by the modules that own each task, and
# never mutated afterwards, so it needs no locking and no resetting between tests.
_registry = {}

# name -> {"lock": Lock, "started_at": float|None}. Runtime-only: which tasks are
# currently executing, and the lock that keeps a task from overlapping itself. Reset
# between tests via clear_caches().
_task_state = {}
_state_lock = threading.Lock()


class BackgroundLoop:
    """A long-running thread that is *not* a scheduled task, described so the admin
    panel can list it alongside the ones that are.

    Three of this app's recurring jobs deliberately did not become tasks (see
    CLAUDE.md -> Scheduled tasks): the health-check loop is the core of the app and a
    browser-reachable off switch for it would also switch off incident detection;
    resource polling runs faster than SCHEDULER_TICK_SECONDS, so the scheduler
    physically cannot drive it; and the Discord bot's refresh runs inside discord.py's
    own asyncio loop, where moving it here would mean scheduling coroutines across
    threads for no gain.

    Leaving them entirely undescribed is the worse answer, though - "what does this
    portal do on a timer" then has two places to look and one of them is the source
    code. So they are registered here as read-only entries: same page, same
    vocabulary, explicitly not controllable."""

    def __init__(self, name, label, description, interval_seconds, configured_by, is_alive):
        self.name = name
        self.label = label
        self.description = description
        self.interval_seconds = interval_seconds
        self.configured_by = configured_by
        self.is_alive = is_alive


# name -> BackgroundLoop. Same shape and lifecycle as _registry above: populated at
# import time, never mutated afterwards, so it needs no locking.
_loops = {}


def register_loop(name, label, description, interval_seconds, configured_by, is_alive):
    """Registers a read-only background loop for the admin page to list. `is_alive` is
    a callable rather than a value because liveness is read at render time - a thread
    that has died since startup must show as dead, not as whatever it was when it was
    registered."""
    _loops[name] = BackgroundLoop(name, label, description, interval_seconds,
                                  configured_by, is_alive)
    return _loops[name]


def registered_loops():
    return list(_loops.values())


def loop_view(loop):
    """One loop's row. `alive` is None when the answer is "not applicable" rather than
    "no" - an optional feature that was never switched on has no thread to be dead."""
    try:
        alive = loop.is_alive()
    except Exception:
        # Reading liveness must never be able to break the page that reports it.
        _logger.exception("Could not read liveness for background loop '%s'", loop.name)
        alive = None
    return {
        "name": loop.name,
        "label": loop.label,
        "description": loop.description,
        "interval_seconds": loop.interval_seconds,
        "configured_by": loop.configured_by,
        "alive": alive,
    }


def list_loop_views():
    return [loop_view(loop) for loop in registered_loops()]


def register(name, label, description, run, **defaults):
    """Called at import time by the module that owns the task. Re-registering the
    same name replaces the definition rather than raising: a module re-imported
    under a test runner must not blow up on the second import."""
    _registry[name] = Task(name, label, description, run, **defaults)
    return _registry[name]


def registered_tasks():
    """Registration order, not alphabetical - the order tasks were added is a more
    meaningful grouping for the admin list than their names happen to be."""
    return list(_registry.values())


def get_task(name):
    return _registry.get(name)


def clear_caches():
    """Drops the in-memory "which tasks are running" state. Nothing persistent is
    touched - schedules and last-run results live in the database. Used by the test
    suite (see tests/conftest.py); deliberately *not* wired to the admin panel's
    clear-caches button, which is about derived data, not about forgetting that a
    thread is currently running."""
    with _state_lock:
        _task_state.clear()


def _state(name):
    with _state_lock:
        if name not in _task_state:
            _task_state[name] = {"lock": threading.Lock(), "started_at": None}
        return _task_state[name]


def is_running(name):
    return _state(name)["started_at"] is not None


def _row(task):
    """The task's database row, created from the registry defaults on first sight."""
    row = db.get_task_row(task.name)
    if row is None:
        db.ensure_task_row(task.name, task.defaults)
        row = db.get_task_row(task.name)
    return row


def _rows_for(tasks):
    """{task_name: row} for every task in `tasks` - one db.list_task_rows() call
    instead of one db.get_task_row() call per task, for the common case where every
    task's row already exists (true after the very first tick this app has ever
    run). A task whose row genuinely doesn't exist yet still goes through _row()
    (get-then-create-then-get-again) exactly as before - this only removes the
    redundant per-task read when there's nothing to create."""
    rows = {r["name"]: r for r in db.list_task_rows()}
    for task in tasks:
        if task.name not in rows:
            rows[task.name] = _row(task)
    return rows


def _parse_iso(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    # Rows written before this app was consistent about it, or by SQLite defaults,
    # can be naive. Treat a naive stamp as UTC - everything this app stores is.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _parse_hhmm(value):
    """'HH:MM' -> (h, m), falling back to 03:00 for anything unparseable rather than
    raising - a malformed stored value must not take the whole scheduler down."""
    try:
        hours, minutes = str(value or "").split(":")
        h, m = int(hours), int(minutes)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except (ValueError, AttributeError):
        pass
    return 3, 0


def next_run_at(task, row=None, now=None):
    """When this task is next due, as an aware UTC datetime, or None if it's
    disabled. Computed from the stored `last_run_at` rather than tracked in memory,
    which is what makes the schedule survive a restart: a portal that was down over
    a daily task's time runs it on the next tick after it comes back, exactly as it
    would have if it had merely been busy.

    A never-run *interval* task is due immediately (there's no reason to wait an hour
    to do something for the first time), while a never-run *daily* task is due at its
    next HH:MM - firing a 03:00 job the moment the portal first starts at 14:00 would
    be surprising, not helpful."""
    row = row or _row(task)
    if not row["enabled"]:
        return None
    now = now or datetime.now(timezone.utc)
    last = _parse_iso(row["last_run_at"])
    if row["schedule_kind"] == "daily":
        hour, minute = _parse_hhmm(row["daily_at"])
        base = last or now
        candidate = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= base:
            candidate += timedelta(days=1)
        return candidate
    if last is None:
        return now
    return last + timedelta(minutes=max(1, int(row["interval_minutes"] or 1)))


def task_view(task, now=None, row=None):
    """Everything the admin page needs about one task, in one dict: the registry's
    static description plus the database row plus the derived next-run time. The
    template does no computation of its own.

    `row` lets list_task_views() pass in an already-fetched row (see _rows_for())
    instead of this doing its own db.get_task_row() per call - every other caller
    omits it and gets the original single-task behavior via _row()."""
    if row is None:
        row = _row(task)
    return {
        "name": task.name,
        "label": task.label,
        "description": task.description,
        "enabled": bool(row["enabled"]),
        "schedule_kind": row["schedule_kind"],
        "interval_minutes": row["interval_minutes"],
        "daily_at": row["daily_at"],
        "last_run_at": row["last_run_at"],
        "last_status": row["last_status"],
        "last_message": row["last_message"],
        "last_duration_ms": row["last_duration_ms"],
        "last_trigger": row["last_trigger"],
        "running": is_running(task.name),
        "next_run_at": (next_run_at(task, row, now).isoformat()
                        if row["enabled"] and not is_running(task.name) else None),
    }


def list_task_views(now=None):
    tasks = registered_tasks()
    rows = _rows_for(tasks)
    return [task_view(task, now, row=rows[task.name]) for task in tasks]


def save_schedule(name, enabled, schedule_kind, interval_minutes, daily_at):
    """The admin panel's entry point for changing a task's settings.

    Goes through here rather than calling db.update_task_schedule() directly for the
    same reason run_task() does: both of those are UPDATE statements, which silently
    write nothing on a task whose row has never been materialised. Ensuring the row
    first is the difference between "saved" and "appeared to save"."""
    task = get_task(name)
    if task is None:
        return False
    _row(task)
    db.update_task_schedule(name, enabled, schedule_kind, interval_minutes, daily_at)
    return True


def run_task(name, trigger="schedule"):
    """Runs one task to completion, in the calling thread, and records the outcome.

    Returns (status, message) where status is 'success', 'failed', 'skipped' or
    'busy'. Never raises: the whole point is that a task's failure is data, recorded
    against that task, rather than an exception propagating into the scheduler loop
    or an admin request handler.

    'busy' is the one outcome that records nothing - the run that *is* in progress
    will write its own result when it finishes, and overwriting last_run_at with a
    run that never happened would corrupt the schedule."""
    task = get_task(name)
    if task is None:
        return "failed", f"No such task: {name}"
    # Make sure the row exists *before* running: record_task_run() is an UPDATE, so
    # on a task whose row has never been materialised it would silently write
    # nothing and the run would vanish without trace. That is exactly what happened
    # the first time this was written - a task triggered before its settings page
    # had ever been opened recorded no result at all.
    _row(task)

    state = _state(name)
    if not state["lock"].acquire(blocking=False):
        return "busy", "Already running."

    started = time.monotonic()
    state["started_at"] = started
    try:
        try:
            message = task.run() or ""
            status = "success"
        except TaskSkipped as e:
            message, status = str(e), "skipped"
            _logger.info("Scheduled task '%s' skipped: %s", name, message)
        except TaskUnavailable as e:
            message, status = str(e) or e.__class__.__name__, "failed"
            _logger.warning("Scheduled task '%s' failed: %s", name, message)
        except Exception as e:  # a task must never take the loop down
            message, status = str(e) or e.__class__.__name__, "failed"
            if _caused_by_network_failure(e):
                # Same reasoning as TaskUnavailable above, applied to every task that
                # just lets a requests error propagate rather than dressing it up.
                _logger.warning("Scheduled task '%s' failed: %s", name, message)
            else:
                _logger.exception("Scheduled task '%s' failed", name)
        duration_ms = int((time.monotonic() - started) * 1000)
        try:
            db.record_task_run(name, status, message[:500], duration_ms, trigger)
        except Exception:
            # A database write failing here must not turn a merely-failed task into
            # an unhandled exception in the scheduler thread.
            _logger.exception("Could not record the result of scheduled task '%s'", name)
        return status, message
    finally:
        state["started_at"] = None
        state["lock"].release()


def _spawn(name):
    """One task, one short-lived daemon thread. This is what keeps a slow or hung
    task from delaying every other task behind it in the same tick - the per-task
    lock above already stops it from being started again while it's still going, so
    a hung task costs exactly one stuck thread, not an unbounded pile of them."""
    thread = threading.Thread(target=run_task, args=(name,), kwargs={"trigger": "schedule"},
                              name=f"task-{name}", daemon=True)
    thread.start()
    return thread


def tick(now=None):
    """One scheduling pass: start every enabled task that is due and not already
    running. Separated from the loop below so tests can drive it directly without
    threads or sleeps. Returns the names it started."""
    now = now or datetime.now(timezone.utc)
    started = []
    tasks = registered_tasks()
    rows = _rows_for(tasks)
    for task in tasks:
        try:
            row = rows[task.name]
            if not row["enabled"] or is_running(task.name):
                continue
            due = next_run_at(task, row, now)
            if due is not None and due <= now:
                _spawn(task.name)
                started.append(task.name)
        except Exception:
            # A single task's row being unreadable must not abort the whole pass -
            # the remaining tasks are unaffected and should still run.
            _logger.exception("Could not evaluate schedule for task '%s'", task.name)
    return started


def _loop():
    while True:
        try:
            tick()
        except Exception:
            _logger.exception("scheduler loop error")
        time.sleep(config.SCHEDULER_TICK_SECONDS)


def start():
    """Started from app.py/serve_waitress.py alongside the other background threads.
    A no-op when nothing is registered, so a build with every optional feature off
    doesn't run an idle thread for no reason."""
    if not _registry:
        _logger.info("No scheduled tasks registered - scheduler not started")
        return None
    thread = threading.Thread(target=_loop, name="scheduler", daemon=True)
    thread.start()
    return thread
