"""Search API: BM25 via Quickwit with Postgres fallback."""

from __future__ import annotations

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
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
    q: str = Query(min_length=1, max_length=500),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=1000),
    media_type: MediaType = Query(default="all"),
    path_prefix: str | None = None,
    tag: str | None = None,
) -> SearchResponse:
    """
    Search assets and video scenes by natural language query.

    media_type:
      - "image": image assets only
      - "video": video scenes only
      - "all": both, merged and ranked by score (default)
    """
    settings = get_settings()
    image_hits: list[dict] = []
    scene_hits: list[dict] = []
    source_parts: list[str] = []

    search_images = media_type in ("image", "all")
    search_scenes = media_type in ("video", "all")

    # Fetch enough results from each index so that after merging and global
    # sorting we can correctly slice [offset:offset+limit]. Passing offset
    # directly to both indices independently would skip globally-relevant
    # results from whichever index doesn't have them at that rank.
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

        if not image_hits and (not settings.quickwit_enabled or settings.quickwit_fallback_to_postgres):
            from src.search.postgres_search import search_assets

            image_hits = search_assets(session, library_id, q, limit=fetch_limit, offset=0)
            source_parts.append("postgres")

        # Enrich image hits. Phase 0 finding: Quickwit returns hits without PG cross-check;
        # get_by_ids uses active_assets so trashed assets are omitted — drop hits not in assets_by_id.
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

        # Enrich scene hits; drop trashed (scene hit's asset not in active_assets).
        if scene_hits:
            asset_repo = AssetRepository(session)
            assets_by_id = {
                a.asset_id: a
                for a in asset_repo.get_by_ids(list({h["asset_id"] for h in scene_hits}))
            }
            for hit in scene_hits:
                asset = assets_by_id.get(hit["asset_id"])
                if asset:
                    hit["rel_path"] = asset.rel_path
                    hit["proxy_key"] = asset.proxy_key
                    hit["duration_sec"] = asset.duration_sec or (
                        asset.duration_ms / 1000.0 if asset.duration_ms else None
                    )
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

    # Build typed hits
    typed_hits: list[SearchHit] = [
        SearchHit(**{k: v for k, v in hit.items() if k != "type"}) for hit in all_hits
    ]

    return SearchResponse(
        query=q,
        hits=typed_hits,
        total=len(typed_hits),
        source=", ".join(source_parts) or "none",
    )

