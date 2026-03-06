"""Admin API: tenant provisioning and management."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from src.api.dependencies import get_db_session, require_admin
from src.core.config import get_settings
from src.core.database import provision_tenant_database
from src.repository.control_plane import (
    ApiKeyRepository,
    TenantDbRoutingRepository,
    TenantRepository,
)

router = APIRouter(prefix="/v1/admin", tags=["admin"])


class CreateTenantRequest(BaseModel):
    name: str
    plan: str = "free"  # free | pro | enterprise
    email: str = ""


class CreateTenantResponse(BaseModel):
    tenant_id: str
    api_key: str
    database: str = "provisioned"


class TenantListItem(BaseModel):
    tenant_id: str
    name: str
    plan: str
    status: str


@router.post("/tenants", response_model=CreateTenantResponse)
def create_tenant(
    body: CreateTenantRequest,
    _: Annotated[None, Depends(require_admin)],
    session: Annotated[Session, Depends(get_db_session)],
) -> CreateTenantResponse:
    """
    Create tenant, provision DB, add routing, create default API key.
    On failure, attempt cleanup and return 500.
    """
    tenant_repo = TenantRepository(session)
    routing_repo = TenantDbRoutingRepository(session)
    key_repo = ApiKeyRepository(session)
    settings = get_settings()

    try:
        tenant = tenant_repo.create(name=body.name, plan=body.plan)
        tenant_id = tenant.tenant_id
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create tenant: {e}") from e

    try:
        provision_tenant_database(tenant_id)
    except Exception as e:
        _cleanup_tenant(tenant_id, tenant_repo, key_repo, routing_repo)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to provision tenant database: {e}",
        ) from e

    try:
        connection_string = settings.tenant_database_url_template.format(tenant_id=tenant_id)
        routing_repo.create(tenant_id=tenant_id, connection_string=connection_string)
    except Exception as e:
        _cleanup_tenant(tenant_id, tenant_repo, key_repo, routing_repo)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create routing: {e}",
        ) from e

    try:
        api_key_row, plaintext_key = key_repo.create(
            tenant_id=tenant_id,
            name="default",
            scopes=["read", "write"],
        )
    except Exception as e:
        _cleanup_tenant(tenant_id, tenant_repo, key_repo, routing_repo)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create API key: {e}",
        ) from e

    return CreateTenantResponse(
        tenant_id=tenant_id,
        api_key=plaintext_key,
        database="provisioned",
    )


def _cleanup_tenant(
    tenant_id: str,
    tenant_repo: TenantRepository,
    key_repo: ApiKeyRepository,
    routing_repo: TenantDbRoutingRepository,
) -> None:
    """Remove tenant and related rows (for rollback on provisioning failure)."""
    try:
        key_repo.delete_by_tenant_id(tenant_id)
    except Exception:
        pass
    try:
        routing_repo.delete_by_tenant_id(tenant_id)
    except Exception:
        pass
    try:
        tenant_repo.delete(tenant_id)
    except Exception:
        pass


@router.get("/tenants", response_model=list[TenantListItem])
def list_tenants(
    _: Annotated[None, Depends(require_admin)],
    session: Annotated[Session, Depends(get_db_session)],
) -> list[TenantListItem]:
    """Return all tenants with status and plan. No API keys in response."""
    tenant_repo = TenantRepository(session)
    tenants = tenant_repo.list_all()
    return [
        TenantListItem(
            tenant_id=t.tenant_id,
            name=t.name,
            plan=t.plan,
            status=t.status,
        )
        for t in tenants
    ]


@router.delete("/tenants/{tenant_id}", status_code=204)
def delete_tenant(
    tenant_id: str,
    _: Annotated[None, Depends(require_admin)],
    session: Annotated[Session, Depends(get_db_session)],
) -> None:
    """Soft delete: set tenant status to 'deleted' and revoke all API keys."""
    tenant_repo = TenantRepository(session)
    key_repo = ApiKeyRepository(session)
    tenant = tenant_repo.get_by_id(tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    for key in key_repo.list_by_tenant_id(tenant_id):
        key_repo.revoke(key.key_id)
    tenant_repo.update_status(tenant_id, "deleted")
    return None
