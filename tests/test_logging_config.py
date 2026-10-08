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
