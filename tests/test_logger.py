"""
Tests for alphalive.utils.logger.

Verifies handler wiring, log level resolution, and both text/JSON output
formats. Uses tmp_path for file logging so nothing is written outside the
pytest sandbox.
"""

import json
import logging

import pytest

from alphalive.utils.logger import JsonFormatter, setup_logger


@pytest.fixture(autouse=True)
def _reset_root_logger():
    """setup_logger() mutates the global root logger - snapshot and restore
    its handlers/level around each test so tests don't interfere with each
    other or with the pytest runner's own logging."""
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    yield
    for h in root.handlers[:]:
        root.removeHandler(h)
    for h in original_handlers:
        root.addHandler(h)
    root.setLevel(original_level)


def test_setup_logger_adds_stdout_handler(monkeypatch):
    monkeypatch.delenv("ENABLE_FILE_LOGS", raising=False)
    setup_logger(log_level="DEBUG")

    root = logging.getLogger()
    assert root.level == logging.DEBUG
    assert any(isinstance(h, logging.StreamHandler) for h in root.handlers)


def test_setup_logger_defaults_to_env_log_level(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    monkeypatch.delenv("ENABLE_FILE_LOGS", raising=False)
    setup_logger()

    assert logging.getLogger().level == logging.WARNING


def test_setup_logger_invalid_level_falls_back_to_info(monkeypatch):
    monkeypatch.delenv("ENABLE_FILE_LOGS", raising=False)
    setup_logger(log_level="NOT_A_REAL_LEVEL")

    assert logging.getLogger().level == logging.INFO


def test_setup_logger_json_format_uses_json_formatter(monkeypatch):
    monkeypatch.setenv("LOG_FORMAT", "json")
    monkeypatch.delenv("ENABLE_FILE_LOGS", raising=False)
    setup_logger(log_level="INFO")

    root = logging.getLogger()
    stdout_handler = next(h for h in root.handlers if isinstance(h, logging.StreamHandler))
    assert isinstance(stdout_handler.formatter, JsonFormatter)


def test_setup_logger_file_logging_creates_file(monkeypatch, tmp_path):
    monkeypatch.setenv("ENABLE_FILE_LOGS", "true")
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    setup_logger(log_level="INFO")

    assert (tmp_path / "alphalive.log").exists()


def test_setup_logger_clears_existing_handlers(monkeypatch):
    monkeypatch.delenv("ENABLE_FILE_LOGS", raising=False)
    root = logging.getLogger()
    stale_handler = logging.NullHandler()
    root.addHandler(stale_handler)

    setup_logger(log_level="INFO")

    assert stale_handler not in root.handlers


def test_json_formatter_emits_valid_json_with_core_fields():
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="alphalive.test", level=logging.INFO, pathname=__file__, lineno=1,
        msg="signal generated", args=(), exc_info=None,
    )

    output = formatter.format(record)
    parsed = json.loads(output)

    assert parsed["level"] == "INFO"
    assert parsed["logger"] == "alphalive.test"
    assert parsed["msg"] == "signal generated"
    assert "ts" in parsed


def test_json_formatter_includes_extra_structured_fields():
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="alphalive.test", level=logging.INFO, pathname=__file__, lineno=1,
        msg="trade executed", args=(), exc_info=None,
    )
    record.ticker = "AAPL"
    record.signal = "BUY"

    parsed = json.loads(formatter.format(record))

    assert parsed["ticker"] == "AAPL"
    assert parsed["signal"] == "BUY"


def test_json_formatter_includes_exception_info():
    formatter = JsonFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        import sys
        record = logging.LogRecord(
            name="alphalive.test", level=logging.ERROR, pathname=__file__, lineno=1,
            msg="order failed", args=(), exc_info=sys.exc_info(),
        )

    parsed = json.loads(formatter.format(record))
    assert "exc" in parsed
    assert "ValueError" in parsed["exc"]
