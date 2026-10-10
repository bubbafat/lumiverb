"""A photo's CLIP vector, from its proxy, on this machine's GPU (the model is
the scheduler's, shared by every account: src/server/scheduler/models.py)."""

from __future__ import annotations

import io
import logging
import time

from src.producers.clip import PROXY_CACHE_EDGE
from src.producers.runner import Failed, Work

logger = logging.getLogger(__name__)


def embed(*, asset_id: str, rel_path: str, clip_provider, proxy_cache) -> dict | None:
    """The clip's CLIP vector, from its proxy; None when there's no proxy."""
    from PIL import Image as PILImage

    t0 = time.perf_counter()
    image_bytes = proxy_cache.get(asset_id, rel_path) if proxy_cache else None
    t_proxy = time.perf_counter() - t0
    if image_bytes is None:
        logger.warning("No proxy for %s", rel_path)
        return None
    t1 = time.perf_counter()
    img = PILImage.open(io.BytesIO(image_bytes)).convert("RGB")
    del image_bytes
    try:
        vector = clip_provider.embed_image(img)
    finally:
        img.close()
    logger.info("embed timings: %s — proxy=%.1fms embed=%.1fms", rel_path, t_proxy * 1000,
                (time.perf_counter() - t1) * 1000)
    return {"asset_id": asset_id, "model_id": clip_provider.model_id, "model_version": clip_provider.model_version,
            "vector": vector}


class Clip(Work):
    artifact = "clip"

    def __init__(self, acct, job) -> None:
        super().__init__(acct, job)
        settings = acct.producers.settings(self.artifact)
        self.provider = acct.models.clip(settings["model"], settings["pretrained"])
        # The images CLIP sees are the proxy cache's: its size is what was used.
        self.used = {**settings, "input_edge": PROXY_CACHE_EDGE}

    def make(self, clip: dict) -> dict:
        item = embed(asset_id=clip["asset_id"], rel_path=clip["rel_path"], clip_provider=self.provider,
                     proxy_cache=self.acct.proxy_cache(clip["library_id"]))
        if item is None:
            raise Failed("no embedding (no proxy)")
        return item

    def save(self, client, made: list[tuple[dict, dict]]) -> None:
        # The batch route, one clip: search is left to the upkeep sweep, not committed per clip.
        for clip, item in made:
            client.post("/v1/assets/batch-embeddings", json={
                "items": [{**item, "source_sha256": clip.get("sha256")}],
                "lineage": self.lineage(None, used=self.used)})
