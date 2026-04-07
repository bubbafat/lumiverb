"""Saved views CRUD endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from src.server.api.dependencies import get_current_user_id, get_tenant_session
from src.server.repository.tenant import SavedViewRepository

router = APIRouter(prefix="/v1/views", tags=["views"])


class CreateViewRequest(BaseModel):
    name: str
    query_params: str
    icon: str | None = None


class UpdateViewRequest(BaseModel):
    name: str | None = None
    query_params: str | None = None
    icon: str | None = None


class ReorderViewsRequest(BaseModel):
    view_ids: list[str]


class ViewItem(BaseModel):
    view_id: str
    name: str
    query_params: str
    icon: str | None
    position: int
    created_at: str
    updated_at: str


class ViewListResponse(BaseModel):
    items: list[ViewItem]


def _to_item(v) -> ViewItem:
    return ViewItem(
        view_id=v.view_id,
        name=v.name,
        query_params=v.query_params,
        icon=v.icon,
        position=v.position,
        created_at=v.created_at.isoformat() if v.created_at else "",
        updated_at=v.updated_at.isoformat() if v.updated_at else "",
    )


@router.post("", status_code=201, response_model=ViewItem)
def create_view(
    body: CreateViewRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
    user_id: Annotated[str, Depends(get_current_user_id)],
) -> ViewItem:
    if not body.name.strip():
        raise HTTPException(status_code=422, detail="Name is required")
    repo = SavedViewRepository(session)
    view = repo.create(
        owner_user_id=user_id,
        name=body.name.strip(),
        query_params=body.query_params,
        icon=body.icon,
    )
    return _to_item(view)


@router.get("", response_model=ViewListResponse)
def list_views(
    session: Annotated[Session, Depends(get_tenant_session)],
    user_id: Annotated[str, Depends(get_current_user_id)],
) -> ViewListResponse:
    repo = SavedViewRepository(session)
    views = repo.list_for_user(user_id)
    return ViewListResponse(items=[_to_item(v) for v in views])


@router.patch("/reorder", response_model=dict)
def reorder_views(
    body: ReorderViewsRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
    user_id: Annotated[str, Depends(get_current_user_id)],
) -> dict:
    if not body.view_ids:
        raise HTTPException(status_code=422, detail="view_ids required")
    repo = SavedViewRepository(session)
    repo.reorder(user_id, body.view_ids)
    return {"ok": True}


@router.patch("/{view_id}", response_model=ViewItem)
def update_view(
    view_id: str,
    body: UpdateViewRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
    user_id: Annotated[str, Depends(get_current_user_id)],
) -> ViewItem:
    repo = SavedViewRepository(session)
    view = repo.get(view_id)
    if view is None or view.owner_user_id != user_id:
        raise HTTPException(status_code=404, detail="View not found")
    if body.name is not None and not body.name.strip():
        raise HTTPException(status_code=422, detail="Name cannot be empty")
    view = repo.update(
        view,
        name=body.name.strip() if body.name else None,
        query_params=body.query_params,
        icon=body.icon,
    )
    return _to_item(view)


@router.delete("/{view_id}", status_code=204)
def delete_view(
    view_id: str,
    session: Annotated[Session, Depends(get_tenant_session)],
    user_id: Annotated[str, Depends(get_current_user_id)],
) -> None:
    repo = SavedViewRepository(session)
    view = repo.get(view_id)
    if view is None or view.owner_user_id != user_id:
        raise HTTPException(status_code=404, detail="View not found")
    repo.delete(view)
