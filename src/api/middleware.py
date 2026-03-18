"""Tenant resolution middleware: API key → tenant DB routing."""

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from src.core.database import get_control_session
from src.repository.control_plane import ApiKeyRepository, TenantDbRoutingRepository


def _skip_tenant_middleware(path: str) -> bool:
    """Return True if path should not run tenant resolution."""
    if path == "/health":
        return True
    if path.startswith("/v1/admin/"):
        return True
    if path in ("/docs", "/redoc", "/openapi.json"):
        return True
    return False


def _error_response(status_code: int, code: str, message: str, details: dict | None = None) -> JSONResponse:
    body = {"error": {"code": code, "message": message, "details": details or {}}}
    return JSONResponse(status_code=status_code, content=body)


class TenantResolutionMiddleware(BaseHTTPMiddleware):
    """
    Run before every request except /health and /v1/admin/*.
    Resolves Authorization: Bearer <token> to tenant_id and connection_string,
    stores them in request.state.
    """

    async def dispatch(self, request: Request, call_next):
        if _skip_tenant_middleware(request.url.path):
            return await call_next(request)

        auth = request.headers.get("Authorization")
        if not auth or not auth.startswith("Bearer "):
            return _error_response(401, "unauthorized", "Missing or invalid Authorization header")

        token = auth[7:].strip()
        if not token:
            return _error_response(401, "unauthorized", "Missing or invalid Authorization header")

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
            request.state.key_id = api_key.key_id
            request.state.role = getattr(api_key, "role", "member")

        return await call_next(request)
