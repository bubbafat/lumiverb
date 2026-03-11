"""Repository classes for the control plane. No ORM calls outside these classes."""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime

from sqlmodel import Session, select

from src.core.utils import utcnow
from src.models.control_plane import ApiKey, Tenant, TenantDbRouting
from ulid import ULID


class TenantRepository:
    """Repository for tenants table."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, name: str, plan: str = "free") -> Tenant:
        """Generate tenant_id as ten_ + ulid(), insert, return the new Tenant."""
        tenant_id = "ten_" + str(ULID())
        tenant = Tenant(
            tenant_id=tenant_id,
            name=name,
            plan=plan,
            status="active",
        )
        self._session.add(tenant)
        self._session.commit()
        self._session.refresh(tenant)
        return tenant

    def get_by_id(self, tenant_id: str) -> Tenant | None:
        """Return tenant by id or None."""
        stmt = select(Tenant).where(Tenant.tenant_id == tenant_id)
        return self._session.exec(stmt).first()

    def list_all(self) -> list[Tenant]:
        """Return all tenants."""
        stmt = select(Tenant)
        return list(self._session.exec(stmt).all())

    def update_status(self, tenant_id: str, status: str) -> Tenant:
        """Update tenant status and return the tenant."""
        tenant = self.get_by_id(tenant_id)
        if tenant is None:
            raise ValueError(f"Tenant not found: {tenant_id}")
        tenant.status = status
        self._session.add(tenant)
        self._session.commit()
        self._session.refresh(tenant)
        return tenant

    def delete(self, tenant_id: str) -> None:
        """Delete tenant record (for cleanup on provisioning failure)."""
        tenant = self.get_by_id(tenant_id)
        if tenant is not None:
            self._session.delete(tenant)
            self._session.commit()


class ApiKeyRepository:
    """Repository for api_keys table."""

    def __init__(self, session: Session) -> None:
        self._session = session

    @staticmethod
    def _hash_key(plaintext: str) -> str:
        return hashlib.sha256(plaintext.encode()).hexdigest()

    def create(
        self,
        tenant_id: str,
        name: str,
        scopes: list[str] | None = None,
    ) -> tuple[ApiKey, str]:
        """
        Generate key as lv_ + secrets.token_urlsafe(32), store SHA256 hash,
        return (ApiKey, plaintext_key). Plaintext is returned ONCE and never stored.
        """
        if scopes is None:
            scopes = ["read", "write"]
        plaintext = "lv_" + secrets.token_urlsafe(32)
        key_hash = self._hash_key(plaintext)
        key_id = "key_" + str(ULID())
        api_key = ApiKey(
            key_id=key_id,
            key_hash=key_hash,
            tenant_id=tenant_id,
            name=name,
            scopes=scopes,
        )
        self._session.add(api_key)
        self._session.commit()
        self._session.refresh(api_key)
        return api_key, plaintext

    def get_by_hash(self, key_hash: str) -> ApiKey | None:
        """Return API key by hash, excluding revoked."""
        stmt = (
            select(ApiKey)
            .where(ApiKey.key_hash == key_hash)
            .where(ApiKey.revoked_at.is_(None))
        )
        return self._session.exec(stmt).first()

    def get_by_plaintext(self, plaintext: str) -> ApiKey | None:
        """Hash the plaintext and call get_by_hash."""
        return self.get_by_hash(self._hash_key(plaintext))

    def revoke(self, key_id: str) -> ApiKey:
        """Set revoked_at and return the key."""
        stmt = select(ApiKey).where(ApiKey.key_id == key_id)
        api_key = self._session.exec(stmt).first()
        if api_key is None:
            raise ValueError(f"API key not found: {key_id}")
        api_key.revoked_at = utcnow()
        self._session.add(api_key)
        self._session.commit()
        self._session.refresh(api_key)
        return api_key

    def touch_last_used(self, key_id: str) -> None:
        """Update last_used_at. Best-effort; do not raise on failure."""
        stmt = select(ApiKey).where(ApiKey.key_id == key_id)
        api_key = self._session.exec(stmt).first()
        if api_key is not None:
            api_key.last_used_at = utcnow()
            self._session.add(api_key)
            try:
                self._session.commit()
            except Exception:
                self._session.rollback()

    def list_by_tenant_id(self, tenant_id: str) -> list[ApiKey]:
        """List all API keys for a tenant (for revoking all on delete)."""
        stmt = select(ApiKey).where(ApiKey.tenant_id == tenant_id)
        return list(self._session.exec(stmt).all())

    def delete_by_tenant_id(self, tenant_id: str) -> None:
        """Delete all API keys for a tenant (for cleanup on provisioning failure)."""
        for key in self.list_by_tenant_id(tenant_id):
            self._session.delete(key)
        self._session.commit()


class TenantDbRoutingRepository:
    """Repository for tenant_db_routing table."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        tenant_id: str,
        connection_string: str,
        region: str = "local",
    ) -> TenantDbRouting:
        """Create a routing entry for a tenant."""
        row = TenantDbRouting(
            tenant_id=tenant_id,
            connection_string=connection_string,
            region=region,
        )
        self._session.add(row)
        self._session.commit()
        self._session.refresh(row)
        return row

    def get_by_tenant_id(self, tenant_id: str) -> TenantDbRouting | None:
        """Return routing entry by tenant_id or None."""
        stmt = select(TenantDbRouting).where(TenantDbRouting.tenant_id == tenant_id)
        return self._session.exec(stmt).first()

    def delete_by_tenant_id(self, tenant_id: str) -> None:
        """Delete routing entry for tenant (for cleanup on provisioning failure)."""
        row = self.get_by_tenant_id(tenant_id)
        if row is not None:
            self._session.delete(row)
            self._session.commit()
