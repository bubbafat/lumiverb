"""Upkeep API: periodic maintenance tasks run by timer or repair CLI.

Uses admin auth (ADMIN_KEY), iterates all tenants automatically.

POST /v1/upkeep             — run all tasks across all tenants
POST /v1/upkeep/search-sync — run search sync sweep only
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel
from sqlmodel import Session

from src.api.dependencies import require_admin
from src.core.config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/upkeep", tags=["upkeep"])


class SearchSyncResult(BaseModel):
    synced: int = 0
    failed: int = 0
    scenes_synced: int = 0
    scenes_failed: int = 0


class UpkeepResult(BaseModel):
    search_sync: SearchSyncResult


def _is_admin_key(authorization: str | None) -> bool:
    """Check if the bearer token is the admin key."""
    if not authorization or not authorization.startswith("Bearer "):
        return False
    token = authorization[7:].strip()
    settings = get_settings()
    return bool(settings.admin_key and token == settings.admin_key)


def _run_sweep_all_tenants() -> dict:
    """Run search sync sweep across all tenants, aggregating results."""
    from src.core.database import get_control_session, get_tenant_session
    from src.repository.control_plane import TenantRepository
    from src.search.sync import run_search_sync_sweep

    totals = {"synced": 0, "failed": 0, "scenes_synced": 0, "scenes_failed": 0}

    with get_control_session() as control_session:
        tenants = TenantRepository(control_session).list_all()

    for tenant in tenants:
        try:
            with get_tenant_session(tenant.tenant_id) as session:
                result = run_search_sync_sweep(session)
                for key in totals:
                    totals[key] += result.get(key, 0)
        except Exception as exc:
            logger.warning("Upkeep failed for tenant %s: %s", tenant.tenant_id, exc)
            totals["failed"] += 1

    return totals


def _run_sweep_single_tenant(request: Request) -> dict:
    """Run search sync sweep for the tenant resolved from the API key."""
    from src.search.sync import run_search_sync_sweep
    from src.api.dependencies import get_tenant_session as dep_get_tenant_session

    session = next(dep_get_tenant_session(request))
    try:
        return run_search_sync_sweep(session)
    finally:
        session.close()


@router.post("", response_model=UpkeepResult)
def run_upkeep(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> UpkeepResult:
    """Run all periodic upkeep tasks.

    With admin key: sweeps all tenants. With tenant API key: sweeps that tenant only.
    """
    if _is_admin_key(authorization):
        sync_result = _run_sweep_all_tenants()
    else:
        sync_result = _run_sweep_single_tenant(request)
    return UpkeepResult(search_sync=SearchSyncResult(**sync_result))


@router.post("/search-sync", response_model=SearchSyncResult)
def run_search_sync(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> SearchSyncResult:
    """Run search sync sweep.

    With admin key: sweeps all tenants. With tenant API key: sweeps that tenant only.
    """
    if _is_admin_key(authorization):
        result = _run_sweep_all_tenants()
    else:
        result = _run_sweep_single_tenant(request)
    return SearchSyncResult(**result)
