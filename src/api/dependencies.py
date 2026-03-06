"""FastAPI dependencies for auth and database sessions."""

from typing import Annotated, Generator

from fastapi import Header, HTTPException
from sqlmodel import Session

from src.core.config import get_settings
from src.core.database import get_control_engine


def get_db_session() -> Generator[Session, None, None]:
    """Yield a control plane session (for use in admin routes only)."""
    engine = get_control_engine()
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
