"""Tenant context endpoint for CLI/worker to obtain connection details."""

from fastapi import APIRouter, Request
from pydantic import BaseModel

router = APIRouter(prefix="/v1/tenant", tags=["tenant"])


class TenantContextResponse(BaseModel):
    tenant_id: str
    connection_string: str


@router.get("/context", response_model=TenantContextResponse)
def get_tenant_context(request: Request) -> TenantContextResponse:
    """
    Return tenant_id and connection_string for the authenticated tenant.
    Used by CLI/worker to open a direct tenant DB session.
    Treat connection_string as a secret — never log it.
    """
    return TenantContextResponse(
        tenant_id=request.state.tenant_id,
        connection_string=request.state.connection_string,
    )
