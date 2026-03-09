"""Similarity search: find visually similar assets via pgvector."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import Session

from src.api.dependencies import get_tenant_session
from src.repository.tenant import AssetRepository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/similar", tags=["similarity"])


class SimilarHit(BaseModel):
    asset_id: str
    rel_path: str
    thumbnail_key: str | None
    proxy_key: str | None
    distance: float  # cosine distance, lower = more similar


class SimilarityResponse(BaseModel):
    source_asset_id: str
    hits: list[SimilarHit]
    total: int
    embedding_available: bool


@router.get("", response_model=SimilarityResponse)
def find_similar(
    asset_id: str,
    library_id: str,
    session: Annotated[Session, Depends(get_tenant_session)],
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> SimilarityResponse:
    """
    Find assets visually similar to the given asset_id.

    Returns empty hits with embedding_available=False if the source
    asset has no embedding vector yet (embedding generation is Step 11.1).

    Similarity is cosine distance on 512-dim embedding vectors.
    Results are ordered most-similar first (ascending distance).
    """
    asset_repo = AssetRepository(session)
    source = asset_repo.get_by_id(asset_id)

    if source is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    if source.library_id != library_id:
        raise HTTPException(status_code=404, detail="Asset not in library")

    embedding_available = source.embedding_vector is not None

    if not embedding_available:
        return SimilarityResponse(
            source_asset_id=asset_id,
            hits=[],
            total=0,
            embedding_available=False,
        )

    results = asset_repo.find_similar(
        asset_id=asset_id,
        library_id=library_id,
        limit=limit,
        offset=offset,
    )

    hits = [
        SimilarHit(
            asset_id=a.asset_id,
            rel_path=a.rel_path,
            thumbnail_key=a.thumbnail_key,
            proxy_key=a.proxy_key,
            distance=dist,
        )
        for a, dist in results
    ]

    return SimilarityResponse(
        source_asset_id=asset_id,
        hits=hits,
        total=len(hits),
        embedding_available=True,
    )

