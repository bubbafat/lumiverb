"""Quickwit search integration. Purge stub until search is implemented."""

import logging


def purge_library_from_quickwit(library_id: str) -> None:
    """
    Delete all Quickwit documents for this library.
    Stub for now — Quickwit search not yet implemented.
    Logs a warning and returns without error.
    """
    logging.getLogger(__name__).warning(
        "purge_library_from_quickwit: not yet implemented for library_id=%s", library_id
    )
