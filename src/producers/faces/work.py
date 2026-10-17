"""The faces in a photo, found in its proxy by the scheduler's face process
(src/producers/faces/detect.py), and a job's photos saved together."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from src.producers.runner import Failed, Waits, Work
from src.shared.waits import SCAN

logger = logging.getLogger(__name__)


def face_proxy(item: dict, root_path: Path | None, proxy_cache: Any) -> bool:
    """Make sure the proxy cache has the photo as its file is now: kept when its
    hash matches, made again from the original when it's reachable here (a
    photo whose file no longer matches its hash: False, it waits for its
    scan). The server's proxy is downloaded later when there's none. Only
    where the API doesn't keep the preview on this machine (Faces.make)."""
    from src.processing.proxy.proxy_gen import generate_face_proxy
    from src.shared.content_hash import matches
    from src.shared.io_utils import UnsafeRelPathError, resolve_source_path

    asset_id, expected = item["asset_id"], item.get("content_hash")
    if proxy_cache.has(asset_id):
        if not expected:
            return True  # no hash to check: the cache is trusted
        if proxy_cache.get_source_hash(asset_id) == expected:
            return True
    if root_path is None:
        return True
    try:
        source = resolve_source_path(root_path, item.get("rel_path", asset_id))
    except UnsafeRelPathError:
        return True  # never read outside the library: the server's proxy instead
    if not source.is_file():
        return True
    try:
        if expected and not matches(source, expected):
            return False
    except OSError:
        return False  # couldn't be read: as a changed file, it waits for its scan
    try:
        proxy_cache.put(asset_id, generate_face_proxy(source))
        if expected:
            proxy_cache.sha_path(asset_id).write_text(expected)
    except Exception:  # noqa: BLE001 — the server's proxy instead
        logger.warning("Couldn't make a face proxy of %s; the server's is used", item.get("rel_path"), exc_info=True)
    return True


class Faces(Work):
    artifact = "faces"
    together = True  # one request for the job's photos

    def __init__(self, acct, job) -> None:
        from src.producers.faces.levels import gates

        super().__init__(acct, job)
        # One read: what's found and its lineage agree.
        self.used = acct.producers.settings(self.artifact)
        self.detail = acct.producers.uses(self.artifact).get("detail")
        # The gates of its level: which faces count.
        self.gates = {**self.used, **gates(self.detail)}

    def make(self, clip: dict) -> dict:
        from src.producers.runner import out_of_memory

        # The preview the API keeps on this machine is the clip as it was
        # hashed (changed content is a new clip): read there, nothing is made.
        image = self.inputs.stored_preview(clip)
        if image is None:
            cache = self.acct.proxy_cache(clip["library_id"])
            if not face_proxy(clip, self.acct.root(clip["library_id"]), cache):
                raise Waits("its file changed since it was hashed; its scan comes first", reason=SCAN)
            image = cache.get(clip["asset_id"], clip.get("rel_path"))
        if image is None:
            raise Failed("no proxy")
        found = self.acct.models.faces().detect(image, self.gates)  # in use: not let go of meanwhile
        if "error" in found:
            if out_of_memory(found["error"]):  # the GPU's trouble, not the photo's
                raise Waits(found["error"])
            raise Failed(found["error"])
        return found

    def save(self, client, made: list[tuple[dict, dict]]) -> None:
        from src.shared.producers import level_of

        client.post("/v1/assets/batch-faces", json={
            "items": [{"asset_id": clip["asset_id"], "source_hash": clip.get("content_hash"), **found}
                      for clip, found in made],
            "lineage": {**self.lineage(None, used=self.used), "level": level_of(self.artifact, {"detail": self.detail})}})
