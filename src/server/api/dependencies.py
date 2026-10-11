"""FastAPI dependencies for auth and database sessions."""

import hmac
from typing import Annotated, Generator

from fastapi import Header, HTTPException, Request
from sqlmodel import Session

from src.server.config import get_settings
from src.server.database import get_control_engine, get_engine_for_url


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
    if not hmac.compare_digest(token, settings.admin_key):
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


def require_signed_in(request: Request) -> None:
    """Raise 401 for a signed-out request let through for a public library: the route isn't public."""
    if getattr(request.state, "is_public_request", False) or not getattr(request.state, "role", None):
        raise HTTPException(status_code=401, detail="Sign in required")


def get_optional_user_id(request: Request) -> str | None:
    """The caller's identity, or None on a public page (a visitor has none)."""
    if getattr(request.state, "is_public_request", False):
        return None
    return get_current_user_id(request)


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




def checked_root_path(root_path: str) -> str:
    """A library root a request sent; 400 unless absolute and in one form:
    no ".", ".." or empty part (so no trailing "/"), no backslash, no NUL."""
    parts = root_path[1:].split("/")
    if (not root_path.startswith("/") or "\x00" in root_path or "\\" in root_path
            or any(p in ("", ".", "..") for p in parts)):
        raise HTTPException(status_code=400, detail="Invalid root_path")
    return root_path


FOLDER_ELSEWHERE = "Choose a library's folder on the computer Lumiverb is installed on."


def require_folder_chosen_here(request: Request) -> None:
    """Where the scheduler reads every folder (a Mac with the files:
    LUMIVERB_ROOT_MAP maps "/"), a library's folder is chosen only on that
    machine: from anywhere else, anyone who may set one could point a library
    at any folder the Mac's user can read. 403 otherwise. Where roots are
    mapped (prod), the map already bounds them. A tunnel or proxy on the
    Mac itself (tailscale serve, cloudflared, ngrok) brings other people's
    requests in over loopback: with one, choosing folders is open to whoever
    reaches it, for as long as the install exists."""
    from src.processing.roots import reads_every_folder
    from src.server.api.client_address import on_this_machine
    from src.server.api.errors import ApiError
    from src.server.scheduler.settings import SchedulerSettings

    if reads_every_folder(SchedulerSettings().root_map) and not on_this_machine(request):
        raise ApiError(403, "folder_elsewhere", FOLDER_ELSEWHERE)


def checked_rel_path(rel_path: str) -> str:
    """A rel_path a request sent, as stored (NFC); 400 unless it stays inside
    its library (relative, no "..", no leading "/"). Every write that takes a
    rel_path goes through here."""
    from src.shared.io_utils import UnsafeRelPathError, check_rel_path

    try:
        return check_rel_path(rel_path)
    except UnsafeRelPathError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid rel_path: {exc}") from None
