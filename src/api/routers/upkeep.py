"""Upkeep API: periodic maintenance tasks run by timer or repair CLI.

Uses admin auth (ADMIN_KEY), iterates all tenants automatically.

POST /v1/upkeep              — run all frequent tasks across all tenants
POST /v1/upkeep/search-sync  — run search sync sweep only
POST /v1/upkeep/cleanup      — run filesystem cleanup (dry_run=true by default)
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request
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


class CleanupResultModel(BaseModel):
    orphan_tenants: int = 0
    orphan_libraries: int = 0
    orphan_files: int = 0
    bytes_freed: int = 0
    skipped_libraries: int = 0
    errors: list[str] = []
    dry_run: bool = True


class UpkeepResult(BaseModel):
    search_sync: SearchSyncResult


def _is_admin_key(authorization: str | None) -> bool:
    """Check if the bearer token is the admin key."""
    if not authorization or not authorization.startswith("Bearer "):
        return False
    token = authorization[7:].strip()
    settings = get_settings()
    return bool(settings.admin_key and token == settings.admin_key)


def _reset_search_synced_at(session) -> None:
    """Clear search_synced_at on all assets and scenes to force full re-index."""
    from sqlalchemy import text
    session.execute(text("UPDATE assets SET search_synced_at = NULL"))
    session.execute(text("UPDATE video_scenes SET search_synced_at = NULL"))
    session.commit()


def _run_sweep_all_tenants(force: bool = False) -> dict:
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
                if force:
                    _reset_search_synced_at(session)
                result = run_search_sync_sweep(session, tenant_id=tenant.tenant_id)
                for key in totals:
                    totals[key] += result.get(key, 0)
        except Exception as exc:
            logger.warning("Upkeep failed for tenant %s: %s", tenant.tenant_id, exc)
            totals["failed"] += 1

    return totals


def _run_sweep_single_tenant(authorization: str | None, force: bool = False) -> dict:
    """Run search sync sweep for the tenant resolved from the API key."""
    from src.search.sync import run_search_sync_sweep
    from src.core.database import get_control_session, get_engine_for_url
    from src.repository.control_plane import ApiKeyRepository, TenantDbRoutingRepository
    from sqlmodel import Session as TenantSession

    if not authorization or not authorization.startswith("Bearer "):
        return {"synced": 0, "failed": 0, "scenes_synced": 0, "scenes_failed": 0}

    token = authorization[7:].strip()
    with get_control_session() as ctrl_session:
        key_repo = ApiKeyRepository(ctrl_session)
        api_key = key_repo.get_by_plaintext(token)
        if api_key is None:
            return {"synced": 0, "failed": 0, "scenes_synced": 0, "scenes_failed": 0}
        tenant_id = api_key.tenant_id
        routing = TenantDbRoutingRepository(ctrl_session).get_by_tenant_id(tenant_id)
        if routing is None:
            return {"synced": 0, "failed": 0, "scenes_synced": 0, "scenes_failed": 0}

    engine = get_engine_for_url(routing.connection_string)
    with TenantSession(engine) as session:
        if force:
            _reset_search_synced_at(session)
        return run_search_sync_sweep(session, tenant_id=tenant_id)


@router.post("", response_model=UpkeepResult)
def run_upkeep(
    authorization: Annotated[str | None, Header()] = None,
) -> UpkeepResult:
    """Run all periodic upkeep tasks.

    With admin key: sweeps all tenants. With tenant API key: sweeps that tenant only.
    """
    if _is_admin_key(authorization):
        sync_result = _run_sweep_all_tenants()
    else:
        sync_result = _run_sweep_single_tenant(authorization)
    return UpkeepResult(search_sync=SearchSyncResult(**sync_result))


@router.post("/search-sync", response_model=SearchSyncResult)
def run_search_sync(
    authorization: Annotated[str | None, Header()] = None,
    force: bool = Query(default=False, description="Reset all search_synced_at timestamps and re-index everything"),
) -> SearchSyncResult:
    """Run search sync sweep.

    With admin key: sweeps all tenants. With tenant API key: sweeps that tenant only.
    force=true: clears search_synced_at on all assets/scenes so everything is re-indexed.
    """
    if _is_admin_key(authorization):
        result = _run_sweep_all_tenants(force=force)
    else:
        result = _run_sweep_single_tenant(authorization, force=force)
    return SearchSyncResult(**result)


@router.post("/cleanup", response_model=CleanupResultModel)
def run_cleanup(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    dry_run: bool = True,
) -> CleanupResultModel:
    """Run filesystem cleanup to remove orphaned files left after trash is emptied.

    dry_run=true (default): report what would be deleted without deleting.
    dry_run=false: actually delete orphaned files.

    With admin key: cleans all tenants. With tenant API key: cleans that tenant only.
    """
    from src.search.cleanup import run_cleanup_all_tenants, run_cleanup_single_tenant

    if _is_admin_key(authorization):
        result = run_cleanup_all_tenants(dry_run=dry_run)
    else:
        from src.api.dependencies import get_tenant_session as dep_get_tenant_session

        tenant_id = request.state.tenant_id
        session = next(dep_get_tenant_session(request))
        try:
            result = run_cleanup_single_tenant(tenant_id, session, dry_run=dry_run)
        finally:
            session.close()

    return CleanupResultModel(
        orphan_tenants=result.orphan_tenants,
        orphan_libraries=result.orphan_libraries,
        orphan_files=result.orphan_files,
        bytes_freed=result.bytes_freed,
        skipped_libraries=result.skipped_libraries,
        errors=result.errors,
        dry_run=dry_run,
    )
