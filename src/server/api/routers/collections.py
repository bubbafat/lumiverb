"""Collections API: CRUD, batch asset management, reorder. All routes require tenant auth."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlmodel import Session

from src.server.api.dependencies import get_current_user_id, get_tenant_session, require_editor
from src.server.database import get_control_session
from src.server.repository.control_plane import PublicCollectionRepository
from src.server.repository.tenant import AssetRepository, CollectionRepository

router = APIRouter(prefix="/v1/collections", tags=["collections"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


_VALID_TYPES = {"static", "smart"}


class CreateCollectionRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    sort_order: str = "manual"
    visibility: str = "private"  # private | shared | public
    type: str = "static"  # static | smart
    saved_query: dict | None = None
    asset_ids: list[str] | None = Field(default=None, max_length=10_000)


class UpdateCollectionRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    visibility: str | None = None
    sort_order: str | None = None
    cover_asset_id: str | None = None
    saved_query: dict | None = None


class CollectionItem(BaseModel):
    collection_id: str
    name: str
    description: str | None
    cover_asset_id: str | None
    owner_user_id: str | None
    visibility: str
    ownership: str  # "own" | "shared"
    sort_order: str
    type: str = "static"
    saved_query: dict | None = None
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
_VALID_VISIBILITIES = {"private", "shared", "public"}


def _ownership_label(col, user_id: str) -> str:
    if col.owner_user_id == user_id:
        return "own"
    return "shared"


def _collection_to_item(
    col, repo: CollectionRepository, user_id: str, *, session: Session | None = None
) -> CollectionItem:
    col_type = getattr(col, "type", "static") or "static"

    # Smart collections compute asset_count via live query
    if col_type == "smart" and col.saved_query and session is not None:
        from src.server.models.browse_filters import BrowseFilters
        from src.server.repository.tenant import UnifiedBrowseRepository

        saved = col.saved_query
        filters_data = saved.get("filters", {})
        if "library_id" in saved:
            filters_data["library_ids"] = [saved["library_id"]]
        filters = BrowseFilters.from_json(filters_data)
        browse_repo = UnifiedBrowseRepository(session)
        # Use a large limit to get the count (no COUNT query on the browse repo yet)
        live_assets = browse_repo.page(
            filters=filters,
            rating_user_id=user_id if filters.needs_rating_join else None,
            limit=10000,
        )
        count = len(live_assets)
    else:
        count = repo.asset_count(col.collection_id)

    return CollectionItem(
        collection_id=col.collection_id,
        name=col.name,
        description=col.description,
        cover_asset_id=repo.resolve_cover(col),
        owner_user_id=col.owner_user_id,
        visibility=col.visibility,
        ownership=_ownership_label(col, user_id),
        sort_order=col.sort_order,
        type=col_type,
        saved_query=getattr(col, "saved_query", None),
        asset_count=count,
        created_at=col.created_at.isoformat(),
        updated_at=col.updated_at.isoformat(),
    )


def _get_collection_or_404(repo: CollectionRepository, collection_id: str):
    col = repo.get_by_id(collection_id)
    if col is None:
        raise HTTPException(status_code=404, detail="Collection not found")
    return col


def _require_owner(col, user_id: str) -> None:
    """Raise 403 if user is not the owner of the collection."""
    if col.owner_user_id is not None and col.owner_user_id != user_id:
        raise HTTPException(status_code=403, detail="Only the collection owner can perform this action")


def _can_view(col, user_id: str) -> bool:
    """Check if user can view this collection."""
    if col.owner_user_id is None:
        return True  # legacy tenant-wide collection
    if col.owner_user_id == user_id:
        return True
    return col.visibility in ("shared", "public")


def _require_static(col) -> None:
    """Raise 400 if collection is smart (dynamic)."""
    if getattr(col, "type", "static") == "smart":
        raise HTTPException(
            status_code=400,
            detail="Smart collections do not support manual asset management",
        )


# ---------------------------------------------------------------------------
# Collection CRUD
# ---------------------------------------------------------------------------


@router.post("", response_model=CollectionItem, status_code=201)
def create_collection(
    body: CreateCollectionRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_editor)],
    user_id: Annotated[str, Depends(get_current_user_id)],
) -> CollectionItem:
    """Create a collection owned by the current user."""
    if body.sort_order not in _VALID_SORT_ORDERS:
        raise HTTPException(status_code=400, detail=f"Invalid sort_order. Must be one of: {', '.join(_VALID_SORT_ORDERS)}")
    if body.visibility not in _VALID_VISIBILITIES:
        raise HTTPException(status_code=400, detail=f"Invalid visibility. Must be one of: {', '.join(_VALID_VISIBILITIES)}")
    if body.type not in _VALID_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid type. Must be one of: {', '.join(_VALID_TYPES)}")
    if body.type == "smart" and not body.saved_query:
        raise HTTPException(status_code=400, detail="Smart collections require a saved_query")
    if body.type == "static" and body.saved_query is not None:
        raise HTTPException(status_code=400, detail="Static collections must not have a saved_query")

    repo = CollectionRepository(session)
    col = repo.create(
        name=body.name,
        owner_user_id=user_id,
        description=body.description,
        sort_order=body.sort_order,
        visibility=body.visibility,
        type=body.type,
        saved_query=body.saved_query,
    )

    if body.asset_ids:
        asset_repo = AssetRepository(session)
        for aid in body.asset_ids:
            asset = asset_repo.get_by_id(aid)
            if asset is None:
                raise HTTPException(status_code=404, detail=f"Asset {aid} not found or trashed")
        repo.add_assets(col.collection_id, body.asset_ids)

    return _collection_to_item(col, repo, user_id, session=session)


@router.get("", response_model=CollectionListResponse)
def list_collections(
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_editor)],
    user_id: Annotated[str, Depends(get_current_user_id)],
) -> CollectionListResponse:
    """List collections owned by user + shared collections."""
    repo = CollectionRepository(session)
    collections = repo.list_for_user(user_id)
    return CollectionListResponse(
        items=[_collection_to_item(c, repo, user_id, session=session) for c in collections]
    )


@router.get("/{collection_id}", response_model=CollectionItem)
def get_collection(
    collection_id: str,
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_editor)],
    user_id: Annotated[str, Depends(get_current_user_id)],
) -> CollectionItem:
    """Get collection detail. Must be owner or collection must be shared."""
    repo = CollectionRepository(session)
    col = _get_collection_or_404(repo, collection_id)
    if not _can_view(col, user_id):
        raise HTTPException(status_code=404, detail="Collection not found")
    return _collection_to_item(col, repo, user_id, session=session)


@router.patch("/{collection_id}", response_model=CollectionItem)
def update_collection(
    collection_id: str,
    body: UpdateCollectionRequest,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_editor)],
    user_id: Annotated[str, Depends(get_current_user_id)],
) -> CollectionItem:
    """Update collection metadata. Only the owner can update."""
    repo = CollectionRepository(session)
    col = _get_collection_or_404(repo, collection_id)
    _require_owner(col, user_id)

    old_visibility = col.visibility

    from src.server.repository.tenant import _SENTINEL

    kwargs: dict = {}
    if body.name is not None:
        kwargs["name"] = body.name
    raw = body.model_dump(exclude_unset=True)
    if "description" in raw:
        kwargs["description"] = body.description
    else:
        kwargs["description"] = _SENTINEL
    if body.visibility is not None:
        if body.visibility not in _VALID_VISIBILITIES:
            raise HTTPException(status_code=400, detail=f"Invalid visibility. Must be one of: {', '.join(_VALID_VISIBILITIES)}")
        kwargs["visibility"] = body.visibility
    if body.sort_order is not None:
        if body.sort_order not in _VALID_SORT_ORDERS:
            raise HTTPException(status_code=400, detail=f"Invalid sort_order. Must be one of: {', '.join(_VALID_SORT_ORDERS)}")
        kwargs["sort_order"] = body.sort_order
    if "cover_asset_id" in raw:
        kwargs["cover_asset_id"] = body.cover_asset_id
    else:
        kwargs["cover_asset_id"] = _SENTINEL
    if "saved_query" in raw:
        kwargs["saved_query"] = body.saved_query

    col = repo.update(collection_id, **kwargs)

    # Maintain public_collections control plane index
    if body.visibility is not None and body.visibility != old_visibility:
        tenant_id = getattr(request.state, "tenant_id", None)
        connection_string = getattr(request.state, "connection_string", None)
        if tenant_id and connection_string:
            with get_control_session() as ctrl_session:
                pub_repo = PublicCollectionRepository(ctrl_session)
                if col.visibility == "public":
                    pub_repo.upsert(collection_id, tenant_id, connection_string)
                else:
                    pub_repo.delete(collection_id)

    return _collection_to_item(col, repo, user_id, session=session)


@router.delete("/{collection_id}", status_code=204)
def delete_collection(
    collection_id: str,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_editor)],
    user_id: Annotated[str, Depends(get_current_user_id)],
) -> None:
    """Delete a collection. Only the owner can delete."""
    repo = CollectionRepository(session)
    col = _get_collection_or_404(repo, collection_id)
    _require_owner(col, user_id)
    was_public = col.visibility == "public"
    repo.delete(collection_id)

    if was_public:
        with get_control_session() as ctrl_session:
            PublicCollectionRepository(ctrl_session).delete(collection_id)


# ---------------------------------------------------------------------------
# Collection assets
# ---------------------------------------------------------------------------


@router.post("/{collection_id}/assets", response_model=BatchAddResponse, status_code=200)
def add_assets_to_collection(
    collection_id: str,
    body: AssetIdsRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_editor)],
    user_id: Annotated[str, Depends(get_current_user_id)],
) -> BatchAddResponse:
    """Add assets to a collection. Only the owner can add. Idempotent."""
    repo = CollectionRepository(session)
    col = _get_collection_or_404(repo, collection_id)
    _require_owner(col, user_id)
    _require_static(col)

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
    user_id: Annotated[str, Depends(get_current_user_id)],
) -> BatchRemoveResponse:
    """Remove assets from a collection. Only the owner can remove."""
    repo = CollectionRepository(session)
    col = _get_collection_or_404(repo, collection_id)
    _require_owner(col, user_id)
    _require_static(col)
    removed = repo.remove_assets(collection_id, body.asset_ids)
    return BatchRemoveResponse(removed=removed)


@router.get("/{collection_id}/assets", response_model=CollectionAssetsResponse)
def list_collection_assets(
    collection_id: str,
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_editor)],
    user_id: Annotated[str, Depends(get_current_user_id)],
    after: str | None = Query(None, description="Pagination cursor"),
    limit: int = Query(200, ge=1, le=1000),
) -> CollectionAssetsResponse:
    """List assets in a collection. Must be owner or collection must be shared.

    For smart collections, executes the saved query to return live results.
    """
    repo = CollectionRepository(session)
    col = _get_collection_or_404(repo, collection_id)
    if not _can_view(col, user_id):
        raise HTTPException(status_code=404, detail="Collection not found")

    if getattr(col, "type", "static") == "smart" and col.saved_query:
        # Smart collection: execute saved query via UnifiedBrowseRepository
        from src.server.models.browse_filters import BrowseFilters
        from src.server.repository.tenant import UnifiedBrowseRepository

        saved = col.saved_query
        filters_data = saved.get("filters", {})
        # Merge top-level scope fields into filters
        if "library_id" in saved:
            filters_data["library_ids"] = [saved["library_id"]]
        filters = BrowseFilters.from_json(filters_data)

        browse_repo = UnifiedBrowseRepository(session)
        assets = browse_repo.page(
            filters=filters,
            rating_user_id=user_id if filters.needs_rating_join else None,
            after=after,
            limit=limit,
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
        return CollectionAssetsResponse(items=items, next_cursor=None)

    # Static collection: list manual assets
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
    user_id: Annotated[str, Depends(get_current_user_id)],
) -> dict:
    """Reorder assets in collection. Only the owner can reorder."""
    repo = CollectionRepository(session)
    col = _get_collection_or_404(repo, collection_id)
    _require_owner(col, user_id)
    _require_static(col)

    try:
        repo.reorder(collection_id, body.asset_ids)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {"ok": True}
