"""Tenant context endpoint for CLI/worker (tenant_id only; no DB connection)."""

# TODO: replace with GET /v1/tenant/id that returns only tenant_id.
# connection_string should never be exposed to clients.
# Blocked on: worker CLI refactor to not need connection_string.

from fastapi import APIRouter, Request
from pydantic import BaseModel

router = APIRouter(prefix="/v1/tenant", tags=["tenant"])


class TenantContextResponse(BaseModel):
    tenant_id: str
    # connection_string removed — workers must not have direct DB access


@router.get("/context", response_model=TenantContextResponse)
def get_tenant_context(request: Request) -> TenantContextResponse:
    """
    Return tenant_id for the authenticated tenant.
    Used by CLI/worker for storage path computation only.
    """
    return TenantContextResponse(tenant_id=request.state.tenant_id)
