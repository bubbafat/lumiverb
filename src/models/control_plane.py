"""Control plane database models: tenants, api_keys, tenant_db_routing."""

from __future__ import annotations

from datetime import datetime, timezone
from sqlalchemy import Column, DateTime
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Tenant(SQLModel, table=True):
    __tablename__ = "tenants"

    tenant_id: str = Field(primary_key=True)
    name: str = Field(nullable=False)
    plan: str = Field(default="free", nullable=False)
    status: str = Field(default="active", nullable=False)
    created_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class ApiKey(SQLModel, table=True):
    __tablename__ = "api_keys"

    key_id: str = Field(primary_key=True)
    key_hash: str = Field(nullable=False, unique=True)
    tenant_id: str = Field(foreign_key="tenants.tenant_id", nullable=False)
    name: str = Field(nullable=False)
    scopes: list[str] = Field(
        default=["read", "write"],
        sa_column=Column(JSONB, nullable=False),
    )
    created_at: datetime = Field(
        default_factory=_utcnow,
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
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
