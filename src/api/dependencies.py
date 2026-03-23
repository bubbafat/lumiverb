"""FastAPI dependencies for auth and database sessions."""

from typing import Annotated, Generator

from fastapi import Header, HTTPException, Request
from sqlmodel import Session

from src.core.config import get_settings
from src.core.database import get_control_engine, get_engine_for_url


def get_db_session() -> Generator[Session, None, None]:
    """Yield a control plane session (for use in admin routes only)."""
    engine = get_control_engine()
    with Session(engine) as session:
        yield session


def get_tenant_session(request: Request) -> Generator[Session, None, None]:
    """
    Yield a tenant DB session from request.state.connection_string (set by middleware).
    Requires tenant resolution middleware to have run; raises 500 if state is missing.
    """
    connection_string = getattr(request.state, "connection_string", None)
    if not connection_string:
        raise HTTPException(status_code=500, detail="Tenant context missing")
    engine = get_engine_for_url(connection_string)
    with Session(engine) as session:
        yield session


def require_admin(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """
    Read Authorization: Bearer <token> and compare to settings.admin_key.
    Raises HTTP 401 if missing or wrong, HTTP 500 if ADMIN_KEY not configured.
    """
    settings = get_settings()
    if not settings.admin_key:
        raise HTTPException(
            status_code=500,
            detail="ADMIN_KEY not configured",
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = authorization[7:].strip()
    if token != settings.admin_key:
        raise HTTPException(status_code=401, detail="Invalid admin key")


def require_tenant_admin(request: Request) -> None:
    """
    Requires role == 'admin' on the resolved tenant API key.
    Must be used on routes that already pass through TenantResolutionMiddleware.
    Raises HTTP 403 if the key is not an admin key.
    """
    role = getattr(request.state, "role", None)
    if role != "admin":
        raise HTTPException(status_code=403, detail="Admin API key required")


def require_auth(request: Request) -> None:
    """
    Raise 403 if this is a public (unauthenticated) request.
    Defense-in-depth for write routes that must never be reachable via public access.
    The middleware already blocks non-GET from the public path, but this makes the
    restriction explicit in route definitions.
    """
    if getattr(request.state, "is_public_request", False):
        raise HTTPException(status_code=403, detail="This operation requires authentication")
