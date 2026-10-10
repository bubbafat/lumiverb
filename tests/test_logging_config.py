"""The API's access log leaves out playback links.

/v1/stream/<token> is a bearer link good for hours: anyone who reads the
log could play the video. uvicorn logs every request on "uvicorn.access".
"""

from __future__ import annotations

import logging
import logging.config

import pytest

pytestmark = pytest.mark.fast


def _access_line(path: str) -> logging.LogRecord:
    # As uvicorn's HTTP protocols log a request.
    return logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                             ("127.0.0.1:50000", "GET", path, "1.1", 206), None)


def test_the_api_keeps_playback_links_out_of_the_access_log() -> None:
    import src.server.api.main  # noqa: F401 — the app's import sets it up

    access = logging.getLogger("uvicorn.access")
    assert not access.filter(_access_line("/v1/stream/tok_secret"))
    assert not access.filter(_access_line("/v1/stream/tok_secret?t=1"))
    assert access.filter(_access_line("/v1/assets/page"))


def test_uvicorn_configuring_its_loggers_keeps_the_filter() -> None:
    from uvicorn.config import LOGGING_CONFIG

    from src.shared.logging_config import hide_stream_links

    hide_stream_links()
    hide_stream_links()  # once is enough
    logging.config.dictConfig(LOGGING_CONFIG)
    access = logging.getLogger("uvicorn.access")
    assert not access.filter(_access_line("/v1/stream/tok_secret"))
    assert sum(type(f).__name__ == "_NoStreamLinks" for f in access.filters) == 1


@pytest.fixture
def plain_records():
    """Records made as logging makes them, put back after."""
    before = logging.getLogRecordFactory()
    logging.setLogRecordFactory(logging.LogRecord)
    yield
    logging.setLogRecordFactory(before)


def _written(logger_name: str, msg: str, *args: object) -> str:
    """What a handler writes for one log call."""
    import io

    out = io.StringIO()
    handler = logging.StreamHandler(out)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    logger = logging.getLogger(logger_name)
    logger.addHandler(handler)
    try:
        logger.warning(msg, *args)
    finally:
        logger.removeHandler(handler)
    return out.getvalue()


def test_a_logged_value_cant_forge_a_line(plain_records) -> None:
    from src.shared.logging_config import configure_logging, escape_log_lines

    rel_path = "a.jpg\r\nWARNING admin logged in\x1b[2J\x00"
    assert _written("test.one_line", "scan: %s", rel_path).count("\n") == 2  # forged
    configure_logging()
    escape_log_lines()  # once is enough
    assert _written("test.one_line", "scan: %s", rel_path) == (
        "WARNING scan: a.jpg\\r\\nWARNING admin logged in\\x1b[2J\\x00\n")
    assert _written("test.one_line", "tab\tkept %d", 3) == "WARNING tab\tkept 3\n"


def test_the_scheduler_escapes_its_log_lines(plain_records) -> None:
    import runpy
    from unittest.mock import patch

    with patch("src.server.scheduler.service.entry") as entry:
        runpy.run_module("src.server.scheduler", run_name="__main__")
    entry.assert_called_once()
    assert _written("src.server.scheduler.service", "%s", "x\ny") == "WARNING x\\ny\n"


def test_the_api_escapes_its_log_lines() -> None:
    import subprocess
    import sys

    code = ("import logging, src.server.api.main; "
            "print(getattr(logging.getLogRecordFactory(), 'one_line', False))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "True"

