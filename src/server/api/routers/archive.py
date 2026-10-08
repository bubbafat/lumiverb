"""Archive API: clips out of sight but kept forever (Robert's model, Oct 8).

A person archives clips (POST /v1/assets/archive) and brings them back
(POST /v1/assets/unarchive); a clip whose file went missing is archived
too, and comes back by itself when the file does. This lists them.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import Session

from src.server.api.dependencies import get_tenant_session, require_signed_in
from src.server.repository.tenant import AssetRepository

router = APIRouter(prefix="/v1/archive", tags=["archive"], dependencies=[Depends(require_signed_in)])


class HiddenClip(BaseModel):
    asset_id: str
    library_id: str
    library_name: str
    rel_path: str
    media_type: str


class ArchivedClip(HiddenClip):
    archived_at: str
    # Archived because a scan no longer finds the file: it comes back when the
    # file does, and Unarchive doesn't apply. Otherwise a person archived it.
    file_missing: bool


class ArchivePage(BaseModel):
    items: list[ArchivedClip]
    next_cursor: str | None = None
    total: int


def encode_cursor(row: dict) -> str:
    raw = json.dumps({"t": row["deleted_at"].isoformat(), "id": row["asset_id"]})
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> tuple[datetime, str] | None:
    if not cursor:
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        return datetime.fromisoformat(data["t"]), str(data["id"])
    except (ValueError, KeyError, TypeError) as e:
        raise HTTPException(status_code=400, detail="Bad cursor") from e


_KINDS = {"all": ("archived", "missing"), "by_hand": ("archived",), "missing": ("missing",)}


@router.get("", response_model=ArchivePage)
def list_archive(
    session: Annotated[Session, Depends(get_tenant_session)],
    library_id: str | None = None,
    path: str | None = Query(default=None, description="Only clips under this folder (recursive)."),
    kind: Literal["all", "by_hand", "missing"] = "all",
    after: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> ArchivePage:
    """Archived clips, most recently archived first: archived by a person, or
    their file went missing. Clips of a library in the trash aren't here."""
    rows, total = AssetRepository(session).page_hidden(
        _KINDS[kind], library_id=library_id, folder=path, after=decode_cursor(after), limit=limit,
    )
    return ArchivePage(
        items=[
            ArchivedClip(
                asset_id=r["asset_id"], library_id=r["library_id"], library_name=r["library_name"],
                rel_path=r["rel_path"], media_type=r["media_type"], archived_at=r["deleted_at"].isoformat(),
                file_missing=r["deleted_reason"] == "missing",
            )
            for r in rows
        ],
        next_cursor=encode_cursor(rows[-1]) if len(rows) == limit else None,
        total=total,
    )
