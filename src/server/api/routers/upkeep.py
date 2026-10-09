"""Upkeep API: periodic maintenance tasks run by timer or repair CLI.

Uses admin auth (ADMIN_KEY), iterates all tenants automatically.

POST /v1/upkeep                  — run all frequent tasks across all tenants
POST /v1/upkeep/search-sync      — run search sync sweep only
POST /v1/upkeep/cleanup          — run filesystem cleanup (dry_run=true by default)
POST /v1/upkeep/cleanup-dismissed — delete dismissed people with zero face matches
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlmodel import Session

from sqlalchemy import text

from src.server.api.dependencies import require_admin
from src.server.config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/upkeep", tags=["upkeep"])


class ReclusterResult(BaseModel):
    clusters: int = 0
    total_faces: int = 0


class CleanupDismissedResult(BaseModel):
    deleted: int = 0



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
    # Skipped because all processing is paused, not "nothing to do"; with the admin key, which accounts.
    paused: bool = False
    paused_tenants: list[str] = Field(default_factory=list)


class FacePropagateResult(BaseModel):
    assigned: int = 0
    scanned: int = 0
    # Skipped because all processing is paused, not "nothing to do"; with the admin key, which accounts.
    paused: bool = False
    paused_tenants: list[str] = Field(default_factory=list)


class TrashPurgeResult(BaseModel):
    """Deleted for good because they'd been in the trash longer than the account's trash days."""
    clips: int = 0
    libraries: int = 0
    projects: int = 0
    # Skipped because all processing is paused, not "nothing to do"; with the admin key, which accounts.
    paused: bool = False
    paused_tenants: list[str] = Field(default_factory=list)


class UpkeepResult(BaseModel):
    search_sync: SearchSyncResult
    face_propagate: FacePropagateResult = FacePropagateResult()
    trash_purge: TrashPurgeResult = TrashPurgeResult()


def _is_admin_key(authorization: str | None) -> bool:
    """Check if the bearer token is the admin key."""
    if not authorization or not authorization.startswith("Bearer "):
        return False
    token = authorization[7:].strip()
    settings = get_settings()
    import hmac
    return bool(settings.admin_key and hmac.compare_digest(token, settings.admin_key))


def _tenant_for_key(authorization: str | None) -> tuple[str, str, str] | None:
    """(tenant_id, connection_string, role) for a live tenant API key, or None.

    Upkeep routes skip the tenant middleware, so handlers resolve the
    tenant from the key themselves.
    """
    from src.server.database import get_control_session
    from src.server.repository.control_plane import ApiKeyRepository, TenantDbRoutingRepository

    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization[7:].strip()
    with get_control_session() as ctrl:
        api_key = ApiKeyRepository(ctrl).get_by_plaintext(token)
        if api_key is None:
            return None
        routing = TenantDbRoutingRepository(ctrl).get_by_tenant_id(api_key.tenant_id)
        if routing is None:
            return None
        return api_key.tenant_id, routing.connection_string, api_key.role


def _all_paused(session) -> bool:
    """An admin paused all of the account's processing (the kill switch):
    upkeep that changes data waits; search sync goes on (Robert, Oct 9)."""
    from src.server.repository import lineage

    return lineage.all_paused(session)


def _skipped(what: str, tenant_id: str, paused: list[str] | None = None) -> dict:
    """An account's upkeep skipped for its Pause all: logged, and said in the result
    (paused), so it reads apart from a run with nothing to do."""
    logger.info("%s for tenant %s: all processing is paused", what, tenant_id)
    if paused is not None:
        paused.append(tenant_id)
    return {"paused": True, "paused_tenants": [tenant_id]}


def _propagate_faces_all_tenants() -> dict:
    """Run face propagation across all tenants."""
    from src.server.database import get_control_session, get_tenant_session
    from src.server.repository.control_plane import TenantRepository
    from src.server.repository.tenant import FaceRepository

    totals = {"assigned": 0, "scanned": 0}
    paused: list[str] = []

    with get_control_session() as ctrl:
        tenants = TenantRepository(ctrl).list_all()

    for tenant in tenants:
        try:
            with get_tenant_session(tenant.tenant_id) as session:
                if _all_paused(session):
                    _skipped("Face names aren't spread", tenant.tenant_id, paused)
                    continue
                result = FaceRepository(session).propagate_assignments()
                totals["assigned"] += result["assigned"]
                totals["scanned"] += result["scanned"]
        except Exception as exc:
            logger.warning("Face propagation failed for tenant %s: %s", tenant.tenant_id, exc)

    return {**totals, "paused": bool(paused), "paused_tenants": paused}


def _propagate_faces_single_tenant(authorization: str | None) -> dict:
    """Run face propagation for the tenant resolved from the API key."""
    from src.server.database import get_engine_for_url
    from src.server.repository.tenant import FaceRepository
    from sqlmodel import Session as TenantSession

    tenant = _tenant_for_key(authorization)
    if tenant is None:
        return {"assigned": 0, "scanned": 0}
    _, connection_string, _ = tenant
    with TenantSession(get_engine_for_url(connection_string)) as session:
        if _all_paused(session):
            return {"assigned": 0, "scanned": 0, **_skipped("Face names aren't spread", tenant[0])}
        return FaceRepository(session).propagate_assignments()


def _purge_expired_trash_all_tenants() -> dict:
    """Empty every tenant's expired trash."""
    from src.server.api.routers.trash import purge_expired_trash
    from src.server.database import get_control_session, get_tenant_session
    from src.server.repository.control_plane import TenantRepository

    totals = {"clips": 0, "libraries": 0, "projects": 0}
    paused: list[str] = []
    with get_control_session() as ctrl:
        tenants = TenantRepository(ctrl).list_all()
    for tenant in tenants:
        try:
            with get_tenant_session(tenant.tenant_id) as session:
                if _all_paused(session):
                    _skipped("The expired trash isn't emptied", tenant.tenant_id, paused)
                    continue
                for key, n in purge_expired_trash(session, tenant.tenant_id).items():
                    totals[key] += n
        except Exception as exc:
            logger.warning("Trash purge failed for tenant %s: %s", tenant.tenant_id, exc)
    return {**totals, "paused": bool(paused), "paused_tenants": paused}


def _purge_expired_trash_single_tenant(authorization: str | None) -> dict:
    """Empty the expired trash of the tenant resolved from the API key."""
    from sqlmodel import Session as TenantSession

    from src.server.api.routers.trash import purge_expired_trash
    from src.server.database import get_engine_for_url

    tenant = _tenant_for_key(authorization)
    if tenant is None:
        return {}
    tenant_id, connection_string, _ = tenant
    try:
        with TenantSession(get_engine_for_url(connection_string)) as session:
            if _all_paused(session):
                return _skipped("The expired trash isn't emptied", tenant_id)
            return purge_expired_trash(session, tenant_id)
    except Exception as exc:
        logger.warning("Trash purge failed for tenant %s: %s", tenant_id, exc)
        return {}


def _reset_search_synced_at(session) -> None:
    """Clear search_synced_at on all assets and scenes to force full re-index."""
    from sqlalchemy import text
    session.execute(text("UPDATE assets SET search_synced_at = NULL"))
    session.execute(text("UPDATE video_scenes SET search_synced_at = NULL"))
    session.commit()


def _run_sweep_all_tenants(force: bool = False) -> dict:
    """Run search sync sweep across all tenants, aggregating results."""
    from src.server.database import get_control_session, get_tenant_session
    from src.server.repository.control_plane import TenantRepository
    from src.server.search.sync import run_search_sync_sweep

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
    from src.server.search.sync import run_search_sync_sweep
    from src.server.database import get_engine_for_url
    from sqlmodel import Session as TenantSession

    tenant = _tenant_for_key(authorization)
    if tenant is None:
        return {"synced": 0, "failed": 0, "scenes_synced": 0, "scenes_failed": 0}
    tenant_id, connection_string, _ = tenant
    with TenantSession(get_engine_for_url(connection_string)) as session:
        if force:
            _reset_search_synced_at(session)
        return run_search_sync_sweep(session, tenant_id=tenant_id)


def _cleanup_revoked_tokens() -> int:
    """Remove revoked tokens older than 8 days (past any possible refresh window)."""
    from datetime import timedelta
    from src.server.database import get_control_session
    from src.shared.utils import utcnow
    from src.server.repository.control_plane import RevokedTokenRepository

    cutoff = utcnow() - timedelta(days=8)
    try:
        with get_control_session() as session:
            return RevokedTokenRepository(session).cleanup_expired(cutoff)
    except Exception as exc:
        logger.warning("Revoked token cleanup failed: %s", exc)
        return 0


@router.post("", response_model=UpkeepResult)
def run_upkeep(
    authorization: Annotated[str | None, Header()] = None,
) -> UpkeepResult:
    """Run all periodic upkeep tasks: search sync, face propagation, and
    deleting for good what has been in the trash past the trash days.

    With admin key: sweeps all tenants. With tenant API key: sweeps that tenant only.
    """
    if _is_admin_key(authorization):
        sync_result = _run_sweep_all_tenants()
        prop_result = _propagate_faces_all_tenants()
        purge_result = _purge_expired_trash_all_tenants()
        _cleanup_revoked_tokens()
    else:
        sync_result = _run_sweep_single_tenant(authorization)
        prop_result = _propagate_faces_single_tenant(authorization)
        purge_result = _purge_expired_trash_single_tenant(authorization)
    return UpkeepResult(
        search_sync=SearchSyncResult(**sync_result),
        face_propagate=FacePropagateResult(**prop_result),
        trash_purge=TrashPurgeResult(**purge_result),
    )


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
    from src.server.search.cleanup import run_cleanup_all_tenants, run_cleanup_single_tenant

    if _is_admin_key(authorization):
        result = run_cleanup_all_tenants(dry_run=dry_run)
    else:
        from src.server.database import get_engine_for_url
        from sqlmodel import Session as TenantSession

        tenant = _tenant_for_key(authorization)
        if tenant is None:
            raise HTTPException(status_code=401, detail="Admin key or tenant API key required")
        tenant_id, connection_string, role = tenant
        # Cleanup deletes files (and lists them on a dry run): admins only.
        if role != "admin":
            raise HTTPException(status_code=403, detail="Admin API key required")
        with TenantSession(get_engine_for_url(connection_string)) as session:
            result = run_cleanup_single_tenant(tenant_id, session, dry_run=dry_run)

    return CleanupResultModel(
        orphan_tenants=result.orphan_tenants,
        orphan_libraries=result.orphan_libraries,
        orphan_files=result.orphan_files,
        bytes_freed=result.bytes_freed,
        skipped_libraries=result.skipped_libraries,
        errors=result.errors,
        dry_run=dry_run,
        paused=result.paused,
        paused_tenants=result.paused_tenants,
    )


@router.post("/recluster", response_model=ReclusterResult)
def run_recluster(
    request: Request,
    _admin: Annotated[None, Depends(require_admin)],
    authorization: Annotated[str | None, Header()] = None,
) -> ReclusterResult:
    """Force recompute face clusters for all tenants."""
    import json as _json
    from datetime import datetime, timezone

    from src.server.database import get_control_session, get_tenant_session
    from src.server.repository.control_plane import TenantRepository
    from src.server.repository.system_metadata import SystemMetadataRepository
    from src.server.repository.tenant import FaceRepository

    totals = {"clusters": 0, "total_faces": 0}

    with get_control_session() as ctrl:
        tenants = TenantRepository(ctrl).list_all()

    for tenant in tenants:
        try:
            with get_tenant_session(tenant.tenant_id) as tsession:
                repo = FaceRepository(tsession)
                clusters, all_face_ids, truncated = repo.compute_clusters(max_clusters=50, faces_per_cluster=20)

                cache_clusters = [
                    {"cluster_index": i, "size": len(ids), "faces": c, "face_ids": ids}
                    for i, (c, ids) in enumerate(zip(clusters, all_face_ids))
                ]
                cache_data = _json.dumps({
                    "clusters": cache_clusters,
                    "truncated": truncated,
                    "computed_at": datetime.now(timezone.utc).isoformat(),
                })
                meta = SystemMetadataRepository(tsession)
                meta.set_value("face_clusters_cache", cache_data)
                meta.set_value("face_clusters_dirty", "false")

                totals["clusters"] += len(clusters)
                totals["total_faces"] += sum(len(ids) for ids in all_face_ids)
        except Exception as exc:
            logger.warning("Recluster failed for tenant %s: %s", tenant.tenant_id, exc)

    return ReclusterResult(**totals)


class RecreateSearchIndexesResult(BaseModel):
    tenants_processed: int = 0
    errors: list[str] = []


@router.post("/recreate-search-indexes", response_model=RecreateSearchIndexesResult)
def recreate_search_indexes(
    authorization: Annotated[str | None, Header()] = None,
) -> RecreateSearchIndexesResult:
    """Wipe and recreate Quickwit indexes. Used to clean up duplicate
    documents accumulated by repeated force-resyncs (Quickwit doesn't
    upsert, so each force-resync used to leave another copy of every
    doc until the dedupe gate landed). After calling this, the caller
    MUST also call POST /v1/upkeep/search-sync to repopulate the empty
    indexes.

    Auth model mirrors POST /v1/upkeep/search-sync — admin key wipes
    all tenants, tenant API key wipes only that tenant. Destructive,
    no undo, but the data can be rebuilt from Postgres via the
    search-sync sweep so it's safe.
    """
    from src.server.database import get_control_session, get_tenant_session
    from src.server.repository.control_plane import (
        ApiKeyRepository,
        TenantRepository,
    )
    from src.server.search.quickwit_client import QuickwitClient

    qw = QuickwitClient()
    if not qw.enabled:
        return RecreateSearchIndexesResult(
            tenants_processed=0, errors=["Quickwit disabled"],
        )

    # Resolve which tenants to process based on auth.
    if _is_admin_key(authorization):
        with get_control_session() as ctrl:
            tenants = TenantRepository(ctrl).list_all()
        tenant_ids = [t.tenant_id for t in tenants]
    else:
        if not authorization or not authorization.startswith("Bearer "):
            return RecreateSearchIndexesResult(
                tenants_processed=0, errors=["Unauthorized"],
            )
        token = authorization[7:].strip()
        with get_control_session() as ctrl:
            api_key = ApiKeyRepository(ctrl).get_by_plaintext(token)
            if api_key is None:
                return RecreateSearchIndexesResult(
                    tenants_processed=0, errors=["Unauthorized"],
                )
            tenant_ids = [api_key.tenant_id]

    errors: list[str] = []
    processed = 0
    for tenant_id in tenant_ids:
        try:
            qw.recreate_tenant_indexes(tenant_id)
            processed += 1
        except Exception as exc:
            errors.append(f"{tenant_id}: {exc}")
            logger.warning("Recreate Quickwit indexes failed for %s: %s", tenant_id, exc)

    # Reset every search_synced_at so the next search-sync sweep
    # actually does the work.
    for tenant_id in tenant_ids:
        try:
            with get_tenant_session(tenant_id) as tsession:
                tsession.execute(text("UPDATE assets SET search_synced_at = NULL"))
                tsession.execute(text("UPDATE video_scenes SET search_synced_at = NULL"))
                tsession.commit()
        except Exception as exc:
            logger.warning("Failed to reset search_synced_at for tenant %s: %s", tenant_id, exc)

    return RecreateSearchIndexesResult(tenants_processed=processed, errors=errors)


@router.post("/cleanup-dismissed", response_model=CleanupDismissedResult)
def run_cleanup_dismissed(
    request: Request,
    _admin: Annotated[None, Depends(require_admin)],
    authorization: Annotated[str | None, Header()] = None,
) -> CleanupDismissedResult:
    """Delete dismissed people that have zero face matches."""
    from src.server.database import get_control_session, get_tenant_session
    from src.server.repository.control_plane import TenantRepository
    from src.server.repository.tenant import PersonRepository

    total_deleted = 0

    with get_control_session() as ctrl:
        tenants = TenantRepository(ctrl).list_all()

    for tenant in tenants:
        try:
            with get_tenant_session(tenant.tenant_id) as tsession:
                deleted = PersonRepository(tsession).cleanup_empty_dismissed()
                if deleted:
                    logger.info("Cleaned up %d empty dismissed people for tenant %s",
                                deleted, tenant.tenant_id)
                total_deleted += deleted
        except Exception as exc:
            logger.warning("Cleanup dismissed failed for tenant %s: %s", tenant.tenant_id, exc)

    return CleanupDismissedResult(deleted=total_deleted)
