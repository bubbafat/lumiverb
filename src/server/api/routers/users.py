"""User management endpoints: list, create (an invite, or with a password),
a new invite link, update role, delete. Invites: src/server/invites.py."""

from __future__ import annotations

import bcrypt
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from src.server.api.dependencies import require_tenant_admin
from src.server.api.errors import ApiError, ConflictError
from src.server.api.middleware import _error_response
from src.server.database import get_control_session, get_engine_for_url
from src.server.api.routers.auth import MAX_PASSWORD_BYTES
from src.server.api.routers.keys import _ROLE_RANK as ROLE_RANK
from src.server.repository.control_plane import ApiKeyRepository, UserRepository
from src.server.repository.library_roles import LibraryRoleRepository
from src.server.repository.ratings import RatingRepository
from src.server.repository.views import SavedViewRepository
from src.server.invites import Invite, new_invite, unusable_password_hash

router = APIRouter(prefix="/v1/users", tags=["users"])

VALID_ROLES = {"admin", "editor", "viewer"}
MIN_PASSWORD_LENGTH = 12


class UserItem(BaseModel):
    user_id: str
    email: str
    role: str
    # Their account role on every library but those set otherwise; false: only the libraries
    # they're given a role on (ADR-018 decision 8).
    every_library: bool = True
    # A server admin (the install's): only another server admin invites, changes or deletes them.
    server_admin: bool = False
    created_at: str
    last_login_at: str | None
    # Made just now by an invite: the link to send them (the same link at each
    # address other people open Lumiverb at, the likeliest first), and until when
    invite_urls: list[str] | None = None
    invite_expires_at: str | None = None


class CreateUserRequest(BaseModel):
    email: str
    password: str | None = None  # none: an invite, so they choose their own
    role: str = "viewer"
    every_library: bool = True


class InviteResponse(BaseModel):
    invite_urls: list[str]
    invite_expires_at: str


class UpdateRoleRequest(BaseModel):
    # Either or both; one left out stays as it is.
    role: str | None = None
    every_library: bool | None = None


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _within_reach(request: Request, user) -> None:
    """A server admin is out of an account admin's reach: whoever holds their
    password has the install, so only a server admin makes them a new invite
    link, changes them or deletes them (request.state.server_admin, as they
    are now). 403 server_admin_user for anyone else, a key included."""
    if user.server_admin and getattr(request.state, "server_admin", False) is not True:
        raise ApiError(403, "server_admin_user",
                       f"{user.email} is a server admin: only a server admin can do that.")


def _user_item(user, invite: Invite | None = None) -> UserItem:
    return UserItem(
        user_id=user.user_id,
        email=user.email,
        role=user.role,
        every_library=user.every_library,
        server_admin=user.server_admin,
        created_at=_iso(user.created_at) or "",
        last_login_at=_iso(user.last_login_at),
        invite_urls=list(invite.urls) if invite else None,
        invite_expires_at=_iso(invite.expires_at) if invite else None,
    )


@router.get("", response_model=list[UserItem])
def list_users(
    request: Request,
    _: Annotated[None, Depends(require_tenant_admin)],
) -> list[UserItem]:
    """Return all users for the current tenant."""
    tenant_id = getattr(request.state, "tenant_id", None)
    if not tenant_id:
        raise HTTPException(status_code=500, detail="Tenant context missing")

    with get_control_session() as session:
        repo = UserRepository(session)
        users = repo.list_by_tenant(tenant_id)

    return [_user_item(u) for u in users]


@router.post("", response_model=UserItem, status_code=201)
def create_user(
    request: Request,
    body: CreateUserRequest,
    _: Annotated[None, Depends(require_tenant_admin)],
) -> UserItem:
    """Create a new user for the current tenant: without a password, an
    invite (the link to send them comes back); with one, ready to sign in."""
    tenant_id = getattr(request.state, "tenant_id", None)
    if not tenant_id:
        raise HTTPException(status_code=500, detail="Tenant context missing")

    if body.role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"Invalid role; must be one of: {', '.join(sorted(VALID_ROLES))}")

    if body.password is None:
        password_hash = unusable_password_hash()
    else:
        if len(body.password) < MIN_PASSWORD_LENGTH:
            raise HTTPException(status_code=400, detail=f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
        if len(body.password.encode()) > MAX_PASSWORD_BYTES:
            raise HTTPException(status_code=400, detail=f"Password must be at most {MAX_PASSWORD_BYTES} bytes")
        password_hash = bcrypt.hashpw(body.password.encode(), bcrypt.gensalt(rounds=12)).decode()

    with get_control_session() as session:
        repo = UserRepository(session)
        existing = repo.get_by_email(body.email)
        if existing is not None:
            return _error_response(409, "email_conflict", "Email already registered")
        user = repo.create(
            tenant_id=tenant_id,
            email=body.email,
            password_hash=password_hash,
            role=body.role,
            every_library=body.every_library,
        )
        invite = new_invite(session, user.user_id) if body.password is None else None
        return _user_item(user, invite)  # in the session: the invite's commit expired the user's fields


@router.post("/{user_id}/invite", response_model=InviteResponse)
def new_invite_link(
    user_id: str,
    request: Request,
    _: Annotated[None, Depends(require_tenant_admin)],
) -> InviteResponse:
    """A new link for someone who hasn't signed in yet; their old one stops
    working. A server admin's only by another (403 server_admin_user)."""
    tenant_id = getattr(request.state, "tenant_id", None)
    if not tenant_id:
        raise HTTPException(status_code=500, detail="Tenant context missing")

    with get_control_session() as session:
        user = UserRepository(session).get_by_id(user_id)
        if user is None or user.tenant_id != tenant_id:
            raise HTTPException(status_code=404, detail="User not found")
        _within_reach(request, user)
        if user.last_login_at is not None:
            raise ApiError(409, "already_joined",
                           f"{user.email} has signed in already. If they've forgotten their password, "
                           "they can use Forgot password.")
        invite = new_invite(session, user_id)

    return InviteResponse(invite_urls=list(invite.urls), invite_expires_at=_iso(invite.expires_at) or "")


@router.patch("/{user_id}", response_model=UserItem)
def update_user_role(
    user_id: str,
    request: Request,
    body: UpdateRoleRequest,
    _: Annotated[None, Depends(require_tenant_admin)],
) -> UserItem:
    """Update a user's role, and whether they see every library or only the
    ones they're given (every_library). Enforces the last-admin invariant. A
    server admin's only by another (403 server_admin_user)."""
    tenant_id = getattr(request.state, "tenant_id", None)
    if not tenant_id:
        raise HTTPException(status_code=500, detail="Tenant context missing")

    if body.role is not None and body.role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"Invalid role; must be one of: {', '.join(sorted(VALID_ROLES))}")

    with get_control_session() as session:
        repo = UserRepository(session)
        user = repo.get_by_id(user_id)
        if user is None or user.tenant_id != tenant_id:
            raise HTTPException(status_code=404, detail="User not found")
        _within_reach(request, user)
        updated = user

        if body.role is not None:
            if user.role == "admin" and body.role != "admin":
                if repo.count_admins_locked(tenant_id) <= 1:
                    return _error_response(409, "last_admin", "Cannot demote the last admin")

            # Their keys mustn't keep the role they lose.
            above = {r for r, rank in ROLE_RANK.items() if rank > ROLE_RANK[body.role]}
            ApiKeyRepository(session).revoke_created_by(tenant_id, user_id, roles=above)
            updated = repo.update_role(user_id, body.role)
        if body.every_library is not None:
            updated = repo.set_every_library(user_id, body.every_library)
        return _user_item(updated)


@router.delete("/{user_id}", status_code=204)
def delete_user(
    user_id: str,
    request: Request,
    _: Annotated[None, Depends(require_tenant_admin)],
) -> None:
    """Delete a user. Enforces the last-admin invariant and blocks self-deletion.
    A server admin only by another (403 server_admin_user), and never the
    last one who can sign in (409 last_server_admin, as removing one as a
    server admin: UserRepository.keeps_a_server_admin)."""
    tenant_id = getattr(request.state, "tenant_id", None)
    caller_user_id = getattr(request.state, "user_id", None)
    if not tenant_id:
        raise HTTPException(status_code=500, detail="Tenant context missing")

    if caller_user_id and user_id == caller_user_id:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")

    with get_control_session() as session:
        repo = UserRepository(session)
        user = repo.get_by_id(user_id)
        if user is None or user.tenant_id != tenant_id:
            raise HTTPException(status_code=404, detail="User not found")
        _within_reach(request, user)

        if user.role == "admin":
            if repo.count_admins_locked(tenant_id) <= 1:
                return _error_response(409, "last_admin", "Cannot remove the last admin")
        # Held locked until the delete is committed (keeps_a_server_admin).
        if user.server_admin and not repo.keeps_a_server_admin(without_user=user_id):
            raise ConflictError("last_server_admin", "They're the last server admin: name another first.")

        ApiKeyRepository(session).revoke_created_by(tenant_id, user_id)
        repo.delete(user_id)

    # Clean up user's ratings in tenant DB
    connection_string = getattr(request.state, "connection_string", None)
    if connection_string:
        from sqlmodel import Session as TenantSession

        engine = get_engine_for_url(connection_string)
        with TenantSession(engine) as tenant_session:
            rating_repo = RatingRepository(tenant_session)
            rating_repo.delete_for_user(user_id)
            views_repo = SavedViewRepository(tenant_session)
            views_repo.delete_for_user(user_id)
            LibraryRoleRepository(tenant_session).forget(user_id)
            tenant_session.commit()

    return None
