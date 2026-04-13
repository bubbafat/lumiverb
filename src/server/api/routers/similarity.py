"""Similarity search: find visually similar assets via pgvector."""

from __future__ import annotations

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlmodel import Session

from src.server.api.dependencies import get_tenant_session
from src.server.models.similarity import CameraSpec, DateRange, SimilarityScope
from src.server.repository.tenant import AssetEmbeddingRepository, AssetRepository, LibraryRepository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/similar", tags=["similarity"])


class SimilarHit(BaseModel):
    asset_id: str
    rel_path: str
    thumbnail_key: str | None
    proxy_key: str | None
    distance: float  # cosine distance, lower = more similar
    media_type: str | None = None
    file_size: int | None = None
    width: int | None = None
    height: int | None = None


class SimilarityResponse(BaseModel):
    source_asset_id: str
    hits: list[SimilarHit]
    total: int
    embedding_available: bool


class ImageSimilarityRequest(BaseModel):
    library_id: str
    image_b64: str  # base64-encoded JPEG/PNG, already resized by client
    model_id: str | None = None  # defaults to "clip"
    model_version: str | None = None  # defaults to server CLIP version
    limit: int = 20
    offset: int = 0
    # Scope filters — mirrors the GET endpoint's query params as a structured body
    from_ts: float | None = None
    to_ts: float | None = None
    asset_types: list[Literal["image", "video"]] | None = None
    cameras: list[CameraSpec] | None = None


class VectorSimilarityRequest(BaseModel):
    """Search by pre-computed embedding vector (for clients that embed locally).

    When `image_b64` is also supplied, the server runs face detection on
    the image, embeds each face with ArcFace, and fuses the per-face
    identity matches with the scene-vector cosine results via Reciprocal
    Rank Fusion. Without `image_b64` the endpoint behaves exactly as
    before — pure scene similarity. The optional bytes are *additive*,
    not a replacement, so legacy callers keep working unchanged.
    """
    library_id: str
    vector: list[float]
    model_id: str
    model_version: str
    limit: int = 20
    offset: int = 0
    from_ts: float | None = None
    to_ts: float | None = None
    asset_types: list[Literal["image", "video"]] | None = None
    cameras: list[CameraSpec] | None = None
    image_b64: str | None = None  # optional JPEG/PNG bytes for hybrid identity search


class ImageSimilarityResponse(BaseModel):
    hits: list[SimilarHit]
    total: int


@router.get("", response_model=SimilarityResponse)
def find_similar(
    asset_id: str,
    library_id: str,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10_000),
    model_id: str | None = Query(default=None, description="Embedding model ID. Defaults to 'clip'."),
    model_version: str | None = Query(default=None, description="Embedding model version. Defaults to server CLIP version."),
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

    asset_repo = AssetRepository(session)
    lib_repo = LibraryRepository(session)
    emb_repo = AssetEmbeddingRepository(session)

    if getattr(request.state, "is_public_request", False):
        lib = lib_repo.get_by_id(library_id)
        if lib is None or not lib.is_public:
            raise HTTPException(status_code=404, detail="Not found")

    source = asset_repo.get_by_id(asset_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    if source.library_id != library_id:
        raise HTTPException(status_code=404, detail="Asset not in library")

    # Fetch top-(limit*3) candidates for re-ranking pool
    K = min(limit * 3, 100)

    # Auto-detect embedding model from source asset when not specified
    if model_id and model_version:
        source_emb = emb_repo.get(asset_id, model_id, model_version)
    elif model_id:
        # model_id given but no version — find any embedding with that model
        source_emb = emb_repo.get_any(asset_id)
        if source_emb and source_emb.model_id != model_id:
            source_emb = None
    else:
        # No model specified — use whatever embedding the asset has
        source_emb = emb_repo.get_any(asset_id)

    if source_emb is not None:
        resolved_model_id = source_emb.model_id
        resolved_model_version = source_emb.model_version
    else:
        resolved_model_id = model_id or "clip"
        resolved_model_version = model_version or "unknown"

    clip_emb = source_emb

    if clip_emb is None:
        return SimilarityResponse(
            source_asset_id=asset_id,
            hits=[],
            total=0,
            embedding_available=False,
        )

    clip_candidates = emb_repo.find_similar(
        library_id=library_id,
        model_id=resolved_model_id,
        model_version=resolved_model_version,
        vector=[float(x) for x in clip_emb.embedding_vector],
        limit=K,
        exclude_asset_id=asset_id,
        scope=scope,
    )
    scores: dict[str, float] = {cand_id: dist for cand_id, dist in clip_candidates}

    # Person-based rerank: boost candidates that share a named person with source
    if scores:
        from sqlalchemy import text as _sa_text
        source_pids = set(
            session.execute(
                _sa_text("SELECT DISTINCT person_id FROM faces WHERE asset_id = :aid AND person_id IS NOT NULL"),
                {"aid": asset_id},
            ).scalars().all()
        )
        if source_pids:
            cand_aids = list(scores.keys())
            matching_aids = set(
                session.execute(
                    _sa_text("SELECT DISTINCT asset_id FROM faces WHERE asset_id = ANY(:aids) AND person_id = ANY(:pids)"),
                    {"aids": cand_aids, "pids": list(source_pids)},
                ).scalars().all()
            )
            for aid in matching_aids:
                if aid in scores:
                    scores[aid] *= 0.85  # lower distance = more similar

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
                media_type=asset.media_type,
                file_size=asset.file_size,
                width=asset.width,
                height=asset.height,
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
    from src.server.embeddings.clip_provider import CLIPEmbeddingProvider, MODEL_VERSION as CLIP_VERSION

    resolved_model_id = body.model_id or "clip"
    resolved_model_version = body.model_version or CLIP_VERSION

    # Validate timestamps
    if body.from_ts is not None and body.to_ts is not None and body.from_ts > body.to_ts:
        raise HTTPException(status_code=422, detail="from_ts must be <= to_ts")

    # Decode image and embed server-side (only works for models the server has)
    if resolved_model_id != "clip":
        raise HTTPException(
            status_code=422,
            detail=f"Server can only embed with model 'clip'. Use /search-by-vector for '{resolved_model_id}'.",
        )

    image_bytes = base64.b64decode(body.image_b64)
    pil_image = PILImage.open(io.BytesIO(image_bytes)).convert("RGB")
    try:
        provider = CLIPEmbeddingProvider()
        vector = provider.embed_image(pil_image)
    finally:
        pil_image.close()

    return _search_by_vector(
        session, body.library_id, vector, resolved_model_id, resolved_model_version,
        body.limit, body.offset, body.from_ts, body.to_ts, body.asset_types, body.cameras,
    )


@router.post("/search-by-vector", response_model=ImageSimilarityResponse)
def search_by_vector(
    body: VectorSimilarityRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> ImageSimilarityResponse:
    """Search by pre-computed embedding vector. For clients that embed
    locally (e.g. Apple Vision feature prints, on-device CLIP).

    When `image_b64` is also supplied, the server runs face detection
    on the image, embeds each face, and fuses the identity matches with
    the scene cosine results via RRF — turning a scene-only search into
    a hybrid that surfaces "looks like this place" *and* "contains this
    person" candidates in one ranked list.
    """
    if body.from_ts is not None and body.to_ts is not None and body.from_ts > body.to_ts:
        raise HTTPException(status_code=422, detail="from_ts must be <= to_ts")

    return _search_by_vector(
        session, body.library_id, body.vector, body.model_id, body.model_version,
        body.limit, body.offset, body.from_ts, body.to_ts, body.asset_types, body.cameras,
        image_b64=body.image_b64,
    )


# ---------------------------------------------------------------------------
# Reciprocal Rank Fusion
# ---------------------------------------------------------------------------

# RRF constant — Cormack et al. 2009 used k=60 in the original paper and
# it remains the standard default. Lower k weights top results more
# aggressively; higher k flattens the curve.
RRF_K = 60


def _rrf_fuse(
    *ranked_lists: list[tuple[str, float]],
    k: int = RRF_K,
) -> list[tuple[str, float]]:
    """Reciprocal Rank Fusion. Each input is an `(asset_id, distance)`
    list sorted by distance ascending. Returns a unified ranking sorted
    by RRF score descending — assets that show up high in *any* list
    rank well, assets that show up high in *multiple* lists dominate.

    Distances are intentionally ignored: RRF operates on ranks only,
    which dodges the calibration trap of comparing CLIP cosine to
    ArcFace cosine (different distributions, different magnitudes).
    The returned floats are RRF scores (not distances) — higher = more
    relevant. The caller must sort/slice on `score DESC`, not ASC.
    """
    rrf_scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, (asset_id, _dist) in enumerate(ranked):
            rrf_scores[asset_id] = rrf_scores.get(asset_id, 0.0) + 1.0 / (k + rank + 1)
    return sorted(rrf_scores.items(), key=lambda kv: kv[1], reverse=True)


def _search_by_vector(
    session: Session,
    library_id: str,
    vector: list[float],
    model_id: str,
    model_version: str,
    limit: int,
    offset: int,
    from_ts: float | None,
    to_ts: float | None,
    asset_types: list[str] | None,
    cameras: list[CameraSpec] | None,
    image_b64: str | None = None,
) -> ImageSimilarityResponse:
    scope: SimilarityScope | None = None
    date_range = (
        DateRange(from_ts=from_ts, to_ts=to_ts)
        if (from_ts is not None or to_ts is not None)
        else None
    )
    at = asset_types if asset_types else "all"
    if date_range is not None or cameras is not None or asset_types is not None:
        scope = SimilarityScope(
            date_range=date_range,
            asset_types=at,
            cameras=cameras,
        )

    emb_repo = AssetEmbeddingRepository(session)
    asset_repo = AssetRepository(session)

    # Pull a wider candidate set for the scene path when we're going
    # to fuse — RRF needs enough headroom on both lists for the
    # reciprocal-rank math to do its job. Without bytes the old
    # behavior is preserved exactly.
    scene_pool_size = max(limit * 5, 100) if image_b64 else limit
    scene_offset = 0 if image_b64 else offset

    scene_candidates = emb_repo.find_similar(
        library_id=library_id,
        model_id=model_id,
        model_version=model_version,
        vector=vector,
        limit=scene_pool_size,
        offset=scene_offset,
        exclude_asset_id=None,
        scope=scope,
    )

    # ── Identity path: only runs when the client uploaded image bytes ──
    # We could surface face errors to the caller, but the failure mode
    # we care about — InsightFace not installed, model download failing
    # mid-query, no faces detected — should all degrade gracefully to
    # scene-only ranking, not break the search outright.
    face_candidates: list[tuple[str, float]] = []
    if image_b64:
        try:
            import base64

            from src.server.faces.server_face_provider import detect_faces_in_image
            from src.server.repository.tenant import FaceRepository

            image_bytes = base64.b64decode(image_b64)
            detections = detect_faces_in_image(image_bytes)
            face_repo = FaceRepository(session)

            # Per-face vector search → aggregate per asset (best face
            # match wins, since FaceRepository.find_similar_by_vector
            # already collapses to one row per asset).
            per_asset_best: dict[str, float] = {}
            for det in detections:
                hits = face_repo.find_similar_by_vector(
                    library_id=library_id,
                    vector=det.embedding,
                    limit=scene_pool_size,
                )
                for aid, dist in hits:
                    if aid not in per_asset_best or dist < per_asset_best[aid]:
                        per_asset_best[aid] = dist

            face_candidates = sorted(per_asset_best.items(), key=lambda kv: kv[1])
            logger.info(
                "hybrid similarity: %d faces detected, %d face-candidate assets",
                len(detections), len(face_candidates),
            )
        except Exception as exc:
            logger.warning("hybrid similarity face path failed, falling back to scene-only: %s", exc)
            face_candidates = []

    # ── Fuse and slice ─────────────────────────────────────────────────
    if face_candidates:
        fused = _rrf_fuse(scene_candidates, face_candidates)
        # `fused` is (asset_id, rrf_score) — score is *higher = better*,
        # opposite of distance. Slice the requested page off the top.
        page = fused[offset : offset + limit]
        # Re-attach a representative distance for the response. We use
        # the scene cosine when present (most callers visualize the
        # field), falling back to the face distance otherwise. RRF
        # itself is not a distance and shouldn't be exposed as one.
        scene_dist = {aid: d for aid, d in scene_candidates}
        face_dist = {aid: d for aid, d in face_candidates}

        def _representative_distance(aid: str) -> float:
            if aid in scene_dist:
                return scene_dist[aid]
            return face_dist.get(aid, 1.0)

        ranked = [(aid, _representative_distance(aid)) for aid, _score in page]
    else:
        ranked = scene_candidates[: limit]  # already offset-applied above when no fusion

    asset_ids = [aid for aid, _ in ranked]
    assets_by_id = {a.asset_id: a for a in asset_repo.get_by_ids(asset_ids)}

    hits = [
        SimilarHit(
            asset_id=asset.asset_id,
            rel_path=asset.rel_path,
            thumbnail_key=asset.thumbnail_key,
            proxy_key=asset.proxy_key,
            distance=dist,
            media_type=asset.media_type,
            file_size=asset.file_size,
            width=asset.width,
            height=asset.height,
        )
        for cand_id, dist in ranked
        if (asset := assets_by_id.get(cand_id)) is not None
    ]

    return ImageSimilarityResponse(hits=hits, total=len(hits))

