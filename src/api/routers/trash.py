"""Trash API: empty trash (permanent delete). Admin only."""

import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlmodel import Session

from src.api.dependencies import get_tenant_session, require_tenant_admin
from src.core.utils import utcnow
from src.repository.tenant import AssetRepository
from src.storage.local import get_storage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/trash", tags=["trash"])


class EmptyTrashRequest(BaseModel):
    asset_ids: list[str] | None = None
    trashed_before: str | None = None  # ISO8601


class EmptyTrashResponse(BaseModel):
    deleted: int


@router.delete("/empty", response_model=EmptyTrashResponse)
def empty_trash(
    request: Request,
    body: EmptyTrashRequest,
    session: Annotated[Session, Depends(get_tenant_session)],
    _: Annotated[None, Depends(require_tenant_admin)],
) -> EmptyTrashResponse:
    """
    Permanently delete trashed assets. Admin only.
    Scope by asset_ids and/or trashed_before. If neither provided, delete all trashed.
    Deletes DB rows in FK-safe order, then best-effort file and Quickwit cleanup.
    """
    asset_repo = AssetRepository(session)
    trashed_before_dt: datetime | None = None
    if body.trashed_before:
        try:
            trashed_before_dt = datetime.fromisoformat(
                body.trashed_before.replace("Z", "+00:00")
            )
        except ValueError:
            trashed_before_dt = None
    if body.asset_ids is None and trashed_before_dt is None:
        trashed_before_dt = utcnow()
    to_delete = asset_repo.list_trashed(
        asset_ids=body.asset_ids,
        trashed_before=trashed_before_dt,
    )
    if not to_delete:
        return EmptyTrashResponse(deleted=0)
    asset_ids = [a.asset_id for a in to_delete]
    # Collect keys for file cleanup before DB delete
    keys_to_remove: list[str] = []
    for a in to_delete:
        if a.proxy_key:
            keys_to_remove.append(a.proxy_key)
        if a.thumbnail_key:
            keys_to_remove.append(a.thumbnail_key)
        if getattr(a, "video_preview_key", None):
            keys_to_remove.append(a.video_preview_key)
    library_by_asset = {a.asset_id: a.library_id for a in to_delete}
    deleted_count = asset_repo.permanently_delete(asset_ids)
    storage = get_storage()
    for key in keys_to_remove:
        try:
            path = storage.abs_path(key)
            if path.exists():
                path.unlink()
        except OSError as e:
            logger.warning("Failed to remove file %s after empty trash: %s", key, e)
    try:
        from src.search.quickwit_client import QuickwitClient
        qw = QuickwitClient()
        for aid in asset_ids:
            lib_id = library_by_asset.get(aid)
            if lib_id:
                qw.delete_documents_by_asset_id(lib_id, aid)
    except Exception as e:
        logger.warning("Quickwit delete after empty trash failed: %s", e)
    return EmptyTrashResponse(deleted=deleted_count)
