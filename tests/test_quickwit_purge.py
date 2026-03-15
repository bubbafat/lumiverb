"""Unit tests for purge_library_from_quickwit."""

from unittest.mock import MagicMock, call, patch

import pytest
import requests

from src.workers.quickwit import purge_library_from_quickwit

LIBRARY_ID = "lib_01TESTLIBRARY"
ASSET_INDEX = f"lumiverb_{LIBRARY_ID}"
SCENE_INDEX = f"lumiverb_{LIBRARY_ID}_scenes"
BASE_URL = "http://localhost:7280"


def _mock_client(enabled: bool = True) -> MagicMock:
    client = MagicMock()
    client.enabled = enabled
    client._base_url = BASE_URL
    client.index_id_for_library.return_value = ASSET_INDEX
    client.scene_index_id_for_library.return_value = SCENE_INDEX
    return client


@patch("src.workers.quickwit.QuickwitClient")
@patch("src.workers.quickwit.requests.delete")
def test_happy_path_deletes_both_indexes(mock_delete, mock_qw_cls):
    """Both indexes deleted when Quickwit returns 200."""
    mock_qw_cls.return_value = _mock_client()
    mock_delete.return_value = MagicMock(status_code=200)

    purge_library_from_quickwit(LIBRARY_ID)

    assert mock_delete.call_count == 2
    mock_delete.assert_any_call(f"{BASE_URL}/api/v1/indexes/{ASSET_INDEX}", timeout=10)
    mock_delete.assert_any_call(f"{BASE_URL}/api/v1/indexes/{SCENE_INDEX}", timeout=10)


@patch("src.workers.quickwit.QuickwitClient")
@patch("src.workers.quickwit.requests.delete")
def test_204_also_succeeds(mock_delete, mock_qw_cls):
    """204 No Content is also a valid success response."""
    mock_qw_cls.return_value = _mock_client()
    mock_delete.return_value = MagicMock(status_code=204)

    purge_library_from_quickwit(LIBRARY_ID)

    assert mock_delete.call_count == 2


@patch("src.workers.quickwit.QuickwitClient")
@patch("src.workers.quickwit.requests.delete")
def test_404_is_silently_ignored(mock_delete, mock_qw_cls):
    """404 means index never existed; no error raised."""
    mock_qw_cls.return_value = _mock_client()
    mock_delete.return_value = MagicMock(status_code=404)

    purge_library_from_quickwit(LIBRARY_ID)  # must not raise

    assert mock_delete.call_count == 2


@patch("src.workers.quickwit.QuickwitClient")
@patch("src.workers.quickwit.requests.delete")
def test_non_2xx_non_404_logs_warning_does_not_raise(mock_delete, mock_qw_cls, caplog):
    """Non-2xx / non-404 response logs a warning but does not raise."""
    mock_qw_cls.return_value = _mock_client()
    mock_delete.return_value = MagicMock(status_code=500, text="internal error")

    import logging
    with caplog.at_level(logging.WARNING, logger="src.workers.quickwit"):
        purge_library_from_quickwit(LIBRARY_ID)  # must not raise

    assert mock_delete.call_count == 2
    assert any("500" in r.message for r in caplog.records)


@patch("src.workers.quickwit.QuickwitClient")
@patch("src.workers.quickwit.requests.delete")
def test_request_exception_logs_warning_does_not_raise(mock_delete, mock_qw_cls, caplog):
    """Connection error logs a warning but does not raise, and continues to second index."""
    mock_qw_cls.return_value = _mock_client()
    mock_delete.side_effect = requests.ConnectionError("refused")

    import logging
    with caplog.at_level(logging.WARNING, logger="src.workers.quickwit"):
        purge_library_from_quickwit(LIBRARY_ID)  # must not raise

    assert mock_delete.call_count == 2
    assert any("refused" in r.message for r in caplog.records)


@patch("src.workers.quickwit.QuickwitClient")
@patch("src.workers.quickwit.requests.delete")
def test_disabled_quickwit_skips_http_calls(mock_delete, mock_qw_cls):
    """When Quickwit is disabled no HTTP calls are made."""
    mock_qw_cls.return_value = _mock_client(enabled=False)

    purge_library_from_quickwit(LIBRARY_ID)

    mock_delete.assert_not_called()


@patch("src.workers.quickwit.QuickwitClient")
@patch("src.workers.quickwit.requests.delete")
def test_first_index_error_does_not_skip_second(mock_delete, mock_qw_cls):
    """A warning-level error on the first index still attempts the second."""
    mock_qw_cls.return_value = _mock_client()
    mock_delete.side_effect = [
        MagicMock(status_code=500, text="oops"),
        MagicMock(status_code=200),
    ]

    purge_library_from_quickwit(LIBRARY_ID)

    assert mock_delete.call_count == 2
