"""The server admin's API (ADR-018 decision 8): the install's accounts, and
who its server admins are. A server admin who signed in, or the install's
own key (ADMIN_KEY, for its scripts and the CLI's `lumiverb admin`):
dependencies.require_server_admin."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from src.server.accounts import AccountError, create_account, remove_account
from src.server.api.dependencies import get_db_session, require_server_admin
from src.server.api.errors import ApiError, ConflictError
from src.server.repository.control_plane import (
    ApiKeyRepository,
    TenantRepository,
    UserRepository,
)

router = APIRouter(prefix="/v1/admin", tags=["admin"], dependencies=[Depends(require_server_admin)])


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
    session: Annotated[Session, Depends(get_db_session)],
) -> CreateTenantResponse:
    """
    Create tenant, provision DB, add routing, create default API key.
    On failure, attempt cleanup and return 500.
    """
    try:
        tenant_id = create_account(
            session, body.name, plan=body.plan,
            vision_api_url=body.vision_api_url, vision_api_key=body.vision_api_key,
        )
    except AccountError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    try:
        _, plaintext_key = ApiKeyRepository(session).create(
            tenant_id=tenant_id,
            label="default",
            role="admin",
        )
    except Exception as e:
        remove_account(session, tenant_id)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create API key: {e}",
        ) from e

    return CreateTenantResponse(
        tenant_id=tenant_id,
        api_key=plaintext_key,
        database="provisioned",
    )


@router.get("/tenants", response_model=list[TenantListItem])
def list_tenants(
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
    session: Annotated[Session, Depends(get_db_session)],
) -> UpdateTenantResponse:
    """Update a tenant's vision endpoint and model. Only provided fields are
    changed. The endpoint is its first machine doing vision (/v1/ai): an
    empty URL removes that machine; another makes or moves it."""
    from src.server.repository.ai_machines import job_model, update_vision

    tenant_repo = TenantRepository(session)
    tenant = tenant_repo.get_by_id(tenant_id)
    if tenant is None or tenant.status == "deleted":
        raise HTTPException(status_code=404, detail="Tenant not found")
    machine = update_vision(session, tenant, api_url=body.vision_api_url, api_key=body.vision_api_key,
                            model=body.vision_model_id)
    return UpdateTenantResponse(
        tenant_id=tenant.tenant_id,
        vision_api_url=machine.api_url if machine is not None else "",
        vision_model_id=job_model(tenant, "vision"),
    )


@router.post("/tenants/{tenant_id}/keys", response_model=CreateTenantKeyResponse)
def create_tenant_key(
    tenant_id: str,
    body: CreateTenantKeyRequest,
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


class ServerAdmin(BaseModel):
    user_id: str
    email: str
    tenant_id: str
    account: str  # their account's name


class ServerAdmins(BaseModel):
    server_admins: list[ServerAdmin]


class NameServerAdmin(BaseModel):
    email: str  # anyone's with a sign-in here, in any account


def _server_admins(session: Session) -> ServerAdmins:
    names = {t.tenant_id: t.name for t in TenantRepository(session).list_all()}
    return ServerAdmins(server_admins=[
        ServerAdmin(user_id=u.user_id, email=u.email, tenant_id=u.tenant_id, account=names.get(u.tenant_id, ""))
        for u in UserRepository(session).server_admins()])


@router.get("/server-admins", response_model=ServerAdmins)
def list_server_admins(session: Annotated[Session, Depends(get_db_session)]) -> ServerAdmins:
    """The install's server admins, by email."""
    return _server_admins(session)


@router.post("/server-admins", response_model=ServerAdmins)
def name_server_admin(body: NameServerAdmin, session: Annotated[Session, Depends(get_db_session)]) -> ServerAdmins:
    """Make someone who signs in here a server admin (one already is: nothing
    changes). 404 user_not_found when nobody here has that email. Then the
    server admins."""
    user = UserRepository(session).get_by_email(body.email.strip())
    if user is None:
        raise ApiError(404, "user_not_found", "Nobody signs in here with that email.")
    UserRepository(session).set_server_admin(user.user_id, True)
    return _server_admins(session)


@router.delete("/server-admins/{user_id}", status_code=204)
def remove_server_admin(user_id: str, session: Annotated[Session, Depends(get_db_session)]) -> None:
    """They're no longer a server admin, at once (their sign-in and their
    account stay). 404 when they aren't one; 409 last_server_admin for the
    last one: name another first (on the machine itself,
    `lumiverb-server server-admins remove` may)."""
    user = UserRepository(session).get_by_id(user_id)
    if user is None or not user.server_admin:
        raise HTTPException(status_code=404, detail="Server admin not found")
    if not UserRepository(session).set_server_admin(user_id, False, keep_one=True):
        raise ConflictError("last_server_admin",
                            "They're the last server admin: name another first.")
