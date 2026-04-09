"""One-time cleanup of ``assets.video_preview_key`` values that point at a
scene_rep JPG instead of an MP4 video preview.

Background: the single-asset artifact upload handler had a fall-through
``else: # video_preview`` branch that also caught ``scene_rep``, so every
scene_rep upload silently called ``set_video_preview()`` with the scene_rep
JPG key. The dispatch was fixed in ``src/server/api/routers/artifacts.py``,
but every video that went through scene enrichment between commit
``531a75c`` (2026-03-20) and the fix has a clobbered ``video_preview_key``
pointing at ``…/scenes/<bucket>/<asset>_<rep_frame_ms>.jpg``. The
``/v1/assets/{id}/preview`` endpoint then streams the JPG bytes labelled as
``video/mp4`` and the browser ``<video>`` element shows a crossed-out play
icon because it cannot decode a JPEG.

This step NULLs ``video_preview_key`` (and the two companion timestamps)
on every affected row so the endpoint reverts to a clean 404 and the macOS
ReEnrichmentRunner / future scans regenerate a real MP4 preview. The bare
JPG files on disk are left in place — they are still the legitimate
scene_rep artifacts and the cleanup-fix in
``src/server/search/cleanup.py`` (same PR) teaches the orphan walker
about them so they will not be deleted.

Predicate: ``video_preview_key LIKE '%/scenes/%' OR video_preview_key
LIKE '%.jpg'``. Both clauses match the same set of rows for this bug, but
the OR is defense-in-depth in case any other corruption ever leaves a
JPEG in that column.
"""

from __future__ import annotations

import logging

from sqlalchemy import text

from src.server.upgrade.context import UpgradeContext
from src.server.upgrade.step import UpgradeStepInfo

logger = logging.getLogger(__name__)


_BAD_KEY_PREDICATE = (
    "video_preview_key IS NOT NULL"
    " AND ("
    "   video_preview_key LIKE '%/scenes/%'"
    "   OR video_preview_key LIKE '%.jpg'"
    " )"
)


class ClearClobberedVideoPreviewKeysStep:
    """NULL out ``video_preview_key`` rows that point at scene_rep JPGs."""

    info = UpgradeStepInfo(
        step_id="clear_clobbered_video_preview_keys",
        version="1",
        display_name="Clear clobbered video_preview_key rows",
    )

    def needs_work(self, ctx: UpgradeContext) -> bool:
        row = ctx.session.exec(
            text(f"SELECT 1 FROM assets WHERE {_BAD_KEY_PREDICATE} LIMIT 1")
        ).first()
        return row is not None

    def run(self, ctx: UpgradeContext) -> dict:
        result = ctx.session.exec(
            text(
                "UPDATE assets"
                " SET video_preview_key = NULL,"
                "     video_preview_generated_at = NULL,"
                "     video_preview_last_accessed_at = NULL"
                f" WHERE {_BAD_KEY_PREDICATE}"
            )
        )
        cleared = result.rowcount or 0
        ctx.session.commit()

        logger.info(
            "clear_clobbered_video_preview_keys complete: cleared=%d", cleared
        )
        return {"cleared": cleared}
