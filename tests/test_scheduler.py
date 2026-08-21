"""Tests for the generic scheduled-task framework (scheduler.py).

The framework's whole value proposition is "a task that misbehaves cannot hurt
anything else", so most of what's here is about failure: a task that raises, a task
that hangs, a task that's already running, a database row that's malformed. The happy
path is the easy part.

Every test registers its own throwaway tasks into the registry and removes them
again, rather than exercising the real jellyfin_user_sync task - the framework has to
be testable without any of its consumers.
"""
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

import db
import scheduler


@pytest.fixture
def registry(isolated_db):
    """A clean registry per test, restored afterwards so tests can't leak tasks into
    each other (or into the real registered tasks, which are registered at import)."""
    saved = dict(scheduler._registry)
    scheduler._registry.clear()
    scheduler.clear_caches()
    yield scheduler
    scheduler._registry.clear()
    scheduler._registry.update(saved)
    scheduler.clear_caches()


def _register(registry, name="demo", run=None, **defaults):
    return registry.register(name, name.title(), "A demo task.", run or (lambda: "done"), **defaults)


# ---------------------------------------------------------------------------
# Registration and the lazily-created database row
# ---------------------------------------------------------------------------
def test_registering_a_task_does_not_touch_the_database(registry):
    """Registration happens at import time, before init_db() has necessarily run in
    a given process. It must therefore be pure - the row is created on first use."""
    _register(registry)
    assert db.get_task_row("demo") is None


def test_the_row_is_created_from_the_registry_defaults_on_first_use(registry):
    _register(registry, default_interval_minutes=17, default_enabled=False)
    view = registry.task_view(registry.get_task("demo"))
    assert view["interval_minutes"] == 17
    assert view["enabled"] is False
    assert db.get_task_row("demo") is not None


def test_an_existing_row_is_never_reset_by_a_later_restart(registry):
    """ensure_task_row uses INSERT OR IGNORE precisely so that an admin's saved
    schedule survives - a restart re-registering the task must not clobber it."""
    _register(registry, default_interval_minutes=60)
    registry.task_view(registry.get_task("demo"))
    db.update_task_schedule("demo", enabled=False, schedule_kind="daily",
                            interval_minutes=60, daily_at="04:30")
    db.ensure_task_row("demo", registry.get_task("demo").defaults)
    row = db.get_task_row("demo")
    assert (row["enabled"], row["schedule_kind"], row["daily_at"]) == (0, "daily", "04:30")


# ---------------------------------------------------------------------------
# Running: success, failure, skip
# ---------------------------------------------------------------------------
def test_a_successful_run_records_its_message(registry):
    _register(registry, run=lambda: "synced 4 users")
    status, message = registry.run_task("demo", trigger="manual")
    assert (status, message) == ("success", "synced 4 users")
    row = db.get_task_row("demo")
    assert row["last_status"] == "success"
    assert row["last_message"] == "synced 4 users"
    assert row["last_trigger"] == "manual"
    assert row["last_run_at"] is not None
    assert row["last_duration_ms"] is not None


def test_a_raising_task_is_recorded_as_failed_and_never_propagates(registry):
    """The single most important property here: run_task() does not raise. A task
    exploding must become a row in a table, not an exception in the scheduler thread."""
    def boom():
        raise RuntimeError("jellyfin said no")

    _register(registry, run=boom)
    status, message = registry.run_task("demo")
    assert status == "failed"
    assert "jellyfin said no" in message
    assert db.get_task_row("demo")["last_status"] == "failed"


def test_a_failed_run_still_stamps_last_run_at(registry):
    """Otherwise an interval schedule - which is measured from last_run_at - would
    make a permanently-failing task re-run on every single scheduler tick instead of
    on its next scheduled run."""
    _register(registry, run=lambda: (_ for _ in ()).throw(RuntimeError("nope")),
              default_interval_minutes=60)
    registry.run_task("demo")
    row = db.get_task_row("demo")
    assert row["last_run_at"] is not None
    # ...and it is therefore no longer due.
    assert registry.next_run_at(registry.get_task("demo"), row) > datetime.now(timezone.utc)


def test_taskskipped_is_recorded_as_skipped_not_failed(registry):
    """"You haven't configured this yet" and "this is broken" must not look the same
    in the admin list."""
    def not_configured():
        raise scheduler.TaskSkipped("No Jellyfin integration selected.")

    _register(registry, run=not_configured)
    status, message = registry.run_task("demo")
    assert status == "skipped"
    assert message == "No Jellyfin integration selected."
    assert db.get_task_row("demo")["last_status"] == "skipped"


def test_a_task_returning_nothing_still_succeeds(registry):
    _register(registry, run=lambda: None)
    assert registry.run_task("demo")[0] == "success"


def test_running_an_unknown_task_fails_cleanly(registry):
    status, message = registry.run_task("does-not-exist")
    assert status == "failed"
    assert "No such task" in message


def test_a_very_long_message_is_truncated_before_storage(registry):
    _register(registry, run=lambda: "x" * 5000)
    registry.run_task("demo")
    assert len(db.get_task_row("demo")["last_message"]) == 500


# ---------------------------------------------------------------------------
# Concurrency: a task never overlaps itself
# ---------------------------------------------------------------------------
def test_a_task_already_running_reports_busy_instead_of_starting_a_second_copy(registry):
    release = threading.Event()
    entered = threading.Event()

    def slow():
        entered.set()
        release.wait(5)
        return "finally"

    _register(registry, run=slow)
    worker = threading.Thread(target=registry.run_task, args=("demo",), daemon=True)
    worker.start()
    assert entered.wait(5), "the task never started"

    try:
        assert registry.is_running("demo") is True
        status, message = registry.run_task("demo", trigger="manual")
        assert status == "busy"
        assert "Already running" in message
        # A busy result must record nothing at all - the run in progress writes the
        # real result when it finishes, and stamping last_run_at for a run that never
        # happened would corrupt the schedule.
        assert db.get_task_row("demo") is None or db.get_task_row("demo")["last_run_at"] is None
    finally:
        release.set()
        worker.join(5)

    assert registry.is_running("demo") is False
    assert db.get_task_row("demo")["last_status"] == "success"


def test_the_lock_is_released_even_when_the_task_raises(registry):
    _register(registry, run=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    registry.run_task("demo")
    assert registry.is_running("demo") is False
    # A second run must be possible immediately.
    assert registry.run_task("demo")[0] == "failed"


# ---------------------------------------------------------------------------
# Schedule computation
# ---------------------------------------------------------------------------
def test_a_never_run_interval_task_is_due_immediately(registry):
    _register(registry, default_interval_minutes=60)
    now = datetime.now(timezone.utc)
    assert registry.next_run_at(registry.get_task("demo"), now=now) == now


def test_a_never_run_daily_task_is_due_at_its_next_time_not_immediately(registry):
    """Firing a 03:00 job the instant the portal first starts at 14:00 would be
    surprising rather than helpful."""
    _register(registry, default_schedule_kind="daily", default_daily_at="03:00")
    now = datetime(2026, 8, 21, 14, 0, tzinfo=timezone.utc)
    due = registry.next_run_at(registry.get_task("demo"), now=now)
    assert due == datetime(2026, 8, 22, 3, 0, tzinfo=timezone.utc)


def test_an_interval_task_is_next_due_one_interval_after_its_last_run(registry):
    _register(registry, default_interval_minutes=30)
    registry.task_view(registry.get_task("demo"))
    ran_at = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
    db.record_task_run("demo", "success", ran_at=ran_at.isoformat())
    due = registry.next_run_at(registry.get_task("demo"))
    assert due == ran_at + timedelta(minutes=30)


def test_a_daily_task_missed_while_the_portal_was_down_is_due_on_the_next_tick(registry):
    """The schedule is derived from the stored last_run_at, which is exactly what
    makes it survive a restart: a portal that was off at 03:00 runs the task when it
    comes back, rather than silently skipping a day."""
    _register(registry, default_schedule_kind="daily", default_daily_at="03:00")
    registry.task_view(registry.get_task("demo"))
    db.update_task_schedule("demo", True, "daily", 60, "03:00")
    db.record_task_run("demo", "success", ran_at="2026-08-20T03:00:00+00:00")
    now = datetime(2026, 8, 21, 9, 0, tzinfo=timezone.utc)
    due = registry.next_run_at(registry.get_task("demo"), now=now)
    assert due == datetime(2026, 8, 21, 3, 0, tzinfo=timezone.utc)
    assert due <= now  # i.e. due right now


def test_a_disabled_task_has_no_next_run(registry):
    _register(registry, default_enabled=False)
    assert registry.next_run_at(registry.get_task("demo")) is None


def test_a_malformed_daily_time_falls_back_instead_of_raising(registry):
    """A bad stored value must not be able to take the whole scheduler down."""
    _register(registry, default_schedule_kind="daily")
    registry.task_view(registry.get_task("demo"))
    db.update_task_schedule("demo", True, "daily", 60, "not a time")
    due = registry.next_run_at(registry.get_task("demo"),
                                now=datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc))
    assert (due.hour, due.minute) == (3, 0)


def test_a_naive_stored_timestamp_is_treated_as_utc(registry):
    _register(registry, default_interval_minutes=60)
    registry.task_view(registry.get_task("demo"))
    db.record_task_run("demo", "success", ran_at="2026-08-21T12:00:00")
    due = registry.next_run_at(registry.get_task("demo"))
    assert due == datetime(2026, 8, 21, 13, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# The tick: what actually gets started
# ---------------------------------------------------------------------------
def test_tick_starts_a_due_task_and_leaves_a_not_yet_due_one_alone(registry):
    _register(registry, name="due", default_interval_minutes=60)
    _register(registry, name="notdue", default_interval_minutes=60)
    registry.task_view(registry.get_task("notdue"))
    db.record_task_run("notdue", "success")  # just ran, so an hour from now
    started = registry.tick()
    assert started == ["due"]


def test_tick_skips_disabled_tasks(registry):
    _register(registry, name="off", default_enabled=False)
    assert registry.tick() == []


def test_tick_skips_a_task_that_is_still_running(registry):
    release = threading.Event()
    _register(registry, run=lambda: release.wait(5), default_interval_minutes=1)
    worker = threading.Thread(target=registry.run_task, args=("demo",), daemon=True)
    worker.start()
    try:
        for _ in range(50):
            if registry.is_running("demo"):
                break
            time.sleep(0.01)
        assert registry.tick() == []
    finally:
        release.set()
        worker.join(5)


def test_one_broken_task_does_not_stop_the_others_from_running(registry):
    """The requirement in one test: a failing task is isolated. 'broken' raises,
    'fine' must still run and succeed in the same tick."""
    done = threading.Event()

    def fine():
        done.set()
        return "ok"

    _register(registry, name="broken", run=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    _register(registry, name="fine", run=fine)
    started = registry.tick()
    assert sorted(started) == ["broken", "fine"]
    assert done.wait(5), "the healthy task never ran"
    for _ in range(200):
        rows = {r["name"]: r for r in db.list_task_rows()}
        if rows.get("broken", {}).get("last_status") and rows.get("fine", {}).get("last_status"):
            break
        time.sleep(0.01)
    rows = {r["name"]: r for r in db.list_task_rows()}
    assert rows["broken"]["last_status"] == "failed"
    assert rows["fine"]["last_status"] == "success"


def test_a_hanging_task_does_not_block_a_second_task_in_the_same_tick(registry):
    """Each due task gets its own thread, so a task that never returns costs exactly
    one stuck thread rather than stalling everything scheduled behind it."""
    release = threading.Event()
    ran = threading.Event()
    _register(registry, name="hangs", run=lambda: release.wait(30))
    _register(registry, name="quick", run=lambda: (ran.set(), "ok")[1])
    try:
        registry.tick()
        assert ran.wait(5), "the quick task was blocked by the hanging one"
    finally:
        release.set()


# ---------------------------------------------------------------------------
# start()
# ---------------------------------------------------------------------------
def test_start_is_a_no_op_when_nothing_is_registered(registry):
    assert registry.start() is None


def test_start_launches_a_daemon_thread_when_tasks_exist(registry, monkeypatch):
    monkeypatch.setattr(scheduler.config, "SCHEDULER_TICK_SECONDS", 3600)
    _register(registry, run=lambda: "ok")
    thread = registry.start()
    assert thread is not None and thread.daemon is True
