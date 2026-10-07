"""Projects API: CRUD, batch asset management, reorder. All routes require tenant auth."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, model_validator, Field
from sqlmodel import Session

from src.server.api.dependencies import get_current_user_id, get_tenant_session, require_editor
from src.server.database import get_control_session
from src.server.repository.control_plane import PublicProjectRepository
from src.server.repository.tenant import AssetRepository, ProjectRepository

# Mounted at /v1/projects, and at /v1/collections (deprecated) until the
# macOS/iOS apps move to the new path. See main.py.
router = APIRouter()


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


_VALID_TYPES = {"static", "smart"}


class CreateProjectRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    sort_order: str = "manual"
    visibility: str = "private"  # private | shared | public
    type: str = "static"  # static | smart
    saved_query: dict | None = None
    asset_ids: list[str] | None = Field(default=None, max_length=10_000)


class UpdateProjectRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    visibility: str | None = None
    sort_order: str | None = None
    cover_asset_id: str | None = None
    saved_query: dict | None = None
    # Lifecycle: archived projects leave the default list (sidebar, pickers)
    # but keep their clips and stay readable and exportable.
    status: Literal["active", "archived"] | None = None


class ProjectItem(BaseModel):
    project_id: str
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
    status: str = "active"  # active | archived
    archived_at: str | None = None
    # Pre-rename name of project_id, for macOS/iOS builds that still read it.
    collection_id: str | None = None

    @model_validator(mode="after")
    def _legacy_collection_id(self):
        self.collection_id = self.project_id
        return self


class ProjectListResponse(BaseModel):
    items: list[ProjectItem]


class AssetIdsRequest(BaseModel):
    asset_ids: list[str]


class BatchAddResponse(BaseModel):
    added: int


class BatchRemoveResponse(BaseModel):
    removed: int


class ProjectAssetItem(BaseModel):
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


class ProjectAssetsResponse(BaseModel):
    items: list[ProjectAssetItem]
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


def _project_to_item(
    col, repo: ProjectRepository, user_id: str, *, session: Session | None = None
) -> ProjectItem:
    col_type = getattr(col, "type", "static") or "static"

    # Smart projects compute asset_count via live query
    if col_type == "smart" and col.saved_query and session is not None:
        from src.server.models.filter_registry import from_json
        from src.server.repository.tenant import UnifiedBrowseRepository

        spec = from_json(col.saved_query)
        browse_repo = UnifiedBrowseRepository(session)
        live_assets = browse_repo.query_page(
            spec=spec,
            rating_user_id=user_id if spec.needs_rating_join else None,
            limit=10000,
        )
        count = len(live_assets)
    else:
        count = repo.asset_count(col.project_id)

    return ProjectItem(
        project_id=col.project_id,
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
        status=col.status,
        archived_at=col.archived_at.isoformat() if col.archived_at else None,
    )


def _get_project_or_404(repo: ProjectRepository, project_id: str):
    col = repo.get_by_id(project_id)
    if col is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return col


def _require_owner(col, user_id: str) -> None:
    """Raise 403 if user is not the owner of the project."""
    if col.owner_user_id is not None and col.owner_user_id != user_id:
        raise HTTPException(status_code=403, detail="Only the project owner can perform this action")


def _can_view(col, user_id: str) -> bool:
    """Check if user can view this project."""
    if col.owner_user_id is None:
        return True  # legacy tenant-wide project
    if col.owner_user_id == user_id:
        return True
    return col.visibility in ("shared", "public")


def _require_static(col) -> None:
    """Raise 400 if project is smart (dynamic)."""
    if getattr(col, "type", "static") == "smart":
        raise HTTPException(
            status_code=400,
            detail="Smart projects do not support manual asset management",
        )


# ---------------------------------------------------------------------------
# Project CRUD
# ---------------------------------------------------------------------------


@router.post("", response_model=ProjectItem, status_code=201)
def create_project(
    body: CreateProjectRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_editor)],
    user_id: Annotated[str, Depends(get_current_user_id)],
) -> ProjectItem:
    """Create a project owned by the current user."""
    if body.sort_order not in _VALID_SORT_ORDERS:
        raise HTTPException(status_code=400, detail=f"Invalid sort_order. Must be one of: {', '.join(_VALID_SORT_ORDERS)}")
    if body.visibility not in _VALID_VISIBILITIES:
        raise HTTPException(status_code=400, detail=f"Invalid visibility. Must be one of: {', '.join(_VALID_VISIBILITIES)}")
    if body.type not in _VALID_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid type. Must be one of: {', '.join(_VALID_TYPES)}")
    if body.type == "smart" and not body.saved_query:
        raise HTTPException(status_code=400, detail="Smart projects require a saved_query")
    if body.type == "static" and body.saved_query is not None:
        raise HTTPException(status_code=400, detail="Static projects must not have a saved_query")

    repo = ProjectRepository(session)
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
        repo.add_assets(col.project_id, body.asset_ids)

    return _project_to_item(col, repo, user_id, session=session)


@router.get("", response_model=ProjectListResponse)
def list_projects(
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_editor)],
    user_id: Annotated[str, Depends(get_current_user_id)],
    status: Literal["active", "archived", "all"] = "active",
) -> ProjectListResponse:
    """List projects owned by user + shared projects. Active only unless status says otherwise."""
    repo = ProjectRepository(session)
    statuses = ("active", "archived") if status == "all" else (status,)
    projects = repo.list_for_user(user_id, statuses=statuses)
    return ProjectListResponse(
        items=[_project_to_item(c, repo, user_id, session=session) for c in projects]
    )


@router.get("/{project_id}", response_model=ProjectItem)
def get_project(
    project_id: str,
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_editor)],
    user_id: Annotated[str, Depends(get_current_user_id)],
) -> ProjectItem:
    """Get project detail. Must be owner or project must be shared."""
    repo = ProjectRepository(session)
    col = _get_project_or_404(repo, project_id)
    if not _can_view(col, user_id):
        raise HTTPException(status_code=404, detail="Project not found")
    return _project_to_item(col, repo, user_id, session=session)


@router.patch("/{project_id}", response_model=ProjectItem)
def update_project(
    project_id: str,
    body: UpdateProjectRequest,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_editor)],
    user_id: Annotated[str, Depends(get_current_user_id)],
) -> ProjectItem:
    """Update project metadata. Only the owner can update."""
    repo = ProjectRepository(session)
    col = _get_project_or_404(repo, project_id)
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
    if body.status is not None:
        kwargs["status"] = body.status

    col = repo.update(project_id, **kwargs)

    # Maintain public_projects control plane index
    if body.visibility is not None and body.visibility != old_visibility:
        tenant_id = getattr(request.state, "tenant_id", None)
        connection_string = getattr(request.state, "connection_string", None)
        if tenant_id and connection_string:
            with get_control_session() as ctrl_session:
                pub_repo = PublicProjectRepository(ctrl_session)
                if col.visibility == "public":
                    pub_repo.upsert(project_id, tenant_id, connection_string)
                else:
                    pub_repo.delete(project_id)

    return _project_to_item(col, repo, user_id, session=session)


@router.delete("/{project_id}", status_code=204)
def delete_project(
    project_id: str,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_editor)],
    user_id: Annotated[str, Depends(get_current_user_id)],
) -> None:
    """Delete a project. Only the owner can delete."""
    repo = ProjectRepository(session)
    col = _get_project_or_404(repo, project_id)
    _require_owner(col, user_id)
    was_public = col.visibility == "public"
    repo.delete(project_id)

    if was_public:
        with get_control_session() as ctrl_session:
            PublicProjectRepository(ctrl_session).delete(project_id)


# ---------------------------------------------------------------------------
# Project assets
# ---------------------------------------------------------------------------


@router.post("/{project_id}/assets", response_model=BatchAddResponse, status_code=200)
def add_assets_to_project(
    project_id: str,
    body: AssetIdsRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_editor)],
    user_id: Annotated[str, Depends(get_current_user_id)],
) -> BatchAddResponse:
    """Add assets to a project. Only the owner can add. Idempotent."""
    repo = ProjectRepository(session)
    col = _get_project_or_404(repo, project_id)
    _require_owner(col, user_id)
    _require_static(col)

    asset_repo = AssetRepository(session)
    for aid in body.asset_ids:
        asset = asset_repo.get_by_id(aid)
        if asset is None:
            raise HTTPException(status_code=404, detail=f"Asset {aid} not found or trashed")

    added = repo.add_assets(project_id, body.asset_ids)
    return BatchAddResponse(added=added)


@router.delete("/{project_id}/assets", response_model=BatchRemoveResponse)
def remove_assets_from_project(
    project_id: str,
    body: AssetIdsRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_editor)],
    user_id: Annotated[str, Depends(get_current_user_id)],
) -> BatchRemoveResponse:
    """Remove assets from a project. Only the owner can remove."""
    repo = ProjectRepository(session)
    col = _get_project_or_404(repo, project_id)
    _require_owner(col, user_id)
    _require_static(col)
    removed = repo.remove_assets(project_id, body.asset_ids)
    return BatchRemoveResponse(removed=removed)


@router.get("/{project_id}/assets", response_model=ProjectAssetsResponse)
def list_project_assets(
    project_id: str,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_editor)],
    user_id: Annotated[str, Depends(get_current_user_id)],
    after: str | None = Query(None, description="Pagination cursor"),
    limit: int = Query(200, ge=1, le=1000),
) -> ProjectAssetsResponse:
    """List assets in a project. Must be owner or project must be shared.

    For smart projects, executes the saved query to return live results.
    """
    repo = ProjectRepository(session)
    col = _get_project_or_404(repo, project_id)
    if not _can_view(col, user_id):
        raise HTTPException(status_code=404, detail="Project not found")

    if getattr(col, "type", "static") == "smart" and col.saved_query:
        # Smart project: execute saved query via filter algebra + query_page
        from src.server.models.filter_registry import from_json
        from src.server.models.query_filter import LibraryScope
        from src.server.repository.tenant import UnifiedBrowseRepository

        spec = from_json(col.saved_query)

        # Candidate-set pattern for text search (SearchTerm filters)
        candidate_ids: list[str] | None = None
        candidate_scores: dict[str, float] | None = None
        if spec.search_terms:
            tenant_id = getattr(request.state, "tenant_id", None)
            library_ids: list[str] | None = None
            for leaf in spec.leaves:
                if isinstance(leaf, LibraryScope):
                    library_ids = list(leaf.library_ids)
                    break
            if tenant_id:
                from src.server.api.routers.query import _run_quickwit_search, _run_postgres_fallback, MAX_CANDIDATE_IDS
                scores, contexts, source = _run_quickwit_search(
                    tenant_id, spec.search_terms, library_ids, limit=MAX_CANDIDATE_IDS,
                )
                if source == "postgres_fallback":
                    pg_query = " ".join(st.q for st in spec.search_terms if st.q)
                    scores, contexts = _run_postgres_fallback(
                        session, pg_query, library_ids, limit=MAX_CANDIDATE_IDS,
                    )
                if not scores:
                    return ProjectAssetsResponse(items=[], next_cursor=None)
                candidate_ids = list(scores.keys())
                candidate_scores = scores

        browse_repo = UnifiedBrowseRepository(session)
        assets = browse_repo.query_page(
            spec=spec,
            candidate_ids=candidate_ids,
            candidate_scores=candidate_scores,
            rating_user_id=user_id if spec.needs_rating_join else None,
            after=after,
            limit=limit,
        )

        items = [
            ProjectAssetItem(
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
        return ProjectAssetsResponse(items=items, next_cursor=None)

    # Static project: list manual assets
    assets, next_cursor = repo.list_assets(
        project_id, sort_order=col.sort_order, after_cursor=after, limit=limit
    )

    items = [
        ProjectAssetItem(
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

    return ProjectAssetsResponse(items=items, next_cursor=next_cursor)


# ---------------------------------------------------------------------------
# Reorder
# ---------------------------------------------------------------------------


@router.patch("/{project_id}/reorder", status_code=200)
def reorder_project(
    project_id: str,
    body: ReorderRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_editor)],
    user_id: Annotated[str, Depends(get_current_user_id)],
) -> dict:
    """Reorder assets in project. Only the owner can reorder."""
    repo = ProjectRepository(session)
    col = _get_project_or_404(repo, project_id)
    _require_owner(col, user_id)
    _require_static(col)

    try:
        repo.reorder(project_id, body.asset_ids)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {"ok": True}
