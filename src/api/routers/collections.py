"""Collections API: CRUD, batch asset management, reorder. All routes require tenant auth."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import Session

from src.api.dependencies import get_tenant_session, require_editor
from src.repository.tenant import AssetRepository, CollectionRepository

router = APIRouter(prefix="/v1/collections", tags=["collections"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class CreateCollectionRequest(BaseModel):
    name: str
    description: str | None = None
    sort_order: str = "manual"
    asset_ids: list[str] | None = None  # optional atomic create+populate


class UpdateCollectionRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    is_public: bool | None = None
    sort_order: str | None = None
    cover_asset_id: str | None = None


class CollectionItem(BaseModel):
    collection_id: str
    name: str
    description: str | None
    cover_asset_id: str | None
    is_public: bool
    sort_order: str
    asset_count: int
    created_at: str
    updated_at: str


class CollectionListResponse(BaseModel):
    items: list[CollectionItem]


class AssetIdsRequest(BaseModel):
    asset_ids: list[str]


class BatchAddResponse(BaseModel):
    added: int


class BatchRemoveResponse(BaseModel):
    removed: int


class CollectionAssetItem(BaseModel):
    asset_id: str
    rel_path: str
    file_size: int
    media_type: str
    width: int | None = None
    height: int | None = None
    taken_at: str | None = None
    status: str = "pending"
    duration_sec: float | None = None
    camera_make: str | None = None
    camera_model: str | None = None


class CollectionAssetsResponse(BaseModel):
    items: list[CollectionAssetItem]
    next_cursor: str | None = None


class ReorderRequest(BaseModel):
    asset_ids: list[str]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_SORT_ORDERS = {"manual", "added_at", "taken_at"}


def _collection_to_item(col, repo: CollectionRepository) -> CollectionItem:
    return CollectionItem(
        collection_id=col.collection_id,
        name=col.name,
        description=col.description,
        cover_asset_id=repo.resolve_cover(col),
        is_public=col.is_public,
        sort_order=col.sort_order,
        asset_count=repo.asset_count(col.collection_id),
        created_at=col.created_at.isoformat(),
        updated_at=col.updated_at.isoformat(),
    )


def _get_collection_or_404(repo: CollectionRepository, collection_id: str):
    col = repo.get_by_id(collection_id)
    if col is None:
        raise HTTPException(status_code=404, detail="Collection not found")
    return col


# ---------------------------------------------------------------------------
# Collection CRUD
# ---------------------------------------------------------------------------


@router.post("", response_model=CollectionItem, status_code=201)
def create_collection(
    body: CreateCollectionRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_editor)],
) -> CollectionItem:
    """Create a collection. Optionally populate with asset_ids atomically."""
    if body.sort_order not in _VALID_SORT_ORDERS:
        raise HTTPException(status_code=400, detail=f"Invalid sort_order. Must be one of: {', '.join(_VALID_SORT_ORDERS)}")

    repo = CollectionRepository(session)
    col = repo.create(name=body.name, description=body.description, sort_order=body.sort_order)

    if body.asset_ids:
        # Validate all assets are active
        asset_repo = AssetRepository(session)
        for aid in body.asset_ids:
            asset = asset_repo.get_by_id(aid)
            if asset is None:
                raise HTTPException(status_code=404, detail=f"Asset {aid} not found or trashed")
        repo.add_assets(col.collection_id, body.asset_ids)

    return _collection_to_item(col, repo)


@router.get("", response_model=CollectionListResponse)
def list_collections(
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_editor)],
) -> CollectionListResponse:
    """List all collections with cover and asset count."""
    repo = CollectionRepository(session)
    collections = repo.list_all()
    return CollectionListResponse(
        items=[_collection_to_item(c, repo) for c in collections]
    )


@router.get("/{collection_id}", response_model=CollectionItem)
def get_collection(
    collection_id: str,
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_editor)],
) -> CollectionItem:
    """Get collection detail."""
    repo = CollectionRepository(session)
    col = _get_collection_or_404(repo, collection_id)
    return _collection_to_item(col, repo)


@router.patch("/{collection_id}", response_model=CollectionItem)
def update_collection(
    collection_id: str,
    body: UpdateCollectionRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_editor)],
) -> CollectionItem:
    """Update collection metadata."""
    repo = CollectionRepository(session)
    _get_collection_or_404(repo, collection_id)

    # Build kwargs, using sentinel for nullable fields that might be intentionally set to None
    from src.repository.tenant import _SENTINEL

    kwargs: dict = {}
    if body.name is not None:
        kwargs["name"] = body.name
    # description and cover_asset_id: only pass if included in request body
    raw = body.model_dump(exclude_unset=True)
    if "description" in raw:
        kwargs["description"] = body.description
    else:
        kwargs["description"] = _SENTINEL
    if body.is_public is not None:
        kwargs["is_public"] = body.is_public
    if body.sort_order is not None:
        if body.sort_order not in _VALID_SORT_ORDERS:
            raise HTTPException(status_code=400, detail=f"Invalid sort_order. Must be one of: {', '.join(_VALID_SORT_ORDERS)}")
        kwargs["sort_order"] = body.sort_order
    if "cover_asset_id" in raw:
        kwargs["cover_asset_id"] = body.cover_asset_id
    else:
        kwargs["cover_asset_id"] = _SENTINEL

    col = repo.update(collection_id, **kwargs)
    return _collection_to_item(col, repo)


@router.delete("/{collection_id}", status_code=204)
def delete_collection(
    collection_id: str,
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_editor)],
) -> None:
    """Delete a collection. Source assets are untouched."""
    repo = CollectionRepository(session)
    if not repo.delete(collection_id):
        raise HTTPException(status_code=404, detail="Collection not found")


# ---------------------------------------------------------------------------
# Collection assets
# ---------------------------------------------------------------------------


@router.post("/{collection_id}/assets", response_model=BatchAddResponse, status_code=200)
def add_assets_to_collection(
    collection_id: str,
    body: AssetIdsRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_editor)],
) -> BatchAddResponse:
    """Add assets to a collection. Idempotent — duplicates are ignored."""
    repo = CollectionRepository(session)
    _get_collection_or_404(repo, collection_id)

    # Reject trashed assets
    asset_repo = AssetRepository(session)
    for aid in body.asset_ids:
        asset = asset_repo.get_by_id(aid)
        if asset is None:
            raise HTTPException(status_code=404, detail=f"Asset {aid} not found or trashed")

    added = repo.add_assets(collection_id, body.asset_ids)
    return BatchAddResponse(added=added)


@router.delete("/{collection_id}/assets", response_model=BatchRemoveResponse)
def remove_assets_from_collection(
    collection_id: str,
    body: AssetIdsRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_editor)],
) -> BatchRemoveResponse:
    """Remove assets from a collection. Does not affect source assets."""
    repo = CollectionRepository(session)
    _get_collection_or_404(repo, collection_id)
    removed = repo.remove_assets(collection_id, body.asset_ids)
    return BatchRemoveResponse(removed=removed)


@router.get("/{collection_id}/assets", response_model=CollectionAssetsResponse)
def list_collection_assets(
    collection_id: str,
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_editor)],
    after: str | None = Query(None, description="Pagination cursor"),
    limit: int = Query(200, ge=1, le=1000),
) -> CollectionAssetsResponse:
    """List assets in a collection, paginated and ordered by collection sort_order."""
    repo = CollectionRepository(session)
    col = _get_collection_or_404(repo, collection_id)

    assets, next_cursor = repo.list_assets(
        collection_id, sort_order=col.sort_order, after_cursor=after, limit=limit
    )

    items = [
        CollectionAssetItem(
            asset_id=a.asset_id,
            rel_path=a.rel_path,
            file_size=a.file_size,
            media_type=a.media_type,
            width=a.width,
            height=a.height,
            taken_at=a.taken_at.isoformat() if a.taken_at else None,
            status=a.status,
            duration_sec=a.duration_sec,
            camera_make=a.camera_make,
            camera_model=a.camera_model,
        )
        for a in assets
    ]

    return CollectionAssetsResponse(items=items, next_cursor=next_cursor)


# ---------------------------------------------------------------------------
# Reorder
# ---------------------------------------------------------------------------


@router.patch("/{collection_id}/reorder", status_code=200)
def reorder_collection(
    collection_id: str,
    body: ReorderRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_editor)],
) -> dict:
    """Reorder assets in collection. Must include ALL active asset IDs."""
    repo = CollectionRepository(session)
    _get_collection_or_404(repo, collection_id)

    try:
        repo.reorder(collection_id, body.asset_ids)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {"ok": True}
