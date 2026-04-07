"""Repository classes for the control plane. No ORM calls outside these classes."""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime

from sqlmodel import Session, select

from src.shared.utils import utcnow
from src.server.models.control_plane import ApiKey, PasswordResetToken, PublicLibrary, RevokedToken, Tenant, TenantDbRouting, User
from ulid import ULID


class TenantRepository:
    """Repository for tenants table."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        name: str,
        plan: str = "free",
        vision_api_url: str = "",
        vision_api_key: str = "",
    ) -> Tenant:
        """Generate tenant_id as ten_ + ulid(), insert, return the new Tenant."""
        tenant_id = "ten_" + str(ULID())
        tenant = Tenant(
            tenant_id=tenant_id,
            name=name,
            plan=plan,
            status="active",
            vision_api_url=vision_api_url,
            vision_api_key=vision_api_key,
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
        # Preserve existing SHA256 hashing scheme for compatibility.
        return hashlib.sha256(plaintext.encode()).hexdigest()

    @staticmethod
    def _generate_plaintext() -> str:
        """
        Generate plaintext key as lv_ + ULID() + random suffix.

        ULID prefix ensures sortable, non-guessable IDs while keeping the
        existing lv_ prefix for callers.
        """
        ulid_part = str(ULID())
        random_part = secrets.token_urlsafe(16)
        return f"lv_{ulid_part}_{random_part}"

    def create(
        self,
        tenant_id: str,
        label: str | None,
        role: str = "admin",
    ) -> tuple[ApiKey, str]:
        """
        Create a new API key for a tenant.

        Generates a new lv_ ULID-prefixed key, hashes it, inserts the row, and
        returns (record, plaintext). Plaintext is returned ONCE and never stored.
        """
        plaintext = self._generate_plaintext()
        key_hash = self._hash_key(plaintext)
        key_id = "key_" + str(ULID())
        api_key = ApiKey(
            key_id=key_id,
            key_hash=key_hash,
            tenant_id=tenant_id,
            name=label or "default",
            label=label,
            scopes=["read", "write"],
            role=role,
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

    def revoke(self, key_id: str, tenant_id: str | None = None) -> bool:
        """
        Set revoked_at for the given key_id (and optional tenant_id).

        Returns False if not found or already revoked; True on successful revoke.
        """
        stmt = select(ApiKey).where(ApiKey.key_id == key_id)
        if tenant_id is not None:
            stmt = stmt.where(ApiKey.tenant_id == tenant_id)
        api_key = self._session.exec(stmt).first()
        if api_key is None or api_key.revoked_at is not None:
            return False
        api_key.revoked_at = utcnow()
        self._session.add(api_key)
        self._session.commit()
        self._session.refresh(api_key)
        return True

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

    def list_by_tenant(self, tenant_id: str) -> list[ApiKey]:
        """
        List all non-revoked API keys for a tenant, ordered by created_at ASC.

        Plaintext is never returned; only metadata.
        """
        stmt = (
            select(ApiKey)
            .where(ApiKey.tenant_id == tenant_id)
            .where(ApiKey.revoked_at.is_(None))
            .order_by(ApiKey.created_at.asc())
        )
        return list(self._session.exec(stmt).all())

    def list_by_tenant_id(self, tenant_id: str) -> list[ApiKey]:
        """List all API keys for a tenant (including revoked)."""
        stmt = select(ApiKey).where(ApiKey.tenant_id == tenant_id)
        return list(self._session.exec(stmt).all())

    def count_admin_keys(self, tenant_id: str) -> int:
        """Return the number of non-revoked admin keys for a tenant."""
        stmt = (
            select(ApiKey)
            .where(ApiKey.tenant_id == tenant_id)
            .where(ApiKey.revoked_at.is_(None))
            .where(ApiKey.role == "admin")
        )
        return len(list(self._session.exec(stmt).all()))

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


class UserRepository:
    """Repository for users table."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, tenant_id: str, email: str, password_hash: str, role: str = "viewer") -> User:
        from ulid import ULID
        user = User(
            user_id="usr_" + str(ULID()),
            tenant_id=tenant_id,
            email=email,
            password_hash=password_hash,
            role=role,
        )
        self._session.add(user)
        self._session.commit()
        self._session.refresh(user)
        return user

    def get_by_email(self, email: str) -> User | None:
        stmt = select(User).where(User.email == email)
        return self._session.exec(stmt).first()

    def get_by_id(self, user_id: str) -> User | None:
        return self._session.get(User, user_id)

    def list_by_tenant(self, tenant_id: str) -> list[User]:
        stmt = select(User).where(User.tenant_id == tenant_id).order_by(User.created_at.asc())
        return list(self._session.exec(stmt).all())

    def count_admins(self, tenant_id: str) -> int:
        stmt = select(User).where(User.tenant_id == tenant_id).where(User.role == "admin")
        return len(list(self._session.exec(stmt).all()))

    def count_admins_locked(self, tenant_id: str) -> int:
        """Count admins with FOR UPDATE lock to serialize concurrent last-admin checks."""
        stmt = (
            select(User)
            .where(User.tenant_id == tenant_id)
            .where(User.role == "admin")
            .with_for_update()
        )
        return len(list(self._session.exec(stmt).all()))

    def update_role(self, user_id: str, role: str) -> User:
        user = self.get_by_id(user_id)
        if user is None:
            raise ValueError(f"User not found: {user_id}")
        user.role = role
        self._session.add(user)
        self._session.commit()
        self._session.refresh(user)
        return user

    def update_last_login(self, user_id: str) -> None:
        user = self.get_by_id(user_id)
        if user is not None:
            user.last_login_at = utcnow()
            self._session.add(user)
            try:
                self._session.commit()
            except Exception:
                self._session.rollback()

    def update_password(self, user_id: str, password_hash: str) -> None:
        user = self.get_by_id(user_id)
        if user is None:
            raise ValueError(f"User not found: {user_id}")
        user.password_hash = password_hash
        self._session.add(user)
        self._session.commit()

    def delete(self, user_id: str) -> None:
        user = self.get_by_id(user_id)
        if user is not None:
            self._session.delete(user)
            self._session.commit()


class PasswordResetTokenRepository:
    """Repository for password_reset_tokens table."""

    def __init__(self, session: Session) -> None:
        self._session = session

    @staticmethod
    def _hash_token(plaintext: str) -> str:
        return hashlib.sha256(plaintext.encode()).hexdigest()

    def create(self, user_id: str, plaintext_token: str, expires_at: datetime) -> PasswordResetToken:
        token = PasswordResetToken(
            token_hash=self._hash_token(plaintext_token),
            user_id=user_id,
            expires_at=expires_at,
        )
        self._session.add(token)
        self._session.commit()
        self._session.refresh(token)
        return token

    def get_by_plaintext(self, plaintext_token: str) -> PasswordResetToken | None:
        token_hash = self._hash_token(plaintext_token)
        return self._session.get(PasswordResetToken, token_hash)

    def mark_used(self, token_hash: str) -> None:
        token = self._session.get(PasswordResetToken, token_hash)
        if token is not None:
            token.used_at = utcnow()
            self._session.add(token)
            self._session.commit()

    def consume_valid_and_update_password(self, plaintext_token: str, password_hash: str) -> bool:
        """
        Atomically consume a valid reset token and update the user's password.

        Returns True when the token was valid and consumed; False when invalid,
        expired, or already used.
        """
        token_hash = self._hash_token(plaintext_token)
        now = utcnow()

        stmt = (
            select(PasswordResetToken)
            .where(PasswordResetToken.token_hash == token_hash)
            .where(PasswordResetToken.used_at.is_(None))
            .where(PasswordResetToken.expires_at > now)
            .with_for_update()
        )
        token = self._session.exec(stmt).first()
        if token is None:
            return False

        user = self._session.get(User, token.user_id)
        if user is None:
            self._session.rollback()
            return False

        user.password_hash = password_hash
        token.used_at = now
        self._session.add(user)
        self._session.add(token)
        self._session.commit()
        return True


class PublicLibraryRepository:
    """Repository for public_libraries control plane table."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, library_id: str) -> PublicLibrary | None:
        return self._session.get(PublicLibrary, library_id)

    def upsert(self, library_id: str, tenant_id: str, connection_string: str) -> None:
        row = PublicLibrary(
            library_id=library_id,
            tenant_id=tenant_id,
            connection_string=connection_string,
        )
        self._session.merge(row)
        self._session.commit()

    def delete(self, library_id: str) -> None:
        row = self._session.get(PublicLibrary, library_id)
        if row:
            self._session.delete(row)
            self._session.commit()


class PublicCollectionRepository:
    """Repository for public_collections control plane table."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, collection_id: str) -> "PublicCollection | None":
        from src.server.models.control_plane import PublicCollection
        return self._session.get(PublicCollection, collection_id)

    def upsert(self, collection_id: str, tenant_id: str, connection_string: str) -> None:
        from src.server.models.control_plane import PublicCollection
        row = PublicCollection(
            collection_id=collection_id,
            tenant_id=tenant_id,
            connection_string=connection_string,
        )
        self._session.merge(row)
        self._session.commit()

    def delete(self, collection_id: str) -> None:
        from src.server.models.control_plane import PublicCollection
        row = self._session.get(PublicCollection, collection_id)
        if row:
            self._session.delete(row)
            self._session.commit()


class RevokedTokenRepository:
    """Repository for JWT revocation (logout / token refresh)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def revoke(self, jti: str) -> None:
        """Mark a JWT as revoked."""
        if self._session.get(RevokedToken, jti):
            return  # already revoked
        self._session.add(RevokedToken(jti=jti))
        self._session.commit()

    def is_revoked(self, jti: str) -> bool:
        return self._session.get(RevokedToken, jti) is not None

    def cleanup_expired(self, before: "datetime") -> int:
        """Delete revoked tokens older than the given timestamp (housekeeping)."""
        from sqlalchemy import text
        result = self._session.execute(
            text("DELETE FROM revoked_tokens WHERE revoked_at < :cutoff"),
            {"cutoff": before},
        )
        self._session.commit()
        return result.rowcount
