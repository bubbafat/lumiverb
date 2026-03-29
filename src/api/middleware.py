"""Tenant resolution middleware: API key → tenant DB routing, with public library fallback."""

import logging
import re

import jwt
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from src.core.config import get_settings
from src.core.database import get_control_session
from src.repository.control_plane import ApiKeyRepository, PublicCollectionRepository, PublicLibraryRepository, TenantDbRoutingRepository

logger = logging.getLogger(__name__)

_JWT_ALGORITHM = "HS256"


def _skip_tenant_middleware(path: str) -> bool:
    """Return True if path should not run tenant resolution."""
    if path == "/health":
        return True
    if path.startswith("/v1/admin/"):
        return True
    if path.startswith("/v1/auth/"):
        return True
    if path.startswith("/v1/upkeep"):
        return True
    if path in ("/docs", "/redoc", "/openapi.json"):
        return True
    return False


def _error_response(status_code: int, code: str, message: str, details: dict | None = None) -> JSONResponse:
    body = {"error": {"code": code, "message": message, "details": details or {}}}
    return JSONResponse(status_code=status_code, content=body)


# Routes where an unauthenticated GET may resolve the tenant from a library_id.
_PUBLIC_ELIGIBLE_PATH = re.compile(
    r"^/v1/(libraries/[^/]+|assets/[^/]+|search|similar)"
)


def _extract_library_id_from_path(path: str) -> str | None:
    """For /v1/libraries/{library_id}/... return the library_id segment, else None."""
    parts = path.split("/")
    # parts: ['', 'v1', 'libraries', '{library_id}', ...]
    if len(parts) >= 4 and parts[2] == "libraries":
        return parts[3] or None
    return None


class TenantResolutionMiddleware(BaseHTTPMiddleware):
    """
    Run before every request except /health and /v1/admin/*.

    Authenticated path: resolves Authorization: Bearer <token> to tenant_id and
    connection_string, stores them in request.state.

    Unauthenticated GET path: if the request URL or query params supply a library_id
    that exists in the public_libraries control plane table, resolves the tenant from
    that table and sets request.state.is_public_request = True.  All other
    unauthenticated requests return 401.
    """

    async def dispatch(self, request: Request, call_next):
        if _skip_tenant_middleware(request.url.path):
            return await call_next(request)

        auth = request.headers.get("Authorization")

        # --- Authenticated path ---
        if auth and auth.startswith("Bearer "):
            token = auth[7:].strip()
            if not token:
                return _error_response(401, "unauthorized", "Missing or invalid Authorization header")

            # Try JWT first; fall through to API key lookup on failure.
            jwt_secret = get_settings().jwt_secret
            if jwt_secret:
                try:
                    claims = jwt.decode(token, jwt_secret, algorithms=[_JWT_ALGORITHM])
                    tenant_id = claims["tenant_id"]
                    user_id = claims["sub"]
                    role = claims["role"]
                    with get_control_session() as session:
                        routing_repo = TenantDbRoutingRepository(session)
                        routing = routing_repo.get_by_tenant_id(tenant_id)
                    if routing is None:
                        return _error_response(500, "tenant_routing_missing", "Tenant database routing not found")
                    request.state.tenant_id = tenant_id
                    request.state.connection_string = routing.connection_string
                    request.state.user_id = user_id
                    request.state.key_id = None
                    request.state.role = role
                    request.state.is_public_request = False
                    return await call_next(request)
                except (jwt.PyJWTError, KeyError):
                    logger.debug("JWT decode failed for request to %s — falling through to API key", request.url.path)

            with get_control_session() as session:
                key_repo = ApiKeyRepository(session)
                api_key = key_repo.get_by_plaintext(token)
                if api_key is None:
                    return _error_response(401, "unauthorized", "Invalid or revoked API key")
                key_repo.touch_last_used(api_key.key_id)
                routing_repo = TenantDbRoutingRepository(session)
                routing = routing_repo.get_by_tenant_id(api_key.tenant_id)
                if routing is None:
                    return _error_response(500, "tenant_routing_missing", "Tenant database routing not found")
                request.state.tenant_id = api_key.tenant_id
                request.state.connection_string = routing.connection_string
                request.state.user_id = None
                request.state.key_id = api_key.key_id
                request.state.role = getattr(api_key, "role", "admin")
                request.state.is_public_request = False
            return await call_next(request)

        # --- Unauthenticated path: attempt public library resolution ---
        # Only GET requests on eligible routes may reach public libraries/collections.
        if request.method == "GET" and _PUBLIC_ELIGIBLE_PATH.match(request.url.path):
            library_id = (
                _extract_library_id_from_path(request.url.path)
                or request.query_params.get("library_id")        # /search, /similar, /assets/page
                or request.query_params.get("public_library_id") # /assets/{id}/...
            )
            if library_id:
                with get_control_session() as session:
                    pub = PublicLibraryRepository(session).get(library_id)
                if pub is not None:
                    request.state.tenant_id = pub.tenant_id
                    request.state.connection_string = pub.connection_string
                    request.state.key_id = None
                    request.state.role = "public"
                    request.state.is_public_request = True
                    return await call_next(request)

            # Also try public_collection_id for asset proxy/thumbnail
            collection_id = request.query_params.get("public_collection_id")
            if collection_id:
                with get_control_session() as session:
                    pub = PublicCollectionRepository(session).get(collection_id)
                if pub is not None:
                    request.state.tenant_id = pub.tenant_id
                    request.state.connection_string = pub.connection_string
                    request.state.key_id = None
                    request.state.role = "public"
                    request.state.is_public_request = True
                    request.state.public_collection_id = collection_id
                    return await call_next(request)

        # --- Unauthenticated path: attempt public collection resolution ---
        if request.method == "GET" and request.url.path.startswith("/v1/public/collections/"):
            parts = request.url.path.split("/")
            # /v1/public/collections/{collection_id}[/assets]
            collection_id = parts[4] if len(parts) > 4 else None
            if collection_id:
                with get_control_session() as session:
                    pub = PublicCollectionRepository(session).get(collection_id)
                if pub is not None:
                    request.state.tenant_id = pub.tenant_id
                    request.state.connection_string = pub.connection_string
                    request.state.key_id = None
                    request.state.role = "public"
                    request.state.is_public_request = True
                    request.state.public_collection_id = collection_id
                    return await call_next(request)

        return _error_response(401, "unauthorized", "Missing or invalid Authorization header")
