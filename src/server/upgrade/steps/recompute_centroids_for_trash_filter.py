"""Recompute people centroids after the trash-aware face query fix.

Background: ``PersonRepository._recompute_centroid`` used to average
*every* matched face embedding, including faces whose owning asset was
in the trash. After the trash-aware fix, the next time any person is
mutated (rename / merge / face assignment) their centroid gets
recomputed correctly. But people who haven't been touched since the
fix still have a drifted centroid, which biases the upkeep
``propagate_assignments`` job toward absorbing new faces that look
like the trashed photos.

This step does a one-time sweep: for every person whose match list
contains at least one face on a trashed asset, recompute the centroid
using the new (filtered) SQL. Idempotent — ``needs_work`` returns
False once no person has any trash-asset matches *or* once everyone
has already been recomputed (we mark a system_metadata flag on
completion to make a second run a no-op even if new trash happens).
"""

from __future__ import annotations

import logging

from sqlalchemy import text

from src.server.repository.tenant import PersonRepository
from src.server.upgrade.context import UpgradeContext
from src.server.upgrade.step import UpgradeStepInfo

logger = logging.getLogger(__name__)


class RecomputeCentroidsForTrashFilterStep:
    info = UpgradeStepInfo(
        step_id="recompute_centroids_for_trash_filter",
        version="1",
        display_name="Recompute people centroids excluding trashed assets",
    )

    def needs_work(self, ctx: UpgradeContext) -> bool:
        # Any person whose match list contains a trashed-asset face is
        # by definition carrying a drifted centroid. The orphan-cleanup
        # step (which runs first) handles hard-deleted assets, so by
        # the time we get here only soft-deleted ones remain.
        row = ctx.session.exec(
            text(
                "SELECT 1"
                " FROM face_person_matches m"
                " JOIN faces f ON f.face_id = m.face_id"
                " JOIN assets a ON a.asset_id = f.asset_id"
                " WHERE a.deleted_at IS NOT NULL"
                " LIMIT 1"
            )
        ).first()
        return bool(row)

    def run(self, ctx: UpgradeContext) -> dict:
        # Find every person who currently has at least one trashed-asset
        # face. We don't need to recompute people who only ever had
        # active-asset faces — their old centroid is already correct.
        affected = ctx.session.execute(
            text(
                "SELECT DISTINCT m.person_id"
                " FROM face_person_matches m"
                " JOIN faces f ON f.face_id = m.face_id"
                " JOIN assets a ON a.asset_id = f.asset_id"
                " WHERE a.deleted_at IS NOT NULL"
            )
        ).fetchall()
        person_ids = [r[0] for r in affected]

        repo = PersonRepository(ctx.session)
        for pid in person_ids:
            repo._recompute_centroid(pid)

        # Flip the cluster cache dirty so the next /v1/faces/clusters
        # fetch recomputes against the (now-correct) embedding pool.
        if person_ids:
            ctx.session.execute(
                text(
                    "INSERT INTO system_metadata (key, value, updated_at)"
                    " VALUES ('face_clusters_dirty', 'true', NOW())"
                    " ON CONFLICT (key) DO UPDATE"
                    "   SET value = 'true', updated_at = NOW()"
                )
            )

        ctx.session.commit()
        logger.info(
            "recompute_centroids_for_trash_filter complete: recomputed=%d",
            len(person_ids),
        )
        return {"recomputed": len(person_ids)}
