"""Unit tests for purge_library_from_quickwit (tenant-scoped)."""

from unittest.mock import MagicMock, patch

from src.server.search.quickwit import purge_library_from_quickwit

LIBRARY_ID = "lib_01TESTLIBRARY"
TENANT_ID = "tnt_01TESTTENANT"


def _mock_client(enabled: bool = True) -> MagicMock:
    client = MagicMock()
    client.enabled = enabled
    return client


@patch("src.server.search.quickwit.QuickwitClient")
def test_purge_calls_delete_by_library_id(mock_qw_cls):
    """Purge with tenant_id delegates to delete_tenant_documents_by_library_id."""
    mock_client = _mock_client()
    mock_qw_cls.return_value = mock_client

    purge_library_from_quickwit(LIBRARY_ID, tenant_id=TENANT_ID)

    mock_client.delete_tenant_documents_by_library_id.assert_called_once_with(TENANT_ID, LIBRARY_ID)


@patch("src.server.search.quickwit.QuickwitClient")
def test_purge_without_tenant_id_logs_warning(mock_qw_cls, caplog):
    """Without tenant_id, purge logs a warning and does nothing."""
    mock_client = _mock_client()
    mock_qw_cls.return_value = mock_client

    import logging
    with caplog.at_level(logging.WARNING, logger="src.server.search.quickwit"):
        purge_library_from_quickwit(LIBRARY_ID)

    mock_client.delete_tenant_documents_by_library_id.assert_not_called()
    assert any("No tenant_id" in r.message for r in caplog.records)


@patch("src.server.search.quickwit.QuickwitClient")
def test_disabled_quickwit_skips(mock_qw_cls):
    """When Quickwit is disabled no calls are made."""
    mock_client = _mock_client(enabled=False)
    mock_qw_cls.return_value = mock_client

    purge_library_from_quickwit(LIBRARY_ID, tenant_id=TENANT_ID)

    mock_client.delete_tenant_documents_by_library_id.assert_not_called()
