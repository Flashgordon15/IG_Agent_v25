"""
Structured logging — file + console handlers under logs/.
"""

from __future__ import annotations

import logging
import threading
from logging.handlers import RotatingFileHandler

from system.paths import logs_dir

_INITIALIZED = False
_LOGGERS: dict[str, logging.Logger] = {}
_SETUP_LOCK = threading.Lock()


def setup_logging(
    name: str = "ig_agent",
    *,
    level: int = logging.INFO,
    log_file: str | None = None,
) -> logging.Logger:
    """
    Configure application logger with rotating file + console output.

    :param name: Logger name.
    :param level: Logging level.
    :param log_file: Override log file path; default is logs/ig_agent.log.
    :returns: Configured logger instance.
    """
    global _INITIALIZED

    with _SETUP_LOCK:
        logger = logging.getLogger(name)
        if name in _LOGGERS and logger.handlers:
            return logger

        logger.setLevel(level)
        logger.propagate = False

        if not logger.handlers:
            fmt = logging.Formatter(
                "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )

            path = log_file or str(logs_dir() / "ig_agent.log")
            logs_dir().mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                path,
                maxBytes=5 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
            file_handler.setFormatter(fmt)
            logger.addHandler(file_handler)

            console = logging.StreamHandler()
            console.setFormatter(fmt)
            logger.addHandler(console)

        _LOGGERS[name] = logger
        _INITIALIZED = True
        return logger


def get_logger(name: str = "ig_agent") -> logging.Logger:
    """Return named logger, calling setup_logging if not yet initialized."""
    existing = logging.getLogger(name)
    if existing.handlers:
        return existing
    return setup_logging(name)
