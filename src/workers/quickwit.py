"""Quickwit search integration: purge library indexes on hard delete."""

import logging

import requests

from src.search.quickwit_client import QuickwitClient

logger = logging.getLogger(__name__)


def purge_library_from_quickwit(library_id: str) -> None:
    """
    Delete both Quickwit indexes for this library (asset index and scene index).

    Silently ignores 404 (index never created). Logs a warning on other errors
    rather than raising, so a Quickwit outage doesn't block the trash empty.
    """
    client = QuickwitClient()
    if not client.enabled:
        logger.debug("Quickwit disabled; skipping purge for library_id=%s", library_id)
        return

    base_url = client._base_url
    for index_id in (
        client.index_id_for_library(library_id),
        client.scene_index_id_for_library(library_id),
    ):
        try:
            resp = requests.delete(
                f"{base_url}/api/v1/indexes/{index_id}",
                timeout=10,
            )
            if resp.status_code == 404:
                logger.debug("Quickwit index %s not found; skipping", index_id)
            elif resp.status_code not in (200, 204):
                logger.warning(
                    "Quickwit index delete failed for %s: %s %s",
                    index_id,
                    resp.status_code,
                    resp.text,
                )
            else:
                logger.info("Deleted Quickwit index %s", index_id)
        except requests.RequestException as exc:
            logger.warning("Quickwit purge request failed for %s: %s", index_id, exc)
