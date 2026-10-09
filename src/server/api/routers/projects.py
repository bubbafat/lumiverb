"""Projects API: CRUD, batch asset management, reorder. All routes require tenant auth."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from src.server.api.dependencies import get_current_user_id, get_tenant_session, require_editor
from src.server.api.errors import DecisionRequiredError
from src.server.database import get_control_session
from src.server.repository.control_plane import PublicProjectRepository
from src.server.repository.tenant import AssetRepository, LibraryRepository, ProjectRepository

# Mounted at /v1/projects. See main.py.
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
    # Clips still in the project but in the trash: hidden and not exported
    # until restored. Always 0 for smart projects, which are a search.
    trashed_asset_count: int = 0
    # Clips a scan trashed because their file went missing; they come back
    # when the file does. Always 0 for smart projects.
    missing_asset_count: int = 0
    # Clips whose library is in the trash: they come back if the library is
    # restored. Always 0 for smart projects.
    library_trashed_asset_count: int = 0
    # Clips a person archived: kept, and back with unarchive. Always 0 for smart projects.
    archived_asset_count: int = 0
    created_at: str
    updated_at: str
    status: str = "active"  # active | archived
    archived_at: str | None = None
    deleted_at: str | None = None  # in the trash when set


class ProjectListResponse(BaseModel):
    items: list[ProjectItem]


class EmptyTrashRequest(BaseModel):
    # Which trashed projects to delete for good; all of the caller's when omitted.
    project_ids: list[str] | None = None


class EmptyTrashResponse(BaseModel):
    deleted: int


class RestoreProjectRequest(BaseModel):
    # Required when the project has clips someone trashed: restore them too
    # (they come back everywhere), or leave them in the trash.
    with_clips: bool | None = None


class RestoreProjectResponse(BaseModel):
    restored_clips: int  # back in sight
    trashed_clips: int  # still in the trash: the request said to leave them
    missing_clips: int  # files missing from disk; back when the files are
    archived_clips: int = 0  # out of the trash, back in the archive where they were


class RestoreClipsResponse(BaseModel):
    restored: int  # clips you trashed, now back in sight everywhere
    missing: int  # clips whose files went missing; back when the files are
    archived: int = 0  # out of the trash, back in the archive where they were (still hidden here)


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
            rating_user_id=_rating_user(col, user_id) if spec.needs_rating_join else None,
            limit=10000,
        )
        count = len(live_assets)
        hidden = {"trashed": 0, "missing": 0, "library_trashed": 0, "archived": 0}
    else:
        count = repo.asset_count(col.project_id)
        hidden = repo.hidden_clip_counts(col.project_id)

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
        trashed_asset_count=hidden["trashed"],
        missing_asset_count=hidden["missing"],
        library_trashed_asset_count=hidden["library_trashed"],
        archived_asset_count=hidden["archived"],
        created_at=col.created_at.isoformat(),
        updated_at=col.updated_at.isoformat(),
        status=col.status,
        archived_at=col.archived_at.isoformat() if col.archived_at else None,
        deleted_at=col.deleted_at.isoformat() if col.deleted_at else None,
    )


def _sync_public_index(request: Request, project_id: str, *, public: bool) -> None:
    """Keep the control plane's public_projects index, which routes
    unauthenticated public links to this tenant, in step with visibility."""
    tenant_id = getattr(request.state, "tenant_id", None)
    connection_string = getattr(request.state, "connection_string", None)
    if not (tenant_id and connection_string):
        return
    with get_control_session() as ctrl_session:
        pub_repo = PublicProjectRepository(ctrl_session)
        if public:
            pub_repo.upsert(project_id, tenant_id, connection_string)
        else:
            pub_repo.delete(project_id)


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
    request: Request,
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
    if col.visibility == "public":
        _sync_public_index(request, col.project_id, public=True)

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
    status: Literal["active", "archived", "all", "trashed"] = "active",
) -> ProjectListResponse:
    """List projects owned by user + shared projects. Active only unless status
    says otherwise; "all" is active and archived. "trashed" is the caller's
    own trash, and trashed projects appear nowhere else."""
    repo = ProjectRepository(session)
    if status == "trashed":
        projects = repo.list_trashed(user_id)
    else:
        statuses = ("active", "archived") if status == "all" else (status,)
        projects = repo.list_for_user(user_id, statuses=statuses)
    return ProjectListResponse(
        items=[_project_to_item(c, repo, user_id, session=session) for c in projects]
    )


@router.post("/empty-trash", response_model=EmptyTrashResponse)
def empty_project_trash(
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_editor)],
    user_id: Annotated[str, Depends(get_current_user_id)],
    body: EmptyTrashRequest | None = None,
) -> EmptyTrashResponse:
    """Delete trashed projects for good: the named ones, or the caller's whole
    trash. Only the caller's own trash; active projects are never touched.
    The clips stay in their libraries."""
    repo = ProjectRepository(session)
    doomed = repo.list_trashed(user_id)
    if body is not None and body.project_ids is not None:
        wanted = set(body.project_ids)
        doomed = [c for c in doomed if c.project_id in wanted]
    return EmptyTrashResponse(deleted=delete_projects_for_good(session, doomed))


def delete_projects_for_good(session: Session, projects: list, *, before: datetime | None = None) -> int:
    """Delete trashed projects and their public pages. The clips stay in their
    libraries. Returns how many. Each is re-checked under a row lock (still in
    the trash, since before `before`): one restored since it was listed stays."""
    repo = ProjectRepository(session)
    deleted = 0
    # Read before anything commits or rolls back (both expire the objects).
    for project_id, was_public in [(col.project_id, col.visibility == "public") for col in projects]:
        if not repo.lock_trashed(project_id, before):
            session.rollback()
            continue
        repo.delete(project_id)  # commits, releasing the lock
        deleted += 1
        if was_public:
            with get_control_session() as ctrl_session:
                PublicProjectRepository(ctrl_session).delete(project_id)
    return deleted


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

    if body.visibility is not None and body.visibility != old_visibility:
        _sync_public_index(request, project_id, public=col.visibility == "public")

    return _project_to_item(col, repo, user_id, session=session)


@router.delete("/{project_id}", status_code=204)
def delete_project(
    project_id: str,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_editor)],
    user_id: Annotated[str, Depends(get_current_user_id)],
) -> None:
    """Move a project to the trash. Only the owner can. It disappears
    everywhere, its public link included, until restored; emptying the trash
    deletes it for good."""
    repo = ProjectRepository(session)
    col = _get_project_or_404(repo, project_id)
    _require_owner(col, user_id)
    # The public_projects row stays, so a restored public project's link
    # works again; the public routes treat a trashed project as missing.
    repo.trash(project_id)


@router.post("/{project_id}/restore", response_model=RestoreProjectResponse)
def restore_project(
    project_id: str,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_editor)],
    user_id: Annotated[str, Depends(get_current_user_id)],
    body: RestoreProjectRequest | None = None,
) -> RestoreProjectResponse:
    """Take a project out of the trash, back to active or archived as it
    was. 404 unless it's in the caller's trash.

    If clips in it are in the trash, the request must say what to do with
    them (with_clips), or it's refused with 409 clips_in_trash and the
    counts: restoring them brings them back everywhere, so the user decides.
    """
    repo = ProjectRepository(session)
    col = repo.get_by_id(project_id, include_trashed=True)
    if col is None or col.deleted_at is None:
        raise HTTPException(status_code=404, detail="Project not in the trash")
    if col.owner_user_id is not None and col.owner_user_id != user_id:
        raise HTTPException(status_code=404, detail="Project not in the trash")
    with_clips = body.with_clips if body else None
    hidden = repo.hidden_clip_counts(project_id)
    trashed, missing = hidden["trashed"], hidden["missing"]
    if trashed and with_clips is None:
        raise DecisionRequiredError(
            "clips_in_trash",
            f"{trashed} {'clip' if trashed == 1 else 'clips'} in this project "
            f"{'is' if trashed == 1 else 'are'} in the trash. Send with_clips: true to restore "
            f"{'it' if trashed == 1 else 'them'} too (everywhere), or false to leave "
            f"{'it' if trashed == 1 else 'them'}.",
            {"trashed_clips": trashed, "missing_clips": missing},
        )
    if not repo.restore(project_id):
        # Deleted for good while this waited for the purge holding it.
        raise HTTPException(status_code=404, detail="Project not in the trash")
    if col.visibility == "public":
        _sync_public_index(request, project_id, public=True)
    restored, archived = _restore_trashed_clips(request, session, repo, project_id) if with_clips and trashed else (0, 0)
    return RestoreProjectResponse(
        restored_clips=restored, trashed_clips=max(0, trashed - restored - archived), missing_clips=missing,
        archived_clips=archived,
    )


def _restore_trashed_clips(
    request: Request, session: Session, repo: ProjectRepository, project_id: str,
) -> tuple[int, int]:
    """Restore the project's clips that a person trashed, everywhere, back to
    where they were. Returns (back in sight, back in the archive)."""
    from src.server.api.routers.assets import reindex_restored_asset

    asset_repo = AssetRepository(session)
    restored, _, to_archive = asset_repo.restore_many(repo.trashed_asset_ids(project_id))
    for asset_id in restored:
        if asset_id in to_archive:
            continue  # back in the archive: not in search
        asset = asset_repo.get_by_id(asset_id)
        if asset is not None:
            reindex_restored_asset(request, asset)
    for library_id in asset_repo.library_ids_of(restored):
        LibraryRepository(session).bump_revision(library_id)
    return len(restored) - len(to_archive), len(to_archive)


@router.post("/{project_id}/restore-clips", response_model=RestoreClipsResponse)
def restore_project_clips(
    project_id: str,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_editor)],
    user_id: Annotated[str, Depends(get_current_user_id)],
) -> RestoreClipsResponse:
    """Restore the project's clips that a person trashed. They come back
    everywhere: the library, search, and every other project holding them.
    Clips a scan trashed because their file went missing are only counted;
    they come back when the file does."""
    repo = ProjectRepository(session)
    col = _get_project_or_404(repo, project_id)
    if not _can_view(col, user_id):
        raise HTTPException(status_code=404, detail="Project not found")
    restored, archived = _restore_trashed_clips(request, session, repo, project_id)
    return RestoreClipsResponse(restored=restored, missing=repo.hidden_clip_counts(project_id)["missing"],
                                archived=archived)


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


def _rating_user(col, viewer_id: str) -> str:
    """Whose ratings a smart project's rating filters read: the owner's.

    A shared smart project such as "my favorites" must show everyone the
    same clips, not each viewer's own favorites. Legacy projects without
    an owner fall back to the viewer.
    """
    return col.owner_user_id or viewer_id


def _smart_project_page(
    col, request: Request, session: Session, user_id: str, *, after: str | None, limit: int
) -> tuple[list, str | None]:
    """One page of a smart project's live results, and the cursor for the next.

    With a text filter, results come from a capped candidate set ordered
    by relevance and aren't paginated (the caller asks for up to
    MAX_CANDIDATE_IDS); otherwise they page by (sort column, asset_id) like
    GET /v1/query.
    """
    from src.server.api.routers.query import (
        MAX_CANDIDATE_IDS,
        SORT_COLUMNS,
        _encode_cursor,
        _run_postgres_fallback,
        _run_quickwit_search,
    )
    from src.server.models.filter_registry import from_json
    from src.server.models.query_filter import LibraryScope
    from src.server.repository.tenant import UnifiedBrowseRepository

    spec = from_json(col.saved_query)

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
            scores, _contexts, source = _run_quickwit_search(
                tenant_id, spec.search_terms, library_ids, limit=MAX_CANDIDATE_IDS,
            )
            if source == "postgres_fallback":
                pg_query = " ".join(st.q for st in spec.search_terms if st.q)
                scores, _contexts = _run_postgres_fallback(
                    session, pg_query, library_ids, limit=MAX_CANDIDATE_IDS,
                )
            if not scores:
                return [], None
            candidate_ids = list(scores.keys())
            candidate_scores = scores

    assets = UnifiedBrowseRepository(session).query_page(
        spec=spec,
        candidate_ids=candidate_ids,
        candidate_scores=candidate_scores,
        rating_user_id=_rating_user(col, user_id) if spec.needs_rating_join else None,
        after=after if candidate_ids is None else None,
        limit=limit,
    )

    next_cursor: str | None = None
    if candidate_ids is None and len(assets) == limit:
        sort_col = spec.sort if spec.sort in SORT_COLUMNS else "taken_at"
        last = assets[-1]
        sort_value = getattr(last, sort_col, None)
        if sort_value is not None and hasattr(sort_value, "isoformat"):
            sort_value = sort_value.isoformat()
        next_cursor = _encode_cursor(sort_col, sort_value, last.asset_id)
    return assets, next_cursor


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
        assets, next_cursor = _smart_project_page(
            col, request, session, user_id, after=after, limit=limit
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
        raise HTTPException(status_code=400, detail=str(e)) from e

    return {"ok": True}


# ---------------------------------------------------------------------------
# Send to editor (ADR-016 phase 1)
# ---------------------------------------------------------------------------

_EXPORT_PAGE = 500
_UNSAFE_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')


def _all_project_assets(col, request: Request, session: Session, user_id: str) -> list:
    """Every live clip in a project, paging through to the end (no 1,000 cap)."""
    if getattr(col, "type", "static") == "smart" and col.saved_query:
        from src.server.api.routers.query import MAX_CANDIDATE_IDS

        assets, cursor = _smart_project_page(
            col, request, session, user_id, after=None, limit=MAX_CANDIDATE_IDS
        )
        while cursor:
            page, cursor = _smart_project_page(
                col, request, session, user_id, after=cursor, limit=MAX_CANDIDATE_IDS
            )
            assets += page
        return assets

    repo = ProjectRepository(session)
    assets, cursor = repo.list_assets(col.project_id, sort_order=col.sort_order, limit=_EXPORT_PAGE)
    while cursor:
        page, cursor = repo.list_assets(
            col.project_id, sort_order=col.sort_order, after_cursor=cursor, limit=_EXPORT_PAGE
        )
        assets += page
    return assets


def _export_filename(name: str, extension: str) -> str:
    return (_UNSAFE_FILENAME_CHARS.sub("_", name).strip() or "project") + extension


def _content_disposition(filename: str) -> str:
    """attachment; ASCII filename for old clients, the real one in filename*.

    HTTP headers are Latin-1: a raw "Mike’s wedding" or "東京" would fail.
    """
    import unicodedata
    from urllib.parse import quote

    stem, dot, extension = filename.rpartition(".")
    ascii_stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode()
    ascii_stem = ascii_stem.replace('"', "_").replace("\\", "_").strip()
    if not any(ch.isalnum() for ch in ascii_stem):
        ascii_stem = "project-export"
    ascii_name = f"{ascii_stem}{dot}{extension}"
    return f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(filename)}'


@router.get("/{project_id}/export")
def export_project(
    project_id: str,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_editor)],
    user_id: Annotated[str, Depends(get_current_user_id)],
    format: str = Query(..., description="Export provider id: fcp7 or fcpxml"),  # noqa: A002
) -> Response:
    """Export a project as a bin of master clips for an editor.

    Clips point at the originals as the libraries know them (each library's
    root plus rel_path; Robert, Oct 9: the brain's paths, relinked in the
    editor when they differ). Videos and photos: a photo is a still of
    STILL_SEC (Robert, Oct 9). Headers count what needs the user's
    attention: X-Lumiverb-Stills (photos, as stills),
    X-Lumiverb-Skipped-No-Duration (videos with no known length, left out),
    X-Lumiverb-Unprobed (videos exported at a fallback frame rate),
    X-Lumiverb-Skipped-Trashed (clips someone trashed, left out until
    restored), X-Lumiverb-Skipped-Missing (clips whose files went missing),
    X-Lumiverb-Skipped-Library-Trashed (clips whose library is in the
    trash) and X-Lumiverb-Skipped-Archived. Archived projects export too.
    """
    import posixpath

    from src.server.export import EXPORT_PROVIDERS, ExportBin, ExportClip, still
    from src.server.models.tenant import Library, VideoFacetRow

    provider = EXPORT_PROVIDERS.get(format)
    if provider is None:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown format. Must be one of: {', '.join(EXPORT_PROVIDERS)}",
        )
    repo = ProjectRepository(session)
    col = _get_project_or_404(repo, project_id)
    if not _can_view(col, user_id):
        raise HTTPException(status_code=404, detail="Project not found")

    assets = _all_project_assets(col, request, session, user_id)
    videos = [a for a in assets if a.media_type == "video"]
    # Hidden clips are still in a static project but never exported.
    is_smart = getattr(col, "type", "static") == "smart"
    hidden = (
        {"trashed": 0, "missing": 0, "library_trashed": 0, "archived": 0}
        if is_smart else repo.hidden_clip_counts(col.project_id)
    )

    roots = {
        lib.library_id: lib.root_path
        for lib in session.exec(
            select(Library).where(Library.library_id.in_({a.library_id for a in assets}))  # type: ignore[attr-defined]
        ).all()
    } if assets else {}
    facets = {
        f.asset_id: f
        for f in session.exec(
            select(VideoFacetRow).where(VideoFacetRow.asset_id.in_([a.asset_id for a in videos]))  # type: ignore[attr-defined]
        ).all()
    } if videos else {}

    def where(a) -> str:
        # A relative root (a library created that way) is taken as relative to "/".
        root = posixpath.normpath(posixpath.join("/", roots.get(a.library_id) or "/"))
        return posixpath.join(root, a.rel_path.lstrip("/"))

    clips = []
    stills = 0
    skipped_no_duration = 0
    unprobed = 0
    for a in assets:
        if a.media_type != "video":
            clips.append(still(a.asset_id, posixpath.basename(a.rel_path), where(a), a.width, a.height))
            stills += 1
            continue
        f = facets.get(a.asset_id)
        duration = f.duration_sec if f and f.duration_sec is not None else a.duration_sec
        if not duration:
            # A zero-length clip makes Final Cut reject the file.
            skipped_no_duration += 1
            continue
        if f is None:
            unprobed += 1
        clips.append(ExportClip(
            asset_id=a.asset_id,
            name=posixpath.basename(a.rel_path),
            path=where(a),
            duration_sec=duration,
            frame_rate_num=f.frame_rate_num if f else None,
            frame_rate_den=f.frame_rate_den if f else None,
            width=f.width if f else a.width,
            height=f.height if f else a.height,
            start_timecode=f.start_timecode if f else None,
            drop_frame=f.drop_frame if f else None,
            audio_channels=f.audio_channels if f else None,
            audio_sample_rate=f.audio_sample_rate if f else None,
        ))

    body = provider.render(ExportBin(name=col.name, clips=clips))
    filename = _export_filename(col.name, provider.file_extension)
    return Response(
        content=body,
        media_type=provider.content_type,
        headers={
            "Content-Disposition": _content_disposition(filename),
            "X-Lumiverb-Stills": str(stills),
            "X-Lumiverb-Skipped-No-Duration": str(skipped_no_duration),
            "X-Lumiverb-Unprobed": str(unprobed),
            "X-Lumiverb-Skipped-Trashed": str(hidden["trashed"]),
            "X-Lumiverb-Skipped-Missing": str(hidden["missing"]),
            "X-Lumiverb-Skipped-Library-Trashed": str(hidden["library_trashed"]),
            "X-Lumiverb-Skipped-Archived": str(hidden["archived"]),
            "Access-Control-Expose-Headers": (
                "Content-Disposition, X-Lumiverb-Stills, "
                "X-Lumiverb-Skipped-No-Duration, X-Lumiverb-Unprobed, "
                "X-Lumiverb-Skipped-Trashed, X-Lumiverb-Skipped-Missing, "
                "X-Lumiverb-Skipped-Library-Trashed, X-Lumiverb-Skipped-Archived"
            ),
        },
    )
