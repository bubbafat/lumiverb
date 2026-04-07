"""Public collection endpoints. No auth required — resolved via public_collections control plane."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import Session

from src.server.api.dependencies import get_tenant_session
from src.server.repository.tenant import CollectionRepository

router = APIRouter(prefix="/v1/public/collections", tags=["public_collections"])


class PublicCollectionDetail(BaseModel):
    collection_id: str
    name: str
    description: str | None
    cover_asset_id: str | None
    asset_count: int


class PublicCollectionAssetItem(BaseModel):
    asset_id: str
    media_type: str
    width: int | None = None
    height: int | None = None
    taken_at: str | None = None
    duration_sec: float | None = None


class PublicCollectionAssetsResponse(BaseModel):
    items: list[PublicCollectionAssetItem]
    next_cursor: str | None = None


def _get_public_collection(session: Session, collection_id: str):
    """Get collection and verify it's actually public."""
    repo = CollectionRepository(session)
    col = repo.get_by_id(collection_id)
    if col is None or col.visibility != "public":
        raise HTTPException(status_code=404, detail="Collection not found")
    return col, repo


@router.get("/{collection_id}", response_model=PublicCollectionDetail)
def get_public_collection(
    collection_id: str,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> PublicCollectionDetail:
    """Get public collection metadata. No auth required."""
    col, repo = _get_public_collection(session, collection_id)
    return PublicCollectionDetail(
        collection_id=col.collection_id,
        name=col.name,
        description=col.description,
        cover_asset_id=repo.resolve_cover(col),
        asset_count=repo.asset_count(col.collection_id),
    )


@router.get("/{collection_id}/assets", response_model=PublicCollectionAssetsResponse)
def list_public_collection_assets(
    collection_id: str,
    session: Annotated[Session, Depends(get_tenant_session)],
    after: str | None = Query(None, description="Pagination cursor"),
    limit: int = Query(200, ge=1, le=1000),
) -> PublicCollectionAssetsResponse:
    """List assets in a public collection. No auth required. Privacy-stripped."""
    col, repo = _get_public_collection(session, collection_id)

    assets, next_cursor = repo.list_assets(
        collection_id, sort_order=col.sort_order, after_cursor=after, limit=limit
    )

    # Privacy: strip library paths, limit metadata
    items = [
        PublicCollectionAssetItem(
            asset_id=a.asset_id,
            media_type=a.media_type,
            width=a.width,
            height=a.height,
            taken_at=a.taken_at.isoformat() if a.taken_at else None,
            duration_sec=a.duration_sec,
        )
        for a in assets
    ]

    return PublicCollectionAssetsResponse(items=items, next_cursor=next_cursor)
