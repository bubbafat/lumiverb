"""User management endpoints: list, create, update role, delete."""

from __future__ import annotations

import bcrypt
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from src.api.dependencies import require_tenant_admin
from src.api.middleware import _error_response
from src.core.database import get_control_session
from src.repository.control_plane import UserRepository

router = APIRouter(prefix="/v1/users", tags=["users"])

VALID_ROLES = {"admin", "editor", "viewer"}


class UserItem(BaseModel):
    user_id: str
    email: str
    role: str
    created_at: str
    last_login_at: str | None


class CreateUserRequest(BaseModel):
    email: str
    password: str
    role: str = "viewer"


class UpdateRoleRequest(BaseModel):
    role: str


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _user_item(user) -> UserItem:
    return UserItem(
        user_id=user.user_id,
        email=user.email,
        role=user.role,
        created_at=_iso(user.created_at) or "",
        last_login_at=_iso(user.last_login_at),
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
    """Create a new user for the current tenant."""
    tenant_id = getattr(request.state, "tenant_id", None)
    if not tenant_id:
        raise HTTPException(status_code=500, detail="Tenant context missing")

    if body.role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"Invalid role; must be one of: {', '.join(sorted(VALID_ROLES))}")

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
        )

    return _user_item(user)


@router.patch("/{user_id}", response_model=UserItem)
def update_user_role(
    user_id: str,
    request: Request,
    body: UpdateRoleRequest,
    _: Annotated[None, Depends(require_tenant_admin)],
) -> UserItem:
    """Update a user's role. Enforces the last-admin invariant."""
    tenant_id = getattr(request.state, "tenant_id", None)
    if not tenant_id:
        raise HTTPException(status_code=500, detail="Tenant context missing")

    if body.role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"Invalid role; must be one of: {', '.join(sorted(VALID_ROLES))}")

    with get_control_session() as session:
        repo = UserRepository(session)
        user = repo.get_by_id(user_id)
        if user is None or user.tenant_id != tenant_id:
            raise HTTPException(status_code=404, detail="User not found")

        if user.role == "admin" and body.role != "admin":
            if repo.count_admins(tenant_id) <= 1:
                return _error_response(409, "last_admin", "Cannot demote the last admin")

        updated = repo.update_role(user_id, body.role)

    return _user_item(updated)


@router.delete("/{user_id}", status_code=204)
def delete_user(
    user_id: str,
    request: Request,
    _: Annotated[None, Depends(require_tenant_admin)],
) -> None:
    """Delete a user. Enforces the last-admin invariant and blocks self-deletion."""
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

        if user.role == "admin":
            if repo.count_admins(tenant_id) <= 1:
                return _error_response(409, "last_admin", "Cannot remove the last admin")

        repo.delete(user_id)

    return None
