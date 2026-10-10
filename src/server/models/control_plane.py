"""Control plane database models: tenants, api_keys, tenant_db_routing, users."""

from __future__ import annotations

from datetime import datetime
from sqlalchemy import Column, DateTime
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from src.shared.utils import utcnow


class Tenant(SQLModel, table=True):
    __tablename__ = "tenants"

    tenant_id: str = Field(primary_key=True)
    name: str = Field(nullable=False)
    plan: str = Field(default="free", nullable=False)
    status: str = Field(default="active", nullable=False)
    # Each AI job's model by the job's name (one model per job; the machines
    # that run it are ai_machines). "" turns the job off; a job not listed has
    # the model its producers declare (src/producers AiJob.default_model).
    # Assign a new dict to change it (JSONB isn't watched in place).
    ai_job_models: dict[str, str] = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False))
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class AiMachine(SQLModel, table=True):
    """A GPU machine the account's AI work runs on: an OpenAI-compatible
    endpoint, the jobs it does (src/shared/ai_jobs.py), and how many requests
    it takes at once. Its status is the latest check of it (the worker's, or
    Connect's when it was saved). The built-in one (one per tenant) is the
    worker's own computer: no URL, only the jobs the worker does itself."""

    __tablename__ = "ai_machines"

    machine_id: str = Field(primary_key=True)
    tenant_id: str = Field(foreign_key="tenants.tenant_id", nullable=False, index=True)
    name: str = Field(nullable=False)
    api_url: str = Field(nullable=False)
    api_key: str = Field(default="", nullable=False)
    jobs: list[str] = Field(default_factory=list, sa_column=Column(JSONB, nullable=False))
    at_once: int = Field(default=2, nullable=False)
    enabled: bool = Field(default=True, nullable=False)
    built_in: bool = Field(default=False, nullable=False)
    # It shares the GPU the scheduler decodes video on: video work comes first there.
    shares_gpu: bool = Field(default=False, nullable=False)
    online: bool | None = Field(default=None, nullable=True)
    status_error: str = Field(default="", nullable=False)
    models: list[str] = Field(default_factory=list, sa_column=Column(JSONB, nullable=False))
    checked_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), nullable=True))
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
    # The user who minted it (None: an admin or operator key, like the scheduler's).
    created_by_user_id: str | None = Field(default=None, nullable=True, index=True)
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
    # In every JWT as "tv"; bumped to revoke all of the user's tokens.
    token_version: int = Field(default=0, nullable=False)
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


class PublicProject(SQLModel, table=True):
    __tablename__ = "public_projects"

    project_id: str = Field(primary_key=True)
    tenant_id: str = Field(foreign_key="tenants.tenant_id", nullable=False)
    connection_string: str = Field(nullable=False)
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
