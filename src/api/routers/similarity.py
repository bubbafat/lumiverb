"""Similarity search: find visually similar assets via pgvector."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import Session

from src.api.dependencies import get_tenant_session
from src.models.similarity import DateRange, SimilarityScope
from src.repository.tenant import AssetEmbeddingRepository, AssetRepository, LibraryRepository

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
    from_ts: float | None = Query(default=None, description="Unix timestamp (seconds), inclusive start of capture-time range."),
    to_ts: float | None = Query(default=None, description="Unix timestamp (seconds), inclusive end of capture-time range."),
    asset_types: str | None = Query(
        default=None,
        description="Comma-separated: image, video. Restrict results to these types (by media_type prefix).",
    ),
    ) -> SimilarityResponse:
    if from_ts is not None and to_ts is not None and from_ts > to_ts:
        raise HTTPException(
            status_code=422,
            detail="from_ts must be less than or equal to to_ts",
        )
    allowed = {"image", "video"}
    asset_types_list: list[str] | None = None
    if asset_types is not None and asset_types.strip():
        parsed = [s.strip().lower() for s in asset_types.split(",") if s.strip()]
        asset_types_list = [t for t in parsed if t in allowed]
        if not asset_types_list:
            asset_types_list = None

    scope: SimilarityScope | None = None
    if from_ts is not None or to_ts is not None or asset_types_list is not None:
        scope = SimilarityScope(
            date_range=DateRange(from_ts=from_ts, to_ts=to_ts) if (from_ts is not None or to_ts is not None) else None,
            asset_types=asset_types_list if asset_types_list is not None else "all",
        )

    from src.models.registry import get_embedding_config
    from src.workers.embeddings.clip_provider import MODEL_VERSION as CLIP_VERSION
    from src.workers.embeddings.moondream_provider import MODEL_VERSION as MD_VERSION

    asset_repo = AssetRepository(session)
    lib_repo = LibraryRepository(session)
    emb_repo = AssetEmbeddingRepository(session)

    source = asset_repo.get_by_id(asset_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    if source.library_id != library_id:
        raise HTTPException(status_code=404, detail="Asset not in library")

    library = lib_repo.get_by_id(library_id)
    vision_model_id = library.vision_model_id if library else "moondream"
    config = get_embedding_config(vision_model_id)

    moondream_weight = config.moondream_weight
    clip_weight = config.clip_weight

    # Fetch top-(limit*3) candidates from each model for re-ranking pool
    K = min(limit * 3, 100)

    # CLIP candidates
    clip_emb = emb_repo.get(asset_id, "clip", CLIP_VERSION)

    # Moondream candidates (only if moondream weight > 0)
    md_emb = emb_repo.get(asset_id, "moondream", MD_VERSION) if moondream_weight > 0 else None

    if clip_emb is None and md_emb is None:
        return SimilarityResponse(
            source_asset_id=asset_id,
            hits=[],
            total=0,
            embedding_available=False,
        )

    # Collect candidates from each available model
    scores: dict[str, float] = {}  # asset_id -> weighted score

    if clip_emb is not None:
        clip_candidates = emb_repo.find_similar(
            library_id=library_id,
            model_id="clip",
            model_version=CLIP_VERSION,
            vector=[float(x) for x in clip_emb.embedding_vector],
            exclude_asset_id=asset_id,
            limit=K,
            scope=scope,
        )
        for cand_id, dist in clip_candidates:
            scores[cand_id] = scores.get(cand_id, 0.0) + clip_weight * dist

    if md_emb is not None:
        md_candidates = emb_repo.find_similar(
            library_id=library_id,
            model_id="moondream",
            model_version=MD_VERSION,
            vector=[float(x) for x in md_emb.embedding_vector],
            exclude_asset_id=asset_id,
            limit=K,
            scope=scope,
        )
        for cand_id, dist in md_candidates:
            scores[cand_id] = scores.get(cand_id, 0.0) + moondream_weight * dist

    # Sort by weighted score ascending (lower = more similar), apply offset/limit
    ranked = sorted(scores.items(), key=lambda x: x[1])[offset : offset + limit]

    if not ranked:
        return SimilarityResponse(
            source_asset_id=asset_id, hits=[], total=0, embedding_available=True
        )

    asset_ids = [aid for aid, _ in ranked]
    assets_by_id = {a.asset_id: a for a in asset_repo.get_by_ids(asset_ids)}

    hits: list[SimilarHit] = []
    for cand_id, score in ranked:
        asset = assets_by_id.get(cand_id)
        if asset is None:
            continue
        hits.append(
            SimilarHit(
                asset_id=asset.asset_id,
                rel_path=asset.rel_path,
                thumbnail_key=asset.thumbnail_key,
                proxy_key=asset.proxy_key,
                distance=score,
            )
        )

    return SimilarityResponse(
        source_asset_id=asset_id,
        hits=hits,
        total=len(hits),
        embedding_available=True,
    )

