"""Accounts (tenants): each one's row in the control plane, its own database
and its routing. The operator's admin API (api/routers/admin.py) and an
install's first-run setup (src/server/setup.py) both make them here."""

from __future__ import annotations

import contextlib

from sqlmodel import Session

from src.server.config import get_settings
from src.server.database import deprovision_tenant_database, provision_tenant_database
from src.server.repository.control_plane import (
    ApiKeyRepository,
    TenantDbRoutingRepository,
    TenantRepository,
)


class AccountError(RuntimeError):
    """An account couldn't be made. What was made of it is removed again."""


def create_account(session: Session, name: str, *, plan: str = "free", vision_api_url: str = "",
                   vision_api_key: str = "") -> str:
    """A new account with its database provisioned (migrated) and routed;
    returns its tenant_id. Raises AccountError saying which step failed."""
    try:
        tenant_id = TenantRepository(session).create(
            name=name, plan=plan, vision_api_url=vision_api_url, vision_api_key=vision_api_key,
        ).tenant_id
    except Exception as e:
        raise AccountError(f"Failed to create tenant: {e}") from e

    try:
        provision_tenant_database(tenant_id)
    except Exception as e:
        remove_account(session, tenant_id)
        raise AccountError(f"Failed to provision tenant database: {e}") from e

    try:
        connection_string = get_settings().tenant_database_url_template.format(tenant_id=tenant_id)
        TenantDbRoutingRepository(session).create(tenant_id=tenant_id, connection_string=connection_string)
    except Exception as e:
        remove_account(session, tenant_id)
        raise AccountError(f"Failed to create routing: {e}") from e
    return tenant_id


def remove_account(session: Session, tenant_id: str) -> None:
    """Best effort: an account that failed to be made, rows and database."""
    for step in (
        lambda: ApiKeyRepository(session).delete_by_tenant_id(tenant_id),
        lambda: TenantDbRoutingRepository(session).delete_by_tenant_id(tenant_id),
        lambda: TenantRepository(session).delete(tenant_id),
        lambda: deprovision_tenant_database(tenant_id),
    ):
        with contextlib.suppress(Exception):
            step()
