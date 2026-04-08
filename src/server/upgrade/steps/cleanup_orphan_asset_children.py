"""One-time cleanup of orphaned child rows pointing at deleted assets.

Background: ``FaceRepository.permanently_delete`` was missing several
child tables — most notably ``faces`` — so for some time hard-deleted
assets left behind orphan rows whose ``asset_id`` no longer existed.
The user-visible symptom: cluster review surfaced face crops (still
served from the file cache via ``/v1/faces/{id}/crop``) whose owning
asset 404'd in the lightbox. The fix in ``permanently_delete`` covers
new deletes; this step cleans up the existing residue on every tenant.

The step is idempotent: ``needs_work`` returns False once there are
zero orphans across every cleanable table. Each per-table delete uses
a single SQL statement (``WHERE NOT EXISTS`` against ``assets``) so
the whole thing fits in one transaction even on large tenants.
"""

from __future__ import annotations

import logging

from sqlalchemy import text

from src.server.upgrade.context import UpgradeContext
from src.server.upgrade.step import UpgradeStepInfo

logger = logging.getLogger(__name__)


# Tables whose ``asset_id`` column should reference an existing assets
# row but won't if the original delete path missed them. Order matters
# only if there are FKs *between* these children — currently there are
# none, so the order is purely cosmetic.
_CLEANABLE_TABLES = (
    "asset_metadata",
    "asset_embeddings",
    "video_scenes",
    "video_index_chunks",
    "collection_assets",
    "asset_ratings",
)


class CleanupOrphanAssetChildrenStep:
    """Delete every child row whose ``asset_id`` no longer exists in ``assets``.

    Faces are handled separately because two other FKs point at them
    (``face_person_matches.face_id`` and ``people.representative_face_id``)
    and must be cleared first or the face delete will FK-violate. The
    ``cover_asset_id`` on collections is nullable and gets a SET NULL
    rather than a delete.
    """

    info = UpgradeStepInfo(
        step_id="cleanup_orphan_asset_children",
        version="1",
        display_name="Clean up orphan asset child rows",
    )

    def needs_work(self, ctx: UpgradeContext) -> bool:
        # Cheap union check: any orphan row across any table → work to do.
        for table in _CLEANABLE_TABLES:
            row = ctx.session.exec(
                text(
                    f"SELECT 1 FROM {table} c"
                    " WHERE NOT EXISTS ("
                    "   SELECT 1 FROM assets a WHERE a.asset_id = c.asset_id"
                    " ) LIMIT 1"
                )
            ).first()
            if row:
                return True
        # Faces have the same kind of orphan + the cluster review symptom
        # is specifically about them, so check separately.
        face_row = ctx.session.exec(
            text(
                "SELECT 1 FROM faces f"
                " WHERE NOT EXISTS ("
                "   SELECT 1 FROM assets a WHERE a.asset_id = f.asset_id"
                " ) LIMIT 1"
            )
        ).first()
        if face_row:
            return True
        # Collections.cover_asset_id is nullable — orphan = non-null and
        # missing in assets.
        cover_row = ctx.session.exec(
            text(
                "SELECT 1 FROM collections c"
                " WHERE c.cover_asset_id IS NOT NULL"
                " AND NOT EXISTS ("
                "   SELECT 1 FROM assets a WHERE a.asset_id = c.cover_asset_id"
                " ) LIMIT 1"
            )
        ).first()
        return bool(cover_row)

    def run(self, ctx: UpgradeContext) -> dict:
        counts: dict[str, int] = {}

        # Plain child tables — single delete each.
        for table in _CLEANABLE_TABLES:
            result = ctx.session.exec(
                text(
                    f"DELETE FROM {table} c"
                    " WHERE NOT EXISTS ("
                    "   SELECT 1 FROM assets a WHERE a.asset_id = c.asset_id"
                    " )"
                )
            )
            counts[table] = result.rowcount or 0

        # Faces are the headline orphan — same NOT EXISTS pattern but
        # the two referencing tables (face_person_matches and people)
        # have to be cleared first.
        ctx.session.exec(
            text(
                "DELETE FROM face_person_matches"
                " WHERE face_id IN ("
                "   SELECT f.face_id FROM faces f"
                "   WHERE NOT EXISTS ("
                "     SELECT 1 FROM assets a WHERE a.asset_id = f.asset_id"
                "   )"
                " )"
            )
        )
        ctx.session.exec(
            text(
                "UPDATE people SET representative_face_id = NULL"
                " WHERE representative_face_id IN ("
                "   SELECT f.face_id FROM faces f"
                "   WHERE NOT EXISTS ("
                "     SELECT 1 FROM assets a WHERE a.asset_id = f.asset_id"
                "   )"
                " )"
            )
        )
        face_result = ctx.session.exec(
            text(
                "DELETE FROM faces f"
                " WHERE NOT EXISTS ("
                "   SELECT 1 FROM assets a WHERE a.asset_id = f.asset_id"
                " )"
            )
        )
        counts["faces"] = face_result.rowcount or 0

        # Collection cover is nullable — null it instead of deleting.
        cover_result = ctx.session.exec(
            text(
                "UPDATE collections SET cover_asset_id = NULL"
                " WHERE cover_asset_id IS NOT NULL"
                " AND NOT EXISTS ("
                "   SELECT 1 FROM assets a WHERE a.asset_id = collections.cover_asset_id"
                " )"
            )
        )
        counts["collections.cover_asset_id"] = cover_result.rowcount or 0

        # The cluster cache references face IDs that we may have just
        # deleted, so flip it dirty so the next /v1/faces/clusters call
        # recomputes from the cleaned-up face table.
        if counts.get("faces", 0) > 0:
            ctx.session.execute(
                text(
                    "INSERT INTO system_metadata (key, value, updated_at)"
                    " VALUES ('face_clusters_dirty', 'true', NOW())"
                    " ON CONFLICT (key) DO UPDATE"
                    "   SET value = 'true', updated_at = NOW()"
                )
            )

        ctx.session.commit()

        total = sum(counts.values())
        logger.info(
            "cleanup_orphan_asset_children complete: total=%d %s",
            total,
            counts,
        )
        return {"total": total, **counts}
