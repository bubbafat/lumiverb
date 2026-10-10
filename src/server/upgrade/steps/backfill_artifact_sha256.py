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

from src.server.storage.local import get_storage
from src.server.upgrade.context import UpgradeContext
from src.server.upgrade.step import UpgradeStepInfo

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


def _hash_key(storage, key: str) -> str | None:
    """_hash_file of a stored key; None (as missing) for a key outside DATA_DIR."""
    from src.server.storage.local import UnsafeKeyError

    try:
        path = storage.abs_path(key)
    except UnsafeKeyError:
        return None
    return _hash_file(path)


def _backfill(ctx: UpgradeContext, *, table: str, id_col: str, key_col: str, sha_col: str, what: str) -> dict:
    """Hash each row's file and write its SHA-256, a page at a time by id
    (keyset: missing files are passed over, not re-read every page). The
    write only lands while the row still names that file and has no hash: a
    re-render meanwhile records its own."""
    storage = get_storage()
    updated = 0
    missing = 0
    after = ""
    while True:
        rows = ctx.session.exec(
            text(
                f"SELECT {id_col}, {key_col} FROM {table}"  # noqa: S608 — fixed names
                f" WHERE {key_col} IS NOT NULL AND {sha_col} IS NULL AND {id_col} > :after"
                f" ORDER BY {id_col} LIMIT :limit"
            ).bindparams(after=after, limit=_BATCH_SIZE)
        ).fetchall()
        if not rows:
            break

        for row_id, key in rows:
            sha256 = _hash_key(storage, key)
            if sha256 is None:
                logger.warning("%s file missing for %s: %s", what, row_id, key)
                missing += 1
                continue
            ctx.session.exec(
                text(
                    f"UPDATE {table} SET {sha_col} = :sha"  # noqa: S608 — fixed names
                    f" WHERE {id_col} = :id AND {key_col} = :key AND {sha_col} IS NULL"
                ).bindparams(sha=sha256, id=row_id, key=key)
            )
            updated += 1

        after = rows[-1][0]
        ctx.session.commit()

    logger.info("backfill %s sha256 complete: updated=%d missing=%d", what, updated, missing)
    return {"updated": updated, "missing": missing}


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
        return _backfill(ctx, table="assets", id_col="asset_id", key_col="proxy_key", sha_col="proxy_sha256",
                         what="proxy")


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
        return _backfill(ctx, table="assets", id_col="asset_id", key_col="thumbnail_key",
                         sha_col="thumbnail_sha256", what="thumbnail")


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
        return _backfill(ctx, table="video_scenes", id_col="scene_id", key_col="proxy_key",
                         sha_col="rep_frame_sha256", what="scene rep")
