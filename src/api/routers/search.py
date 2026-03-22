"""Search API: BM25 via Quickwit with Postgres fallback."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlmodel import Session

from src.api.dependencies import get_tenant_session
from src.core.config import get_settings
from src.repository.tenant import AssetRepository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/search", tags=["search"])


class SearchHit(BaseModel):
    """
    Unified search hit model for both image assets and video scenes.

    - Image hits use: type="image", asset_id, rel_path, thumbnail_key, proxy_key,
      camera_make/model, description, tags, score, source.
    - Scene hits use: type="scene", scene_id, asset_id, rel_path, thumbnail_key,
      proxy_key, start_ms, end_ms, rep_frame_ms, duration_sec, description,
      tags, score, source.
    """

    type: Literal["image", "scene"] = "image"

    # Common fields
    asset_id: str
    rel_path: str
    thumbnail_key: str | None = None
    proxy_key: str | None = None
    description: str
    tags: list[str]
    score: float
    source: str

    # Image-only fields
    camera_make: str | None = None
    camera_model: str | None = None
    media_type: str | None = None
    file_size: int | None = None
    width: int | None = None
    height: int | None = None
    taken_at: str | None = None

    # Scene-only fields
    scene_id: str | None = None
    start_ms: int | None = None
    end_ms: int | None = None
    rep_frame_ms: int | None = None
    duration_sec: float | None = None


class SearchResponse(BaseModel):
    query: str
    hits: list[SearchHit]
    total: int
    source: str


MediaType = Literal["image", "video", "all"]


@router.get("", response_model=SearchResponse)
def search(
    session: Annotated[Session, Depends(get_tenant_session)],
    library_id: str,
    q: str = Query(default="", max_length=500),
    limit: int = Query(default=20, ge=1, le=500),
    offset: int = Query(default=0, ge=0, le=10000),
    media_type: MediaType = Query(default="all"),
    path_prefix: str | None = None,
    tag: str | None = None,
    date_from: str | None = Query(
        default=None,
        description="ISO date YYYY-MM-DD, inclusive lower bound on taken_at / file_mtime",
    ),
    date_to: str | None = Query(
        default=None,
        description="ISO date YYYY-MM-DD, inclusive upper bound",
    ),
) -> SearchResponse:
    """
    Search assets and video scenes by natural language query and/or date range.

    media_type:
      - "image": image assets only
      - "video": video scenes only
      - "all": both, merged and ranked by score (default)

    date_from / date_to: filter by COALESCE(taken_at, file_mtime). Both are optional
    and may be combined with q. When q is empty and only date filters are provided
    a direct DB query is used (no BM25).
    """
    # Require at least a text query or a date filter
    if not q and not date_from and not date_to:
        return SearchResponse(query="", hits=[], total=0, source="none")

    # Parse date bounds
    dt_from: datetime | None = None
    dt_to: datetime | None = None
    if date_from:
        try:
            dt_from = datetime.strptime(date_from, "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            pass
    if date_to:
        try:
            dt_to = (
                datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
            ).replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    # Date-only mode: skip BM25, go directly to DB
    if not q and (dt_from or dt_to):
        return _search_by_date(
            session=session,
            library_id=library_id,
            dt_from=dt_from,
            dt_to=dt_to,
            path_prefix=path_prefix,
            tag=tag,
            limit=limit,
            offset=offset,
        )

    settings = get_settings()
    image_hits: list[dict] = []
    scene_hits: list[dict] = []
    source_parts: list[str] = []

    search_images = media_type in ("image", "all")
    search_scenes = media_type in ("video", "all")

    # Fetch enough results from each index so that after merging and global
    # sorting we can correctly slice [offset:offset+limit].
    fetch_limit = offset + limit

    # --- Image search ---
    if search_images:
        if settings.quickwit_enabled:
            try:
                from src.search.quickwit_client import QuickwitClient

                qw = QuickwitClient()
                image_hits = qw.search(
                    library_id=library_id,
                    query=q,
                    max_hits=fetch_limit,
                    start_offset=0,
                )
                source_parts.append("quickwit")
            except Exception as e:
                logger.warning("Quickwit image search failed: %s", e)
                if not settings.quickwit_fallback_to_postgres:
                    raise
                image_hits = []

        if not image_hits and (
            not settings.quickwit_enabled or settings.quickwit_fallback_to_postgres
        ):
            from src.search.postgres_search import search_assets

            image_hits = search_assets(
                session, library_id, q, limit=fetch_limit, offset=0
            )
            source_parts.append("postgres")

        # Enrich image hits from DB; drop trashed assets
        if image_hits:
            asset_repo = AssetRepository(session)
            assets_by_id = {
                a.asset_id: a
                for a in asset_repo.get_by_ids([h["asset_id"] for h in image_hits])
            }
            for hit in image_hits:
                asset = assets_by_id.get(hit["asset_id"])
                if asset:
                    hit["thumbnail_key"] = asset.thumbnail_key
                    hit["proxy_key"] = asset.proxy_key
                    hit["camera_make"] = asset.camera_make
                    hit["camera_model"] = asset.camera_model
                    hit["media_type"] = asset.media_type
                    hit["file_size"] = asset.file_size
                    hit["width"] = asset.width
                    hit["height"] = asset.height
                    hit["taken_at"] = (
                        asset.taken_at.isoformat() if asset.taken_at else None
                    )
                    # Temp key for date-range post-filter; popped before serialization
                    hit["_file_mtime"] = asset.file_mtime
                if hit.get("tags") is None:
                    hit["tags"] = []
                hit["type"] = "image"
            image_hits = [h for h in image_hits if h["asset_id"] in assets_by_id]

    # --- Scene search ---
    if search_scenes and settings.quickwit_enabled:
        try:
            from src.search.quickwit_client import QuickwitClient

            qw = QuickwitClient()
            scene_hits = qw.search_scenes(
                library_id=library_id,
                query=q,
                max_hits=fetch_limit,
                start_offset=0,
            )
            source_parts.append("quickwit_scenes")
        except Exception as e:
            logger.warning("Quickwit scene search failed: %s", e)
            scene_hits = []

        # Enrich scene hits; drop trashed
        if scene_hits:
            asset_repo = AssetRepository(session)
            assets_by_id = {
                a.asset_id: a
                for a in asset_repo.get_by_ids(
                    list({h["asset_id"] for h in scene_hits})
                )
            }
            for hit in scene_hits:
                asset = assets_by_id.get(hit["asset_id"])
                if asset:
                    hit["rel_path"] = asset.rel_path
                    hit["proxy_key"] = asset.proxy_key
                    hit["duration_sec"] = asset.duration_sec
                    hit["taken_at"] = (
                        asset.taken_at.isoformat() if asset.taken_at else None
                    )
                    hit["_file_mtime"] = asset.file_mtime
                hit["start_ms"] = int(hit.get("start_ms") or 0)
                hit["end_ms"] = int(hit.get("end_ms") or 0)
                hit["rep_frame_ms"] = int(hit.get("rep_frame_ms") or 0)
                if hit.get("tags") is None:
                    hit["tags"] = []
                hit["type"] = "scene"
            scene_hits = [h for h in scene_hits if h["asset_id"] in assets_by_id]

    # --- Apply optional path_prefix and tag filters after enrichment ---
    def _path_matches(rel_path: str | None) -> bool:
        if not rel_path or not path_prefix:
            return False
        return rel_path == path_prefix or rel_path.startswith(path_prefix + "/")

    def _has_tag(tags: list[str] | None) -> bool:
        if tag is None:
            return True
        return tag in (tags or [])

    if path_prefix or tag:
        if image_hits:
            image_hits = [
                h
                for h in image_hits
                if (not path_prefix or _path_matches(h.get("rel_path")))
                and _has_tag(h.get("tags"))
            ]
        if scene_hits:
            scene_hits = [
                h
                for h in scene_hits
                if (not path_prefix or _path_matches(h.get("rel_path")))
                and _has_tag(h.get("tags"))
            ]

    # --- Apply date filter (text + date combined mode) ---
    if dt_from or dt_to:

        def _date_in_range(hit: dict) -> bool:
            taken_str = hit.get("taken_at")
            effective: datetime | None = None
            if taken_str:
                try:
                    dt = datetime.fromisoformat(taken_str.replace("Z", "+00:00"))
                    effective = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
                except ValueError:
                    pass
            # Fall back to file_mtime if taken_at is absent (consistent with COALESCE)
            if effective is None:
                file_mtime = hit.get("_file_mtime")
                if file_mtime is not None:
                    effective = (
                        file_mtime
                        if file_mtime.tzinfo
                        else file_mtime.replace(tzinfo=timezone.utc)
                    )
            if effective is None:
                return False
            if dt_from and effective < dt_from:
                return False
            if dt_to and effective >= dt_to:
                return False
            return True

        image_hits = [h for h in image_hits if _date_in_range(h)]
        scene_hits = [h for h in scene_hits if _date_in_range(h)]

    # --- Merge, deduplicate images by asset_id, sort by score ---
    seen_images: dict[str, dict] = {}
    for hit in image_hits:
        aid = hit["asset_id"]
        if aid not in seen_images or hit["score"] > seen_images[aid]["score"]:
            seen_images[aid] = hit

    all_hits: list[dict] = list(seen_images.values()) + scene_hits
    all_hits.sort(key=lambda h: h["score"], reverse=True)

    # Apply global offset+limit after merge so cross-index ranking is correct.
    all_hits = all_hits[offset : offset + limit]

    # Remove temp keys that are not part of the SearchHit model
    for hit in all_hits:
        hit.pop("_file_mtime", None)

    # Build typed hits
    typed_hits: list[SearchHit] = [
        SearchHit(**{k: v for k, v in hit.items() if k != "type"})
        for hit in all_hits
    ]

    return SearchResponse(
        query=q,
        hits=typed_hits,
        total=len(typed_hits),
        source=", ".join(source_parts) or "none",
    )


def _search_by_date(
    *,
    session: Session,
    library_id: str,
    dt_from: datetime | None,
    dt_to: datetime | None,
    path_prefix: str | None,
    tag: str | None,
    limit: int,
    offset: int,
) -> SearchResponse:
    """Direct DB query for date-only search (no text query)."""
    conditions = ["a.library_id = :library_id"]
    params: dict = {"library_id": library_id, "limit": limit, "offset": offset}

    if dt_from:
        conditions.append("COALESCE(a.taken_at, a.file_mtime) >= :dt_from")
        params["dt_from"] = dt_from
    if dt_to:
        conditions.append("COALESCE(a.taken_at, a.file_mtime) < :dt_to")
        params["dt_to"] = dt_to
    if path_prefix:
        conditions.append(
            "(a.rel_path = :path_prefix OR a.rel_path LIKE :path_prefix_like)"
        )
        params["path_prefix"] = path_prefix
        params["path_prefix_like"] = path_prefix + "/%"

    tag_join = ""
    if tag:
        conditions.append("m.tags @> jsonb_build_array(:tag)")
        params["tag"] = tag
        tag_join = """
            LEFT JOIN LATERAL (
                SELECT data->'tags' AS tags
                FROM asset_metadata
                WHERE asset_id = a.asset_id
                ORDER BY generated_at DESC
                LIMIT 1
            ) m ON TRUE
        """

    where_sql = " AND ".join(conditions)

    # Count total matching rows for accurate capped-results messaging
    count_params = {k: v for k, v in params.items() if k not in ("limit", "offset")}
    count_sql = text(
        f"SELECT COUNT(*) FROM active_assets a {tag_join} WHERE {where_sql}"
    ).bindparams(**count_params)
    total_count: int = session.execute(count_sql).scalar_one()

    sql = text(
        f"""
        SELECT a.asset_id, a.rel_path, a.media_type, a.file_size,
               a.width, a.height, a.thumbnail_key, a.proxy_key,
               a.camera_make, a.camera_model, a.taken_at
        FROM active_assets a
        {tag_join}
        WHERE {where_sql}
        ORDER BY COALESCE(a.taken_at, a.file_mtime) DESC NULLS LAST
        LIMIT :limit OFFSET :offset
    """
    ).bindparams(**params)

    rows = session.execute(sql).all()
    hits = [
        SearchHit(
            type="image",
            asset_id=row.asset_id,
            rel_path=row.rel_path,
            thumbnail_key=row.thumbnail_key,
            proxy_key=row.proxy_key,
            description="",
            tags=[],
            score=0.0,
            source="postgres_date",
            media_type=row.media_type,
            file_size=row.file_size,
            width=row.width,
            height=row.height,
            camera_make=row.camera_make,
            camera_model=row.camera_model,
            taken_at=row.taken_at.isoformat() if row.taken_at else None,
        )
        for row in rows
    ]
    return SearchResponse(
        query="", hits=hits, total=total_count, source="postgres_date"
    )
