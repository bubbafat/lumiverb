"""A photo described and tagged by the account's vision machines, from its proxy."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path

from src.producers.names import DESCRIPTION
from src.producers.runner import Work

logger = logging.getLogger(__name__)


def describe(proxy_bytes: bytes, model: str, provider, work_dir: Path) -> dict | None:
    """The model's description and tags of an image (put in work_dir: the
    job's scratch); None when it said nothing."""
    tmp_path = work_dir / "vision.img"
    tmp_path.write_bytes(proxy_bytes)
    try:
        result = provider.describe(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)
    if not result:
        return None
    return {
        "model_id": model,
        "model_version": "1",
        "description": (result.get("description") or "").strip(),
        "tags": [t.strip() for t in (result.get("tags") or []) if isinstance(t, str) and t.strip()],
    }


def describe_clip(*, asset_id: str, rel_path: str, model: str, provider, preview: Callable[[], bytes | None],
                  work_dir: Path, client=None) -> dict | None:
    """The clip's description from its proxy (preview() gets it, else the
    server's); None when there's no proxy or the model said nothing."""
    t0 = time.perf_counter()
    proxy_bytes = preview()
    if proxy_bytes is None and client is not None:
        from src.processing.api import LumiverbAPIError
        from src.producers.names import PREVIEW

        try:
            proxy_bytes = client.get(f"/v1/assets/{asset_id}/artifacts/{PREVIEW}").content
        except LumiverbAPIError as e:
            if e.status_code != 404:
                raise  # whose() says (the API away: nobody's)
            proxy_bytes = None  # no proxy: nothing to describe
    t_proxy = time.perf_counter() - t0
    if proxy_bytes is None:
        return None
    t1 = time.perf_counter()
    result = describe(proxy_bytes, model, provider, work_dir)
    logger.info("vision timings: %s — proxy=%.1fms vision=%.1fms", rel_path, t_proxy * 1000,
                (time.perf_counter() - t1) * 1000)
    return None if result is None else {"asset_id": asset_id, **result}


class Vision(Work):
    artifact = DESCRIPTION

    def __init__(self, acct, job) -> None:
        super().__init__(acct, job)
        self.provider, self.model = acct.vision_provider()
        self.used = acct.producers.with_model(self.artifact, self.model)

    def make(self, clip: dict) -> dict:
        result = describe_clip(asset_id=clip["asset_id"], rel_path=clip["rel_path"], model=self.model,
                               provider=self.provider, preview=lambda: self.inputs.preview_bytes(clip),
                               work_dir=self.scratch, client=self.acct.client)
        if result is None:
            raise self.machines_or_clip("no description (no proxy, or the model returned nothing)")
        return result

    def judge(self, clip: dict, error: Exception) -> Exception:
        return self.machines_or_clip(error)  # the machines' guard decides whose it is

    def save(self, client, made: list[tuple[dict, dict]]) -> None:
        for clip, result in made:
            client.post("/v1/assets/batch-vision", json={
                "items": [{**result, "source_hash": clip.get("content_hash")}],
                "lineage": self.lineage(None, used=self.used)})
