"""Backfill SHA-256 hashes for proxy, thumbnail, and video scene rep-frame artifacts.

These steps hash artifacts that were generated before Phase 1 (hash capture) was deployed.
Each step reads files already present on disk and writes their SHA-256 into the DB.
Workers must be drained before running; the system should be in maintenance mode.

Missing files (key set but file not on disk) are skipped — proxy_sha256 remains NULL.
The runner marks each step completed only after the batch loop exhausts all hashable rows.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from sqlalchemy import text

from src.storage.local import get_storage
from src.upgrade.context import UpgradeContext
from src.upgrade.step import UpgradeStepInfo

logger = logging.getLogger(__name__)

_BATCH_SIZE = 500


def _hash_file(path: Path) -> str | None:
    """Return SHA-256 hex digest of a file, or None if the file does not exist."""
    if not path.exists():
        return None
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


class BackfillProxySha256Step:
    """Hash all existing proxy files and write proxy_sha256 on the assets table."""

    info = UpgradeStepInfo(
        step_id="backfill_proxy_sha256",
        version="1",
        display_name="Backfill proxy SHA-256 hashes",
    )

    def needs_work(self, ctx: UpgradeContext) -> bool:
        row = ctx.session.exec(
            text(
                "SELECT COUNT(*) FROM assets"
                " WHERE proxy_key IS NOT NULL AND proxy_sha256 IS NULL"
            )
        ).first()
        return bool(row and row[0] > 0)

    def run(self, ctx: UpgradeContext) -> dict:
        storage = get_storage()
        updated = 0
        missing = 0
        # IDs whose files are missing — excluded from subsequent batches so the loop terminates.
        skip_ids: set[str] = set()

        while True:
            query = (
                "SELECT asset_id, proxy_key FROM assets"
                " WHERE proxy_key IS NOT NULL AND proxy_sha256 IS NULL"
            )
            params: dict = {"limit": _BATCH_SIZE}
            if skip_ids:
                placeholders = ", ".join(f":skip_{i}" for i in range(len(skip_ids)))
                query += f" AND asset_id NOT IN ({placeholders})"
                for i, sid in enumerate(skip_ids):
                    params[f"skip_{i}"] = sid
            query += " LIMIT :limit"

            rows = ctx.session.exec(text(query).bindparams(**params)).fetchall()
            if not rows:
                break

            for asset_id, proxy_key in rows:
                sha256 = _hash_file(storage.abs_path(proxy_key))
                if sha256 is None:
                    logger.warning("proxy file missing for asset %s: %s", asset_id, proxy_key)
                    missing += 1
                    skip_ids.add(asset_id)
                    continue
                ctx.session.exec(
                    text(
                        "UPDATE assets SET proxy_sha256 = :sha WHERE asset_id = :id"
                    ).bindparams(sha=sha256, id=asset_id)
                )
                updated += 1

            ctx.session.commit()

        logger.info("backfill_proxy_sha256 complete: updated=%d missing=%d", updated, missing)
        return {"updated": updated, "missing": missing}


class BackfillThumbnailSha256Step:
    """Hash all existing thumbnail files and write thumbnail_sha256 on the assets table."""

    info = UpgradeStepInfo(
        step_id="backfill_thumbnail_sha256",
        version="1",
        display_name="Backfill thumbnail SHA-256 hashes",
    )

    def needs_work(self, ctx: UpgradeContext) -> bool:
        row = ctx.session.exec(
            text(
                "SELECT COUNT(*) FROM assets"
                " WHERE thumbnail_key IS NOT NULL AND thumbnail_sha256 IS NULL"
            )
        ).first()
        return bool(row and row[0] > 0)

    def run(self, ctx: UpgradeContext) -> dict:
        storage = get_storage()
        updated = 0
        missing = 0
        skip_ids: set[str] = set()

        while True:
            query = (
                "SELECT asset_id, thumbnail_key FROM assets"
                " WHERE thumbnail_key IS NOT NULL AND thumbnail_sha256 IS NULL"
            )
            params: dict = {"limit": _BATCH_SIZE}
            if skip_ids:
                placeholders = ", ".join(f":skip_{i}" for i in range(len(skip_ids)))
                query += f" AND asset_id NOT IN ({placeholders})"
                for i, sid in enumerate(skip_ids):
                    params[f"skip_{i}"] = sid
            query += " LIMIT :limit"

            rows = ctx.session.exec(text(query).bindparams(**params)).fetchall()
            if not rows:
                break

            for asset_id, thumbnail_key in rows:
                sha256 = _hash_file(storage.abs_path(thumbnail_key))
                if sha256 is None:
                    logger.warning(
                        "thumbnail file missing for asset %s: %s", asset_id, thumbnail_key
                    )
                    missing += 1
                    skip_ids.add(asset_id)
                    continue
                ctx.session.exec(
                    text(
                        "UPDATE assets SET thumbnail_sha256 = :sha WHERE asset_id = :id"
                    ).bindparams(sha=sha256, id=asset_id)
                )
                updated += 1

            ctx.session.commit()

        logger.info(
            "backfill_thumbnail_sha256 complete: updated=%d missing=%d", updated, missing
        )
        return {"updated": updated, "missing": missing}


class BackfillSceneRepSha256Step:
    """Hash all existing video scene rep-frame files and write rep_frame_sha256."""

    info = UpgradeStepInfo(
        step_id="backfill_scene_rep_sha256",
        version="1",
        display_name="Backfill video scene rep-frame SHA-256 hashes",
    )

    def needs_work(self, ctx: UpgradeContext) -> bool:
        row = ctx.session.exec(
            text(
                "SELECT COUNT(*) FROM video_scenes"
                " WHERE proxy_key IS NOT NULL AND rep_frame_sha256 IS NULL"
            )
        ).first()
        return bool(row and row[0] > 0)

    def run(self, ctx: UpgradeContext) -> dict:
        storage = get_storage()
        updated = 0
        missing = 0
        skip_ids: set[str] = set()

        while True:
            query = (
                "SELECT scene_id, proxy_key FROM video_scenes"
                " WHERE proxy_key IS NOT NULL AND rep_frame_sha256 IS NULL"
            )
            params: dict = {"limit": _BATCH_SIZE}
            if skip_ids:
                placeholders = ", ".join(f":skip_{i}" for i in range(len(skip_ids)))
                query += f" AND scene_id NOT IN ({placeholders})"
                for i, sid in enumerate(skip_ids):
                    params[f"skip_{i}"] = sid
            query += " LIMIT :limit"

            rows = ctx.session.exec(text(query).bindparams(**params)).fetchall()
            if not rows:
                break

            for scene_id, rep_key in rows:
                sha256 = _hash_file(storage.abs_path(rep_key))
                if sha256 is None:
                    logger.warning(
                        "scene rep file missing for scene %s: %s", scene_id, rep_key
                    )
                    missing += 1
                    skip_ids.add(scene_id)
                    continue
                ctx.session.exec(
                    text(
                        "UPDATE video_scenes SET rep_frame_sha256 = :sha WHERE scene_id = :id"
                    ).bindparams(sha=sha256, id=scene_id)
                )
                updated += 1

            ctx.session.commit()

        logger.info(
            "backfill_scene_rep_sha256 complete: updated=%d missing=%d", updated, missing
        )
        return {"updated": updated, "missing": missing}
