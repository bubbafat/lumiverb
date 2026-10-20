"""The text in a photo, read by the account's vision machines from its proxy."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path

from src.producers.names import TEXT
from src.producers.runner import Work

logger = logging.getLogger(__name__)


def read_text(*, asset_id: str, rel_path: str, ocr_provider, preview: Callable[[], bytes | None],
              work_dir: Path) -> dict | None:
    """{"asset_id", "ocr_text"}, or None when there's no proxy (preview() gets
    it) or the image can't be prepared (in work_dir: the job's scratch). The
    model's failure raises CaptionError (saying whether the machine was at
    fault); this machine's (OSError: a disk error) goes up as it is, a crash;
    and so does getting the preview's (its storage away: a wait)."""
    from src.processing.workers.captions.base import CaptionError

    t0 = time.perf_counter()
    image_bytes = preview()
    t_proxy = time.perf_counter() - t0
    if image_bytes is None:
        return None
    try:
        tmp_path = work_dir / "ocr.jpg"
        tmp_path.write_bytes(image_bytes)
        del image_bytes
        t1 = time.perf_counter()
        try:
            ocr_text = ocr_provider.extract_text(tmp_path)
        finally:
            tmp_path.unlink(missing_ok=True)
        if ocr_text:
            logger.info("OCR found: %s", ocr_text[:200])
        logger.info("ocr timings: %s — proxy=%.1fms ocr=%.1fms", rel_path, t_proxy * 1000,
                    (time.perf_counter() - t1) * 1000)
        return {"asset_id": asset_id, "ocr_text": ocr_text or ""}
    except (CaptionError, OSError):
        raise
    except Exception as e:  # noqa: BLE001 — an image that can't be read: nothing made
        logger.exception("Failed OCR for %s: %s", rel_path, e)
        return None


class Ocr(Work):
    artifact = TEXT

    def __init__(self, acct, job) -> None:
        super().__init__(acct, job)
        self.provider, self.model = acct.vision_provider()
        self.used = acct.producers.with_model(self.artifact, self.model)

    def make(self, clip: dict) -> dict:
        result = read_text(asset_id=clip["asset_id"], rel_path=clip["rel_path"], ocr_provider=self.provider,
                           preview=lambda: self.inputs.preview_bytes(clip), work_dir=self.scratch)
        if result is None:
            raise self.machines_or_clip("no OCR result (no proxy, or the image couldn't be read: see the log)")
        return result

    def judge(self, clip: dict, error: Exception) -> Exception:
        return self.machines_or_clip(error)

    def save(self, client, made: list[tuple[dict, dict]]) -> None:
        for clip, result in made:
            client.post("/v1/assets/batch-ocr", json={
                "items": [{"asset_id": clip["asset_id"], "ocr_text": result["ocr_text"],
                           "source_hash": clip.get("content_hash")}],
                "model_id": self.model, "lineage": self.lineage(None, used=self.used)})
