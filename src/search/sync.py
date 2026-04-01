"""Search sync: build Quickwit documents and sync assets/scenes.

This module provides:
- Document builders (shared between inline sync and maintenance sweep)
- try_sync_asset / try_sync_scene: best-effort inline sync (never raises)
- run_search_sync_sweep: maintenance sweep for stale/missing syncs
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

from sqlalchemy import text
from sqlmodel import Session

from src.core.utils import utcnow
from src.models.tenant import Asset, AssetMetadata, VideoScene
from src.search.quickwit_client import QuickwitClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Document builders
# ---------------------------------------------------------------------------

def _path_to_tokens(rel_path: str) -> str:
    """Turn rel_path into space-separated tokens for BM25 search."""
    s = re.sub(r"[/\\_\-.]", " ", rel_path)
    return re.sub(r" +", " ", s).strip()


def build_asset_document(asset: Asset, meta: AssetMetadata) -> dict:
    """Build a Quickwit document for an asset + its latest AI metadata."""
    data = meta.data or {}
    description = data.get("description", "")
    tags = data.get("tags") or []

    capture_ts = None
    if asset.taken_at:
        capture_ts = int(asset.taken_at.timestamp())

    return {
        "id": asset.asset_id,
        "asset_id": asset.asset_id,
        "library_id": asset.library_id,
        "rel_path": asset.rel_path,
        "path_tokens": _path_to_tokens(asset.rel_path),
        "media_type": asset.media_type,
        "description": description,
        "tags": tags,
        "capture_ts": capture_ts,
        "camera_make": asset.camera_make,
        "camera_model": asset.camera_model,
        "gps_lat": asset.gps_lat,
        "gps_lon": asset.gps_lon,
        "transcript_text": asset.transcript_text or "",
        "searchable": True,
        "model_id": meta.model_id,
        "model_version": meta.model_version,
        "indexed_at": int(utcnow().timestamp()),
    }


def build_scene_document(scene: VideoScene, asset: Asset) -> dict:
    """Build a Quickwit document for a video scene."""
    return {
        "id": scene.scene_id,
        "scene_id": scene.scene_id,
        "asset_id": asset.asset_id,
        "library_id": asset.library_id,
        "rel_path": asset.rel_path,
        "start_ms": scene.start_ms,
        "end_ms": scene.end_ms,
        "rep_frame_ms": scene.rep_frame_ms,
        "thumbnail_key": scene.thumbnail_key,
        "duration_sec": asset.duration_sec,
        "description": scene.description or "",
        "tags": scene.tags or [],
        "sharpness_score": scene.sharpness_score,
        "keep_reason": scene.keep_reason,
        "model_id": "",
        "model_version": "",
        "indexed_at": int(utcnow().timestamp()),
    }


# ---------------------------------------------------------------------------
# Inline sync (best-effort, never raises)
# ---------------------------------------------------------------------------

def _get_quickwit() -> QuickwitClient | None:
    """Get a QuickwitClient, returning None if disabled or unavailable."""
    try:
        qw = QuickwitClient()
        return qw if qw.enabled else None
    except Exception:
        return None


def try_sync_asset(
    session: Session,
    asset: Asset,
    meta: AssetMetadata,
    tenant_id: str | None = None,
    quickwit: QuickwitClient | None = None,
) -> bool:
    """Try to sync an asset to Quickwit. Returns True on success, False on failure.

    On success, sets asset.search_synced_at and commits. On failure, logs a
    warning but never raises — the maintenance sweep will catch it later.
    """
    qw = quickwit or _get_quickwit()
    if qw is None:
        return False

    try:
        if tenant_id:
            qw.ensure_tenant_index(tenant_id)
        doc = build_asset_document(asset, meta)
        if tenant_id:
            qw.ingest_tenant_documents(tenant_id, [doc])
        asset.search_synced_at = utcnow()
        session.add(asset)
        session.commit()
        return True
    except Exception as exc:
        logger.warning("Inline search sync failed for asset %s: %s", asset.asset_id, exc)
        return False


def try_sync_scene(
    session: Session,
    scene: VideoScene,
    asset: Asset,
    tenant_id: str | None = None,
    quickwit: QuickwitClient | None = None,
) -> bool:
    """Try to sync a video scene to Quickwit. Returns True on success."""
    qw = quickwit or _get_quickwit()
    if qw is None:
        return False

    try:
        if tenant_id:
            qw.ensure_tenant_scene_index(tenant_id)
        doc = build_scene_document(scene, asset)
        if tenant_id:
            qw.ingest_tenant_scene_documents(tenant_id, [doc])
        scene.search_synced_at = utcnow()
        session.add(scene)
        session.commit()
        return True
    except Exception as exc:
        logger.warning("Inline search sync failed for scene %s: %s", scene.scene_id, exc)
        return False


# ---------------------------------------------------------------------------
# Maintenance sweep
# ---------------------------------------------------------------------------

def run_search_sync_sweep(session: Session, tenant_id: str | None = None) -> dict:
    """Find and sync all assets/scenes with stale or missing search_synced_at.

    Uses per-tenant indexes. tenant_id is required for Quickwit sync.
    Returns {"synced": N, "failed": M, "scenes_synced": S, "scenes_failed": F}.
    """
    qw = _get_quickwit()
    if qw is None or not tenant_id:
        return {"synced": 0, "failed": 0, "scenes_synced": 0, "scenes_failed": 0}

    from src.repository.tenant import AssetMetadataRepository

    # --- Asset sync ---
    rows = session.execute(text("""
        SELECT a.asset_id, a.library_id
        FROM active_assets a
        JOIN LATERAL (
            SELECT generated_at
            FROM asset_metadata
            WHERE asset_id = a.asset_id
            ORDER BY generated_at DESC
            LIMIT 1
        ) m ON TRUE
        WHERE a.search_synced_at IS NULL
           OR a.search_synced_at < m.generated_at
        ORDER BY a.library_id, a.asset_id
        LIMIT 1000
    """)).fetchall()

    synced = 0
    failed = 0
    meta_repo = AssetMetadataRepository(session)

    try:
        qw.ensure_tenant_index(tenant_id)
    except Exception as exc:
        logger.warning("Cannot ensure tenant Quickwit index for %s: %s", tenant_id, exc)
        return {"synced": 0, "failed": len(rows), "scenes_synced": 0, "scenes_failed": 0}

    all_docs: list[dict] = []
    all_asset_ids: list[str] = []

    for r in rows:
        asset = session.get(Asset, r.asset_id)
        if asset is None:
            continue
        meta = meta_repo.get_latest(asset_id=r.asset_id)
        if meta is None:
            continue
        all_docs.append(build_asset_document(asset, meta))
        all_asset_ids.append(r.asset_id)

    if all_docs:
        try:
            qw.ingest_tenant_documents(tenant_id, all_docs)
            now = utcnow()
            session.execute(
                text("UPDATE assets SET search_synced_at = :now WHERE asset_id = ANY(:ids)"),
                {"now": now, "ids": all_asset_ids},
            )
            session.commit()
            synced += len(all_asset_ids)
        except Exception as exc:
            logger.warning("Quickwit tenant batch ingest failed for %s: %s", tenant_id, exc)
            session.rollback()
            failed += len(all_docs)

    # --- Scene sync ---
    scene_rows = session.execute(text("""
        SELECT vs.scene_id, a.asset_id, a.library_id
        FROM video_scenes vs
        JOIN active_assets a ON a.asset_id = vs.asset_id
        WHERE vs.description IS NOT NULL
          AND (vs.search_synced_at IS NULL OR vs.search_synced_at < vs.created_at)
        ORDER BY a.library_id, vs.scene_id
        LIMIT 1000
    """)).fetchall()

    scenes_synced = 0
    scenes_failed = 0

    try:
        qw.ensure_tenant_scene_index(tenant_id)
    except Exception as exc:
        logger.warning("Cannot ensure tenant scene index for %s: %s", tenant_id, exc)
        return {"synced": synced, "failed": failed, "scenes_synced": 0, "scenes_failed": len(scene_rows)}

    all_scene_docs: list[dict] = []
    all_scene_ids: list[str] = []
    for r in scene_rows:
        scene = session.get(VideoScene, r.scene_id)
        asset = session.get(Asset, r.asset_id)
        if scene is None or asset is None:
            continue
        all_scene_docs.append(build_scene_document(scene, asset))
        all_scene_ids.append(r.scene_id)

    if all_scene_docs:
        try:
            qw.ingest_tenant_scene_documents(tenant_id, all_scene_docs)
            now = utcnow()
            session.execute(
                text("UPDATE video_scenes SET search_synced_at = :now WHERE scene_id = ANY(:ids)"),
                {"now": now, "ids": all_scene_ids},
            )
            session.commit()
            scenes_synced += len(all_scene_ids)
        except Exception as exc:
            logger.warning("Quickwit tenant scene batch ingest failed for %s: %s", tenant_id, exc)
            session.rollback()
            scenes_failed += len(all_scene_docs)

    return {
        "synced": synced,
        "failed": failed,
        "scenes_synced": scenes_synced,
        "scenes_failed": scenes_failed,
    }
