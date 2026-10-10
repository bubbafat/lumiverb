"""Logging configuration for Lumiverb CLI and workers, and the API's access log."""

from __future__ import annotations

import logging
import os
import re


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
    escape_log_lines()


# CR, LF and the other control characters (not tab): a value logged with one,
# a file name say, can't forge a line of its own or drive a terminal.
_CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")
_SHOWN = {"\n": "\\n", "\r": "\\r"}


def _escape(text: str) -> str:
    return _CONTROL.sub(lambda m: _SHOWN.get(m.group(), f"\\x{ord(m.group()):02x}"), text)


def _one_line(record: logging.LogRecord) -> None:
    """Escape the control characters in the record's message. uvicorn's access
    log is left be: its formatter reads the args, and uvicorn quotes the path."""
    if record.name == "uvicorn.access":
        return
    try:
        message = record.getMessage()
    except Exception:
        return  # a bad format: the handler reports it as it always has
    if _CONTROL.search(message):
        record.msg, record.args = _escape(message), None


def escape_log_lines() -> None:
    """Keep every log message this process makes on one line, whichever
    handler writes it (uvicorn's in the API, the scheduler's, the CLI's).
    Tracebacks keep their lines."""
    make = logging.getLogRecordFactory()
    if getattr(make, "one_line", False):
        return

    def factory(*args, **kwargs) -> logging.LogRecord:
        record = make(*args, **kwargs)
        _one_line(record)
        return record

    factory.one_line = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(factory)


class _NoStreamLinks(logging.Filter):
    """Drops access-log lines for /v1/stream/<token>: the token is a bearer link."""

    def filter(self, record: logging.LogRecord) -> bool:
        return " /v1/stream/" not in record.getMessage()


def hide_stream_links() -> None:
    """Keep playback links, good for hours, out of uvicorn's access log."""
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _NoStreamLinks) for f in access.filters):
        access.addFilter(_NoStreamLinks())
