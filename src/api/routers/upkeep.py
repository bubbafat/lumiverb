"""Upkeep API: periodic maintenance tasks run by timer or repair CLI.

POST /v1/upkeep         — run all tasks
POST /v1/upkeep/search-sync — run search sync sweep only
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlmodel import Session

from src.api.dependencies import get_tenant_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/upkeep", tags=["upkeep"])


class SearchSyncResult(BaseModel):
    synced: int = 0
    failed: int = 0
    scenes_synced: int = 0
    scenes_failed: int = 0


class UpkeepResult(BaseModel):
    search_sync: SearchSyncResult


@router.post("", response_model=UpkeepResult)
def run_upkeep(
    session: Annotated[Session, Depends(get_tenant_session)],
) -> UpkeepResult:
    """Run all periodic upkeep tasks. Called by systemd timer."""
    from src.search.sync import run_search_sync_sweep

    sync_result = run_search_sync_sweep(session)

    return UpkeepResult(
        search_sync=SearchSyncResult(**sync_result),
    )


@router.post("/search-sync", response_model=SearchSyncResult)
def run_search_sync(
    session: Annotated[Session, Depends(get_tenant_session)],
) -> SearchSyncResult:
    """Run search sync sweep only."""
    from src.search.sync import run_search_sync_sweep

    result = run_search_sync_sweep(session)
    return SearchSyncResult(**result)
