"""The server's storage, for a test: a LocalStorage over the test's folder.

The Store (src/server/storage/store.py get_store) is built over get_storage
whenever it's asked for, so patching get_storage where it's defined covers
every caller of the Store. Modules that still hold get_storage by name
(imported before ADR-020 phase 2 moved them onto the Store) are patched too,
found as they are now, so no test keeps a list of them.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

from src.server.storage import local


@contextmanager
def stored_in(storage: local.LocalStorage) -> Iterator[None]:
    """Every server module's storage is `storage` while the block lasts."""
    import src.server.api.main  # noqa: F401 — every router loaded, so each holder is found

    real = local.get_storage
    holders = [module for name, module in list(sys.modules.items())
               if name.startswith("src.") and module is not local and getattr(module, "get_storage", None) is real]
    with ExitStack() as stack:
        for module in (local, *holders):
            stack.enter_context(patch.object(module, "get_storage", return_value=storage))
        yield
