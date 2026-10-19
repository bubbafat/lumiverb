"""FastAPI dependencies for auth and database sessions."""

import hmac
from typing import Generator

from fastapi import HTTPException, Request
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
    # The producers as installed now: a plug-in's work is one (src/server/plugin_producers.py).
    from src.server.plugin_producers import keep_current

    keep_current()
    engine = get_engine_for_url(connection_string)
    with Session(engine) as session:
        # Whose database it is, for what's the account's rather than its database's (its AI jobs' models).
        session.info["tenant_id"] = getattr(request.state, "tenant_id", None)
        yield session


def is_install_key(token: str) -> bool:
    """The install's own key (ADMIN_KEY in its env file), for what runs on
    it: its upkeep, the scripts that set it up, the CLI's `lumiverb admin`
    there. Not an account's key, and not a person."""
    admin_key = get_settings().admin_key
    # As bytes: a header may carry any Latin-1 character, which compare_digest refuses in a str.
    return bool(admin_key and hmac.compare_digest(token.encode(), admin_key.encode()))


SERVER_ADMIN_REQUIRED = "This is the server admin's: sign in as one."


def require_server_admin(request: Request) -> None:
    """What belongs to the install rather than an account (ADR-018 decision
    8): its accounts, and what they share (where previews live, the network,
    backups, logs). A server admin who signed in (users.server_admin, as it
    is now: the middleware reads it at each request), or, on /v1/admin, the
    install's own key (the middleware's install_key). A key a person made
    is never a server admin, nor is an account admin who isn't one: 403
    server_admin_required."""
    from src.server.api.errors import ApiError

    if getattr(request.state, "server_admin", False) is True or getattr(request.state, "install_key", False) is True:
        return
    raise ApiError(403, "server_admin_required", SERVER_ADMIN_REQUIRED)


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
    """Raise 403 unless the caller is Admin or Editor where it acts: on the
    libraries the request edits when it names any (a person's role on each,
    src/server/api/library_access.py), else in the account; or a device on
    a route the device gate let it by (src/server/api/devices.py). Blocks
    Viewer and unauthenticated public requests."""
    from src.server.api.library_access import role_here

    if role_here(request) not in ("admin", "editor") and not _device_allowed(request):
        raise HTTPException(status_code=403, detail="Editor access required")


LIBRARY_ADMIN_REQUIRED = "This is the library's admin's: its settings, who reads it and who can see it."


def require_library_admin(request: Request) -> None:
    """The admin of every library the request names (ADR-018 decision 8: a
    library's settings, its reader, its people), an account admin being
    every library's; when it names none, an account admin. 403
    library_admin_required otherwise (src/server/api/library_access.py
    keeps the roles on what the request names)."""
    from src.server.api.errors import ApiError
    from src.server.api.library_access import role_here

    if role_here(request) != "admin":
        raise ApiError(403, "library_admin_required", LIBRARY_ADMIN_REQUIRED)


def require_reader(request: Request) -> None:
    """A library's reader: this server's scheduler (an admin's key, as its
    is), or a device on a route the device gate let it by, for a library
    it reads. 403 otherwise."""
    if getattr(request.state, "role", None) != "admin" and not _device_allowed(request):
        raise HTTPException(status_code=403, detail="Admin API key required")


def _device_allowed(request: Request) -> bool:
    return (getattr(request.state, "device_id", None) is not None
            and getattr(request.state, "device_allowed", False) is True)


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
    no ".", ".." or empty part (so no trailing "/"), no backslash, no control
    character (a newline, a tab, NUL)."""
    from src.shared.path_filter import has_control_character

    parts = root_path[1:].split("/")
    if (not root_path.startswith("/") or has_control_character(root_path) or "\\" in root_path
            or any(p in ("", ".", "..") for p in parts)):
        raise HTTPException(status_code=400, detail="Invalid root_path")
    return root_path


FOLDER_ELSEWHERE = "Choose a library's folder on the computer Lumiverb is installed on."


def require_folder_chosen_here(request: Request) -> None:
    """Where the scheduler reads every folder (a Mac with the files:
    LUMIVERB_ROOT_MAP maps "/"), a library's folder is chosen only on that
    machine: from anywhere else, anyone who may set one could point a library
    at any folder the Mac's user can read. 403 otherwise. Where roots are
    mapped (a Linux install), the map already bounds them. A tunnel or proxy
    on the Mac itself (tailscale serve, cloudflared, ngrok) brings other people's
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
    its library in its one form (relative, no "..", no leading "/", no
    control character: check_rel_path). Every write that takes a rel_path
    goes through here."""
    from src.shared.io_utils import UnsafeRelPathError, check_rel_path

    try:
        return check_rel_path(rel_path)
    except UnsafeRelPathError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid rel_path: {exc}") from None
