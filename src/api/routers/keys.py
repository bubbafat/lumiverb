from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from src.api.dependencies import require_tenant_admin
from src.api.middleware import _error_response
from src.core.database import get_control_session
from src.repository.control_plane import ApiKeyRepository


router = APIRouter(prefix="/v1/keys", tags=["keys"])


class KeyItem(BaseModel):
    key_id: str
    label: str | None
    is_admin: bool
    last_used_at: str | None
    created_at: str


class KeyListResponse(BaseModel):
    keys: list[KeyItem]


class CreateKeyRequest(BaseModel):
    label: str | None = None
    is_admin: bool = False


class CreateKeyResponse(BaseModel):
    key_id: str
    label: str | None
    is_admin: bool
    plaintext: str
    created_at: str


@router.get("", response_model=KeyListResponse)
def list_keys(request: Request) -> KeyListResponse:
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
                is_admin=getattr(k, "is_admin", False),
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
    _: Annotated[None, Depends(require_tenant_admin)],
) -> CreateKeyResponse:
    """Create a new key for the current tenant. Returns plaintext exactly once."""
    tenant_id = getattr(request.state, "tenant_id", None)
    if not tenant_id:
        raise HTTPException(status_code=500, detail="Tenant context missing")

    with get_control_session() as session:
        repo = ApiKeyRepository(session)
        api_key, plaintext = repo.create(
            tenant_id=tenant_id,
            label=body.label,
            is_admin=body.is_admin,
        )

    created_at = api_key.created_at.isoformat()
    return CreateKeyResponse(
        key_id=api_key.key_id,
        label=getattr(api_key, "label", None),
        is_admin=getattr(api_key, "is_admin", False),
        plaintext=plaintext,
        created_at=created_at,
    )


@router.delete("/{key_id}", status_code=204)
def revoke_key(
    key_id: str,
    request: Request,
    _: Annotated[None, Depends(require_tenant_admin)],
) -> None:
    """
    Revoke a key for the current tenant.

    Rules:
    1. A key cannot revoke itself (409).
    2. The last admin key cannot be revoked (409, code=last_admin_key).
    """
    tenant_id = getattr(request.state, "tenant_id", None)
    current_key_id = getattr(request.state, "key_id", None)
    if not tenant_id or not current_key_id:
        raise HTTPException(status_code=500, detail="Tenant context missing")

    if key_id == current_key_id:
        # Cannot revoke the key used for this request.
        raise HTTPException(status_code=409, detail="A key cannot revoke itself")

    with get_control_session() as session:
        repo = ApiKeyRepository(session)

        # Load the target key first so we can inspect is_admin.
        target = repo.get_by_hash  # type: ignore[assignment]  # placeholder to satisfy type checkers
        # Fetch by key_id and tenant_id directly via the session.
        from sqlmodel import select
        from src.models.control_plane import ApiKey

        stmt = select(ApiKey).where(ApiKey.key_id == key_id, ApiKey.tenant_id == tenant_id)
        target = session.exec(stmt).first()
        if target is None or target.revoked_at is not None:
            raise HTTPException(status_code=404, detail="Key not found")

        # Enforce "last admin key" constraint only when revoking an admin key.
        if getattr(target, "is_admin", False):
            admin_count = repo.count_admin_keys(tenant_id)
            if admin_count <= 1:
                return _error_response(
                    409,
                    "last_admin_key",
                    "Cannot revoke the last remaining admin key for this tenant",
                )

        ok = repo.revoke(key_id=key_id, tenant_id=tenant_id)
        if not ok:
            # Should not happen given the checks above; treat as 404.
            raise HTTPException(status_code=404, detail="Key not found")

    return None

