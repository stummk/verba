"""Logging with daily rotation; retention comes from the settings."""

from __future__ import annotations

import logging
import logging.handlers
import sys

from . import config

_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_configured = False


def setup_logging(settings: config.Settings) -> None:
    """Configure root logging once per process (idempotent)."""
    global _configured
    root = logging.getLogger()
    if _configured:
        root.setLevel(settings.logging.level)
        return

    root.setLevel(settings.logging.level)
    formatter = logging.Formatter(_FORMAT)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    log_file = config.logs_dir() / "app.log"
    file_handler = logging.handlers.TimedRotatingFileHandler(
        log_file,
        when="midnight",
        backupCount=settings.logging.retention_days,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    error_file = config.logs_dir() / "error.log"
    error_handler = logging.handlers.TimedRotatingFileHandler(
        error_file,
        when="midnight",
        backupCount=settings.logging.retention_days,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)
    root.addHandler(error_handler)

    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    _configured = True
