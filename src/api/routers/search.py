"""Search API: BM25 via Quickwit with Postgres fallback."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlmodel import Session

from src.api.dependencies import get_tenant_session
from src.core.config import get_settings
from src.repository.tenant import AssetRepository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/search", tags=["search"])


class SearchHit(BaseModel):
    asset_id: str
    rel_path: str
    thumbnail_key: str | None
    proxy_key: str | None
    camera_make: str | None = None
    camera_model: str | None = None
    description: str
    tags: list[str]
    score: float
    source: str  # "quickwit" or "postgres"


class SearchResponse(BaseModel):
    query: str
    hits: list[SearchHit]
    total: int
    source: str


class SceneSearchHit(BaseModel):
    scene_id: str
    asset_id: str
    rel_path: str
    start_ms: int
    end_ms: int
    rep_frame_ms: int
    thumbnail_key: str | None
    duration_sec: float | None
    description: str
    tags: list[str]
    score: float
    source: str  # "quickwit_scenes"


class SceneSearchResponse(BaseModel):
    query: str
    hits: list[SceneSearchHit]
    total: int
    source: str


@router.get("", response_model=SearchResponse)
def search(
    library_id: str,
    q: Annotated[str, Query(min_length=1, max_length=500)],
    session: Annotated[Session, Depends(get_tenant_session)],
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> SearchResponse:
    """
    Search assets in a library by natural language query.

    Tries Quickwit BM25 first. Falls back to Postgres ILIKE if:
    - quickwit_enabled=False, OR
    - Quickwit returns an error AND quickwit_fallback_to_postgres=True
    """
    settings = get_settings()
    hits: list[dict] = []
    source = "postgres"

    if settings.quickwit_enabled:
        try:
            from src.search.quickwit_client import QuickwitClient

            qw = QuickwitClient()
            hits = qw.search(
                library_id=library_id,
                query=q,
                max_hits=limit,
                start_offset=offset,
            )
            source = "quickwit"

            # Enrich hits with thumbnail_key/proxy_key from Postgres
            # (these are not stored in Quickwit)
            if hits:
                asset_repo = AssetRepository(session)
                asset_ids = [h["asset_id"] for h in hits]
                assets_by_id = {a.asset_id: a for a in asset_repo.get_by_ids(asset_ids)}
                for hit in hits:
                    asset = assets_by_id.get(hit["asset_id"])
                    if asset:
                        hit["thumbnail_key"] = asset.thumbnail_key
                        hit["proxy_key"] = asset.proxy_key
                        hit["camera_make"] = asset.camera_make
                        hit["camera_model"] = asset.camera_model

        except Exception as e:  # pragma: no cover - defensive logging
            logger.warning("Quickwit search failed, falling back to Postgres: %s", e)
            if not settings.quickwit_fallback_to_postgres:
                raise
            hits = []

    if not hits and (not settings.quickwit_enabled or settings.quickwit_fallback_to_postgres):
        from src.search.postgres_search import search_assets

        hits = search_assets(session, library_id, q, limit=limit, offset=offset)
        source = "postgres"

    # Deduplicate by asset_id, keeping highest score
    seen: dict[str, dict] = {}
    for hit in hits:
        asset_id = hit["asset_id"]
        if asset_id not in seen or hit["score"] > seen[asset_id]["score"]:
            seen[asset_id] = hit
    hits = list(seen.values())

    return SearchResponse(
        query=q,
        hits=[SearchHit(**h) for h in hits],
        total=len(hits),
        source=source,
    )


@router.get("/scenes", response_model=SceneSearchResponse)
def search_scenes(
    library_id: str,
    q: Annotated[str, Query(min_length=1, max_length=500)],
    session: Annotated[Session, Depends(get_tenant_session)],
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> SceneSearchResponse:
    """
    Search video scenes by natural language query.
    Returns scene-level hits with timestamp, thumbnail, and containing asset info.
    Quickwit only — no Postgres fallback.
    """
    settings = get_settings()

    if not settings.quickwit_enabled:
        return SceneSearchResponse(query=q, hits=[], total=0, source="disabled")

    try:
        from src.search.quickwit_client import QuickwitClient

        qw = QuickwitClient()
        hits = qw.search_scenes(
            library_id=library_id,
            query=q,
            max_hits=limit,
            start_offset=offset,
        )
    except Exception as e:
        logger.warning("Quickwit scene search failed: %s", e)
        raise

    # Enrich hits with live asset data from Postgres (rel_path, duration_sec may
    # have changed; thumbnail_key is already stored in Quickwit but verify it's current)
    if hits:
        asset_repo = AssetRepository(session)
        asset_ids = list({h["asset_id"] for h in hits})
        assets_by_id = {a.asset_id: a for a in asset_repo.get_by_ids(asset_ids)}
        for hit in hits:
            asset = assets_by_id.get(hit["asset_id"])
            if asset:
                hit["rel_path"] = asset.rel_path
                hit["duration_sec"] = asset.duration_sec or (
                    asset.duration_ms / 1000.0 if asset.duration_ms else None
                )
            # Ensure int fields and list for Pydantic (Quickwit may return None or other types)
            hit["start_ms"] = int(hit.get("start_ms") or 0)
            hit["end_ms"] = int(hit.get("end_ms") or 0)
            hit["rep_frame_ms"] = int(hit.get("rep_frame_ms") or 0)
            if hit.get("tags") is None:
                hit["tags"] = []

    return SceneSearchResponse(
        query=q,
        hits=[SceneSearchHit(**h) for h in hits],
        total=len(hits),
        source="quickwit_scenes",
    )

