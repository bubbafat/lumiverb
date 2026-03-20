from __future__ import annotations

from typing import Sequence

from src.upgrade.step import UpgradeStep


def registered_upgrade_steps() -> Sequence[UpgradeStep]:
    """Return the ordered list of upgrade steps for the current code.

    Phase-specific steps will be registered here as we implement them.
    """

    # Phase 1 hashes backfill steps will be added in a subsequent change.
    return []

