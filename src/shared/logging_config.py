"""Logging configuration for Lumiverb CLI and workers, and the API's access log."""

from __future__ import annotations

import logging
import os


def configure_logging() -> None:
    """Configure root logger based on LOG_LEVEL env var (default INFO)."""
    level_name = os.environ.get("LOG_LEVEL", "WARNING").upper()
    level = getattr(logging, level_name, logging.WARNING)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    # Suppress noisy third-party loggers
    logging.getLogger("pyvips").setLevel(logging.WARNING)
    # One line per request drowns a long-running worker's log.
    logging.getLogger("httpx").setLevel(logging.WARNING)


class _NoStreamLinks(logging.Filter):
    """Drops access-log lines for /v1/stream/<token>: the token is a bearer link."""

    def filter(self, record: logging.LogRecord) -> bool:
        return " /v1/stream/" not in record.getMessage()


def hide_stream_links() -> None:
    """Keep playback links, good for hours, out of uvicorn's access log."""
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _NoStreamLinks) for f in access.filters):
        access.addFilter(_NoStreamLinks())
