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


def require_editor(request: Request) -> None:
    """Raise 403 unless the caller is Admin or Editor. Blocks Viewer and unauthenticated public requests."""
    if getattr(request.state, "role", None) not in ("admin", "editor"):
        raise HTTPException(status_code=403, detail="Editor access required")


def get_current_user_id(request: Request) -> str:
    """Return user_id from JWT or API key context. Raises 401 if not available."""
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        # For API key auth, use key_id as the owner identity
        key_id = getattr(request.state, "key_id", None)
        if not key_id:
            raise HTTPException(status_code=401, detail="User identity not available")
        return f"key:{key_id}"
    return user_id


