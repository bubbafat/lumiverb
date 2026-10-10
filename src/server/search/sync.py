"""Search sync: build Quickwit documents and sync assets, scenes and transcripts.

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

from src.shared.utils import utcnow
from src.server.models.tenant import Asset, AssetMetadata, VideoScene
from src.server.search.quickwit_client import QuickwitClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Reindex after an index was remade
# ---------------------------------------------------------------------------

# A remade index (it was missing, or its schema changed) holds nothing, so
# everything goes in again. Whatever remakes one only records it here, in
# system_metadata; the upkeep sweep clears the sync times and reindexes.
REINDEX_RESETS = {
    "assets": "UPDATE assets SET search_synced_at = NULL",
    "scenes": "UPDATE video_scenes SET search_synced_at = NULL",
    "transcripts": "UPDATE assets SET transcript_synced_at = NULL",
}
_REINDEX_KEY = "search.reindex."


def mark_reindex(session: Session, kinds: list[str]) -> None:
    """Record that these indexes ("assets", "scenes", "transcripts") need
    everything again; the next sweep does it. Commits."""
    for kind in kinds:
        if kind not in REINDEX_RESETS:
            raise ValueError(f"Unknown search index kind: {kind!r}")
        session.execute(text(
            "INSERT INTO system_metadata (key, value, updated_at) VALUES (:k, 'needed', now())"
            " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"),
            {"k": _REINDEX_KEY + kind})
    session.commit()


def reset_marked(session: Session) -> list[str]:
    """Clear the sync times of the indexes marked for a reindex, and the
    marks, in one transaction. Returns the kinds reset."""
    keys = session.execute(text("DELETE FROM system_metadata WHERE key = ANY(:keys) RETURNING key"),
                           {"keys": [_REINDEX_KEY + k for k in REINDEX_RESETS]}).scalars().all()
    kinds = sorted(k.removeprefix(_REINDEX_KEY) for k in keys)
    for kind in kinds:
        session.execute(text(REINDEX_RESETS[kind]))
    session.commit()
    return kinds


# ---------------------------------------------------------------------------
# Document builders
# ---------------------------------------------------------------------------

def _path_to_tokens(rel_path: str) -> str:
    """Turn rel_path into space-separated tokens for BM25 search."""
    s = re.sub(r"[/\\_\-.]", " ", rel_path)
    return re.sub(r" +", " ", s).strip()


def build_asset_document(asset: Asset, meta: AssetMetadata | None, ocr_text: str = "",
                         corrections: dict | None = None) -> dict:
    """Build a Quickwit document for an asset as a person sees it: its latest
    AI description and the text read in its image, or what a person wrote."""
    from src.server.repository.corrections import apply, machine_tags

    data = (meta.data if meta else None) or {}
    description, tags, ocr_text, _ = apply(corrections, data.get("description", ""), machine_tags(data), ocr_text)

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
        "description": description or "",
        "tags": tags,
        "capture_ts": capture_ts,
        "camera_make": asset.camera_make,
        "camera_model": asset.camera_model,
        "gps_lat": asset.gps_lat,
        "gps_lon": asset.gps_lon,
        "ocr_text": ocr_text or "",
        "note": asset.note or "",
        "transcript_text": asset.transcript_text or "",
        "searchable": True,
        "model_id": meta.model_id if meta else "",
        "model_version": meta.model_version if meta else "",
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

def _transcript_documents(asset: Asset, srt: str | None) -> list[dict]:
    """The transcript index's documents for a clip: one per SRT segment."""
    from src.server.srt import parse_srt_segments

    now = int(utcnow().timestamp())
    return [
        {
            "id": f"{asset.asset_id}_{seg.start_ms}_{seg.end_ms}",
            "asset_id": asset.asset_id,
            "library_id": asset.library_id,
            "rel_path": asset.rel_path,
            "media_type": asset.media_type,
            "start_ms": seg.start_ms,
            "end_ms": seg.end_ms,
            "text": seg.text,
            "language": asset.transcript_language or "",
            "indexed_at": now,
        }
        for seg in (parse_srt_segments(srt) if srt else [])
    ]


def index_transcript_segments(session: Session, tenant_id: str, asset: Asset) -> None:
    """Replace an asset's documents in the transcript-segment index with its
    transcript as committed (none: its documents just go).

    Called wherever the segments change or come back: when a transcript is
    submitted or removed, and when a trashed asset is restored (trashing
    deletes them). One at a time per clip (an advisory lock), each reading
    the transcript once it holds the clip: of two saves at once, the later
    one's segments are what search keeps. Best effort: on failure the clip's
    transcript_synced_at is cleared and the sync sweep indexes it. That's
    written in its own transaction on the session's database (the caller's
    isn't touched).
    """
    from src.server.search import quickwit_client as qwc

    remade = False
    try:
        with Session(session.get_bind()) as own:
            own.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
                        {"k": f"transcript_index:{asset.asset_id}"})
            started = utcnow()
            synced: datetime | None = None
            try:
                qw = qwc.QuickwitClient()
                if not qw.enabled:
                    return
                remade = qw.ensure_tenant_transcript_index(tenant_id)
                qw.delete_tenant_transcript_documents(tenant_id, asset.asset_id)
                now = own.get(Asset, asset.asset_id)
                docs = _transcript_documents(now, now.transcript_srt) if now is not None else []
                if docs:
                    qw.ingest_tenant_transcript_documents(tenant_id, docs)
                synced = started
            except Exception as exc:
                logger.warning("Transcript segment indexing failed for %s: %s", asset.asset_id, exc)
            # Never waits long on a lock the caller holds on the row.
            own.execute(text("SET LOCAL lock_timeout = '2s'"))
            own.execute(text("UPDATE assets SET transcript_synced_at = :t WHERE asset_id = :a"),
                        {"t": synced, "a": asset.asset_id})
            own.commit()
        if remade:
            with Session(session.get_bind()) as own:
                mark_reindex(own, ["transcripts"])
    except Exception as exc:
        logger.warning("Couldn't record %s's transcript sync: %s", asset.asset_id, exc)


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
    meta: AssetMetadata | None = None,
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
        if tenant_id and qw.ensure_tenant_index(tenant_id):
            # Made just now (it was missing): every clip goes in again, by the sweep.
            logger.info("Quickwit asset index made for %s — the sweep reindexes every clip", tenant_id)
            mark_reindex(session, ["assets"])
        from src.server.repository.corrections import CorrectionsRepository
        from src.server.repository.tenant import AssetOcrRepository

        doc = build_asset_document(asset, meta, AssetOcrRepository(session).text_for(asset.asset_id),
                                   CorrectionsRepository(session).get(asset.asset_id))
        if tenant_id:
            # Quickwit doesn't upsert: the old document goes first (the asset
            # index's only; the clip's scenes and transcript stay), or its old
            # words stay searchable.
            qw.delete_asset_index_documents_by_asset_ids(tenant_id, [asset.asset_id])
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
        if tenant_id and qw.ensure_tenant_scene_index(tenant_id):
            logger.info("Quickwit scene index made for %s — the sweep reindexes every scene", tenant_id)
            mark_reindex(session, ["scenes"])
        doc = build_scene_document(scene, asset)
        if tenant_id:
            qw.delete_scene_index_documents_by_scene_ids(tenant_id, [scene.scene_id])  # no upsert: the old one goes
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

# Every clip has a search document (its path, notes, transcript, description
# and the text in its image); it's stale until synced after the latest of its
# description, its OCR and its corrections. SQL on active_assets a; the repair summary counts
# with the same rule.
STALE_SEARCH = (
    "(a.search_synced_at IS NULL"
    " OR a.search_synced_at < (SELECT MAX(sm.generated_at) FROM asset_metadata sm WHERE sm.asset_id = a.asset_id)"
    " OR a.search_synced_at < (SELECT so.generated_at FROM asset_ocr so WHERE so.asset_id = a.asset_id)"
    " OR a.search_synced_at < (SELECT sc.updated_at FROM asset_corrections sc WHERE sc.asset_id = a.asset_id))"
)


def _mark_synced(session: Session, table: str, key: str, at: datetime, ids: list[str], xmins: list[str]) -> None:
    """Mark these rows synced at `at`, each only if its xmin is still the one
    read (nothing wrote the row since its document was built)."""
    session.execute(
        text(f"UPDATE {table} t SET search_synced_at = :at"
             " FROM unnest(CAST(:ids AS text[]), CAST(:xmins AS text[])) AS v(id, x)"
             f" WHERE t.{key} = v.id AND t.xmin::text = v.x"),
        {"at": at, "ids": ids, "xmins": xmins},
    )


def run_search_sync_sweep(session: Session, tenant_id: str | None = None) -> dict:
    """Find and sync all assets, scenes and transcripts with stale or missing sync times.

    Uses per-tenant indexes. tenant_id is required for Quickwit sync.
    First makes any missing index, and clears the sync times of every index
    marked for a reindex (mark_reindex), so what a lost index held goes back.
    Returns {"synced", "failed", "scenes_synced", "scenes_failed",
    "transcripts_synced", "transcripts_failed"}.
    """
    result = {"synced": 0, "failed": 0, "scenes_synced": 0, "scenes_failed": 0,
              "transcripts_synced": 0, "transcripts_failed": 0}
    qw = _get_quickwit()
    if qw is None or not tenant_id:
        return result

    from src.server.repository.tenant import AssetMetadataRepository

    # Every index is made here even with nothing to put in it yet (searches
    # on a missing transcript index fail).
    ready: set[str] = set()
    for kind, ensure in (("assets", qw.ensure_tenant_index), ("scenes", qw.ensure_tenant_scene_index),
                         ("transcripts", qw.ensure_tenant_transcript_index)):
        try:
            if ensure(tenant_id):
                logger.info("Quickwit %s index made for %s — reindexing all of it", kind, tenant_id)
                mark_reindex(session, [kind])
            ready.add(kind)
        except Exception as exc:
            logger.warning("Cannot ensure tenant Quickwit %s index for %s: %s", kind, tenant_id, exc)
    for kind in reset_marked(session):
        logger.info("Search sync times cleared for %s's %s: reindexing", tenant_id, kind)

    # Synced as of when this began: a description, OCR, correction or
    # transcript saved meanwhile is newer, and goes in next time. And only a
    # row nothing wrote meanwhile (its xmin) is marked: a write that cleared
    # its sync time (a note, a move) stays cleared.
    started = utcnow()

    # --- Asset sync ---
    rows = session.execute(text(f"""
        SELECT a.asset_id, a.library_id, COALESCE(o.text, '') AS ocr_text,
               c.description AS c_description, c.ocr_text AS c_ocr_text,
               c.asset_id AS c_asset_id, c.tags AS c_tags, x.xmin::text AS row_xmin
        FROM active_assets a
        JOIN assets x ON x.asset_id = a.asset_id
        LEFT JOIN asset_ocr o ON o.asset_id = a.asset_id
        LEFT JOIN asset_corrections c ON c.asset_id = a.asset_id
        WHERE {STALE_SEARCH}
        ORDER BY a.library_id, a.asset_id
        LIMIT 1000
    """)).fetchall()
    if "assets" not in ready:
        result["failed"] += len(rows)
        rows = []

    meta_repo = AssetMetadataRepository(session)
    all_docs: list[dict] = []
    all_asset_ids: list[str] = []
    all_xmins: list[str] = []

    for r in rows:
        asset = session.get(Asset, r.asset_id)
        if asset is None:
            continue
        meta = meta_repo.get_latest(asset_id=r.asset_id)
        corrections = None
        if r.c_asset_id is not None:  # a person wrote something for the clip
            corrections = {"description": r.c_description, "ocr_text": r.c_ocr_text, "tags": r.c_tags}
        all_docs.append(build_asset_document(asset, meta, r.ocr_text, corrections))
        all_asset_ids.append(r.asset_id)
        all_xmins.append(r.row_xmin)

    if all_docs:
        try:
            # Delete-then-insert: Quickwit doesn't upsert, so without
            # an explicit delete the same asset's doc piles up on every
            # re-sync. We were seeing 3x duplication after a few force
            # re-syncs, which broke the position-based ranking math.
            qw.delete_asset_index_documents_by_asset_ids(tenant_id, all_asset_ids)
            qw.ingest_tenant_documents(tenant_id, all_docs)
            _mark_synced(session, "assets", "asset_id", started, all_asset_ids, all_xmins)
            session.commit()
            result["synced"] += len(all_asset_ids)
        except Exception as exc:
            logger.warning("Quickwit tenant batch ingest failed for %s: %s", tenant_id, exc)
            session.rollback()
            result["failed"] += len(all_docs)

    # --- Scene sync ---
    # A clip's scene documents are deleted together (by clip: that also takes
    # those of scenes found again under new ids), so every described scene of
    # a clip with a stale one goes in again, not only the stale ones; and the
    # limit is on clips, so none is split.
    scene_rows = session.execute(text("""
        SELECT vs.scene_id, vs.asset_id, vs.xmin::text AS row_xmin
        FROM video_scenes vs
        WHERE vs.description IS NOT NULL
          AND vs.asset_id IN (
              SELECT DISTINCT s.asset_id FROM video_scenes s
              JOIN active_assets a ON a.asset_id = s.asset_id
              WHERE s.description IS NOT NULL
                AND (s.search_synced_at IS NULL OR s.search_synced_at < s.created_at)
              ORDER BY s.asset_id
              LIMIT 100)
        ORDER BY vs.asset_id, vs.scene_id
    """)).fetchall()
    if "scenes" not in ready:
        result["scenes_failed"] += len(scene_rows)
        scene_rows = []

    all_scene_docs: list[dict] = []
    all_scene_ids: list[str] = []
    scene_xmins: list[str] = []
    for r in scene_rows:
        scene = session.get(VideoScene, r.scene_id)
        asset = session.get(Asset, r.asset_id)
        if scene is None or asset is None:
            continue
        all_scene_docs.append(build_scene_document(scene, asset))
        all_scene_ids.append(r.scene_id)
        scene_xmins.append(r.row_xmin)

    if all_scene_docs:
        try:
            # Same delete-then-insert dedupe gate as the asset path, by clip
            # (above: each picked clip's described scenes all go in again).
            qw.delete_scene_index_documents_by_asset_ids(tenant_id, sorted({r.asset_id for r in scene_rows}))
            qw.ingest_tenant_scene_documents(tenant_id, all_scene_docs)
            _mark_synced(session, "video_scenes", "scene_id", started, all_scene_ids, scene_xmins)
            session.commit()
            result["scenes_synced"] += len(all_scene_ids)
        except Exception as exc:
            logger.warning("Quickwit tenant scene batch ingest failed for %s: %s", tenant_id, exc)
            session.rollback()
            result["scenes_failed"] += len(all_scene_docs)

    # --- Transcript sync ---
    # Synced as of when this began (above): a transcript replaced meanwhile is newer, and goes in next time.
    transcript_ids = list(session.execute(text("""
        SELECT a.asset_id FROM active_assets a
        WHERE a.transcript_srt IS NOT NULL
          AND (a.transcript_synced_at IS NULL OR a.transcript_synced_at < a.transcribed_at)
        ORDER BY a.asset_id
        LIMIT 200
    """)).scalars().all())
    if "transcripts" not in ready:
        result["transcripts_failed"] += len(transcript_ids)
        transcript_ids = []

    if transcript_ids:
        try:
            docs: list[dict] = []
            for asset_id in transcript_ids:
                asset = session.get(Asset, asset_id)
                if asset is not None:
                    docs.extend(_transcript_documents(asset, asset.transcript_srt))
            qw.delete_transcript_index_documents_by_asset_ids(tenant_id, transcript_ids)
            qw.ingest_tenant_transcript_documents(tenant_id, docs)
            session.execute(
                text("UPDATE assets SET transcript_synced_at = :t WHERE asset_id = ANY(:ids)"),
                {"t": started, "ids": transcript_ids},
            )
            session.commit()
            result["transcripts_synced"] += len(transcript_ids)
        except Exception as exc:
            logger.warning("Quickwit tenant transcript batch ingest failed for %s: %s", tenant_id, exc)
            session.rollback()
            result["transcripts_failed"] += len(transcript_ids)

    return result
