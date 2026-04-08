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


def registered_upgrade_steps() -> Sequence[UpgradeStep]:
    """Return the ordered list of upgrade steps for the current code."""
    return [
        BackfillProxySha256Step(),
        BackfillThumbnailSha256Step(),
        BackfillSceneRepSha256Step(),
        CleanupOrphanAssetChildrenStep(),
    ]

