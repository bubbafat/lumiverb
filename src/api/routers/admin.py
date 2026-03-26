"""Admin API: tenant provisioning and management."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from src.api.dependencies import get_db_session, require_admin
from src.core.config import get_settings
from src.core.database import deprovision_tenant_database, provision_tenant_database
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
    vision_api_url: str = ""
    vision_api_key: str = ""


class CreateTenantResponse(BaseModel):
    tenant_id: str
    api_key: str
    database: str = "provisioned"


class TenantListItem(BaseModel):
    tenant_id: str
    name: str
    plan: str
    status: str


class UpdateTenantRequest(BaseModel):
    vision_api_url: str | None = None
    vision_api_key: str | None = None
    vision_model_id: str | None = None


class UpdateTenantResponse(BaseModel):
    tenant_id: str
    vision_api_url: str
    vision_model_id: str = ""


class CreateTenantKeyRequest(BaseModel):
    name: str


class CreateTenantKeyResponse(BaseModel):
    api_key: str
    name: str
    tenant_id: str


class KeyMetadataItem(BaseModel):
    name: str
    tenant_id: str
    created_at: str


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
        tenant = tenant_repo.create(
            name=body.name,
            plan=body.plan,
            vision_api_url=body.vision_api_url,
            vision_api_key=body.vision_api_key,
        )
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
            label="default",
            role="admin",
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
    try:
        deprovision_tenant_database(tenant_id)
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


@router.patch("/tenants/{tenant_id}", response_model=UpdateTenantResponse)
def update_tenant(
    tenant_id: str,
    body: UpdateTenantRequest,
    _: Annotated[None, Depends(require_admin)],
    session: Annotated[Session, Depends(get_db_session)],
) -> UpdateTenantResponse:
    """Update tenant vision API config. Only provided fields are changed."""
    tenant_repo = TenantRepository(session)
    tenant = tenant_repo.get_by_id(tenant_id)
    if tenant is None or tenant.status == "deleted":
        raise HTTPException(status_code=404, detail="Tenant not found")
    if body.vision_api_url is not None:
        tenant.vision_api_url = body.vision_api_url
    if body.vision_api_key is not None:
        tenant.vision_api_key = body.vision_api_key
    if body.vision_model_id is not None:
        tenant.vision_model_id = body.vision_model_id
    session.add(tenant)
    session.commit()
    session.refresh(tenant)
    return UpdateTenantResponse(
        tenant_id=tenant.tenant_id,
        vision_api_url=tenant.vision_api_url,
        vision_model_id=tenant.vision_model_id,
    )


@router.post("/tenants/{tenant_id}/keys", response_model=CreateTenantKeyResponse)
def create_tenant_key(
    tenant_id: str,
    body: CreateTenantKeyRequest,
    _: Annotated[None, Depends(require_admin)],
    session: Annotated[Session, Depends(get_db_session)],
) -> CreateTenantKeyResponse:
    """
    Create a new API key for a tenant. Returns the raw key once; it is never stored.
    Returns 404 if the tenant does not exist or is soft-deleted.
    """
    tenant_repo = TenantRepository(session)
    key_repo = ApiKeyRepository(session)
    tenant = tenant_repo.get_by_id(tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    if tenant.status == "deleted":
        raise HTTPException(status_code=404, detail="Tenant not found")

    api_key_row, plaintext_key = key_repo.create(
        tenant_id=tenant_id,
        label=body.name,
        role="admin",
    )
    return CreateTenantKeyResponse(
        api_key=plaintext_key,
        name=body.name,
        tenant_id=tenant_id,
    )


@router.get("/tenants/{tenant_id}/keys", response_model=list[KeyMetadataItem])
def list_tenant_keys(
    tenant_id: str,
    _: Annotated[None, Depends(require_admin)],
    session: Annotated[Session, Depends(get_db_session)],
) -> list[KeyMetadataItem]:
    """
    List API key metadata for a tenant. Never returns raw keys.
    Returns 404 if the tenant does not exist or is soft-deleted.
    """
    tenant_repo = TenantRepository(session)
    key_repo = ApiKeyRepository(session)
    tenant = tenant_repo.get_by_id(tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    if tenant.status == "deleted":
        raise HTTPException(status_code=404, detail="Tenant not found")

    keys = key_repo.list_by_tenant_id(tenant_id)
    return [
        KeyMetadataItem(
            name=k.name,
            tenant_id=k.tenant_id,
            created_at=k.created_at.isoformat(),
        )
        for k in keys
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
