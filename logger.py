"""
logger.py — Structured logging system for AI Shorts Automator.

Supports:
- Human-readable text format for local development and CLI.
- Single-line JSON format for production environments and log aggregators (ELK, Datadog, CloudWatch).
- Environment variable overrides: LOG_LEVEL and LOG_FORMAT.
- Seamless extraction of custom 'extra' parameters.
"""
from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
import os
import sys
import threading
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Dict, Optional


class LogFormat(StrEnum):
    TEXT = "text"
    JSON = "json"


class JsonFormatter(logging.Formatter):
    """
    Serializes Python logging records into clean, single-line JSON objects.
    Extracts standard log fields, tracebacks, and custom `extra` kwargs.
    """

    _RESERVED_ATTRS = {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "module", "msecs",
        "message", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "thread", "threadName",
    }

    def format(self, record: logging.LogRecord) -> str:
        # Standard structured payload
        log_entry: Dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "func": record.funcName,
            "line": record.lineno,
        }

        # Format exception trace if present
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)
        elif record.exc_text:
            log_entry["exception"] = record.exc_text

        # Capture any extra attributes passed (e.g. log.info("...", extra={"clip_id": 1}))
        extra: Dict[str, Any] = {}
        for k, v in record.__dict__.items():
            if k not in self._RESERVED_ATTRS and not k.startswith("_"):
                try:
                    # Verify json serializability
                    json.dumps(v)
                    extra[k] = v
                except (TypeError, OverflowError):
                    extra[k] = str(v)

        if extra:
            log_entry["extra"] = extra

        return json.dumps(log_entry, ensure_ascii=False)


_setup_lock = threading.Lock()
_is_configured = False


def setup_logging(
    level: Optional[str] = None,
    format_type: Optional[str] = None,
    log_file: Optional[str] = "shorts_automator.log",
    force_reconfigure: bool = False,
) -> None:
    """
    Initializes application logging with text or structured JSON format.

    Priority order:
    1. Explicit function arguments (level, format_type, log_file)
    2. Environment variables: LOG_LEVEL, LOG_FORMAT
    3. Application config (config.yaml)
    4. Safe defaults: level=INFO, format=text
    """
    global _is_configured

    with _setup_lock:
        if _is_configured and not force_reconfigure:
            return

        # 1. Determine level
        if not level:
            level = os.environ.get("LOG_LEVEL")
        if not level:
            try:
                from config_loader import app_config
                level = app_config.logging.level
            except Exception:
                level = "INFO"
        level_num = getattr(logging, str(level).upper(), logging.INFO)

        # 2. Determine format
        if not format_type:
            format_type = os.environ.get("LOG_FORMAT")
        if not format_type:
            try:
                from config_loader import app_config
                format_type = app_config.logging.format
            except Exception:
                format_type = LogFormat.TEXT

        # 3. Determine file (SHORTS_LOG_FILE env var overrides config; used by tests)
        if log_file == "shorts_automator.log":
            env_log_file = os.environ.get("SHORTS_LOG_FILE")
            if env_log_file:
                log_file = env_log_file
            else:
                try:
                    from config_loader import app_config
                    log_file = app_config.logging.file
                except Exception:
                    pass

        # Select formatter
        if str(format_type).lower() == LogFormat.JSON:
            formatter = JsonFormatter()
        else:
            formatter = logging.Formatter(
                fmt="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )

        root_logger = logging.getLogger()
        root_logger.setLevel(level_num)

        # Remove any existing handlers to prevent duplicates during reconfiguration
        for handler in list(root_logger.handlers):
            root_logger.removeHandler(handler)

        # Console handler
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(level_num)
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)

        # File handler with automatic log rotation (5 MB x 3 backups)
        if log_file:
            try:
                file_handler = RotatingFileHandler(
                    log_file,
                    maxBytes=5 * 1024 * 1024,
                    backupCount=3,
                    encoding="utf-8",
                )
                file_handler.setLevel(level_num)
                file_handler.setFormatter(formatter)
                root_logger.addHandler(file_handler)
            except Exception as e:
                print(f"[!] Warning: failed to configure log file '{log_file}': {e}", file=sys.stderr)

        _is_configured = True


def get_logger(name: str = "shorts_core") -> logging.Logger:
    """Convenience helper to obtain a named logger."""
    if not _is_configured:
        setup_logging()
    return logging.getLogger(name)
