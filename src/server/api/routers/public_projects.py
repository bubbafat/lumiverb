"""Public project endpoints. No auth required — resolved via public_projects control plane."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import Session

from src.server.api.dependencies import get_tenant_session
from src.server.repository.tenant import ProjectRepository

# Mounted at /v1/public/projects, and at /v1/public/collections (deprecated)
# for existing share links and app builds. See main.py.
router = APIRouter()


class PublicProjectDetail(BaseModel):
    project_id: str
    name: str
    description: str | None
    cover_asset_id: str | None
    asset_count: int


class PublicProjectAssetItem(BaseModel):
    asset_id: str
    media_type: str
    width: int | None = None
    height: int | None = None
    taken_at: str | None = None
    duration_sec: float | None = None


class PublicProjectAssetsResponse(BaseModel):
    items: list[PublicProjectAssetItem]
    next_cursor: str | None = None


def _get_public_project(session: Session, project_id: str):
    """Get project and verify it's actually public."""
    repo = ProjectRepository(session)
    col = repo.get_by_id(project_id)
    if col is None or col.visibility != "public":
        raise HTTPException(status_code=404, detail="Project not found")
    return col, repo


@router.get("/{project_id}", response_model=PublicProjectDetail)
def get_public_project(
    project_id: str,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> PublicProjectDetail:
    """Get public project metadata. No auth required."""
    col, repo = _get_public_project(session, project_id)
    return PublicProjectDetail(
        project_id=col.project_id,
        name=col.name,
        description=col.description,
        cover_asset_id=repo.resolve_cover(col),
        asset_count=repo.asset_count(col.project_id),
    )


@router.get("/{project_id}/assets", response_model=PublicProjectAssetsResponse)
def list_public_project_assets(
    project_id: str,
    session: Annotated[Session, Depends(get_tenant_session)],
    after: str | None = Query(None, description="Pagination cursor"),
    limit: int = Query(200, ge=1, le=1000),
) -> PublicProjectAssetsResponse:
    """List assets in a public project. No auth required. Privacy-stripped."""
    col, repo = _get_public_project(session, project_id)

    assets, next_cursor = repo.list_assets(
        project_id, sort_order=col.sort_order, after_cursor=after, limit=limit
    )

    # Privacy: strip library paths, limit metadata
    items = [
        PublicProjectAssetItem(
            asset_id=a.asset_id,
            media_type=a.media_type,
            width=a.width,
            height=a.height,
            taken_at=a.taken_at.isoformat() if a.taken_at else None,
            duration_sec=a.duration_sec,
        )
        for a in assets
    ]

    return PublicProjectAssetsResponse(items=items, next_cursor=next_cursor)
