from __future__ import annotations

from typing import Sequence

from src.server.upgrade.step import UpgradeStep
from src.server.upgrade.steps.backfill_artifact_sha256 import (
    BackfillProxySha256Step,
    BackfillThumbnailSha256Step,
    BackfillSceneRepSha256Step,
)
from src.server.upgrade.steps.cleanup_orphan_asset_children import (
    CleanupOrphanAssetChildrenStep,
)
from src.server.upgrade.steps.recompute_centroids_for_trash_filter import (
    RecomputeCentroidsForTrashFilterStep,
)


def registered_upgrade_steps() -> Sequence[UpgradeStep]:
    """Return the ordered list of upgrade steps for the current code."""
    return [
        BackfillProxySha256Step(),
        BackfillThumbnailSha256Step(),
        BackfillSceneRepSha256Step(),
        CleanupOrphanAssetChildrenStep(),
        # Must run AFTER orphan cleanup so the centroid math doesn't
        # waste a recompute on people who would have been emptied by
        # the cleanup step anyway.
        RecomputeCentroidsForTrashFilterStep(),
    ]

