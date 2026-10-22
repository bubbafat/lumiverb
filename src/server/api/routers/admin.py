"""The server admin's API (ADR-018 decision 8): the install's accounts, and
who its server admins are. A server admin who signed in, or the install's
own key (ADMIN_KEY, for its scripts and the CLI's `lumiverb admin`):
dependencies.require_server_admin. The web app's Admin › Accounts is a
server admin's: an account made with its first admin's invite, renamed, or
deleted once what it holds is said and its name typed. A deleted account is
kept and never reached again: nobody signs in to it, its keys and public
links answer nothing, and it isn't worked on (TenantRepository.gone)."""

import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlmodel import Session

from src.server.accounts import AccountError, create_account, remove_account
from src.server.api.dependencies import get_db_session, require_server_admin
from src.server.api.errors import ApiError, ConflictError, DecisionRequiredError
from src.server.repository.control_plane import (
    DELETED,
    SCHEDULER_LABEL_TAKEN,
    ApiKeyRepository,
    TenantRepository,
    UserRepository,
    scheduler_label,
)
from src.shared.path_filter import has_control_character

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/admin", tags=["admin"], dependencies=[Depends(require_server_admin)])


def _plain_name(name: str) -> str:
    name = name.strip()
    if not name or has_control_character(name):
        raise ValueError("a name, with no control characters")
    return name


class CreateTenantRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    plan: str = "free"  # free | pro | enterprise
    # Its first admin, invited: they choose their password at the link that comes back. Empty: none (a script's,
    # which uses the key that comes back).
    email: str = Field(default="", max_length=320)
    vision_api_url: str = ""
    vision_api_key: str = ""

    @field_validator("name")
    @classmethod
    def _plain(cls, v: str) -> str:
        return _plain_name(v)


class CreateTenantResponse(BaseModel):
    tenant_id: str
    api_key: str
    database: str = "provisioned"
    # With an email: its first admin's invite (shown once), as a user's invite is (routers/users.py).
    invite_urls: list[str] | None = None
    invite_expires_at: datetime | None = None


class TenantListItem(BaseModel):
    tenant_id: str
    name: str
    plan: str
    status: str
    users: int = 0  # the people who sign in to it
    created_at: datetime | None = None


class UpdateTenantRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)  # renamed
    vision_api_url: str | None = None
    vision_api_key: str | None = None
    vision_model_id: str | None = None

    @field_validator("name")
    @classmethod
    def _plain(cls, v: str | None) -> str | None:
        return None if v is None else _plain_name(v)


class UpdateTenantResponse(BaseModel):
    tenant_id: str
    name: str = ""
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
    Create tenant, provision DB, add routing, create default API key; with
    an email, its first admin too, invited (the link comes back, once).
    409 email_conflict when someone signs in with that email already.
    On failure, attempt cleanup and return 500.
    """
    email = body.email.strip()
    if email and UserRepository(session).get_by_email(email) is not None:
        raise ConflictError("email_conflict", "Someone signs in with that email already.", {"email": email})
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

    invite = None
    if email:
        from src.server.invites import new_invite, unusable_password_hash

        user = UserRepository(session).create(tenant_id, email, unusable_password_hash(), role="admin")
        invite = new_invite(session, user.user_id)
    return CreateTenantResponse(
        tenant_id=tenant_id,
        api_key=plaintext_key,
        database="provisioned",
        invite_urls=list(invite.urls) if invite else None,
        invite_expires_at=invite.expires_at if invite else None,
    )


@router.get("/tenants", response_model=list[TenantListItem])
def list_tenants(
    session: Annotated[Session, Depends(get_db_session)],
) -> list[TenantListItem]:
    """Return all tenants with status and plan. No API keys in response."""
    tenant_repo = TenantRepository(session)
    tenants = tenant_repo.list_all()
    people = UserRepository(session).count_by_tenant()
    return [
        TenantListItem(
            tenant_id=t.tenant_id,
            name=t.name,
            plan=t.plan,
            status=t.status,
            users=people.get(t.tenant_id, 0),
            created_at=t.created_at,
        )
        for t in tenants
    ]


@router.patch("/tenants/{tenant_id}", response_model=UpdateTenantResponse)
def update_tenant(
    tenant_id: str,
    body: UpdateTenantRequest,
    session: Annotated[Session, Depends(get_db_session)],
) -> UpdateTenantResponse:
    """Rename an account, or change its vision endpoint and model. Only
    provided fields are changed. The endpoint is its first machine doing
    vision (/v1/ai): an empty URL removes that machine; another makes or
    moves it."""
    from src.server.repository.ai_machines import job_model, update_vision

    tenant_repo = TenantRepository(session)
    tenant = tenant_repo.get_by_id(tenant_id)
    if tenant is None or tenant.status == DELETED:
        raise HTTPException(status_code=404, detail="Tenant not found")
    if body.name is not None:
        tenant_repo.rename(tenant_id, body.name)
        tenant = tenant_repo.get_by_id(tenant_id)
    machine = update_vision(session, tenant, api_url=body.vision_api_url, api_key=body.vision_api_key,
                            model=body.vision_model_id)
    return UpdateTenantResponse(
        tenant_id=tenant.tenant_id,
        name=tenant.name,
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
    Returns 404 if the tenant does not exist or is soft-deleted; 400
    scheduler_label for the name of Lumiverb's own key, whatever its case.
    """
    tenant_repo = TenantRepository(session)
    key_repo = ApiKeyRepository(session)
    tenant = tenant_repo.get_by_id(tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    if tenant.status == "deleted":
        raise HTTPException(status_code=404, detail="Tenant not found")
    if scheduler_label(body.name):
        raise ApiError(400, "scheduler_label", SCHEDULER_LABEL_TAKEN)

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


def _holds(tenant_id: str, users: int) -> dict:
    """What an account holds, said before it's deleted: its people, libraries and clips."""
    from src.server.database import get_tenant_session
    from src.server.repository.assets import AssetRepository
    from src.server.repository.libraries import LibraryRepository

    try:
        with get_tenant_session(tenant_id) as tenant_session:
            ids = [lib.library_id for lib in LibraryRepository(tenant_session).list_all()]
            clips = AssetRepository(tenant_session).count_all_for_libraries(ids) if ids else 0
    except Exception as exc:  # noqa: BLE001 — its database away: what it holds isn't known, and it's said so
        logger.warning("Account %s's database can't be read: %s", tenant_id, exc)
        return {"users": users, "libraries": None, "clips": None}
    return {"users": users, "libraries": len(ids), "clips": clips}


@router.delete("/tenants/{tenant_id}", status_code=204)
def delete_tenant(
    tenant_id: str,
    request: Request,
    session: Annotated[Session, Depends(get_db_session)],
    confirm: str | None = None,
) -> None:
    """Delete an account: kept (its database, its rows), never reached again: nobody signs in to it, its keys are
    revoked, its public links answer nothing, and it isn't worked on. 409 confirm_delete_account (details: name,
    users, libraries, clips; null where its database can't be read) until confirm is its name, as typed. 409
    own_account for the account of the server admin asking; 409 last_server_admin when no server admin would be
    left in an account not deleted. 404 for no such account, or one deleted already."""
    tenant_repo = TenantRepository(session)
    key_repo = ApiKeyRepository(session)
    tenant = tenant_repo.get_by_id(tenant_id)
    if tenant is None or tenant.status == DELETED:
        raise HTTPException(status_code=404, detail="Tenant not found")
    if getattr(request.state, "tenant_id", None) == tenant_id and not getattr(request.state, "install_key", False):
        raise ConflictError("own_account", "That's the account you're signed in to: another server admin deletes it.")
    users = UserRepository(session)
    # The server admins are held (keeps_a_server_admin) until the account's deletion is committed.
    if not users.keeps_a_server_admin(without_tenant=tenant_id) and any(
            u.tenant_id == tenant_id for u in users.server_admins()):
        raise ConflictError("last_server_admin", "No server admin would be left in an account that isn't deleted: "
                            "name one in another account first.")
    if confirm != tenant.name:
        session.rollback()  # they aren't held while what it holds is counted
        held = _holds(tenant_id, users.count_by_tenant().get(tenant_id, 0))
        raise DecisionRequiredError(
            "confirm_delete_account",
            f"{tenant.name} will be deleted: nobody signs in to it again. Send confirm: its name, to go ahead.",
            {"name": tenant.name, **held})
    key_repo.revoke_all(tenant_id)
    tenant_repo.update_status(tenant_id, DELETED)  # commits the keys with it
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


class ServerAdminNamed(ServerAdmins):
    # Their links not yet used (an invite, a reset) that ended with it: only a
    # server admin gives a server admin a link (UserRepository.name_server_admin).
    links_ended: int = 0


@router.post("/server-admins", response_model=ServerAdminNamed)
def name_server_admin(body: NameServerAdmin,
                      session: Annotated[Session, Depends(get_db_session)]) -> ServerAdminNamed:
    """Make someone who signs in here a server admin (one already is: nothing
    changes); their links not yet used end with it. 404 user_not_found when
    nobody here has that email. Then the server admins, and how many links
    ended."""
    user = UserRepository(session).get_by_email(body.email.strip())
    if user is None:
        raise ApiError(404, "user_not_found", "Nobody signs in here with that email.")
    ended = UserRepository(session).name_server_admin(user.user_id)
    return ServerAdminNamed(**_server_admins(session).model_dump(), links_ended=ended)


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
