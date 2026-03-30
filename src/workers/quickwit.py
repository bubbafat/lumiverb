"""Quickwit search integration: purge library documents on hard delete."""

import logging

from src.search.quickwit_client import QuickwitClient

logger = logging.getLogger(__name__)


def purge_library_from_quickwit(library_id: str, tenant_id: str | None = None) -> None:
    """
    Delete all Quickwit documents for this library from the tenant indexes.

    Silently ignores errors so a Quickwit outage doesn't block trash empty.
    """
    client = QuickwitClient()
    if not client.enabled:
        logger.debug("Quickwit disabled; skipping purge for library_id=%s", library_id)
        return

    if tenant_id:
        client.delete_tenant_documents_by_library_id(tenant_id, library_id)
    else:
        logger.warning("No tenant_id for Quickwit purge of library %s; skipping", library_id)
