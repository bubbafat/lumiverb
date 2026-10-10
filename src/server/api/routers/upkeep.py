"""Upkeep API: periodic maintenance, run by the systemd timers and the CLI.

Who and what (upkeep_scope): an account admin's API key acts on that
account. The operator's admin key (ADMIN_KEY) acts on every account, and
must say so with ?tenants=all; without it the call is a 400, never
"everything". No key or a dead one is a 401; another role's key a 403.

POST /v1/upkeep                    — search sync, face names, expired trash, missing artifact files,
                                     empty dismissed people, face clusters when faces changed
                                     (and, all accounts, old revoked tokens)
POST /v1/upkeep/search-sync        — search sync sweep only (force=true: reindex everything)
POST /v1/upkeep/cleanup            — orphaned files (dry_run=true by default; library_id or all=true)
POST /v1/upkeep/recluster          — recompute face clusters
POST /v1/upkeep/recreate-search-indexes — wipe and remake the Quickwit indexes
POST /v1/upkeep/cleanup-dismissed  — delete dismissed people with zero face matches

Every-account sweeps run in the request, on purpose: their callers are the
systemd timers (oneshot units: the unit's exit status and journal are the
run's result) and the operator by hand, who needs the result (recreating
the indexes must be followed by a search sync). FastAPI runs these plain
handlers on its thread pool, so the API keeps answering meanwhile, and each
call's work is bounded (1,000 clips and scenes per account per search sync,
500 trashed clips per purge). The scheduler hands out per-clip jobs per
account and has no every-account periodic task to give them to.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text

from src.server.api.limits import require_scope
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
    transcripts_synced: int = 0
    transcripts_failed: int = 0


class CleanupResultModel(BaseModel):
    orphan_tenants: int = 0
    orphan_libraries: int = 0
    orphan_files: int = 0
    bytes_freed: int = 0
    skipped_libraries: int = 0
    errors: list[str] = []
    dry_run: bool = True
    # Skipped because Upkeep is paused, not "nothing to do"; with the admin key, which accounts.
    paused: bool = False
    paused_tenants: list[str] = Field(default_factory=list)


class FacePropagateResult(BaseModel):
    assigned: int = 0
    scanned: int = 0
    # Skipped because Upkeep is paused, not "nothing to do"; with the admin key, which accounts.
    paused: bool = False
    paused_tenants: list[str] = Field(default_factory=list)


class TrashPurgeResult(BaseModel):
    """Deleted for good because they'd been in the trash longer than the account's trash days."""
    clips: int = 0
    libraries: int = 0
    projects: int = 0
    # Skipped because Upkeep is paused, not "nothing to do"; with the admin key, which accounts.
    paused: bool = False
    paused_tenants: list[str] = Field(default_factory=list)


class MissingFilesResult(BaseModel):
    """Artifact files a request found gone (missing_artifacts): checked again,
    and the keys of those still gone cleared, for the producers to make again."""
    checked: int = 0
    cleared: int = 0
    # Skipped because Upkeep is paused, not "nothing to do"; with the admin key, which accounts.
    paused: bool = False
    paused_tenants: list[str] = Field(default_factory=list)


class FaceClustersResult(BaseModel):
    """Accounts whose face clusters were computed again because faces changed
    since (GET /v1/faces/clusters only reads them)."""
    computed: int = 0


class DismissedPeopleResult(BaseModel):
    """Empty dismissed people deleted (a faces redo can leave them)."""
    deleted: int = 0
    paused: bool = False
    paused_tenants: list[str] = Field(default_factory=list)


class UpkeepResult(BaseModel):
    search_sync: SearchSyncResult
    face_propagate: FacePropagateResult = FacePropagateResult()
    trash_purge: TrashPurgeResult = TrashPurgeResult()
    missing_files: MissingFilesResult = MissingFilesResult()
    dismissed_people: DismissedPeopleResult = DismissedPeopleResult()
    face_clusters: FaceClustersResult = FaceClustersResult()


def _bearer(authorization: str | None) -> str | None:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    return authorization[7:].strip() or None


def _is_admin_key(token: str) -> bool:
    """The operator's admin key (ADMIN_KEY), not an account's key."""
    import hmac

    settings = get_settings()
    return bool(settings.admin_key and hmac.compare_digest(token, settings.admin_key))


def _tenant_for_key(token: str) -> tuple[str, str, str] | None:
    """(tenant_id, connection_string, role) for a live tenant API key, or None.

    Upkeep routes skip the tenant middleware, so handlers resolve the
    tenant from the key themselves.
    """
    from src.server.database import get_control_session
    from src.server.repository.control_plane import ApiKeyRepository, TenantDbRoutingRepository

    with get_control_session() as ctrl:
        api_key = ApiKeyRepository(ctrl).get_by_plaintext(token)
        if api_key is None:
            return None
        routing = TenantDbRoutingRepository(ctrl).get_by_tenant_id(api_key.tenant_id)
        if routing is None:
            return None
        return api_key.tenant_id, routing.connection_string, api_key.role


@dataclass(frozen=True)
class UpkeepScope:
    """Whom an upkeep call acts on: one account (its admin's key), or every
    account (the operator's admin key, with tenants=all)."""

    all_tenants: bool
    tenant_id: str | None = None
    connection_string: str | None = None


def upkeep_scope(
    authorization: Annotated[str | None, Header()] = None,
    tenants: Annotated[
        Literal["all"] | None,
        Query(description="With the operator's admin key: 'all', every account. Required with that key."),
    ] = None,
) -> UpkeepScope:
    """Who may run upkeep, and on what. Nothing acts on every account from a
    missing argument (Robert): the admin key must say tenants=all.

    401: no key, or one that isn't live. 403: an account key that isn't an
    admin's, or one sending tenants=all. 400: the admin key without tenants=all.
    """
    token = _bearer(authorization)
    if token is None:
        raise HTTPException(status_code=401, detail="Admin key or tenant API key required")
    if _is_admin_key(token):
        if tenants != "all":
            raise HTTPException(
                status_code=400,
                detail="The admin key acts on every account: say so with tenants=all",
            )
        return UpkeepScope(all_tenants=True)
    tenant = _tenant_for_key(token)
    if tenant is None:
        raise HTTPException(status_code=401, detail="Invalid or revoked API key")
    tenant_id, connection_string, role = tenant
    if role != "admin":
        raise HTTPException(status_code=403, detail="Admin API key required")
    if tenants is not None:
        raise HTTPException(status_code=403, detail="tenants=all needs the operator's admin key")
    return UpkeepScope(all_tenants=False, tenant_id=tenant_id, connection_string=connection_string)


Scope = Annotated[UpkeepScope, Depends(upkeep_scope)]


def _tenant_session(scope: UpkeepScope):
    from sqlmodel import Session as TenantSession

    from src.server.database import get_engine_for_url

    return TenantSession(get_engine_for_url(scope.connection_string))


def _upkeep_paused(session) -> bool:
    """An admin paused the account's Upkeep switch: upkeep that changes data
    waits; search sync goes on (Robert, Oct 9)."""
    from src.server.repository import lineage

    return lineage.upkeep_paused(session)


def _skipped(what: str, tenant_id: str, paused: list[str] | None = None) -> dict:
    """An account's upkeep skipped for its paused Upkeep switch: logged, and said in the result
    (paused), so it reads apart from a run with nothing to do."""
    logger.info("%s for tenant %s: upkeep is paused", what, tenant_id)
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
                if _upkeep_paused(session):
                    _skipped("Face names aren't spread", tenant.tenant_id, paused)
                    continue
                result = FaceRepository(session).propagate_assignments()
                totals["assigned"] += result["assigned"]
                totals["scanned"] += result["scanned"]
        except Exception as exc:
            logger.warning("Face propagation failed for tenant %s: %s", tenant.tenant_id, exc)

    return {**totals, "paused": bool(paused), "paused_tenants": paused}


def _propagate_faces_single_tenant(scope: UpkeepScope) -> dict:
    """Run face propagation for one account."""
    from src.server.repository.tenant import FaceRepository

    with _tenant_session(scope) as session:
        if _upkeep_paused(session):
            return {"assigned": 0, "scanned": 0, **_skipped("Face names aren't spread", scope.tenant_id)}
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
                if _upkeep_paused(session):
                    _skipped("The expired trash isn't emptied", tenant.tenant_id, paused)
                    continue
                for key, n in purge_expired_trash(session, tenant.tenant_id).items():
                    totals[key] += n
        except Exception as exc:
            logger.warning("Trash purge failed for tenant %s: %s", tenant.tenant_id, exc)
    return {**totals, "paused": bool(paused), "paused_tenants": paused}


def _purge_expired_trash_single_tenant(scope: UpkeepScope) -> dict:
    """Empty one account's expired trash."""
    from src.server.api.routers.trash import purge_expired_trash

    try:
        with _tenant_session(scope) as session:
            if _upkeep_paused(session):
                return _skipped("The expired trash isn't emptied", scope.tenant_id)
            return purge_expired_trash(session, scope.tenant_id)
    except Exception as exc:
        logger.warning("Trash purge failed for tenant %s: %s", scope.tenant_id, exc)
        return {}


def _each_account(scope: UpkeepScope, what: str, work, *, pausable: bool) -> dict:
    """Run work(session) -> dict of counts for each account of the scope and
    add them up. pausable: an account whose Upkeep switch is paused is skipped."""
    from src.server.database import get_tenant_session

    totals: dict[str, int] = {}
    paused: list[str] = []
    for tenant_id in _tenant_ids(scope):
        try:
            with (get_tenant_session(tenant_id) if scope.all_tenants else _tenant_session(scope)) as session:
                if pausable and _upkeep_paused(session):
                    _skipped(what, tenant_id, paused)
                    continue
                for key, n in work(session).items():
                    totals[key] = totals.get(key, 0) + n
        except Exception as exc:
            logger.warning("%s failed for tenant %s: %s", what, tenant_id, exc)
    return {**totals, **({"paused": True, "paused_tenants": paused} if paused else {})}


def _repair_missing_files(session) -> dict:
    from src.server import missing_artifacts

    return missing_artifacts.repair(session)


def _cleanup_dismissed(session) -> dict:
    from src.server.repository.tenant import PersonRepository

    return {"deleted": PersonRepository(session).cleanup_empty_dismissed()}


def _recluster_if_changed(session) -> dict:
    """Compute the face clusters again when faces changed since they were."""
    from src.server.api.routers.people import cached_clusters, clusters_current, store_clusters

    if clusters_current(session, cached_clusters(session)):
        return {"computed": 0}
    store_clusters(session)
    return {"computed": 1}


def _reset_search_synced_at(session) -> None:
    """Clear the sync times of all assets, scenes and transcripts to force full re-index."""
    from src.server.search.sync import REINDEX_RESETS

    for reset in REINDEX_RESETS.values():
        session.execute(text(reset))
    session.commit()


def _run_sweep_all_tenants(force: bool = False) -> dict:
    """Run search sync sweep across all tenants, aggregating results."""
    from src.server.database import get_control_session, get_tenant_session
    from src.server.repository.control_plane import TenantRepository
    from src.server.search.sync import run_search_sync_sweep

    totals = {"synced": 0, "failed": 0, "scenes_synced": 0, "scenes_failed": 0,
              "transcripts_synced": 0, "transcripts_failed": 0}

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


def _run_sweep_single_tenant(scope: UpkeepScope, force: bool = False) -> dict:
    """Run search sync sweep for one account."""
    from src.server.search.sync import run_search_sync_sweep

    with _tenant_session(scope) as session:
        if force:
            _reset_search_synced_at(session)
        return run_search_sync_sweep(session, tenant_id=scope.tenant_id)


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


def _tenant_ids(scope: UpkeepScope) -> list[str]:
    """The accounts a call acts on."""
    if not scope.all_tenants:
        return [scope.tenant_id]
    from src.server.database import get_control_session
    from src.server.repository.control_plane import TenantRepository

    with get_control_session() as ctrl:
        return [t.tenant_id for t in TenantRepository(ctrl).list_all()]


@router.post("", response_model=UpkeepResult)
def run_upkeep(scope: Scope) -> UpkeepResult:
    """Run all periodic upkeep tasks: search sync, face propagation,
    deleting for good what has been in the trash past the trash days,
    repairing artifact files reported missing, deleting empty dismissed
    people, and computing the face clusters again when faces changed since they were.

    An account admin's key: that account. The admin key with tenants=all:
    every account, and old revoked tokens are dropped.
    """
    if scope.all_tenants:
        sync_result = _run_sweep_all_tenants()
        prop_result = _propagate_faces_all_tenants()
        purge_result = _purge_expired_trash_all_tenants()
        _cleanup_revoked_tokens()
    else:
        sync_result = _run_sweep_single_tenant(scope)
        prop_result = _propagate_faces_single_tenant(scope)
        purge_result = _purge_expired_trash_single_tenant(scope)
    # After the face names spread, so the clusters leave out what they named.
    missing = _each_account(scope, "Missing files aren't repaired", _repair_missing_files, pausable=True)
    dismissed = _each_account(scope, "Empty dismissed people aren't deleted", _cleanup_dismissed, pausable=True)
    clusters = _each_account(scope, "Face clusters", _recluster_if_changed, pausable=False)
    return UpkeepResult(
        search_sync=SearchSyncResult(**sync_result),
        face_propagate=FacePropagateResult(**prop_result),
        trash_purge=TrashPurgeResult(**purge_result),
        missing_files=MissingFilesResult(**missing),
        dismissed_people=DismissedPeopleResult(**dismissed),
        face_clusters=FaceClustersResult(**clusters),
    )


@router.post("/search-sync", response_model=SearchSyncResult)
def run_search_sync(
    scope: Scope,
    force: bool = Query(default=False, description="Reset all search_synced_at timestamps and re-index everything"),
) -> SearchSyncResult:
    """Run search sync sweep, for one account or (tenants=all) every account.

    force=true: clears the sync times of all assets, scenes and transcripts so everything is re-indexed.
    """
    if scope.all_tenants:
        result = _run_sweep_all_tenants(force=force)
    else:
        result = _run_sweep_single_tenant(scope, force=force)
    return SearchSyncResult(**result)


@router.post("/cleanup", response_model=CleanupResultModel)
def run_cleanup(
    scope: Scope,
    dry_run: bool = True,
    library_id: str | None = Query(default=None, description="One library of the account (account keys only)."),
    all_: bool = Query(default=False, alias="all", description="The whole account (account keys only)."),
) -> CleanupResultModel:
    """Run filesystem cleanup to remove orphaned files left after trash is emptied.

    dry_run=true (default): report what would be deleted without deleting.
    dry_run=false: actually delete orphaned files.

    An account admin's key: one library of the account (library_id; 404 when
    it isn't the account's) or the whole account (all=true); neither or both
    is a 400. The admin key with tenants=all: every account, and the
    directories of accounts that are gone.
    """
    from src.server.search.cleanup import run_cleanup_all_tenants, run_cleanup_single_tenant

    if scope.all_tenants:
        if library_id is not None or all_:
            raise HTTPException(status_code=400, detail="library_id and all need an account's key, not tenants=all")
        result = run_cleanup_all_tenants(dry_run=dry_run)
    else:
        require_scope(library_id is not None, all_, "library_id")
        with _tenant_session(scope) as session:
            if library_id is not None and session.execute(
                text("SELECT 1 FROM libraries WHERE library_id = :id"), {"id": library_id},
            ).first() is None:
                raise HTTPException(status_code=404, detail="Library not found")
            result = run_cleanup_single_tenant(scope.tenant_id, session, dry_run=dry_run, library_id=library_id)

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
def run_recluster(scope: Scope) -> ReclusterResult:
    """Force recompute face clusters, for one account or (tenants=all) every account."""
    from src.server.api.routers.people import store_clusters

    def recluster(session) -> dict:
        clusters = store_clusters(session)["clusters"]
        return {"clusters": len(clusters), "total_faces": sum(c["size"] for c in clusters)}

    return ReclusterResult(**_each_account(scope, "Recluster", recluster, pausable=False))


class RecreateSearchIndexesResult(BaseModel):
    tenants_processed: int = 0
    errors: list[str] = []


@router.post("/recreate-search-indexes", response_model=RecreateSearchIndexesResult)
def recreate_search_indexes(scope: Scope) -> RecreateSearchIndexesResult:
    """Wipe and recreate Quickwit indexes. Used to clean up duplicate
    documents accumulated by repeated force-resyncs (Quickwit doesn't
    upsert, so each force-resync used to leave another copy of every
    doc until the dedupe gate landed). After calling this, the caller
    MUST also call POST /v1/upkeep/search-sync to repopulate the empty
    indexes.

    An account admin's key wipes that account's indexes; the admin key
    with tenants=all wipes every account's. Destructive, no undo, but
    the data can be rebuilt from Postgres via the search-sync sweep.
    """
    from src.server.database import get_tenant_session
    from src.server.search.quickwit_client import QuickwitClient

    qw = QuickwitClient()
    if not qw.enabled:
        return RecreateSearchIndexesResult(
            tenants_processed=0, errors=["Quickwit disabled"],
        )

    tenant_ids = _tenant_ids(scope)
    errors: list[str] = []
    processed = 0
    for tenant_id in tenant_ids:
        try:
            qw.recreate_tenant_indexes(tenant_id)
            processed += 1
        except Exception as exc:
            errors.append(f"{tenant_id}: {exc}")
            logger.warning("Recreate Quickwit indexes failed for %s: %s", tenant_id, exc)

    # Marked for a reindex: the next search-sync sweep clears the sync times and does the work.
    from src.server.search.sync import REINDEX_RESETS, mark_reindex

    for tenant_id in tenant_ids:
        try:
            with get_tenant_session(tenant_id) as tsession:
                mark_reindex(tsession, list(REINDEX_RESETS))
        except Exception as exc:
            logger.warning("Failed to mark tenant %s's search for a reindex: %s", tenant_id, exc)

    return RecreateSearchIndexesResult(tenants_processed=processed, errors=errors)


@router.post("/cleanup-dismissed", response_model=CleanupDismissedResult)
def run_cleanup_dismissed(scope: Scope) -> CleanupDismissedResult:
    """Delete dismissed people that have zero face matches, for one account
    or (tenants=all) every account."""
    from src.server.database import get_tenant_session
    from src.server.repository.tenant import PersonRepository

    total_deleted = 0

    for tenant_id in _tenant_ids(scope):
        try:
            with get_tenant_session(tenant_id) as tsession:
                deleted = PersonRepository(tsession).cleanup_empty_dismissed()
                if deleted:
                    logger.info("Cleaned up %d empty dismissed people for tenant %s",
                                deleted, tenant_id)
                total_deleted += deleted
        except Exception as exc:
            logger.warning("Cleanup dismissed failed for tenant %s: %s", tenant_id, exc)

    return CleanupDismissedResult(deleted=total_deleted)
