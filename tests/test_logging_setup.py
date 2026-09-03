import logging
import types

import logging_setup


def test_init_logging_creates_log_file_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(logging_setup, "LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(logging_setup, "LOG_FILE", str(tmp_path / "logs" / "app.log"))
    monkeypatch.setattr(logging_setup, "_configured", False)
    root = logging.getLogger()
    before_handlers = list(root.handlers)

    try:
        logging_setup.init_logging()
        added = len(root.handlers) - len(before_handlers)
        assert added == 2  # file handler + console handler
        assert (tmp_path / "logs").is_dir()

        # Idempotent - calling again must not add more handlers.
        logging_setup.init_logging()
        assert len(root.handlers) - len(before_handlers) == 2
    finally:
        for h in list(root.handlers):
            if h not in before_handlers:
                root.removeHandler(h)
        monkeypatch.setattr(logging_setup, "_configured", False)


def test_log_thread_exception_logs_critical(caplog):
    fake_thread = types.SimpleNamespace(name="fake-thread")
    try:
        raise ValueError("boom")
    except ValueError:
        import sys
        exc_type, exc_value, exc_tb = sys.exc_info()

    args = types.SimpleNamespace(thread=fake_thread, exc_type=exc_type,
                                  exc_value=exc_value, exc_traceback=exc_tb)
    with caplog.at_level("CRITICAL"):
        logging_setup._log_thread_exception(args)

    assert any(r.levelno == logging.CRITICAL and "fake-thread" == r.name for r in caplog.records)
    assert "boom" in caplog.text


# ---------------------------------------------------------------------------
# Reading the logs back (/admin/logs)
# ---------------------------------------------------------------------------
SAMPLE = (
    "2026-09-03 10:00:00,000 INFO [app] started\n"
    "2026-09-03 10:00:01,000 WARNING [discord_bot] watchdog: not running\n"
    "2026-09-03 10:00:02,000 ERROR [monitoring] failed\n"
    "Traceback (most recent call last):\n"
    "  File \"monitoring.py\", line 1, in <module>\n"
    "RuntimeError: boom\n"
    "2026-09-03 10:00:03,000 INFO [app] carried on\n"
)


def _write_logs(tmp_path, monkeypatch, files):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    for name, content in files.items():
        (log_dir / name).write_text(content, encoding="utf-8")
    monkeypatch.setattr(logging_setup, "LOG_DIR", str(log_dir))
    monkeypatch.setattr(logging_setup, "LOG_FILE", str(log_dir / "app.log"))
    return log_dir


def test_parse_entries_keeps_a_traceback_attached_to_its_error():
    """Filtering line-by-line would separate a traceback from the ERROR that produced
    it and then drop it - which is exactly what someone opens a log page to read."""
    entries = logging_setup.parse_entries(SAMPLE)

    assert [e["level"] for e in entries] == ["INFO", "WARNING", "ERROR", "INFO"]
    error = entries[2]
    assert "RuntimeError: boom" in error["text"]
    assert error["text"].count("\n") == 3  # the three continuation lines


def test_tail_entries_filters_by_level_without_losing_the_traceback(tmp_path, monkeypatch):
    _write_logs(tmp_path, monkeypatch, {"app.log": SAMPLE})

    entries = logging_setup.tail_entries(min_level="WARNING")

    assert [e["level"] for e in entries] == ["WARNING", "ERROR"]
    assert "RuntimeError: boom" in entries[-1]["text"]


def test_tail_entries_returns_the_newest_entries_oldest_first(tmp_path, monkeypatch):
    lines = "".join(f"2026-09-03 10:00:{i:02d},000 INFO [app] line {i}\n" for i in range(30))
    _write_logs(tmp_path, monkeypatch, {"app.log": lines})

    entries = logging_setup.tail_entries(limit=5)

    assert len(entries) == 5
    assert "line 25" in entries[0]["text"]
    assert "line 29" in entries[-1]["text"]


def test_read_tail_is_bounded_and_drops_the_partial_first_line(tmp_path, monkeypatch):
    """The seek lands mid-line, and a half-line rendered as an entry looks like
    corruption rather than a truncation."""
    _write_logs(tmp_path, monkeypatch, {"app.log": ""})
    big = tmp_path / "logs" / "app.log"
    big.write_text("x" * 500 + "\n" + "2026-09-03 10:00:00,000 INFO [app] tail\n",
                    encoding="utf-8")

    text = logging_setup.read_tail(str(big), max_bytes=60)

    assert len(text) <= 60
    assert not text.startswith("x")
    assert "tail" in text


def test_log_files_only_lists_our_own_files(tmp_path, monkeypatch):
    """Anything else in instance/logs/ must not be listed - and therefore must not be
    downloadable - merely for sitting there."""
    _write_logs(tmp_path, monkeypatch, {
        "app.log": "current\n", "app.log.1": "older\n", "secrets.txt": "nope\n"})

    names = [f["name"] for f in logging_setup.log_files()]

    assert names == ["app.log", "app.log.1"]
    assert logging_setup.log_file_path("secrets.txt") is None
    assert logging_setup.log_file_path("../portal.db") is None
    assert logging_setup.log_file_path("app.log.1").endswith("app.log.1")


def test_iter_all_log_bytes_concatenates_oldest_first(tmp_path, monkeypatch):
    _write_logs(tmp_path, monkeypatch, {
        "app.log": "newest\n", "app.log.1": "middle\n", "app.log.2": "oldest\n"})

    blob = b"".join(logging_setup.iter_all_log_bytes()).decode()

    assert blob == "oldest\nmiddle\nnewest\n"


def test_readers_degrade_to_empty_when_nothing_has_been_logged(tmp_path, monkeypatch):
    """init_logging() only runs from the __main__ blocks, so a portal started some
    other way genuinely has no log file - the page must render, not raise."""
    monkeypatch.setattr(logging_setup, "LOG_DIR", str(tmp_path / "missing"))
    monkeypatch.setattr(logging_setup, "LOG_FILE", str(tmp_path / "missing" / "app.log"))

    assert logging_setup.log_files() == []
    assert logging_setup.tail_entries() == []
    assert logging_setup.read_tail(str(tmp_path / "missing" / "app.log")) == ""
    assert list(logging_setup.iter_all_log_bytes()) == []


def test_parse_entries_strips_terminal_colour_codes():
    """werkzeug colours its own console output and doesn't know a file handler is
    also listening, so the file really does contain escape sequences - which a
    browser renders as gibberish mid-line. Found in the real log, not in a test."""
    coloured = ("2026-09-03 10:00:00,000 INFO [werkzeug] "
                "\x1b[31m\x1b[1mWARNING: development server\x1b[0m\n")

    entries = logging_setup.parse_entries(coloured)

    assert entries[0]["text"].endswith("WARNING: development server")
    assert "\x1b" not in entries[0]["text"]
