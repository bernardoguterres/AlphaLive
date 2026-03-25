"""
Logging Configuration

Configures structured logging to STDOUT and optionally to rotating log files.
Railway captures stdout automatically. Local runs can enable file logging.
"""

import logging
import os
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Optional


def setup_logger(log_level: Optional[str] = None):
    """
    Configure logging for AlphaLive.

    Logs to STDOUT (always) and to daily rotating files (if ENABLE_FILE_LOGS=true).

    Args:
        log_level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL).
                   Defaults to env var LOG_LEVEL or INFO.

    Environment Variables:
        LOG_LEVEL: Logging level (default: INFO)
        ENABLE_FILE_LOGS: Enable file-based logging (default: false)
        LOG_DIR: Directory for log files (default: ./logs)
    """
    # Get log level from env or parameter
    if log_level is None:
        log_level = os.getenv("LOG_LEVEL", "INFO").upper()

    # Validate log level
    numeric_level = getattr(logging, log_level, logging.INFO)

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)

    # Remove existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # Create formatter (structured for Railway and files)
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Always add stdout handler (for Railway)
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(numeric_level)
    stdout_handler.setFormatter(formatter)
    root_logger.addHandler(stdout_handler)

    # Optionally add file handler (for local runs and analysis)
    enable_file_logs = os.getenv("ENABLE_FILE_LOGS", "false").lower() == "true"
    if enable_file_logs:
        log_dir = Path(os.getenv("LOG_DIR", "./logs"))
        log_dir.mkdir(parents=True, exist_ok=True)

        # Daily rotating file handler (keeps 30 days)
        file_handler = TimedRotatingFileHandler(
            filename=log_dir / "alphalive.log",
            when="midnight",
            interval=1,
            backupCount=30,
            encoding="utf-8"
        )
        file_handler.setLevel(numeric_level)
        file_handler.setFormatter(formatter)
        file_handler.suffix = "%Y-%m-%d"  # Append date to rotated logs
        root_logger.addHandler(file_handler)

        logging.info(f"File logging enabled | Directory: {log_dir.absolute()}")

    # Set third-party library log levels (reduce noise)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("alpaca").setLevel(logging.INFO)

    logging.info(f"Logging configured | Level: {log_level}")
