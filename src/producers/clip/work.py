"""A photo's CLIP vector, from its proxy, by the CLIP job's machines (ADR-022):
the built-in one is the scheduler's model, shared by every account
(src/server/scheduler/models.py); a Lumiverb AI worker's is another machine's."""

from __future__ import annotations

import io
import logging
import time
from collections.abc import Callable

from src.producers.clip import CLIP_MODEL_ID, PROXY_CACHE_EDGE
from src.producers.names import LIKENESS
from src.producers.runner import Failed, Work

logger = logging.getLogger(__name__)


def embed(*, asset_id: str, rel_path: str, clip_provider, preview: Callable[[], bytes | None]) -> dict | None:
    """The clip's CLIP vector, from its proxy (preview() gets it); None when there's no proxy."""
    from PIL import Image as PILImage

    t0 = time.perf_counter()
    image_bytes = preview()
    t_proxy = time.perf_counter() - t0
    if image_bytes is None:
        logger.warning("No proxy for %s", rel_path)
        return None
    t1 = time.perf_counter()
    try:
        img = PILImage.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception as e:  # noqa: BLE001 — a proxy that can't be read is the clip's (PIL's errors are OSErrors)
        raise Failed(f"its proxy can't be read: {e}") from e
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
    """A photo's vector by whichever of the CLIP job's machines is free (ADR-022): the
    built-in one (this machine's CLIP, as embed() above) or a Lumiverb AI worker."""

    artifact = LIKENESS

    def __init__(self, acct, job) -> None:
        super().__init__(acct, job)
        settings = acct.producers.settings(self.artifact)
        self.embedder = acct.guard("clip").embedder(acct.models)
        # What its vectors must be (the model and weights in use): one made with another isn't saved.
        self.expected = (CLIP_MODEL_ID, f"{settings['model']}-{settings['pretrained']}")
        # The images CLIP sees are the proxy cache's: its size is what was used.
        self.used = {**settings, "input_edge": PROXY_CACHE_EDGE}

    def make(self, clip: dict) -> dict:
        from src.processing.workers.embeddings.base import EmbeddingError

        image = self.inputs.preview_bytes(clip)
        if image is None:
            raise Failed("no embedding (no proxy)")
        made = self.embedder.embed(image)
        if (made.get("model_id"), made.get("model_version")) != self.expected:
            # The machine's: it served another model under the name.
            raise EmbeddingError(f"made with {made.get('model_id')} {made.get('model_version')}, not "
                                 f"{self.expected[1]}", endpoint_fault=True)
        return {"asset_id": clip["asset_id"], "model_id": made["model_id"], "model_version": made["model_version"],
                "vector": made["vector"]}

    def judge(self, clip: dict, error: Exception) -> Exception:
        return self.machines_or_clip(error)  # the machines' guard decides whose it is

    def save(self, client, made: list[tuple[dict, dict]]) -> None:
        # The batch route, one clip: search is left to the upkeep sweep, not committed per clip.
        for clip, item in made:
            client.post("/v1/assets/batch-embeddings", json={
                "items": [{**item, "source_hash": clip.get("content_hash")}],
                "lineage": self.lineage(None, used=self.used)})
