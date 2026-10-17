"""What a job's work is made from (ADR-020 decision 4): each clip's stored
preview and analysis proxy, by the keys its queue row carries
(src/server/scheduler/queue.py). A producer asks its job (Work.inputs);
where the file comes from is this module's:

- beside the API (its Store on this machine's disk: service.stores_here),
  from the Store by key: a file a tool reads is held for the block
  (store.local_file), and nothing is copied or fetched;
- elsewhere, from this machine's caches, filled through the API
  (src/processing/proxy/).

The producer can't tell which. Storage that can't be reached (the previews'
drive unplugged) is nobody's fault: the clip waits (Waits), and nothing is
taken for missing.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from src.processing.proxy.proxy_cache import fit
from src.producers.clip import PROXY_CACHE_EDGE
from src.producers.runner import Waits
from src.server.storage.store import NotFound, Store, Unavailable


class Inputs:
    def __init__(self, acct: Any, store: Store | None) -> None:
        """store: the API's, where it's on this machine's disk (None: not here)."""
        self._acct = acct
        self._store = store

    def stored_preview(self, clip: dict) -> bytes | None:
        """The clip's preview as the API keeps it on this machine, at the size
        producers see it (PROXY_CACHE_EDGE on its long edge), or None when
        it isn't kept here. The clip as it was hashed: changed content is
        another clip."""
        key = clip.get("proxy_key")
        if self._store is None or not key:
            return None
        try:
            data = self._store.read(key)
        except NotFound:
            return None
        except Unavailable as exc:
            raise Waits(f"its preview can't be reached now: {exc}") from exc
        return fit(data, PROXY_CACHE_EDGE)

    def preview_bytes(self, clip: dict) -> bytes | None:
        """The clip's preview, at most PROXY_CACHE_EDGE on its long edge (what
        CLIP, faces, OCR and descriptions see): the API's on this machine,
        else this machine's cache's (src/processing/proxy/proxy_cache.py).
        None when there's none to be had."""
        if (stored := self.stored_preview(clip)) is not None:
            return stored
        return self._acct.proxy_cache(clip["library_id"]).get(clip["asset_id"], clip.get("rel_path"))

    @contextlib.contextmanager
    def analysis_proxy(self, clip: dict) -> Iterator[Path | None]:
        """The clip's analysis proxy as a file on this machine for the block
        (ffmpeg and Whisper read it), or None when it has none."""
        with contextlib.ExitStack() as held:
            yield self._analysis_proxy(clip, held)

    def _analysis_proxy(self, clip: dict, held: contextlib.ExitStack) -> Path | None:
        key = clip.get("analysis_proxy_key")
        if self._store is not None and key:
            try:
                return held.enter_context(self._store.local_file(key))
            except NotFound:
                pass  # gone since the queue listed it: the cache's way, as for any other
            except Unavailable as exc:
                raise Waits(f"its analysis proxy can't be reached now: {exc}") from exc
        return held.enter_context(self._acct.analysis_cache.held(clip["asset_id"]))

    def rendered(self, clip: dict, path: Path) -> None:
        """An analysis proxy this machine rendered, saved to the server: kept
        in this machine's cache for transcripts and scenes, unless the API
        keeps it on this disk (they read it there): then the render goes
        with the job's scratch, and no copy is kept."""
        if self._store is None:
            self._acct.analysis_cache.put(clip["asset_id"], path)
