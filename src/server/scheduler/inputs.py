"""What a job's work is made from (ADR-020 decision 4): each clip's stored
preview and analysis proxy, by the keys its queue row carries
(src/server/scheduler/queue.py), and its original for a producer that reads
the storage (storage=True: src/producers/runner.py lets no other ask),
through its library's originals (src/processing/originals.py). A producer
asks its job (Work.inputs); where the file comes from is this module's:

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
import logging
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from src.processing.proxy.proxy_cache import fit
from src.producers.clip import PROXY_CACHE_EDGE
from src.producers.runner import Failed, NotTried, Waits
from src.server.storage.store import NotFound, Store, Unavailable
from src.shared.waits import SCAN

logger = logging.getLogger(__name__)


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
        return self._acct.proxy_cache(clip["library_id"]).get(clip["asset_id"], content_hash=clip.get("content_hash"))

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

    # -- Originals (storage=True) ---------------------------------------------------

    @contextlib.contextmanager
    def original(self, clip: dict) -> Iterator[Path]:
        """The clip's original on its library's storage, as a file for the
        block (its library's originals at the last look:
        src/processing/originals.py). The storage away at the last look, or
        gone since: the job isn't tried (NotTried). The file missing from
        storage that's there waits for the scan that says so (Waits, SCAN). A
        path that would leave the library is the clip's (Failed), as is a
        file that can't be read (another OSError, saying why)."""
        from src.shared.io_utils import UnsafeRelPathError

        acct, library_id = self._acct, clip["library_id"]
        originals = acct.originals(library_id)
        if originals is None:
            logger.info("scheduler: %s's storage isn't reachable; its storage work waits", clip["rel_path"])
            acct.unreachable(library_id)
            raise NotTried(clip["rel_path"])
        with contextlib.ExitStack() as held:
            try:
                source = held.enter_context(originals.local_file(clip["rel_path"]))
            except UnsafeRelPathError:
                raise Failed("outside the library") from None
            except OSError as exc:
                if acct.storage_gone(library_id):
                    logger.info("scheduler: %s's storage went away; its storage work waits", clip["rel_path"])
                    acct.unreachable(library_id)
                    raise NotTried(clip["rel_path"]) from None
                if not isinstance(exc, FileNotFoundError):
                    raise
                logger.warning("scheduler: %s isn't on its storage; it waits for the scan", clip["rel_path"])
                raise Waits("missing", reason=SCAN) from None
            yield source

    @contextlib.contextmanager
    def originals(self, clips: Iterable[dict]) -> Iterator[dict[str, Path]]:
        """The clips' originals that are there now, by asset id, as files for
        the block: for reading many at once (one exiftool). Quietly without
        the ones that aren't: each clip's own original() says why."""
        from src.shared.io_utils import UnsafeRelPathError

        found: dict[str, Path] = {}
        with contextlib.ExitStack() as held:
            for clip in clips:
                originals = self._acct.originals(clip["library_id"])
                if originals is None:
                    continue
                try:
                    found[clip["asset_id"]] = held.enter_context(originals.local_file(clip["rel_path"]))
                except (OSError, UnsafeRelPathError):
                    continue
            yield found
