"""
tests/test_logger.py — Unit tests for structured logging system.
"""
import json
import logging
import os
from unittest.mock import patch

import pytest

from logger import JsonFormatter, LogFormat, get_logger, setup_logging


def test_log_format_enum():
    assert LogFormat.TEXT == "text"
    assert LogFormat.JSON == "json"


def test_json_formatter_basic():
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="test_logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=25,
        msg="Processing video %s",
        args=("sample.mp4",),
        exc_info=None,
    )
    formatted = formatter.format(record)
    data = json.loads(formatted)

    assert data["level"] == "INFO"
    assert data["logger"] == "test_logger"
    assert data["message"] == "Processing video sample.mp4"
    assert "timestamp" in data
    assert "line" in data
    assert "module" in data


def test_json_formatter_with_exception():
    formatter = JsonFormatter()
    try:
        raise ValueError("Simulated video error")
    except ValueError:
        import sys
        exc_info = sys.exc_info()

    record = logging.LogRecord(
        name="test_logger",
        level=logging.ERROR,
        pathname=__file__,
        lineno=50,
        msg="Failed to render clip",
        args=(),
        exc_info=exc_info,
    )
    formatted = formatter.format(record)
    data = json.loads(formatted)

    assert data["level"] == "ERROR"
    assert "exception" in data
    assert "ValueError: Simulated video error" in data["exception"]


def test_json_formatter_with_extra():
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="test_logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=70,
        msg="Clip ready",
        args=(),
        exc_info=None,
    )
    # Add custom extra fields
    record.clip_id = 42
    record.score = 95.5
    record.custom_obj = object()  # non-serializable object

    formatted = formatter.format(record)
    data = json.loads(formatted)

    assert "extra" in data
    assert data["extra"]["clip_id"] == 42
    assert data["extra"]["score"] == 95.5
    assert "object at" in data["extra"]["custom_obj"]


def test_setup_logging_text(tmp_path):
    log_file = tmp_path / "test.log"
    setup_logging(level="DEBUG", format_type="text", log_file=str(log_file), force_reconfigure=True)

    test_logger = get_logger("test_text_logger")
    test_logger.debug("Debug message text")

    assert log_file.exists()
    content = log_file.read_text(encoding="utf-8")
    assert "[DEBUG] test_text_logger — Debug message text" in content


def test_setup_logging_json(tmp_path):
    log_file = tmp_path / "test_json.log"
    setup_logging(level="WARNING", format_type="json", log_file=str(log_file), force_reconfigure=True)

    test_logger = get_logger("test_json_logger")
    test_logger.warning("Warning message json")

    assert log_file.exists()
    lines = [line for line in log_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) >= 1
    data = json.loads(lines[-1])
    assert data["level"] == "WARNING"
    assert data["logger"] == "test_json_logger"
    assert data["message"] == "Warning message json"


def test_setup_logging_env_override(tmp_path):
    log_file = tmp_path / "test_env.log"
    with patch.dict(os.environ, {"LOG_LEVEL": "ERROR", "LOG_FORMAT": "json"}):
        setup_logging(log_file=str(log_file), force_reconfigure=True)

    root = logging.getLogger()
    assert root.level == logging.ERROR
    # Restore default text logging
    setup_logging(level="INFO", format_type="text", force_reconfigure=True)


def test_setup_logging_uses_rotating_file_handler(tmp_path):
    """OPS-01: File handler must use RotatingFileHandler to prevent unbounded log growth."""
    from logging.handlers import RotatingFileHandler

    log_file = tmp_path / "rotated.log"
    setup_logging(level="INFO", format_type="text", log_file=str(log_file), force_reconfigure=True)
    root = logging.getLogger()
    rotating_handlers = [h for h in root.handlers if isinstance(h, RotatingFileHandler)]
    assert len(rotating_handlers) == 1
    assert rotating_handlers[0].maxBytes == 5 * 1024 * 1024
    assert rotating_handlers[0].backupCount == 3
    # Restore default logging
    setup_logging(level="INFO", format_type="text", force_reconfigure=True)


def test_shorts_log_file_env_redirects_default_log(tmp_path):
    """Tests must never write into the real shorts_automator.log."""
    from logging.handlers import RotatingFileHandler

    target = tmp_path / "redirected.log"
    with patch.dict(os.environ, {"SHORTS_LOG_FILE": str(target)}):
        setup_logging(level="INFO", format_type="text", force_reconfigure=True)
        logging.getLogger("shorts_core").info("redirect check")

    root = logging.getLogger()
    files = [os.path.abspath(h.baseFilename) for h in root.handlers if isinstance(h, RotatingFileHandler)]
    assert files == [os.path.abspath(str(target))]
    assert "redirect check" in target.read_text(encoding="utf-8")
    setup_logging(level="INFO", format_type="text", force_reconfigure=True)


def test_conftest_isolates_log_from_project_file():
    env_file = os.environ.get("SHORTS_LOG_FILE", "")
    assert env_file
    assert os.path.basename(env_file) != "shorts_automator.log"
