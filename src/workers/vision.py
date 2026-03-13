"""Moondream vision inference. Lazy-loads model on first call."""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_model = None


def _load_model():
    global _model
    if _model is not None:
        return _model
    logger.info("Loading Moondream model...")
    import moondream as md

    _model = md.vl(model="moondream-2b-int8.mf")
    logger.info("Moondream model loaded.")
    return _model


def describe_image(proxy_path: Path) -> dict:
    """
    Run Moondream inference on a proxy image.
    Returns dict with 'description' and 'tags' keys.
    Returns empty dict on failure.

    Uses the local proxy file (not the source RAW) — proxy is already
    a JPEG at 2048px which is ideal for vision models.
    """
    if not proxy_path.exists():
        return {}
    try:
        from PIL import Image as PILImage

        model = _load_model()
        image = PILImage.open(proxy_path)
        description = model.caption(image)["caption"]
        tags_raw = model.query(
            image,
            "List 5-10 descriptive tags for this image as a comma-separated list. "
            "Only output the tags, no other text.",
        )["answer"]
        tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
        return {
            "description": description,
            "tags": tags,
        }
    except Exception as e:
        logger.warning("Vision inference failed for %s: %s", proxy_path, e)
        return {}

