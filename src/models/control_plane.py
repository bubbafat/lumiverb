"""Control plane database models: tenants, api_keys, tenant_db_routing, users."""

from __future__ import annotations

from datetime import datetime
from sqlalchemy import Column, DateTime
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from src.core.utils import utcnow


class Tenant(SQLModel, table=True):
    __tablename__ = "tenants"

    tenant_id: str = Field(primary_key=True)
    name: str = Field(nullable=False)
    plan: str = Field(default="free", nullable=False)
    status: str = Field(default="active", nullable=False)
    vision_api_url: str = Field(default="", nullable=False)
    vision_api_key: str = Field(default="", nullable=False)
    vision_model_id: str = Field(default="", nullable=False)
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class ApiKey(SQLModel, table=True):
    __tablename__ = "api_keys"

    key_id: str = Field(primary_key=True)
    key_hash: str = Field(nullable=False, unique=True)
    tenant_id: str = Field(foreign_key="tenants.tenant_id", nullable=False)
    name: str = Field(nullable=False)
    # Optional human-readable label for the key. For new features, prefer label over name.
    label: str | None = Field(default=None, nullable=True)
    scopes: list[str] = Field(
        default=["read", "write"],
        sa_column=Column(JSONB, nullable=False),
    )
    role: str = Field(default="admin", nullable=False)
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    last_used_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    revoked_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )


class TenantDbRouting(SQLModel, table=True):
    __tablename__ = "tenant_db_routing"

    tenant_id: str = Field(primary_key=True, foreign_key="tenants.tenant_id")
    connection_string: str = Field(nullable=False)
    region: str = Field(default="local", nullable=False)
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class User(SQLModel, table=True):
    __tablename__ = "users"

    user_id: str = Field(primary_key=True)
    tenant_id: str = Field(foreign_key="tenants.tenant_id", nullable=False)
    email: str = Field(nullable=False, unique=True)
    password_hash: str = Field(nullable=False)
    role: str = Field(default="viewer", nullable=False)
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    last_login_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )


class PasswordResetToken(SQLModel, table=True):
    __tablename__ = "password_reset_tokens"

    token_hash: str = Field(primary_key=True)  # SHA-256 of the emailed token
    user_id: str = Field(foreign_key="users.user_id", nullable=False)
    expires_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    used_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )


class RevokedToken(SQLModel, table=True):
    __tablename__ = "revoked_tokens"

    jti: str = Field(primary_key=True)  # JWT ID — unique per token
    revoked_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class PublicLibrary(SQLModel, table=True):
    __tablename__ = "public_libraries"

    library_id: str = Field(primary_key=True)
    tenant_id: str = Field(foreign_key="tenants.tenant_id", nullable=False)
    connection_string: str = Field(nullable=False)
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class PublicCollection(SQLModel, table=True):
    __tablename__ = "public_collections"

    collection_id: str = Field(primary_key=True)
    tenant_id: str = Field(foreign_key="tenants.tenant_id", nullable=False)
    connection_string: str = Field(nullable=False)
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
