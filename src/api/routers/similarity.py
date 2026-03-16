"""Similarity search: find visually similar assets via pgvector."""

from __future__ import annotations

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import Session

from src.api.dependencies import get_tenant_session
from src.models.similarity import CameraSpec, DateRange, SimilarityScope
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


class ImageSimilarityRequest(BaseModel):
    library_id: str
    image_b64: str  # base64-encoded JPEG/PNG, already resized by client
    limit: int = 20
    offset: int = 0
    # Scope filters — mirrors the GET endpoint's query params as a structured body
    from_ts: float | None = None
    to_ts: float | None = None
    asset_types: list[Literal["image", "video"]] | None = None
    cameras: list[CameraSpec] | None = None


class ImageSimilarityResponse(BaseModel):
    hits: list[SimilarHit]
    total: int


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
    camera_make: list[str] | None = Query(
        default=None,
        description="Camera make(s); pair with camera_model by index. OR across pairs.",
    ),
    camera_model: list[str] | None = Query(
        default=None,
        description="Camera model(s); pair with camera_make by index. OR across pairs.",
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

    # Build list of (make, model) pairs from repeated params; OR across pairs
    cameras_list: list[CameraSpec] | None = None
    if camera_make is not None or camera_model is not None:
        makes = camera_make or []
        models = camera_model or []
        if makes or models:
            n = max(len(makes), len(models))
            cameras_list = [
                CameraSpec(
                    make=makes[i] if i < len(makes) else None,
                    model=models[i] if i < len(models) else None,
                )
                for i in range(n)
            ]
            # Drop pairs that are both None
            cameras_list = [c for c in cameras_list if c.make is not None or c.model is not None]
            if not cameras_list:
                cameras_list = None

    scope: SimilarityScope | None = None
    if from_ts is not None or to_ts is not None or asset_types_list is not None or cameras_list is not None:
        scope = SimilarityScope(
            date_range=DateRange(from_ts=from_ts, to_ts=to_ts) if (from_ts is not None or to_ts is not None) else None,
            asset_types=asset_types_list if asset_types_list is not None else "all",
            cameras=cameras_list,
        )

    from src.workers.embeddings.clip_provider import MODEL_VERSION as CLIP_VERSION

    asset_repo = AssetRepository(session)
    lib_repo = LibraryRepository(session)
    emb_repo = AssetEmbeddingRepository(session)

    source = asset_repo.get_by_id(asset_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    if source.library_id != library_id:
        raise HTTPException(status_code=404, detail="Asset not in library")

    # Fetch top-(limit*3) candidates for re-ranking pool
    K = min(limit * 3, 100)

    clip_emb = emb_repo.get(asset_id, "clip", CLIP_VERSION)

    if clip_emb is None:
        return SimilarityResponse(
            source_asset_id=asset_id,
            hits=[],
            total=0,
            embedding_available=False,
        )

    clip_candidates = emb_repo.find_similar(
        library_id=library_id,
        model_id="clip",
        model_version=CLIP_VERSION,
        vector=[float(x) for x in clip_emb.embedding_vector],
        limit=K,
        exclude_asset_id=asset_id,
        scope=scope,
    )
    scores: dict[str, float] = {cand_id: dist for cand_id, dist in clip_candidates}

    # Sort by score ascending (lower = more similar), apply offset/limit
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


@router.post("/search-by-image", response_model=ImageSimilarityResponse)
def search_by_image(
    body: ImageSimilarityRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> ImageSimilarityResponse:
    import base64
    import io
    from PIL import Image as PILImage
    from src.workers.embeddings.clip_provider import CLIPEmbeddingProvider, MODEL_VERSION as CLIP_VERSION

    # Validate timestamps
    if body.from_ts is not None and body.to_ts is not None and body.from_ts > body.to_ts:
        raise HTTPException(status_code=422, detail="from_ts must be <= to_ts")

    # Decode image
    image_bytes = base64.b64decode(body.image_b64)
    pil_image = PILImage.open(io.BytesIO(image_bytes)).convert("RGB")

    # Generate CLIP embedding in memory
    provider = CLIPEmbeddingProvider()
    vector = provider.embed_image(pil_image)

    # Build scope (same logic as GET endpoint)
    scope: SimilarityScope | None = None
    date_range = (
        DateRange(from_ts=body.from_ts, to_ts=body.to_ts)
        if (body.from_ts is not None or body.to_ts is not None)
        else None
    )
    asset_types = body.asset_types if body.asset_types else "all"
    if date_range is not None or body.cameras is not None or body.asset_types is not None:
        scope = SimilarityScope(
            date_range=date_range,
            asset_types=asset_types,
            cameras=body.cameras,
        )

    # Query
    emb_repo = AssetEmbeddingRepository(session)
    asset_repo = AssetRepository(session)

    candidates = emb_repo.find_similar(
        library_id=body.library_id,
        model_id="clip",
        model_version=CLIP_VERSION,
        vector=vector,
        limit=body.limit,
        offset=body.offset,
        exclude_asset_id=None,  # no source asset to exclude
        scope=scope,
    )

    asset_ids = [aid for aid, _ in candidates]
    assets_by_id = {a.asset_id: a for a in asset_repo.get_by_ids(asset_ids)}

    hits = [
        SimilarHit(
            asset_id=asset.asset_id,
            rel_path=asset.rel_path,
            thumbnail_key=asset.thumbnail_key,
            proxy_key=asset.proxy_key,
            distance=dist,
        )
        for cand_id, dist in candidates
        if (asset := assets_by_id.get(cand_id)) is not None
    ]

    return ImageSimilarityResponse(hits=hits, total=len(hits))

