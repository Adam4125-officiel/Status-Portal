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
