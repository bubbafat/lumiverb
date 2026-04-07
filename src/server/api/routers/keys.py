from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from src.server.api.dependencies import require_editor
from src.server.api.middleware import _error_response
from src.server.database import get_control_session
from src.server.repository.control_plane import ApiKeyRepository


router = APIRouter(prefix="/v1/keys", tags=["keys"])


class KeyItem(BaseModel):
    key_id: str
    label: str | None
    role: str
    last_used_at: str | None
    created_at: str


class KeyListResponse(BaseModel):
    keys: list[KeyItem]


_ROLE_RANK = {"admin": 2, "editor": 1, "viewer": 0}


class CreateKeyRequest(BaseModel):
    label: str | None = None
    role: str | None = None


class CreateKeyResponse(BaseModel):
    key_id: str
    label: str | None
    role: str
    plaintext: str
    created_at: str


@router.get("", response_model=KeyListResponse)
def list_keys(
    request: Request,
    _: Annotated[None, Depends(require_editor)],
) -> KeyListResponse:
    """Return all non-revoked keys for the current tenant. Never includes plaintext."""
    tenant_id = getattr(request.state, "tenant_id", None)
    if not tenant_id:
        raise HTTPException(status_code=500, detail="Tenant context missing")

    with get_control_session() as session:
        repo = ApiKeyRepository(session)
        keys = repo.list_by_tenant(tenant_id)

    def _iso(dt: datetime | None) -> str | None:
        return dt.isoformat() if dt is not None else None

    return KeyListResponse(
        keys=[
            KeyItem(
                key_id=k.key_id,
                label=getattr(k, "label", None),
                role=getattr(k, "role", "viewer"),
                last_used_at=_iso(k.last_used_at),
                created_at=_iso(k.created_at) or "",
            )
            for k in keys
        ]
    )


@router.post("", response_model=CreateKeyResponse)
def create_key(
    request: Request,
    body: CreateKeyRequest,
    _: Annotated[None, Depends(require_editor)],
) -> CreateKeyResponse:
    """Create a new key for the current tenant. Returns plaintext exactly once."""
    tenant_id = getattr(request.state, "tenant_id", None)
    if not tenant_id:
        raise HTTPException(status_code=500, detail="Tenant context missing")

    caller_role = getattr(request.state, "role", "viewer")
    requested_role = body.role or caller_role

    if requested_role not in _ROLE_RANK:
        return _error_response(400, "invalid_role", f"Invalid role: {requested_role}")
    if _ROLE_RANK[requested_role] > _ROLE_RANK[caller_role]:
        return _error_response(403, "role_escalation", "Cannot create a key with higher privileges than your own")

    with get_control_session() as session:
        repo = ApiKeyRepository(session)
        api_key, plaintext = repo.create(
            tenant_id=tenant_id,
            label=body.label,
            role=requested_role,
        )

    created_at = api_key.created_at.isoformat()
    return CreateKeyResponse(
        key_id=api_key.key_id,
        label=getattr(api_key, "label", None),
        role=getattr(api_key, "role", "viewer"),
        plaintext=plaintext,
        created_at=created_at,
    )


@router.delete("/{key_id}", status_code=204)
def revoke_key(
    key_id: str,
    request: Request,
) -> None:
    """
    Revoke a key for the current tenant.

    Rules:
    1. A key cannot revoke itself (409).
    2. The last admin key cannot be revoked (409, code=last_admin_key) — checked
       before the role gate so that the constraint is visible regardless of
       the caller's role.
    3. Only admin or editor may revoke keys (403).
    """
    tenant_id = getattr(request.state, "tenant_id", None)
    if not tenant_id:
        raise HTTPException(status_code=500, detail="Tenant context missing")

    # key_id is only set for API key auth; None for JWT users.
    current_key_id: str | None = getattr(request.state, "key_id", None)

    with get_control_session() as session:
        repo = ApiKeyRepository(session)

        from sqlmodel import select
        from src.server.models.control_plane import ApiKey

        stmt = select(ApiKey).where(ApiKey.key_id == key_id, ApiKey.tenant_id == tenant_id)
        target = session.exec(stmt).first()
        if target is None or target.revoked_at is not None:
            raise HTTPException(status_code=404, detail="Key not found")

        # 1. Check "last admin key" before the role gate so that this hard
        #    constraint surfaces as 409 regardless of the caller's role.
        if getattr(target, "role", "viewer") == "admin":
            admin_count = repo.count_admin_keys(tenant_id)
            if admin_count <= 1:
                return _error_response(
                    409,
                    "last_admin_key",
                    "Cannot revoke the last remaining admin key for this tenant",
                )

        # 2. Enforce editor+ after the last_admin_key constraint check.
        caller_role = getattr(request.state, "role", None)
        if caller_role not in ("admin", "editor"):
            raise HTTPException(status_code=403, detail="Editor access required")

        # 3. A key cannot revoke itself (only relevant for API key auth).
        if current_key_id and key_id == current_key_id:
            raise HTTPException(status_code=409, detail="A key cannot revoke itself")

        ok = repo.revoke(key_id=key_id, tenant_id=tenant_id)
        if not ok:
            # Should not happen given the checks above; treat as 404.
            raise HTTPException(status_code=404, detail="Key not found")

    return None

